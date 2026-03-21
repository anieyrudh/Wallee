# Device Pack Guide

A device pack connects Wallee to a specific piece of hardware. It translates the generic tool interface into hardware-specific API calls. The framework does not know what a printer, CNC mill, incubator, or fermentation controller is. Device packs teach it.

Device packs usually expose two different runtime surfaces:

- sensor publishers, which run in the background and publish state to the whiteboard
- actuator tools, which the LLM can propose and the engine can dispatch

Only actuator tools are proposed by the LLM. Sensor publishers are never called directly by the model.

## What a device pack contains

```text
wallee/device_packs/your_device/
├── __init__.py
├── sensors.py
├── actuators.py
├── SETUP.md
└── CAPABILITIES.md
```

- `__init__.py` defines `PACK_META` so the registry can identify the pack.
- `sensors.py` contains background publishers that read hardware state and return whiteboard payloads.
- `actuators.py` contains tools the agent can propose and the engine can dispatch.
- `SETUP.md` explains operator setup, network wiring, credentials, ports, and firmware prerequisites.
- `CAPABILITIES.md` is optional but useful for describing the hardware contract, supported commands, and known gaps.

## Minimal `__init__.py`

```python
PACK_META = {
    "name": "your_device",
    "description": "Short description of the hardware integration",
    "bus": "http",  # or serial, gpio, mqtt, udp, local, etc.
    "discovery_match": {"type": "manual"},
}
```

Wallee loads packs by module path. If `DEVICE_PACKS=your_device` is set in `.env`, `main.py` will try to import `wallee.device_packs.your_device`.

## The actual `@tool` contract

All device-pack entry points are normal Python functions decorated with `@tool(...)` from [`wallee/tools/decorator.py`](../tools/decorator.py).

The live decorator supports:

- `kind="sensor"` or `kind="actuator"`
- `refresh_hz` for sensors
- `history_depth` for sensors
- `ttl_ms` override for sensors
- `requires_approval` for actuators
- `max_proposal_age_ms` for actuators
- `precheck_fn` for actuator TOCTOU validation
- `gate_bypass` for non-hardware tools only
- `state_effects` for actuators that change whiteboard state

Sensors must declare `refresh_hz`. The decorator automatically computes `ttl_ms` as twice the refresh period unless you override it.

## Sensors: reading from the Hardware API

Sensors are background functions that periodically read hardware state and return a dict of whiteboard keys. The agent never calls sensors directly. The registry runs them on their own schedule and publishes the returned payload to Redis.

```python
from wallee.tools.decorator import tool


@tool(kind="sensor", refresh_hz=1.0, history_depth=10)
def read_temperatures() -> dict:
    """Read current temperatures from the hardware."""
    response = your_hardware_api.get_temperatures()
    return {
        "device.temp_process": round(float(response["process"]), 1),
        "device.temp_target": round(float(response["target"]), 1),
    }
```

### Sensor rules

- Naming: use descriptive whiteboard keys in `{device}.{measurement}` form.
- Return payloads: sensor functions should return a dict. The registry publishes that dict for you; do not write directly to Redis from the sensor unless you have a very specific reason.
- TTL: every sensor value gets a TTL through the framework. By default it is `2 × refresh period`. Override `ttl_ms` only when the hardware warrants it.
- Frequency: choose `refresh_hz` based on how fast the signal matters. Motion and current can be fast. Identity and inventory can be slow.
- History: use `history_depth > 0` for values that benefit from trend detection.
- No side effects: sensors read hardware. They do not command hardware.
- Error handling: catch communication errors and return `{"error": "..."}`. The sensor loop logs the failure and keeps running.
- Shared clients: if multiple sensors share one HTTP client, serial port, or metrics buffer, protect lazy singletons with a lock. The shipped `prusa_link`, `prusa_metrics`, and `pi_cameras` packs all use this pattern.

## Actuators: writing to the Hardware API

Actuators are tools the LLM can propose. Every hardware actuator call goes through the engine gate pipeline before reaching hardware.

```python
from wallee.tools.decorator import tool


def _precheck_set_temperature(whiteboard=None, target=None, **kwargs) -> dict:
    if target is None:
        return {"error": "target is required"}
    target = float(target)
    if target < 0 or target > 300:
        return {"error": f"Temperature {target}C out of range 0-300"}
    return {"status": "ok"}


@tool(
    kind="actuator",
    requires_approval=False,
    precheck_fn=_precheck_set_temperature,
    state_effects=["device.target_temperature"],
)
def set_temperature(whiteboard=None, target=None, zone: str = "process", **kwargs) -> dict:
    """Set a target temperature on the hardware."""
    if target is None:
        return {"error": "target is required"}

    result = your_hardware_api.set_temperature(zone=zone, target=float(target))
    if not result.ok:
        return {"error": f"hardware returned {result.status_code}"}

    return {"status": "ok", "zone": zone, "target": float(target)}
```

### Actuator rules

