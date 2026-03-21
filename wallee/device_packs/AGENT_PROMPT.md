# Prompt for AI Agents: Creating a Wallee Device Pack

Use this prompt when asking an AI coding agent to build a new device pack for Wallee.

## The prompt

```text
You are creating a device pack for Wallee, an architecture for safely letting LLMs operate physical hardware.

Read these files first:
- wallee/device_packs/DEVICE_PACK_GUIDE.md
- wallee/tools/decorator.py
- wallee/device_packs/prusa_link/sensors.py
- wallee/device_packs/prusa_link/actuators.py

Create a device pack for [YOUR HARDWARE] in wallee/device_packs/[your_device]/.

The pack must include:

1. __init__.py
   - Define PACK_META with name, description, bus, and discovery_match.

2. sensors.py
   - Add background publisher tools using @tool(kind="sensor", refresh_hz=..., history_depth=...).
   - Sensor functions should read from the hardware and return dicts of whiteboard keys.
   - Use descriptive keys in {device}.{measurement} form.
   - Catch communication failures and return {"error": "..."} instead of crashing.
   - If multiple sensors share one client or connection, guard lazy singleton creation with a threading lock.

3. actuators.py
   - Add hardware tools using @tool(kind="actuator", ...).
   - Every actuator that changes whiteboard state must declare state_effects=[...].
   - Set requires_approval=True for destructive or irreversible actions.
   - Never use gate_bypass=True for hardware actuators.
   - Use clear parameter names and docstrings because the LLM sees both.
   - Return {"status": "ok", ...} on success or {"error": "..."} on failure.
   - Add precheck helpers and pass them with precheck_fn=... for actions that depend on discrete state.

4. SETUP.md
   - Document wiring, network addresses, ports, credentials, firmware settings, and how to enable the hardware.

5. CAPABILITIES.md (optional but preferred)
   - Document the available sensors, actuators, limits, and known gaps.

6. tests/
   - Add focused pytest coverage for sensor success/failure behavior and actuator success/precheck/error behavior.

Hardware details for [YOUR HARDWARE]:
- Communication interface: [HTTP / serial / GPIO / MQTT / Modbus / local process / etc.]
- Available sensors: [list what can be read]
- Available actuators: [list what can be controlled]
- Safety considerations: [what can go wrong physically]
- Whiteboard prefix: [device name prefix such as cnc, fermenter, oven]

Follow the shipped device-pack patterns:
- Sensors return dicts; the framework publishes those payloads to Redis.
- Sensors are background publishers, not tools the LLM calls directly.
- Hardware actuators accept whiteboard=None when they need current state.
- state_effects are declared on actuators that change observable state.
- requires_approval is used on destructive actions.
- gate_bypass is reserved for non-hardware tools like knowledge lookup or human notification.
- Shared hardware clients are cached behind locks.

After creating the pack:
- Add the pack name to DEVICE_PACKS in .env.example if it should be a documented option.
- Add or update knowledge guidance in wallee/knowledge/ for this hardware domain:
  - SOUL.md additions for operator posture
  - LEARNED.md additions for signal interpretation
  - REFERENCE.md entries for common issues and interventions
- Run targeted tests, then run the full suite if the pack integrates cleanly.

Verification commands:
- pytest wallee/device_packs/[your_device]/tests/
- pytest -q

Output requirements:
- Show the created file tree.
- Summarize each sensor and actuator.
- Call out any assumptions about the hardware API.
- If something is missing from the hardware spec, choose the safer interpretation and note it explicitly.
```
