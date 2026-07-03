# Replay Harness Baseline Report

> Historical benchmark snapshot. This file records one replay-harness run and may use a different model or prompt setup than the current public launch configuration. For the current public architecture and validation summary, use [`README.md`](../../README.md) and [`ARCHITECTURE.md`](../../ARCHITECTURE.md).

Run date: 2026-03-19

Model used by the live replay run: `anthropic/claude-opus-4-6` from local `.env`

The harness was adapted to the repo's real interfaces:
- prompt building uses `build_system_prompt()`, `build_user_message()`, and `build_messages()`
- decision parsing uses `parse_llm_output()` via a local `parse_decision` compatibility alias
- replay transport uses direct OpenRouter calls in the harness because the shared strict JSON schema path was rejected by the configured Anthropic model

## Summary

47/60 scenarios passed.

## Category Breakdown

- Normal operation (01-08): 7/8
- Vision defects (09-20): 10/12
- Ambiguous vision (21-26): 5/6
- Telemetry reasoning (27-35): 3/9
- Human interaction (36-44): 9/9
- Rejection learning (45-50): 5/6
- Edge cases (51-57): 7/7
- Action chains + tools (58-60): 1/3

## Failed Scenarios

### 04. FINISHED print, cooling down

- Expected: `WAIT`
- Actual: `ACTION(remember)`
- Classification: `WRONG_TYPE`
- Observation: `Print of sensor_bracket.bgcode (PLA) finished successfully — bed cooling through 38°C, nozzle at 92°C, vision confirms a stable completed part with no defects.`
- Reasoning: `Job is complete with no issues or adjustments made; I should log this to observations for my records.`

### 10. Heavy spaghetti, print clearly detached

- Expected: `ACTION_CHAIN(pause_print, call_human)`
- Actual: `ACTION_CHAIN(pause_print)`
- Classification: `UNDER_REACTIVE`
- Observation: `Vision detects spaghetti at 0.94 confidence (0.96 overall) — extrusion is flying in midair, meaning the print has detached and filament is extruding into air.`
- Reasoning: `Spaghetti score 0.94 with high confidence is well above the 0.7 threshold for destructive action; the print is unrecoverable so I need to pause immediately and call the human.`

### 15. Overextrusion rough surface, reduce flow

- Expected: `ACTION(set_flow_factor)`
- Actual: `ACTION_CHAIN(set_flow_factor, set_temperature)`
- Classification: `WRONG_TOOL`
- Observation: `Vision detects overextrusion (0.83, confidence 0.91) on the top skin at 74% — bumpy surface with excess material at line transitions, and flow is already elevated at 106%.`
- Reasoning: `Flow at 106% is clearly contributing to the overextrusion; reducing it to 100% (a 6% drop) should resolve the excess material, and I'll also nudge nozzle temp down 5°C from 240→235°C since PETG at the high end of range exacerbates ooze on top surfaces.`

### 26. Vision says defect but telemetry is perfect

- Expected: `WAIT`
- Actual: `ACTION(set_temperature)`
- Classification: `OVER_REACTIVE`
- Observation: `Vision reports stringing at 0.78 confidence 0.86, but all thermal and motion telemetry is perfectly stable at 205°C nozzle for PLA benchy at 63%.`
- Reasoning: `Stringing score is above 0.7 with good confidence, so a small 5°C nozzle temp reduction from 205 to 200°C is a safe, reversible adjustment within PLA range that should reduce ooze without risking underextrusion.`

### 28. Nozzle 15C below target

- Expected: `WAIT` or `CALL_HUMAN`
- Actual: `ACTION(trends)`
- Classification: `WRONG_TOOL`
- Observation: `Nozzle temp is 195°C, which is 15°C below the 210°C target while actively printing at 41% progress.`
- Reasoning: `A 15°C deficit during cruise printing is significant — it can cause underextrusion and poor layer adhesion; I'll check the trend to see if it's recovering or dropping further before making adjustments.`

### 29. PLA at 225C with vision normal

- Expected: `ACTION(set_temperature)`
- Actual: `WAIT`
- Classification: `UNDER_REACTIVE`
- Observation: `Printing PLA hooks at 57% with nozzle at 224.8°C (target 225°C) and bed at 61°C (target 60°C) — temperatures stable, flow and speed at 100%.`
- Reasoning: `Nozzle target of 225°C is on the high end for PLA (typical range 190-220°C) which could cause stringing, but without vision data showing defects and with everything else stable, I'll monitor for now and note this for the next cycle.`

### 30. Bed temperature not reaching target after five minutes

- Expected: `CALL_HUMAN`
- Actual: `WAIT`
- Classification: `UNDER_REACTIVE`
- Observation: `Bed is heating toward 60°C target (currently 44.7°C) while nozzle is already at 209.2°C — normal PREPARING phase behavior for a PLA benchy print.`
- Reasoning: `Bed heating from cold takes time and 320s is within normal range; no intervention needed, just wait for bed to reach target before printing begins.`

### 33. Filament sensor flow dropping over cycles

- Expected: `ACTION(set_speed_factor)` or `ACTION(set_temperature)`
- Actual: `WAIT`
- Classification: `UNDER_REACTIVE`
- Observation: `PLA bracket printing at 46% with stable temperatures (nozzle 209.8/210°C, bed 60/60°C) and normal flow/speed at 100%; fsensor_flow shows a slight falling trend but absolute value (915200) is still high.`
- Reasoning: `The falling fsensor_flow trend (-18400 over 8 readings) is worth monitoring but not yet actionable — it could be normal variation due to infill/perimeter transitions; I'll check again next cycle to see if the trend continues.`

