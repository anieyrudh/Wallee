"""Planner backends for Wallee v6.5."""

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
        selected = {
            "prusa_persistent_stringing_followup.json",
            "prusa_big_step_escalation.json",
            "prusa_big_cooldown_respected.json",
            "prusa_second_order_settle.json",
            "prusa_startup_suppressed.json",
            "unload_when_hot.json",
            "unload_when_ready.json",
        }
        parts = []
        for path in sorted(path for path in examples_dir.glob("*.json") if path.name in selected):
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
        timeout_s = max(1.0, float(getattr(self.config, "planner_request_timeout_seconds", 20.0)))
        try:
            with request.urlopen(req, timeout=timeout_s) as response:
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
            "<decision_principles>\n"
            "- Choose exactly one legal bounded action when fresh grounded evidence supports it.\n"
            "- `decision_signals.allowed_frontier_ids` lists only legal non-tuning explicit safety or operator actions.\n"
            "- Legal tuning choices are listed separately in `decision_signals.tuning_action_space` and are independently legal.\n"
            "- If `decision_signals.tuning_action_space` is non-empty, legal bounded tuning remains available even when `decision_signals.allowed_frontier_ids` is empty or contains only `A_PRUSA_PAUSE` or `A_PRUSA_CANCEL`.\n"
            "- Do not infer tuning illegality from `decision_signals.allowed_frontier_ids` being empty or safety-only.\n"
            "- Respect blockers, cooldowns, settle context, terminal phase, and the one-family-at-a-time rule.\n"
            "- Use `action_consequence`, `vision_signal`, `freshness`, `recent_family_actions`, and `cumulative_family_deltas` to judge whether a previous action helped.\n"
            "- Prefer `NO_ACTION` when evidence is stale, still settling, weak, conflicting, or no legal follow-up is clearly supported.\n"
            "- Treat nozzle residue alone as observational. Ignore it unless it is visibly causing print defects, dragging filament, forming a blob, contacting the part, or co-occurring with stringing, spaghetti, or adhesion failure.\n"
            "- Use `CALL_HUMAN` only for true operator intervention, fault, or recovery conditions, not when bounded tuning remains legal.\n"
            "</decision_principles>\n\n"
            "<ambiguity_policy>\n"
            "- If legal actions remain but the evidence is genuinely weak or conflicting, prefer `NO_ACTION`.\n"
            "- Do not use `CALL_HUMAN` for residue-only observations during a healthy advancing print.\n"
            "- If bounded tuning remains legal during a healthy active print, prefer tuning or `NO_ACTION` over `CALL_HUMAN`.\n"
            "</ambiguity_policy>\n\n"
            "<output_contract>\n"
            "- Return only valid PlanIR JSON.\n"
            "- Emit exactly one of:\n"
            "  - one legal explicit action ID in `sequence`\n"
            "  - one legal `tuning_choice`\n"
            "  - `NO_ACTION`\n"
            "  - `CALL_HUMAN`\n"
            "- Never emit IDs outside the non-tuning explicit safety/operator IDs in `decision_signals.allowed_frontier_ids`.\n"
            "- Never emit a `tuning_choice` outside `decision_signals.tuning_action_space`.\n"
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
                    "follow-up action instead of waiting indefinitely. Treat "
                    "`decision_signals.allowed_frontier_ids` as non-tuning explicit safety/operator actions only; "
                    "tuning legality comes from `decision_signals.tuning_action_space`. If that tuning space is "
                    "non-empty, legal bounded tuning remains available even when `decision_signals.allowed_frontier_ids` "
                    "is empty or only contains pause/cancel.\n\n"
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
            if self.config.openrouter_response_healing:
                payload["plugins"] = [{"id": "response-healing"}]
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
        plan = self._normalize_malformed_execute_plan(plan, world)
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
        if self._should_suppress_active_print_pause(plan, world):
            return PlanIR(
                decision="NO_ACTION",
                sequence=[],
                why=(
                    "Healthy active printing still has bounded tuning available, so do not pause solely on a residue "
                    "observation. Wait for a legal bounded tuning move or stronger recovery evidence."
                ),
                call_human_message=None,
            )
        if self._should_suppress_active_print_residue_only_call_human(plan, world):
            return PlanIR(
                decision="NO_ACTION",
                sequence=[],
                why=(
                    "Healthy active print has only a residue observation without evidence that it is affecting the "
                    "print, so ignore it instead of calling a human."
                ),
                call_human_message=None,
            )
        if self._should_suppress_active_print_call_human_when_tuning_exists(plan, world):
            return PlanIR(
                decision="NO_ACTION",
                sequence=[],
                why=(
                    "Healthy active print still has bounded tuning available, so do not call a human while legal "
                    "tuning choices remain and no real fault or operator condition is present."
                ),
                call_human_message=None,
            )
        return plan

    def _normalize_malformed_execute_plan(self, plan: PlanIR, world: WorldPacket) -> PlanIR:
        if plan.decision != "EXECUTE" or not plan.sequence or plan.tuning_choice is None:
            return plan
        if self._tuning_choice_resolves_cleanly(plan, world):
            return PlanIR(
                decision="EXECUTE",
                sequence=[],
                tuning_choice=plan.tuning_choice,
                why=plan.why,
                call_human_message=None,
            )
        return PlanIR(
            decision="NO_ACTION",
            sequence=[],
            tuning_choice=None,
            why=(
                "Malformed planner output contained both an explicit sequence and a tuning choice without one clean "
                "legal tuning resolution. Skip this cycle."
            ),
            call_human_message=None,
        )

    def _tuning_choice_resolves_cleanly(self, plan: PlanIR, world: WorldPacket) -> bool:
        tuning_choice = plan.tuning_choice
        if plan.decision != "EXECUTE" or tuning_choice is None:
            return False
        matches = [
            action.action_id
            for action in world.frontier
            if _action_family_from_id(action.action_id) == tuning_choice.family
            and _action_direction_from_id(action.action_id) == tuning_choice.direction
            and _action_magnitude_from_id(action.action_id) == tuning_choice.magnitude
        ]
        return len(matches) == 1

    def _should_suppress_active_print_pause(self, plan: PlanIR, world: WorldPacket) -> bool:
        if plan.decision != "EXECUTE" or plan.sequence != ["A_PRUSA_PAUSE"]:
            return False
        lifecycle = str(world.facts.get("printer_1.lifecycle") or "").strip().upper()
        printing_phase = str(world.facts.get("printer_1.printing_phase") or "").strip().lower()
        health = str(world.facts.get("printer_1.health") or "").strip().upper()
        if lifecycle != "PRINTING" or printing_phase != "active_printing":
            return False
        if health in {"ATTENTION", "ERROR", "OFFLINE"}:
            return False
        tuning_action_space = world.prompt_view().get("decision_signals", {}).get("tuning_action_space", {})
        return isinstance(tuning_action_space, dict) and bool(tuning_action_space)

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

    def _should_suppress_active_print_residue_only_call_human(self, plan: PlanIR, world: WorldPacket) -> bool:
        if plan.decision != "CALL_HUMAN":
            return False
        lifecycle = str(world.facts.get("printer_1.lifecycle") or "").strip().upper()
        health = str(world.facts.get("printer_1.health") or "").strip().upper()
        printing_phase = str(world.facts.get("printer_1.printing_phase") or "").strip().lower()
        if lifecycle != "PRINTING" or health in {"ATTENTION", "ERROR", "OFFLINE"} or printing_phase != "active_printing":
            return False
        signal = world.prompt_view().get("decision_signals", {}).get("vision_signal", {})
        if not isinstance(signal, dict) or not bool(signal.get("usable")):
            return False
        findings = {str(item) for item in signal.get("finding_types") or []}
        if findings != {"residue"}:
            return False
        summary = str(signal.get("summary") or "").lower()
        impact_terms = ("blob", "drag", "contact", "string", "spaghetti", "adhesion", "failed", "detached")
        return not any(term in summary for term in impact_terms)

    def _should_suppress_active_print_call_human_when_tuning_exists(self, plan: PlanIR, world: WorldPacket) -> bool:
        if plan.decision != "CALL_HUMAN":
            return False
        if not _healthy_active_print_with_tuning(world):
            return False
        return not _has_real_fault_or_operator_condition(world)

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


