"""Planner backends for Wallee v6."""

from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any
from urllib import error, request

from jsonschema import Draft202012Validator

from .config import Config
from .models import PlanIR, WorldPacket
from .openrouter_observability import normalize_openrouter_metadata, utc_now_iso


@dataclass(frozen=True)
class PromptPackage:
    contract_text: str
    rubric_text: str
    examples_text: str


class Planner(ABC):
    """Small interface implemented by all planner backends."""

    @abstractmethod
    def plan(self, world: WorldPacket) -> PlanIR:
        """Return a plan decision for *world*."""


class HeuristicPlanner(Planner):
    """Local fallback planner used for development and tests.

    This planner is intentionally simple.  It makes the repository runnable
    without cloud access and provides deterministic behavior for tests.  The
    point is not to be smart; the point is to preserve the contract that the
    real planner must satisfy.
    """

    def plan(self, world: WorldPacket) -> PlanIR:
        frontier = world.frontier
        if not frontier:
            return PlanIR(decision="NO_ACTION", sequence=[], why="No legal actions available")

        wait_actions = [action for action in frontier if action.verb == "WAIT_UNTIL"]
        if wait_actions:
            return PlanIR(
                decision="EXECUTE",
                sequence=[wait_actions[0].action_id],
                why="A blocker is expected to clear without hardware intervention",
            )

        non_human = [action for action in frontier if action.verb != "CALL_HUMAN"]
        if non_human:
            return PlanIR(
                decision="EXECUTE",
                sequence=[non_human[0].action_id],
                why=f"Selected lowest-rank legal action {non_human[0].verb}",
            )

        return PlanIR(
            decision="CALL_HUMAN",
            sequence=[],
            why="Only escalation remains legal",
            call_human_message=frontier[0].description,
        )


