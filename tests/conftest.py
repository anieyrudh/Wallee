from __future__ import annotations

from pathlib import Path

import pytest

from wallee.config import Config
from wallee.engine import Engine
from wallee.human import HumanGateway
from wallee.planning_context import WorldCompiler
from wallee.predicates import PredicateEvaluator
from wallee.registry import PackRegistry
from wallee.runtime_db import RuntimeDB
from wallee.safety import SafetyKernel
from wallee.whiteboard import InMemoryWhiteboard


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "sim_printer,sim_arm")
    monkeypatch.setenv("WALLEE_SIMULATION", "1")
    config = Config.from_env(repo_root=repo_root)

    runtime_db = RuntimeDB(config.db_path)
    whiteboard = InMemoryWhiteboard()
    registry = PackRegistry(config)
    registry.load()
    registry.publish_all_raw_state(whiteboard)
    predicate_evaluator = PredicateEvaluator()
    world_compiler = WorldCompiler(
        config=config,
        registry=registry,
        whiteboard=whiteboard,
        runtime_db=runtime_db,
        predicate_evaluator=predicate_evaluator,
    )
    human = HumanGateway(config=config, runtime_db=runtime_db)
    safety = SafetyKernel(
        heartbeat_timeout_s=config.heartbeat_timeout_seconds,
        latch_path=config.estop_latch_path,
    )
    engine = Engine(
        config=config,
        runtime_db=runtime_db,
        whiteboard=whiteboard,
        registry=registry,
        world_compiler=world_compiler,
        human_gateway=human,
        safety=safety,
        predicate_evaluator=predicate_evaluator,
    )

    yield {
        "config": config,
        "db": runtime_db,
        "whiteboard": whiteboard,
        "registry": registry,
        "compiler": world_compiler,
        "human": human,
        "safety": safety,
        "engine": engine,
    }

    runtime_db.close()
