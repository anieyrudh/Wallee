"""Replay recorded planner traffic through the PRODUCTION OpenRouterPlanner.

Zero network, zero cost, fully deterministic. Each cassette pins:
  - the outbound request (fingerprint over model + messages + response_format)
    — any prompt-stack or payload-shaping change turns this red until the
    corpus is deliberately regenerated (scripts/record_cassettes.py) in a
    separate [re-record] commit;
  - the parse + schema-validation + normalization/suppression pipeline —
    the expected PlanIR stored in each cassette is the FINAL plan after
    deterministic policy, so policy regressions are caught too.
"""

from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from wallee.config import Config
from wallee.llm_transport import (
    CassetteMiss,
    ReplayTransport,
    load_cassettes,
    request_fingerprint,
)
from wallee.models import PlanIR, WorldPacket
from wallee.planner import OpenRouterPlanner

REPO_V6 = Path(__file__).resolve().parents[1]
CASSETTE_ROOT = REPO_V6 / "tests" / "fixtures" / "cassettes" / "planner"

OK_CASSETTES = [
    (path, cassette)
    for path, cassette in load_cassettes(CASSETTE_ROOT)
    if cassette.response_status < 400
]
ERROR_CASSETTES = [
    (path, cassette)
    for path, cassette in load_cassettes(CASSETTE_ROOT)
    if cassette.response_status >= 400
]


@pytest.fixture
def planner_env(tmp_path, monkeypatch):
    """The exact environment scripts/record_cassettes.py records under."""
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_MODEL", "synthetic/planner-cassette")
    monkeypatch.setenv("WALLEE_REASONING_EFFORT", "low")
    monkeypatch.setenv("WALLEE_OPENROUTER_RESPONSE_HEALING", "1")
    return Config.from_env(repo_root=REPO_V6)


def _planner(config: Config, cassette) -> OpenRouterPlanner:
    return OpenRouterPlanner(
        config,
        REPO_V6 / "schemas" / "plan_ir.schema.json",
        transport=ReplayTransport([cassette]),
    )


def test_cassette_corpus_is_nonempty():
    assert len(OK_CASSETTES) >= 3, "cassette corpus missing — regenerate with scripts/record_cassettes.py"
    assert ERROR_CASSETTES, "error cassette missing"


@pytest.mark.parametrize(
    "path,cassette",
    OK_CASSETTES,
    ids=[c.meta.get("case_id", p.stem) for p, c in OK_CASSETTES],
)
def test_replay_through_production_planner(planner_env, path, cassette):
    world = WorldPacket.model_validate(cassette.meta["world"])
    planner = _planner(planner_env, cassette)

    plan = planner.plan(world)

    expected = PlanIR.model_validate(cassette.meta["expected_plan"])
    assert plan == expected, f"cassette {path.name}: normalized plan drifted"

    meta = planner.last_provider_metadata
    assert meta is not None and meta["schema_valid"] is True
    # Header allowlisting is a hard property of every persistence path.
    assert set(k.lower() for k in meta["raw_provider_metadata"]["headers"]) <= {
        "content-type",
        "x-request-id",
        "x-openrouter-provider",
        "x-ratelimit-remaining",
    }


@pytest.mark.parametrize(
    "path,cassette",
    ERROR_CASSETTES,
    ids=[c.meta.get("case_id", p.stem) for p, c in ERROR_CASSETTES],
)
def test_replayed_provider_errors_surface_as_http_errors(planner_env, path, cassette):
    world = WorldPacket.model_validate(cassette.meta["world"])
    planner = _planner(planner_env, cassette)

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        planner.plan(world)

    assert excinfo.value.code == cassette.meta["expected_error_status"]
    meta = planner.last_provider_metadata
    assert meta is not None and meta["schema_valid"] is False


def test_request_drift_is_a_loud_miss(planner_env):
    """Changing the outbound prompt/payload must fail, not silently pass."""
    path, cassette = OK_CASSETTES[0]
    world = WorldPacket.model_validate(cassette.meta["world"])
    world.goal = world.goal + " (drifted)"
    planner = _planner(planner_env, cassette)

    with pytest.raises(CassetteMiss):
        planner.plan(world)


def test_fingerprint_covers_prompt_stack(planner_env):
    """The fingerprint must change when any prompt segment changes."""
    path, cassette = OK_CASSETTES[0]
    payload = dict(cassette.request_payload)
    assert request_fingerprint(payload) == cassette.fingerprint

    mutated = dict(payload)
    mutated["messages"] = list(payload["messages"])
    first = dict(mutated["messages"][0])
    first["content"] = first["content"] + "\nDRIFT"
    mutated["messages"][0] = first
    assert request_fingerprint(mutated) != cassette.fingerprint


def test_no_secret_shaped_content_in_cassettes():
    import re

    secret = re.compile(r"sk-or-[A-Za-z0-9]|Bearer\s+[A-Za-z0-9]{8,}")
    for path in CASSETTE_ROOT.rglob("*.json"):
        assert not secret.search(path.read_text(encoding="utf-8")), path