- `state_effects`: declare which whiteboard keys this tool changes. The external-change detector relies on `state_effects` to avoid treating the agent's own actions as unexplained outside interference.
- `requires_approval`: set `True` for destructive, irreversible, or operator-sensitive actions such as `cancel_job`, `start_batch`, `purge`, or anything that can waste material or create risk.
- `gate_bypass`: set `True` only for non-hardware tools such as knowledge lookup, web search, or human notification. Never set `gate_bypass=True` on a physical actuator.
- Parameter names matter: the LLM sees the tool name, signature, and docstring in the prompt. Choose names that make misuse unlikely.
- Return a dict: use `{"status": "ok", ...}` on success or `{"error": "..."}` on failure. The engine logs the result into the ledger and diary.
- Whiteboard access: actuator functions may accept `whiteboard=None`. The engine passes the whiteboard in so prechecks and tool bodies can read current discrete state safely.
- Deadlines: reduce `max_proposal_age_ms` for time-sensitive actions such as pause, resume, or motion nudges.

## TOCTOU prechecks

If a tool has state-sensitive preconditions, write a separate helper and pass it through `precheck_fn=...` on the decorator. This is how the shipped Prusa pack protects actions like `pause_print`, `resume_print`, `start_print`, `set_speed_factor`, and `set_position`.

Use prechecks for discrete states such as:

- controller is `IDLE`
- enclosure door is closed
- spindle is stopped
- job is actually `PAUSED`

Do not use prechecks for noisy continuous values that may drift by a tiny amount between validation and dispatch.

## Registering your pack

You do not edit the decorator or registry for normal pack creation.

1. Create `wallee/device_packs/your_device/`.
2. Add `__init__.py`, `sensors.py`, and `actuators.py`.
3. Set `DEVICE_PACKS=your_device` in `.env`, or append it to the existing comma-separated list.

The registry automatically imports `sensors.py` and `actuators.py` and registers every decorated function it finds.

## Whiteboard naming conventions

Use stable, domain-readable prefixes:

- `printer.*` for a printer
- `cnc.*` for a CNC controller
- `fermentation.*` for a fermenter
- `oven.*` for a kiln or heater bank

Good examples:

- `cnc.spindle_rpm`
- `cnc.feed_override`
- `fermentation.temp_beer`
- `fermentation.temp_target`
- `fermentation.cooling_relay`

Bad examples:

- `value1`
- `status_now`
- `currentThing`

The prompt builder will show these keys directly to the agent. Naming quality affects reasoning quality.

## The Human API

The human operator is effectively a built-in high-latency device with physical capabilities. `call_human` is just another actuator tool, except it bypasses the hardware safety gates and routes through Telegram, CLI, and a durable outbox.

Your device pack should not send messages to Telegram directly. If hardware needs human hands, surface that in the tool result and let the agent decide whether to call the human tool.

Example:

```python
return {"error": "Manual clearing required: jam is beyond automatic recovery"}
```

That keeps the pack focused on reporting hardware truth. The agent decides whether the next step is `call_human(...)`, `WAIT`, or another tool.

## Knowledge files for your domain

If you are adding a new hardware family, extend the knowledge layer as well:

- `SOUL.md`: identity, operating posture, and domain temperament
- `LEARNED.md`: compressed reasoning heuristics and cross-signal intuition
- `REFERENCE.md`: issue lookup matrix for `lookup_issue`
- optional domain observations in `OBSERVATIONS.md` or seed material in `docs/internal/BOOTSTRAP_NOTES.md`

The better the knowledge matches the hardware, the less the agent has to guess.

## Example: minimal device pack

This is a small, real-framework example for a temperature-only controller.

```python
# wallee/device_packs/simple_heater/sensors.py
from wallee.tools.decorator import tool
import requests


@tool(kind="sensor", refresh_hz=1.0, history_depth=10)
def read_temperature() -> dict:
    resp = requests.get("http://heater.local/api/temperature", timeout=3)
    resp.raise_for_status()
    data = resp.json()
    return {
        "heater.current_temp": round(float(data["temperature"]), 1),
        "heater.target_temp": round(float(data["target"]), 1),
    }
```

```python
# wallee/device_packs/simple_heater/actuators.py
from wallee.tools.decorator import tool
import requests


def _precheck_set_target(whiteboard=None, target=None, **kwargs) -> dict:
    if target is None:
        return {"error": "target is required"}
    target = float(target)
    if target < 0 or target > 100:
        return {"error": f"Target {target}C outside bounds 0-100"}
    return {"status": "ok"}


@tool(
    kind="actuator",
    precheck_fn=_precheck_set_target,
    state_effects=["heater.target_temp"],
)
def set_target(whiteboard=None, target=None, **kwargs) -> dict:
    resp = requests.post(
        "http://heater.local/api/target",
        json={"target": float(target)},
        timeout=3,
    )
    if resp.ok:
        return {"status": "ok", "target": float(target)}
    return {"error": f"API returned {resp.status_code}"}
```

That is enough for the framework to:

- publish `heater.current_temp` and `heater.target_temp`
- expose `set_target(target=...)` to the agent
- validate dispatch through the engine
- log results in the ledger

## Checklist

- `PACK_META` exists in `__init__.py`
- every sensor has `@tool(kind="sensor", refresh_hz=...)`
- every actuator that changes state declares `state_effects`
- every destructive actuator sets `requires_approval=True`
- no hardware actuator uses `gate_bypass=True`
- shared hardware clients are lock-protected
- `SETUP.md` explains credentials, ports, wiring, and startup requirements
- tests cover the happy path and at least one communication failure path