class OpenRouterPlanner(Planner):
    """Strict-JSON planner that talks to OpenRouter.

    The request shape deliberately keeps the first system message and first
    non-system message stable so the provider's prompt-caching path has a chance
    to help.  The dynamic world packet is injected later in the message list.
    """

    def __init__(self, config: Config, plan_schema_path: str | Path, prompt_package: PromptPackage | None = None) -> None:
        self.config = config
        self.plan_validator = Draft202012Validator(json.loads(Path(plan_schema_path).read_text(encoding="utf-8")))
        self.last_request_payload: dict[str, Any] | None = None
        self.last_response_payload: dict[str, Any] | None = None
        self.last_provider_metadata: dict[str, Any] | None = None
        self.prompt_package = prompt_package or PromptPackage(
            contract_text=(config.knowledge_dir / "CONTRACT.md").read_text(encoding="utf-8"),
            rubric_text=(config.knowledge_dir / "RUBRIC.md").read_text(encoding="utf-8"),
            examples_text=self._load_examples(config.knowledge_dir / "EXAMPLES"),
        )

    def _load_examples(self, examples_dir: Path) -> str:
        parts = []
        for path in sorted(examples_dir.glob("*.json")):
            parts.append(path.read_text(encoding="utf-8").strip())
        return "\n\n".join(parts)

    def plan(self, world: WorldPacket) -> PlanIR:
        payload = self.build_payload(world)
        request_started_at = utc_now_iso()
        request_started_monotonic = time.time()
        body = json.dumps(payload).encode("utf-8")
        self.last_request_payload = payload
        self.last_response_payload = None
        self.last_provider_metadata = None
        req = request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.openrouter_api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=45) as response:
                raw_body = response.read()
                response_headers = dict(response.headers.items())
        except error.HTTPError as exc:
            raw_body = exc.read()
            response_headers = dict(exc.headers.items()) if exc.headers is not None else {}
            response_received_at = utc_now_iso()
            latency_ms = round((time.time() - request_started_monotonic) * 1000.0, 1)
            data = self._decode_error_payload(raw_body, exc)
            self.last_response_payload = data
            self.last_provider_metadata = normalize_openrouter_metadata(
                payload=data,
                requested_model=self.config.openrouter_model,
                latency_ms=latency_ms,
                response_size_bytes=len(raw_body),
                retry_count=0,
                schema_valid=False,
                request_started_at=request_started_at,
                response_received_at=response_received_at,
                headers=response_headers,
            )
            raise
        except error.URLError:
            self.last_provider_metadata = normalize_openrouter_metadata(
                payload=None,
                requested_model=self.config.openrouter_model,
                latency_ms=round((time.time() - request_started_monotonic) * 1000.0, 1),
                response_size_bytes=None,
                retry_count=0,
                schema_valid=False,
                request_started_at=request_started_at,
                response_received_at=utc_now_iso(),
                headers=None,
            )
            raise
        response_received_at = utc_now_iso()
        latency_ms = round((time.time() - request_started_monotonic) * 1000.0, 1)
        data = json.loads(raw_body.decode("utf-8"))
        self.last_response_payload = data
        try:
            plan = self._normalize_plan(self._parse_response(data), world)
        except Exception:
            self.last_provider_metadata = normalize_openrouter_metadata(
                payload=data,
                requested_model=self.config.openrouter_model,
                latency_ms=latency_ms,
                response_size_bytes=len(raw_body),
                retry_count=0,
                schema_valid=False,
                request_started_at=request_started_at,
                response_received_at=response_received_at,
                headers=response_headers,
            )
            raise
        self.last_provider_metadata = normalize_openrouter_metadata(
            payload=data,
            requested_model=self.config.openrouter_model,
            latency_ms=latency_ms,
            response_size_bytes=len(raw_body),
            retry_count=0,
            schema_valid=True,
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            headers=response_headers,
        )
        return plan

    def developer_prompt_text(self) -> str:
        return (
            "<task>\n"
            "Return only valid PlanIR JSON for one bounded planner decision.\n"
            "</task>\n\n"
            "<decision_procedure>\n"
            "1. Read `decision_signals.allowed_frontier_ids`.\n"
            "2. If `allowed_frontier_ids` is empty, return `NO_ACTION`.\n"
            "3. Exclude any action contradicted by current blockers or bounded runtime rules.\n"
            "4. Rank remaining legal actions using fresh grounded live evidence on supported surfaces first.\n"
            "5. Treat `decision_signals.vision_signal`, `decision_signals.notebook_notes`, and `last_result_notes` as trustworthy bounded planning signals when they are present and fresh.\n"
            "6. If one legal bounded action is best supported by the current evidence, choose it. Do not wait for perfect certainty.\n"
            "7. Use `last_result_notes` and `decision_signals.symptom_feedback` together. If a recent action verified and the same issue still persists or is not improving, treat that as positive support for one additional legal bounded follow-up action.\n"
            "8. Use `temporal_action_context` to avoid repeating a very recent action only inside the short settle window. Once the action is no longer immediate and `symptom_feedback.not_improving_after_last_action` is true, do not keep waiting.\n"
            "9. Use `freshness` to discount stale or still-settling observations, not to dismiss fresh supported evidence.\n"
            "10. Prefer `NO_ACTION` only when no single legal follow-up action is clearly supported or the evidence is genuinely weak or conflicting.\n"
            "11. During healthy print startup, if only pause or cancel remain because tuning is not yet admitted, prefer `NO_ACTION` rather than escalation.\n"
            "12. Near print finish, if extrusion-related symptoms are still present and a legal speed, flow, or nozzle action remains admitted, prefer taking that bounded action now rather than waiting for the print to end.\n"
            "13. Emit at most one action ID.\n"
            "14. Never use the order of actions in the goal text as evidence.\n"
            "15. Never bundle families in one decision.\n"
            "</decision_procedure>\n\n"
            "<ambiguity_policy>\n"
            "- If legal actions remain but the evidence is genuinely weak or conflicting, prefer `NO_ACTION`.\n"
            "- Use `CALL_HUMAN` only for true operator intervention, fault, or recovery conditions.\n"
            "</ambiguity_policy>\n\n"
            "<output_contract>\n"
            "- Return only valid PlanIR JSON.\n"
            "- Emit exactly one of:\n"
            "  - one legal action ID\n"
            "  - `NO_ACTION`\n"
            "  - `CALL_HUMAN`\n"
            "- Never emit IDs outside `decision_signals.allowed_frontier_ids`.\n"
            "</output_contract>"
        )

    def prompt_stack_view(self) -> dict[str, Any]:
        return {
            "system_prompt": self.prompt_package.contract_text,
            "developer_prompt": self.developer_prompt_text(),
            "rubric_text": self.prompt_package.rubric_text,
            "examples_text": self.prompt_package.examples_text,
            "provider_name": "openrouter",
            "model": self.config.openrouter_model,
        }

    def _cacheable_message(self, *, role: str, content: str) -> dict[str, Any]:
        return {
            "role": role,
            "content": content,
            # Mark the stable leading prompt segments so provider-side prompt caching
            # can reuse them across cycles. The dynamic world packet remains unmarked.
            "cache_control": {"type": "ephemeral"},
        }

    def build_payload(self, world: WorldPacket, *, response_format: str = "json_schema") -> dict[str, Any]:
        payload = {
            "model": self.config.openrouter_model,
            "stream": False,
            "temperature": 0.1,
            "messages": [
                self._cacheable_message(role="system", content=self.prompt_package.contract_text),
                self._cacheable_message(role="developer", content=self.developer_prompt_text()),
                self._cacheable_message(
                    role="user",
                    content=(
                        "Planning rubric and examples. Keep horizons short and one-family-at-a-time. When fresh "
                        "grounded evidence supports one legal bounded action, take it. If a recent action verified "
                        "and the same issue persists across fresh later observations, choose one additional legal "
                        "follow-up action instead of waiting indefinitely.\n\n"
                        f"{self.prompt_package.rubric_text}\n\n{self.prompt_package.examples_text}"
                    ),
                ),
                {
                    "role": "user",
                    "content": json.dumps(world.prompt_view(), sort_keys=True),
                },
            ],
        }
        if response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "plan_ir",
                    "strict": True,
                    "schema": self._transport_plan_schema(),
                },
            }
        elif response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if self.config.reasoning_effort:
            payload["reasoning"] = {"effort": self.config.reasoning_effort}
        return payload

    def _parse_response(self, payload: dict[str, Any]) -> PlanIR:
        choices = payload.get("choices") or []
        if not choices:
            raise ValueError("OpenRouter returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
            content_text = "".join(parts)
        else:
            content_text = str(content or "")

        plan_json = json.loads(content_text)
        self.plan_validator.validate(plan_json)
        return PlanIR.model_validate(plan_json)

    def _normalize_plan(self, plan: PlanIR, world: WorldPacket) -> PlanIR:
        if self._should_suppress_startup_call_human(plan, world):
            return PlanIR(
                decision="NO_ACTION",
                sequence=[],
                why=(
                    "Healthy print startup still has bounded tuning gated off, so wait instead of escalating while only "
                    "pause or cancel are legal."
                ),
                call_human_message=None,
            )
        return plan

    def _should_suppress_startup_call_human(self, plan: PlanIR, world: WorldPacket) -> bool:
        if plan.decision != "CALL_HUMAN":
            return False
        if not world.frontier or not _frontier_is_pause_cancel_only(world):
            return False
        lifecycle = str(world.facts.get("printer_1.lifecycle") or "").strip().upper()
        health = str(world.facts.get("printer_1.health") or "").strip().upper()
        job_active = bool(world.facts.get("printer_1.job_active"))
        if lifecycle != "PRINTING" or not job_active:
            return False
        if health in {"ATTENTION", "ERROR", "OFFLINE"}:
            return False
        progress = _float_or_none(world.facts.get("printer_1.job_progress_pct"))
        family_blockers = world.prompt_view().get("decision_signals", {}).get("family_blockers", {})
        startup_blockers = {
            blocker
            for blockers in family_blockers.values()
            for blocker in blockers
        }
        startup_like = any(
            "progress_not_ready" in blocker or blocker.startswith("startup_") or blocker == "startup_printing"
            for blocker in startup_blockers
        )
        return startup_like or progress is None or progress <= 2.0

    def _transport_plan_schema(self) -> dict[str, Any]:
        schema = deepcopy(self.plan_validator.schema)
        properties = schema.get("properties")
        if isinstance(properties, dict):
            schema["required"] = sorted(properties.keys())
        return schema

    def _decode_error_payload(self, raw_body: bytes, exc: Exception) -> dict[str, Any]:
        text = raw_body.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"error": {"message": text or str(exc)}}
        if not isinstance(data, dict):
            data = {"error": {"message": text or str(exc)}}
        return data


def _frontier_is_pause_cancel_only(world: WorldPacket) -> bool:
    allowed_verbs = {"PAUSE", "CANCEL"}
    for action in world.frontier:
        verb = str(action.verb or "").strip().upper()
        action_id = str(action.action_id or "").strip().upper()
        if verb in allowed_verbs:
            continue
        if "PAUSE" in action_id or "CANCEL" in action_id:
            continue
        return False
    return True


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
