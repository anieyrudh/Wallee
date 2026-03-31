from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from wallee_v6.models import WorldPacket
from wallee_v6.packs.prusa_core_one_plus.adapters import PrintableFile, PrusaCoreOneSettings
from wallee_v6.packs.prusa_core_one_plus.pack import Pack
from wallee_v6.whiteboard import InMemoryWhiteboard
from wallee_v6.models import PackManifest


@dataclass
class FakeMetrics:
    data: dict[str, float | int | bool]

    def drain(self):
        return dict(self.data)

    def close(self):
        return None


@dataclass
class FakeSerial:
    data: dict[str, float] | None

    def maybe_poll(self):
        return self.data

    def close(self):
        return None


class FakeHttp:
    def __init__(self, *, status: dict[str, Any], info: dict[str, Any] | None = None, files: list[PrintableFile] | None = None):
        self.status = status
        self.info = info or {"serial": "15715-TEST", "nozzle_diameter": 0.4, "min_extrusion_temp": 170}
        self.files = files or []
        self.commands: list[tuple[str, Any]] = []

    def get_status(self):
        return self.status

    def get_info(self):
        return self.info

    def list_usb_files(self):
        return list(self.files)

    def pause_job(self, job_id=None):
        self.commands.append(("pause", job_id))
        self.status.setdefault("printer", {})["state"] = "PAUSED"
        self.status.setdefault("job", {})["state"] = "PAUSED"

    def resume_job(self, job_id=None):
        self.commands.append(("resume", job_id))
        self.status.setdefault("printer", {})["state"] = "PRINTING"
        self.status.setdefault("job", {})["state"] = "PRINTING"

    def cancel_job(self, job_id=None):
        self.commands.append(("cancel", job_id))
        self.status.setdefault("printer", {})["state"] = "IDLE"
        self.status.pop("job", None)

    def start_print(self, file_path: str):
        self.commands.append(("start", file_path))
        self.status.setdefault("printer", {})["state"] = "PRINTING"
        self.status["job"] = {
            "id": 42,
            "state": "PRINTING",
            "progress": 0,
            "file": {"name": "BENCHY~2.BGC", "display_name": file_path},
        }

    def post_gcode(self, command: str):
        self.commands.append(("gcode", command))


def _manifest() -> PackManifest:
    return PackManifest(
        pack_id="prusa_core_one_plus",
        display_name="Prusa Core One+",
        category="additive",
        python_entrypoint="wallee_v6.packs.prusa_core_one_plus.pack:Pack",
    )


def _settings() -> PrusaCoreOneSettings:
    return PrusaCoreOneSettings(
        host="http://printer.local",
        api_key="token",
        http_timeout_s=1.0,
        file_action_limit=2,
        state_transition_timeout_s=0.01,
        safe_to_unload_bed_c=35.0,
        safe_to_touch_nozzle_c=50.0,
        serial_enabled=False,
        serial_port=None,
        serial_baud=115200,
        serial_timeout_s=0.5,
        serial_poll_interval_s=10.0,
        serial_error_backoff_s=30.0,
        metrics_enabled=True,
        metrics_bind_host="127.0.0.1",
        metrics_port=8514,
    )


def _world_from_pack(pack: Pack, whiteboard: InMemoryWhiteboard, goal: str = "Test goal") -> WorldPacket:
    pack.publish_raw_state(whiteboard)
    normalized = pack.normalize(whiteboard.snapshot().values)
    return WorldPacket(
        goal=goal,
        device_summaries=[normalized.summary],
        facts=normalized.facts,
        resources=normalized.resources,
        blockers=normalized.blockers,
        deltas=[],
        frontier=[],
        last_result={},
        pending_human=[],
    )