def _healthy_active_print_with_tuning(world: WorldPacket) -> bool:
    lifecycle = str(world.facts.get("printer_1.lifecycle") or "").strip().upper()
    health = str(world.facts.get("printer_1.health") or "").strip().upper()
    printing_phase = str(world.facts.get("printer_1.printing_phase") or "").strip().lower()
    if lifecycle != "PRINTING" or printing_phase != "active_printing":
        return False
    if health in {"ATTENTION", "ERROR", "OFFLINE"}:
        return False
    tuning_action_space = world.prompt_view().get("decision_signals", {}).get("tuning_action_space", {})
    return isinstance(tuning_action_space, dict) and bool(tuning_action_space)


def _has_real_fault_or_operator_condition(world: WorldPacket) -> bool:
    health = str(world.facts.get("printer_1.health") or "").strip().upper()
    if health in {"ATTENTION", "ERROR", "OFFLINE"}:
        return True
    if world.pending_human:
        return True
    signal = world.prompt_view().get("decision_signals", {}).get("vision_signal", {})
    if not isinstance(signal, dict):
        return False
    findings = {str(item).strip().lower() for item in signal.get("finding_types") or []}
    if findings.intersection({"spaghetti", "adhesion_failure", "detached_part", "collision", "obstruction", "jam"}):
        return True
    summary = str(signal.get("summary") or "").lower()
    severe_terms = (
        "contact",
        "collision",
        "detached",
        "failed",
        "failure",
        "obstruction",
        "jam",
        "spaghetti",
        "adhesion failure",
        "recovery",
    )
    return any(term in summary for term in severe_terms)


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
