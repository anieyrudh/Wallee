# Bootstrap Observations

Synthetic first-person experiences for seeding OBSERVATIONS.md. Review before deploying.

## Early experience — PLA benchy
Noticed stringing between pillars. Vision showed stringing around 0.65. Nozzle was at 215C. Reduced temp to 210C with set_temperature. Stringing dropped to 0.3 after two cycles.
Lesson: PLA strings noticeably above roughly 212C on this printer. A 5C reduction is usually enough.

## Early experience — PETG bracket
Vision flagged stringing at 0.5 on PETG at 240C. Investigated but did not act — PETG strings by nature at normal printing temperatures. Print finished fine with no structural issues.
Lesson: PETG baseline stringing is higher than PLA. Do not act on stringing below roughly 0.7 for PETG.

## Early experience — PLA phone stand
Vision showed overextrusion at 0.65. Top surface was bumpy with excess material collecting at corners. Flow was at 100%. Reduced flow to 97% with set_flow_factor. Surface improved noticeably after two cycles.
Lesson: small flow reductions (3-5%) are effective for overextrusion. Start with flow before touching temperature.

## Early experience — PLA hook
Bed temp was 44C after 5 minutes of heating, target 60C. Waited three cycles, barely climbing. Called human — turned out the bed thermistor connector was loose.
Lesson: bed not approaching target after roughly 4-5 minutes of heating is a hardware problem, not patience. Call human early.

## Early experience — ASA enclosure part
Print paused externally while I thought everything was fine. Telemetry was normal, vision was normal. Resumed. Paused again. Resumed again. Turned out the human paused because they saw warping I could not detect from my camera angles.
Lesson: external pauses are never random. If something paused me and I do not know why, investigate and call the human — do not assume it is safe to resume.

## Early experience — PLA gear
Vision buddy camera showed spaghetti at 0.7 for one cycle. Telemetry was completely normal — temps on target, flow steady, no stalls. I discounted it as noise. Print had actually failed — filament was extruding into air with nothing beneath it.
Lesson: normal telemetry plus buddy camera spaghetti means print detached. The extruder runs fine when printing into air — that is WHY telemetry looks normal. Trust the camera over telemetry for detachment.

## Early experience — PLA calibration cube
During PREPARING phase, vision flagged blob at 0.6 on the nozzle camera. I almost paused. But PREPARING involves purge and nozzle wipe — blobs during this phase are normal.
Lesson: blob scores during PREPARING are expected. Only act on blob during PRINTING or PAUSED phases.

## Early experience — PETG lid
During PRINTING at 62%, vision flagged blob at 0.75 and growing across two consecutive cycles. Nozzle camera showed plastic accumulating on the heater block. Paused immediately and called human for cleanup.
Lesson: growing blob during PRINTING is real and dangerous. Pause and call human before it reaches wiring.

## Early experience — PLA bracket
Overcurrent flag (oc_nozzle) went non-zero during heating. Called human immediately without waiting for a second reading. Turned out to be a heater cartridge issue.
Lesson: overcurrent is an electrical safety signal. Do not wait, do not investigate further — shut off heaters and call human immediately.

## Early experience — PLA vase
Heatbreak fan RPM dropped to zero while PWM was still active. Nozzle temperature started climbing. Paused and called human. Fan had accumulated dust and seized.
Lesson: fan RPM zero plus active PWM is a stalled fan. For heatbreak fan, this is urgent — heat creep follows quickly. Pause and call human.

## Early experience — PLA figurine
Nozzle temp overshot target by 8C during a fast heating ramp. PID controller started correcting. I watched for two cycles — it came back to within 2C of target.
Lesson: brief temperature overshoot during heating is normal PID behavior. Only intervene if it stays more than 5C above target for more than 2 minutes.

## Early experience — PETG cover
Underextrusion score rose to 0.55 over three cycles. Filament flow was trending down but rotation was still present. Reduced speed to 80% — underextrusion improved to 0.3.
Lesson: for mild underextrusion with filament still moving, reduce speed first. If that does not help, raise nozzle temp. If flow keeps declining, suspect partial clog.

## Early experience — PLA mount
At 92% progress, vision showed minor warping at 0.45 on one corner. Print was almost done. I chose to WAIT and observe rather than adjust — the part was structurally complete and the warping was cosmetic at that stage.
Lesson: late-stage minor defects on nearly complete prints are often better left alone. The risk of intervention exceeding the remaining print time is not worth it.

## Early experience — PLA test print
Proposed set_speed_factor but forgot to include the percent parameter. Engine rejected with "TOCTOU: percent is required." Fixed params and retried with set_speed_factor(percent=85). Worked on second attempt.
Lesson: always check that required params are included before proposing. Read rejection reasons carefully — they tell you exactly what was wrong.

## Early experience — ASA housing
First layer adhesion score dropped to 0.35 during the first two layers. Bed was at 100C, target 105C for ASA. Raised bed temp 5C to 105C. Adhesion improved to 0.7 by next cycle.
Lesson: ASA first layer needs bed temp close to target. Even a small deficit matters. Act quickly on low adhesion scores for ASA.

## Early experience — PETG functional part
Vision showed alternating underextrusion (0.4) and overextrusion (0.35) across cycles. Flow variance was high. Reduced speed to 75%. The alternation settled.
Lesson: alternating under/over extrusion with flow variance usually means feed resistance or a developing restriction. Slowing down reduces pressure swings and is the best first move.

## Early experience — PLA phone case
Tried reducing nozzle temp for stringing, then tried reducing speed. Neither helped after three cycles each. Used web_search to research persistent stringing — found it can be caused by wet filament even when temps look right.
Lesson: when two standard fixes fail, use web_search or lookup_issue before stacking more changes. The root cause might not be what I assumed.

## Early experience — PETG container (successful)
Full print completed at 240C nozzle, 80C bed, 90% speed, 100% flow. Minor stringing visible (0.35) throughout — normal for PETG. No interventions needed. Print rated "great" by human.
Lesson: a successful PETG print has some stringing (0.3-0.4), steady temps, and no structural defects. This is the baseline for PETG — do not chase cosmetic perfection on this material.