def test_normalize_uses_safe_unload_proxy_and_requested_file():
    http = FakeHttp(
        status={
            "printer": {"state": "FINISHED", "temp_nozzle": 34.0, "target_nozzle": 0.0, "temp_bed": 30.0, "target_bed": 0.0},
            "job": {"id": 42, "state": "FINISHED", "progress": 100, "file": {"display_name": "Benchy Rules.bgcode"}},
        },
        files=[PrintableFile(path="/usb/PRINTS/BENCHY~2.BGC", display_name="Benchy Rules.bgcode")],
    )
    pack = Pack(_manifest(), http_client=http, metrics=FakeMetrics({"metrics_fresh": True}), serial_diag=FakeSerial({"temp_chamber_c": 30.0}), settings=_settings())
    whiteboard = InMemoryWhiteboard()
    whiteboard.publish("factory.requested_file", "Benchy Rules.bgcode")

    world = _world_from_pack(pack, whiteboard)

    assert world.facts["printer_1.safe_to_unload"] is True
    assert world.facts["printer_1.requested_file_present"] is True
    assert any(resource.id == "printer_1.bed" and resource.state == "OCCUPIED" for resource in world.resources)


def test_candidate_actions_running_contains_pause_and_cancel():
    http = FakeHttp(
        status={
            "printer": {"state": "PRINTING", "temp_nozzle": 215.0, "target_nozzle": 215.0, "temp_bed": 60.0, "target_bed": 60.0},
            "job": {"id": 42, "state": "PRINTING", "progress": 50, "file": {"display_name": "Benchy Rules.bgcode"}},
        }
    )
    pack = Pack(_manifest(), http_client=http, metrics=FakeMetrics({}), serial_diag=FakeSerial(None), settings=_settings())
    whiteboard = InMemoryWhiteboard()
    world = _world_from_pack(pack, whiteboard)

    actions = pack.candidate_actions(world)
    action_ids = {action.action_id for action in actions}
    assert {"A_PRUSA_PAUSE", "A_PRUSA_CANCEL"}.issubset(action_ids)


def test_candidate_actions_idle_with_requested_file_contains_start():
    http = FakeHttp(
        status={
            "printer": {"state": "IDLE", "temp_nozzle": 28.0, "target_nozzle": 0.0, "temp_bed": 27.0, "target_bed": 0.0},
        },
        files=[PrintableFile(path="/usb/PRINTS/BENCHY~2.BGC", display_name="Benchy Rules.bgcode")],
    )
    pack = Pack(_manifest(), http_client=http, metrics=FakeMetrics({}), serial_diag=FakeSerial(None), settings=_settings())
    whiteboard = InMemoryWhiteboard()
    whiteboard.publish("factory.requested_file", "Benchy Rules.bgcode")
    world = _world_from_pack(pack, whiteboard, goal="Start the benchy print")

    actions = pack.candidate_actions(world)
    assert any(action.verb == "START_PROCESS" for action in actions)


def test_candidate_actions_safe_part_without_handler_calls_human_to_unload():
    http = FakeHttp(
        status={
            "printer": {"state": "FINISHED", "temp_nozzle": 34.0, "target_nozzle": 0.0, "temp_bed": 30.0, "target_bed": 0.0},
            "job": {"id": 99, "state": "FINISHED", "progress": 100, "file": {"display_name": "Widget.bgcode"}},
        }
    )
    pack = Pack(_manifest(), http_client=http, metrics=FakeMetrics({}), serial_diag=FakeSerial(None), settings=_settings())
    whiteboard = InMemoryWhiteboard()
    world = _world_from_pack(pack, whiteboard, goal="Unload the finished print")

    actions = pack.candidate_actions(world)
    assert any(action.action_id == "A_PRUSA_MANUAL_UNLOAD" for action in actions)


