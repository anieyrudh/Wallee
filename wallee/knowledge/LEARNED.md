# Wallee — Printing Knowledge

This is what experienced 3D printer operators know. Use this knowledge alongside your sensor data and camera feeds to make informed decisions about print quality and diagnosis.

---

## Quick visual diagnosis from camera

What you see on nozzle camera → What it means → What to do:
- Thin strings between features → Stringing/ooze → Reduce nozzle temp 5°C
- Rough/bumpy top surface → Over-extrusion → Reduce flow 3-5%
- Gaps in top surface → Under-extrusion → Increase flow 3-5% or check filament
- First layer not sticking → Bed adhesion failure → Increase bed temp 5°C or reduce speed
- Curling corners → Warping → Increase bed temp 5°C, reduce speed 10%
- Spaghetti (loose filament everywhere) → Print detached from bed → PAUSE immediately, call human
- Blob on nozzle → Filament buildup → PAUSE, call human for cleanup
- Normal extrusion bead → Print is fine → WAIT and observe
- Filament oozing during pause → Normal → Don't panic, minor ooze during pause is expected

IMPORTANT: Camera images are low resolution and often foggy/blurry.
If you're not confident in what you see (< 0.7 confidence), say so and WAIT.
Do NOT take destructive actions (pause/cancel) on uncertain visual readings.
The cost of a false alarm (unnecessary pause) is higher than the cost of
one more observation cycle. When in doubt, observe again next cycle.

---

## Printing fundamentals

A good print has: consistent layer lines, no gaps, no excess material, no warping, and dimensional accuracy. The key variables you can control are nozzle temperature, bed temperature, chamber temperature, print speed, and flow rate. Small adjustments (5-10°C, 5-10% speed/flow) are safe to try. Large adjustments need more caution.

PLA: nozzle 190-220°C, bed 50-65°C. Sensitive to heat creep and stringing at high temps.
PETG: nozzle 220-250°C, bed 70-90°C. Strings more than PLA, needs slower speeds and higher retraction.
ASA/ABS: nozzle 240-260°C, bed 90-110°C, chamber 35-45°C. Warps without enclosure. Needs stable chamber temp.
TPU: nozzle 210-230°C, bed 40-60°C. Very slow printing, flexible — don't retract aggressively.

The material being used is visible in the metrics (material field from OctoPrint compat endpoint) and in the print filename convention.

---

## What you can fix autonomously

### Stringing / oozing
Thin threads of filament between travel moves. Visible on nozzle camera as wisps or threads.
**What operators do:** Lower nozzle temp by 5-10°C. Reduce speed slightly. This reduces ooze during travel moves. If it's severe, the print is still usually salvageable — cosmetic issue, not structural.

### Slight overextrusion
Lines look too fat, surface is bumpy/rough, corners have buildup.
**What operators do:** Reduce flow rate by 2-5%. If nozzle temp is at the high end for the material, reduce by 5°C.

### Slight underextrusion
Lines have gaps, surface looks thin or rough, infill is sparse.
**What operators do:** Increase flow rate by 2-5%. If nozzle temp is at the low end, increase by 5°C. Check if filament sensor flow count is lower than expected — could indicate partial clog building up.

### Temperature not reaching target
Temp stays more than 5°C below target for more than 60 seconds.
**What operators do:** Check heater PWM — if it's at max and temp still isn't rising, that's a hardware problem (escalate to human). If PWM is below max, the firmware PID controller might be struggling — usually resolves itself. Wait and monitor for 2-3 minutes before acting.

### Temperature overshooting
Temp exceeds target by more than 10°C.
**What operators do:** Usually the PID controller recovers. If it doesn't come back within 2 minutes, reduce the target temperature by 5°C to give the controller room. If overshooting repeatedly, flag to human — PID might need tuning.

### Print speed causing artifacts
Ringing/ghosting visible as ripples on surface near corners. Position data may show oscillation after direction changes.
**What operators do:** Reduce speed by 10-20%. This is the most common quality-vs-time tradeoff. Slower almost always means better quality.

### Chamber too hot / too cold
Chamber temp drifting from target. Affects ASA/ABS prints significantly.
**What operators do:** If too hot — chamber fan should be running. If door_sensor shows open, the operator probably opened it intentionally. If too cold — check chamber heater PWM. Small drifts (±3°C) are normal. Larger drifts on enclosed prints: adjust chamber target up/down.

### First layer issues (camera detected)
Nozzle camera shows first layer not adhering, curling up, or being dragged by nozzle.
**What operators do:** Increase bed temp by 5°C. Slow down first few layers (reduce speed to 70-80%). If it's really bad, pause and let the human clean the bed — you can't fix adhesion remotely if the bed surface is contaminated.

### Door opened during print
door_sensor state changes.
**What operators do:** For PLA prints, usually fine — PLA doesn't need enclosure. For ASA/ABS, monitor chamber temp. If it drops more than 5°C, slow the print down 10% to compensate for the thermal change. Don't panic — brief door opens are normal (operator checking the print).

---

## What needs human hands

Only escalate to human (CALL_HUMAN) for these situations:

- **Filament runout** — fsensor state drops, flow stops. You can't load filament remotely. Pause and wait.
- **Filament jam** — fsensor shows motor turning but no flow. You can pause, but clearing a jam requires hands.
- **Complete print detachment** — nozzle camera shows spaghetti (filament in air, not on the print). Nothing to save. Cancel the print.
- **Overcurrent** — oc_nozz or oc_inp goes non-zero. Safety kernel handles the alert. You should pause. This is electrical — don't touch.
- **Voltage anomaly** — volt_bed drops below 22V during active heating. PSU or wiring issue. Pause, alert human.
- **Mechanical failure** — stepper stall counter incrementing rapidly, position data showing large unexpected jumps. Pause immediately. Belt or motor issue.
- **Network loss to printer** — HTTP API unreachable. You still have cameras. Report what you see, wait for network recovery.
- **Anything you're genuinely uncertain about** — better to ask than guess wrong. But try to include your analysis and what you'd recommend.

---

## Consistency and repeatability

The goal is to produce structurally and visually similar products across prints. What affects consistency:

**Environmental conditions:** Ambient temperature changes between prints affect cooling rates. If ambient_temp differs by more than 5°C from the previous successful print, expect potential dimensional differences. Log the ambient conditions at print start.

**Temperature stability during print:** Nozzle should stay within ±2°C of target, bed within ±1°C. If you see drift, the first move is to check if something external changed (door opened, ambient shift). Small autonomous corrections are fine — nudge the target to compensate.

**First layer baseline:** The first layer sets the foundation. Compare nozzle camera images from the first layer of this print to previous successful prints. Consistent first layers predict consistent prints.

**Flow and speed adjustments:** If you adjust flow or speed during a print, record why. If the same adjustment is needed on multiple consecutive prints, it might indicate a systematic issue (partial clog developing, filament diameter variation, ambient temp change).

**Between prints:** Before starting a new print of the same model, compare the environment: ambient temp, chamber temp, bed temp at idle, time since last print (bed may still be warm). Closer conditions to the previous successful print mean more consistent results.

---

## Prusa Core One+ quirks

- PUT /api/v1/job returns 405 during many states. Pause/resume uses M25/M24 G-code injection.
- HTTP API reports PRINTING during purge/preparation. Use job.phase to distinguish PREPARING from actual PRINTING.
- Metrics stream stops sending temp_bed and chamber_temp during IDLE. HTTP API always reports them.
- USB serial disconnects every 1-3 seconds. Only used for diagnostic commands (M119 endstops).
- Filament sensor false positives are common above 60% humidity.