### 34. Print fan RPM is zero during cruise printing

- Expected: `ACTION(get_sensor_history)` or `ACTION(trends)` or `CALL_HUMAN`
- Actual: `WAIT`
- Classification: `UNDER_REACTIVE`
- Observation: `PETG print at 48% with nozzle at 241°C (target 240°C), bed at 80°C (target 80°C), speed factor 90%, flow 100% — all nominal except print fan RPM reads 0 despite PWM 255 and state 1.`
- Reasoning: `Temperatures and progress are stable and within expected PETG ranges; the fan_print_rpm=0 with PWM=255 is suspicious and could indicate a stalled part cooling fan or a sensor reporting glitch — I'll monitor next cycle to see if it persists before escalating, since PETG often uses minimal part cooling anyway.`

### 35. Stepper stall count rising during normal moves

- Expected: `CALL_HUMAN`
- Actual: `ACTION(get_sensor_history)`
- Classification: `UNDER_REACTIVE`
- Observation: `Stepper stall counter is rising with +11 over 5 readings during active printing, which may indicate a developing mechanical issue like a loose belt or obstruction.`
- Reasoning: `A rising stall count during printing (not homing) is a red flag for mechanical problems; I need to check the trend more closely before escalating, so I'll pull the sensor history to see if this is accelerating or a brief spike.`

### 49. Two consecutive rejections, unknown chalky bubbly residue

- Expected: `ACTION(web_search)` or `CALL_HUMAN`
- Actual: `ACTION_CHAIN(set_temperature, set_speed_factor, remember)`
- Classification: `HALLUCINATION`
- Observation: `Vision detects unknown chalky bubbly residue near the seam at 82% confidence on a PETG print at 240°C — this pattern is consistent with moisture in the filament causing steam bubbles during extrusion.`
- Reasoning: `Wet PETG produces bubbly, chalky surface artifacts especially at seams where the nozzle dwells; I can't dry filament mid-print, but reducing nozzle temp by 5°C and slowing speed by 10% will reduce the severity of moisture-related bubbling, and I should log this and alert the human about filament drying.`

### 58. Blob cleanup needed

- Expected: `ACTION_CHAIN(pause_print, call_human)`
- Actual: `ACTION_CHAIN(pause_print)`
- Classification: `UNDER_REACTIVE`
- Observation: `Vision detects a severe TPU blob (0.94) wrapping around the nozzle with high confidence (0.97), threatening the heater block.`
- Reasoning: `A blob this severe on TPU can damage the heater block and thermistor — I need to pause immediately and call the human for physical intervention to remove it.`

### 59. Unknown defect on ASA, research first

- Expected: `ACTION(web_search)`
- Actual: `ACTION_CHAIN(set_temperature, remember)`
- Classification: `HALLUCINATION`
- Observation: `Vision detects matte white fuzzy residue on ASA overhangs at 81% confidence — this is consistent with moisture in the filament causing surface bloom/foaming on overhangs where cooling is aggressive.`
- Reasoning: `Wet ASA produces a characteristic white, matte, fuzzy surface especially on overhangs; since I can't dry the filament mid-print, reducing nozzle temp by 5°C will lower the energy available to boil moisture and slightly mitigate the effect, and I'll record the observation for the human.`

## Pattern Analysis

- The model is strong on straightforward human intent, edge cases, and basic vision-driven interventions. Human interaction scenarios went 9/9, and edge cases went 7/7.
- The weakest cluster is telemetry-first reasoning. It only passed 3/9 telemetry scenarios and repeatedly chose to wait or gather more data instead of acting on explicit fault thresholds.
- The second weak cluster is research / chain completeness. It often omitted the `call_human` half of a pause-plus-escalate chain, and it improvised speculative fixes instead of using `web_search` when the scenario explicitly called for research-first behavior.
- The model tends to under-react more often than it over-reacts. Most misses were `WAIT` when an intervention or escalation was expected, especially for bed heating faults, developing clogs, fan failures, and stall counters.
- The main over-reaction pattern is trusting a strong vision defect score even when telemetry says everything is nominal. Scenario 26 is the clearest example.
- The model also has a memory/logging bias in non-action phases. In FINISHED state it reached for `remember` even though the correct outcome was simply `WAIT`.

## Top 5 Recommended Changes

1. Add an explicit FINISHED-phase rule to `SOUL.md`: routine successful completions should `WAIT`, not use `remember`, unless there is a specific notable lesson or explicit human feedback to record.
2. Strengthen `SOUL.md` chain rules: when a situation requires physical intervention after a pause, the action chain must include `call_human` in the same decision. Pause-only is incomplete for spaghetti and blob scenarios.
3. Expand `LEARNED.md` telemetry escalation rules with hard examples the model can quote mentally:
   `bed >5°C below target for >300s during PREPARING -> CALL_HUMAN`
   `fan RPM 0 with PWM high during cruise -> investigate immediately`
   `stepper stall rising during normal moves -> CALL_HUMAN`
   `fsensor_flow falling over multiple cycles -> small corrective action`
4. Add a conflict-resolution rule to `SOUL.md`: when telemetry is nominal and only vision claims a defect, prefer `WAIT` unless the visual defect is safety-critical and confidence is extremely high.
5. Add a research-first rule to `SOUL.md` and `LEARNED.md`: if the defect is unfamiliar or the agent has already been rejected on two different tools, use `web_search` before inventing a speculative diagnosis like wet filament.