def test_realize_pause_and_start_update_http_state_and_whiteboard():
    http = FakeHttp(
        status={
            "printer": {"state": "PRINTING", "temp_nozzle": 215.0, "target_nozzle": 215.0, "temp_bed": 60.0, "target_bed": 60.0},
            "job": {"id": 42, "state": "PRINTING", "progress": 50, "file": {"display_name": "Benchy Rules.bgcode"}},
        },
        files=[PrintableFile(path="/usb/PRINTS/BENCHY~2.BGC", display_name="Benchy Rules.bgcode")],
    )
    pack = Pack(_manifest(), http_client=http, metrics=FakeMetrics({}), serial_diag=FakeSerial(None), settings=_settings())
    whiteboard = InMemoryWhiteboard()

    world = _world_from_pack(pack, whiteboard)
    pause = next(action for action in pack.candidate_actions(world) if action.verb == "PAUSE_PROCESS")
    result = pack._realize(pause, whiteboard)
    assert result["mode"] == "PAUSED"
    assert http.commands[-1][0] == "pause"

    http.status = {
        "printer": {"state": "IDLE", "temp_nozzle": 27.0, "target_nozzle": 0.0, "temp_bed": 27.0, "target_bed": 0.0},
    }
    whiteboard.publish("printer_1.part_present", False)
    whiteboard.publish("factory.requested_file", "Benchy Rules.bgcode")
    world = _world_from_pack(pack, whiteboard)
    start = next(action for action in pack.candidate_actions(world) if action.verb == "START_PROCESS")
    result = pack._realize(start, whiteboard)
    assert result["job_active"] is True
    assert whiteboard.get("printer_1.part_present") is True
    assert http.commands[-1] == ("start", "Benchy Rules.bgcode")


def test_publish_raw_state_degrades_cleanly_when_http_is_unavailable():
    class BrokenHttp:
        def get_status(self):
            raise RuntimeError("connection refused")

        def get_info(self):
            raise AssertionError("should not be called")

        def list_usb_files(self):
            raise AssertionError("should not be called")

        def pause_job(self, job_id=None):
            raise AssertionError

        def resume_job(self, job_id=None):
            raise AssertionError

        def cancel_job(self, job_id=None):
            raise AssertionError

        def start_print(self, file_path):
            raise AssertionError

        def post_gcode(self, command):
            raise AssertionError

    pack = Pack(_manifest(), http_client=BrokenHttp(), metrics=FakeMetrics({}), serial_diag=FakeSerial(None), settings=_settings())
    whiteboard = InMemoryWhiteboard()
    pack.publish_raw_state(whiteboard)

    assert whiteboard.get("printer_1.raw.connected") is False
    assert whiteboard.get("printer_1.raw.health") == "OFFLINE"


def test_registry_can_load_prusa_pack_without_touching_hardware(monkeypatch, tmp_path):
    from pathlib import Path

    from wallee_v6.config import Config
    from wallee_v6.registry import PackRegistry

    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "prusa_core_one_plus")
    monkeypatch.setenv("WALLEE_SIMULATION", "0")
    monkeypatch.setenv("PRUSA_CORE_ONE_HOST", "http://printer.local")
    monkeypatch.setenv("PRUSA_CORE_ONE_ENABLE_METRICS", "0")
    monkeypatch.setenv("PRUSA_CORE_ONE_ENABLE_SERIAL", "0")

    config = Config.from_env(repo_root=repo_root)
    registry = PackRegistry(config)
    registry.load()
    assert registry.get("prusa_core_one_plus").pack_id == "prusa_core_one_plus"
    registry.close_all()


def test_registry_can_load_prusa_pack_from_v5_host_alias_only(monkeypatch, tmp_path):
    from pathlib import Path

    from wallee_v6.config import Config
    from wallee_v6.registry import PackRegistry

    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("WALLEE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLEE_ENABLED_PACKS", "")
    monkeypatch.setenv("WALLEE_SIMULATION", "0")
    monkeypatch.delenv("PRUSA_CORE_ONE_HOST", raising=False)
    monkeypatch.setenv("PRUSALINK_HOST", "http://printer.local")
    monkeypatch.setenv("PRUSA_CORE_ONE_ENABLE_METRICS", "0")
    monkeypatch.setenv("PRUSA_CORE_ONE_ENABLE_SERIAL", "0")

    config = Config.from_env(repo_root=repo_root)
    registry = PackRegistry(config)
    registry.load()
    assert registry.get("prusa_core_one_plus").pack_id == "prusa_core_one_plus"
    registry.close_all()
