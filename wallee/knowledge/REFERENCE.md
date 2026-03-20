| Issue | Tier | Primary detection method | First intervention |
|---|---:|---|---|
| Stringing | 3 | Stringing score rising during travel, with nozzle temp running hot for the material | `set_temperature(target=temp_nozzle_target - 5, heater="nozzle")` |
| Underextrusion | 2 | Underextrusion score plus falling flow / rotation | `set_speed_factor(percent=80)` |
| Overextrusion | 2 | Overextrusion score plus thick lines, blob rise, or high flow | `set_flow_factor(percent=95)` |
| Inconsistent extrusion | 2 | Alternating under/over scores plus high flow variance | `set_speed_factor(percent=75)` |
| Blobs / zits at seam | 3 | Repeated blob spike at the same seam location | `set_flow_factor(percent=97)` |
| Nozzle blob | 1 | Hotend-local blob score growing across cycles | `pause_print()` |
| First layer failure | 1 | Low `bed_adhesion_ok` in the first layers, often with early spaghetti | `set_temperature(target=temp_bed_target + 5, heater="bed")` |
| Warping | 2 | Corner-lift / warping score rising while adhesion decays | `set_temperature(target=temp_bed_target + 5, heater="bed")` |
| Mid-print detachment | 1 | Spaghetti score rising after the model loses structure | `pause_print()` |
| Elephant's foot | 3 | Overextrusion confined to the first layers with a widened base | `set_temperature(target=temp_bed_target - 5, heater="bed")` |
| Layer shift | 1 | `layer_shift` score or a real-vs-interpolated X/Y jump | `set_speed_factor(percent=70)` |
| Stepper stall | 2 | Stall counter rising during PRINTING | `set_speed_factor(percent=65)` |
| Ghosting / ringing | 3 | Surface texture degrades at speed without a stronger defect signal | `set_speed_factor(percent=80)` |
| Z-banding | 3 | Periodic horizontal bands or repeating `z_real - z_interpolated` error | `set_speed_factor(percent=85)` |
| Heat creep | 2 | Heatbreak temperature rising with falling flow and rising extrusion resistance | `set_temperature(target=temp_nozzle_target - 5, heater="nozzle")` |
| Nozzle not reaching temperature | 1 | Nozzle remains well below target during heat-up | `pause_print()` |
| Bed not reaching temperature | 1 | Bed remains well below target during heat-up | `pause_print()` |
| Thermal runaway | 1 | Implausible temperature rise/drop or thermal firmware error | `set_temperature(target=0, heater="nozzle")` |
| Temperature oscillation | 2 | Repeating nozzle or bed temperature swings around target | `set_speed_factor(percent=85)` |
| Partial clog | 2 | Rising underextrusion with declining flow but some filament still moving | `set_temperature(target=temp_nozzle_target + 10, heater="nozzle")` |
| Full clog | 1 | Flow near zero, rotation near zero, underextrusion very high | `pause_print()` |
| Wet filament | 2 | Stringing + zits + flow variance that does not match motion | `set_speed_factor(percent=85)` |
| Filament tangle | 1 | Loaded sensor with repeated stop-go feed resistance | `pause_print()` |
| Filament runout | 2 | Unloaded filament sensor during PRINTING | `pause_print()` |
| Overcurrent | 1 | `overcurrent_nozzle` or `overcurrent_input` flags, often with current spike | `set_temperature(target=0, heater="nozzle")` |
| Fan stall | 1 | Fan RPM near zero while PWM stays on | `pause_print()` |
| Voltage drop | 1 | Bed/nozzle voltage sag while heaters are demanding power | `set_speed_factor(percent=80)` |
| Burn marks | 2 | Burn-marks score plus overheating or nozzle contamination pattern | `set_temperature(target=temp_nozzle_target - 10, heater="nozzle")` |
| Top surface gaps | 2 | Late-print top layers show holes, poor closure, or exposed infill | `set_speed_factor(percent=70)` |
| Top surface roughness | 3 | Top skin looks torn, ploughed, or uneven without clear gaps | `set_flow_factor(percent=95)` |
| Scarring | 2 | Drag marks appear where the nozzle is grazing raised plastic | `set_flow_factor(percent=95)` |

## Extrusion quality

### Stringing

**Detection:**
- Vision: monitor when `nozzle_camera.stringing` or `buddy_camera.stringing` sits roughly in the `0.40-0.55` range for 2-3 cycles; intervene around `0.60-0.75`; escalate around `0.80+`, especially if strands remain after travel over open space.
- Supporting telemetry: confidence rises when nozzle temperature is running roughly `5-10°C` above the job target, or when filament motion does not return close to zero during travel.

**Root causes (by likelihood):**
1. Nozzle temperature is a little too high for the material and geometry.
2. Pressure release is inadequate for the current melt viscosity.
3. Filament moisture or a mild restriction is causing ooze even when motion looks normal.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Lower nozzle temperature one step | `set_temperature(target=temp_nozzle_target - 5, heater="nozzle")` | 1 | High | Yes |
| Trim flow if strands are thick rather than wispy | `set_flow_factor(percent=95)` | 1 | Medium | Yes |
| Escalate when moisture is more likely than tuning | `call_human(message="Persistent stringing suggests wet filament or a retraction-limited setup. Check spool dryness and profile.", severity="info")` | 4 | High | No |

**Decision ladder:**
1. Lower nozzle temperature one step → observe 2-3 cycles.
2. If strands remain around the same or get thicker → trim flow and observe 2-3 cycles.
3. If stringing is still strong, or PETG/TPU quality is no longer acceptable → call human.

**False positives:**
- Purge lines, skirts, and tightly spaced support moves often look stringy without needing intervention.
- PETG and TPU can show mild hairing that is cosmetic rather than actionable.

**Material-specific notes:**
- PLA: Act earlier; a single `-5°C` step often helps quickly.
- PETG: Allow a slightly higher baseline before acting; chase only the stringing that feeds blobs.
- ASA: Chamber heat can compound ooze, so temperature changes matter more than travel cosmetics.
- TPU: Prefer gentle speed/pressure changes over aggressive retraction thinking.

### Underextrusion

**Detection:**
- Vision: monitor around `0.35-0.50`; intervene around `0.60-0.75`; escalate around `0.80+`, especially when gaps persist across multiple passes.
- Supporting telemetry: intervene sooner when `filament_sensor.flow_rate` falls roughly `15-20%` below its recent baseline, `filament_sensor.rotation` slows, or `stepper_stall_counter` starts climbing.

**Root causes (by likelihood):**
1. A partial restriction is reducing actual flow.
2. The printer is asking for more melt than the current temperature-speed pair can supply.
3. Feed resistance from spool drag, gear slip, or a developing jam is starving the nozzle.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Reduce volumetric demand | `set_speed_factor(percent=80)` | 1 | High | Yes |
| Add melt capacity | `set_temperature(target=temp_nozzle_target + 5, heater="nozzle")` | 1 | High | Yes |
| Test whether flow can recover under pause | `pause_print()` | 2 | High | Yes |
| Probe flow while paused | `extrude(length_mm=10)` | 2 | Medium | Yes |
| Escalate when the path still cannot move material | `call_human(message="Persistent underextrusion suggests a clog, feed-path friction, or extruder slip. Inspect nozzle and filament path.", severity="warning")` | 4 | High | No |

**Decision ladder:**
1. Reduce speed → observe 2-3 cycles.
2. If the defect persists, raise nozzle temperature one step → observe 2-3 cycles.
3. If flow is still poor, pause, test with a short extrude, and call human if recovery is weak or stall counts continue rising.

**False positives:**
- Very sparse infill islands and post-pause re-priming can look underfilled for a single cycle.
- The first layer can briefly look underfilled if the view catches only the start of a perimeter.

**Material-specific notes:**
- PLA: Heat creep is a common hidden cause on long enclosed prints.
- PETG: Moisture and residue can mimic a clog pattern.
- ASA: Underextrusion at normal ASA temperatures more often points to feed resistance than lack of heat.
- TPU: Reduce speed first; flexible filament compresses before it truly flows less.

### Overextrusion

**Detection:**
- Vision: monitor around `0.40-0.55`; intervene around `0.60-0.75`; escalate around `0.80+` or when blobs and drag marks begin to rise with it.
- Supporting telemetry: confidence rises when `filament_sensor.flow_rate` runs roughly `10-15%` above its baseline or when nozzle-local blob scores rise without a separate adhesion failure.

**Root causes (by likelihood):**
1. The commanded flow factor is too high for the material and nozzle.
2. The nozzle is too hot, so deposited lines spread and pile up.
3. A profile mismatch is causing systematic over-delivery rather than a transient defect.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Reduce flow one step | `set_flow_factor(percent=95)` | 1 | High | Yes |
| Lower nozzle temperature slightly | `set_temperature(target=temp_nozzle_target - 5, heater="nozzle")` | 1 | Medium | Yes |
| Stretch lines by running a little faster | `set_speed_factor(percent=110)` | 1 | Medium | Yes |
| Escalate if the profile itself is wrong | `call_human(message="Persistent overextrusion suggests a profile or filament-size mismatch. Verify spool/profile pairing.", severity="info")` | 4 | High | No |

**Decision ladder:**
1. Reduce flow one step → observe 2-3 cycles.
2. If bulging remains, lower nozzle temperature slightly → observe 2-3 cycles.
3. If the part is still swelling or nozzle drag starts to appear → increase speed modestly or call human if the pattern looks systematic.

**False positives:**
- The first layer is intentionally wider than later layers on many jobs.
- Rounded seams can look fat in the buddy view even when the wall is acceptable.

**Material-specific notes:**
- PLA: Usually responds cleanly to small flow changes.
- PETG: Sticky surfaces can look overfilled before they are truly overextruded.
- ASA: High chamber and bed heat can make sidewalls look softer than they are.
- TPU: Overextrusion often begins with compression in the feed path, so use smaller changes.

### Inconsistent extrusion

**Detection:**
- Vision: monitor when under- and overextrusion scores alternate in the `0.30-0.45` range; intervene when both are taking turns around `0.45-0.65`; escalate when the alternation persists beyond a few cycles.
- Supporting telemetry: a flow-rate coefficient of variation around `15-25%`, with matching rotation irregularity, raises confidence that this is a real delivery problem rather than geometry.

**Root causes (by likelihood):**
1. Flow is being modulated by intermittent feed resistance or a partial clog.
2. Filament diameter, moisture, or spool drag is causing cyclic pressure swings.
3. Temperature stability is poor enough that viscosity keeps moving around.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Slow the print to reduce pressure swings | `set_speed_factor(percent=75)` | 1 | High | Yes |
| Purge a short amount after a pause | `pause_print()` | 2 | High | Yes |
| Probe whether the nozzle can deliver steadily | `extrude(length_mm=10)` | 2 | Medium | Yes |
| Escalate when the pattern still cycles | `call_human(message="Inconsistent extrusion suggests spool drag, gear contamination, or a cycling partial clog. Inspect feed path and nozzle.", severity="warning")` | 4 | High | No |

**Decision ladder:**
1. Slow the print → observe 2-3 cycles.
2. If the alternation continues, pause and perform a short extrude test.
3. If the flow still pulses or recovers only briefly → call human.

**False positives:**
- Tiny islands and abrupt geometry changes naturally create fast flow variation.
- TPU can look inconsistent on a frame-by-frame basis even when the average flow is acceptable.

**Material-specific notes:**
- PLA: If inconsistency grows with print time, think heat creep early.
- PETG: Moisture produces a more cyclical, sputtering pattern.
- ASA: A strong oscillation at normal ASA temperatures often points to feed mechanics.
- TPU: Speed reduction is usually the best first lever.

### Blobs / zits at seam

**Detection:**
- Vision: monitor when `nozzle_camera.blob` repeatedly flickers around `0.30-0.45` at the same seam location; intervene around `0.50-0.65`; escalate if protrusions start growing rather than repeating at the same small size.
- Supporting telemetry: confidence is higher when temperature and bulk flow otherwise look normal, meaning the excess is local to restart pressure.

**Root causes (by likelihood):**
1. Restart pressure at the seam is too high.
2. Slight overextrusion or slightly excessive nozzle temperature is turning a normal seam into a raised seam.
3. Moisture is adding tiny pops exactly where restart pressure is already concentrated.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Reduce flow slightly | `set_flow_factor(percent=97)` | 1 | Medium | Yes |
| Lower nozzle temperature one step | `set_temperature(target=temp_nozzle_target - 5, heater="nozzle")` | 1 | Medium | Yes |
| Escalate when this is clearly a profile-level seam problem | `call_human(message="Seam zits persist. Likely needs seam/restart tuning in the profile rather than more live changes.", severity="info")` | 4 | High | No |

**Decision ladder:**
1. Trim flow slightly → observe 2-3 cycles.
2. If the seam still grows, lower nozzle temperature one step → observe 2-3 cycles.
3. If the defect remains local and systematic → call human for slicer/profile correction.

**False positives:**
- A small start-point bump on layer one is common and often harmless.
- Randomized seams scatter tiny marks that are cosmetic rather than fix-worthy.

**Material-specific notes:**
- PLA: Small changes usually show up quickly.
- PETG: Expect a slightly rougher seam baseline before acting.
- ASA: Warm enclosure and high nozzle pressure can make seams stand proud.
- TPU: Avoid chasing every seam mark with aggressive live tuning.

### Nozzle blob

**Detection:**
- Vision: monitor when `nozzle_camera.blob` sits around `0.55-0.70` and grows across 2-3 cycles; intervene around `0.70-0.85`; escalate around `0.85+` or if the mass begins to cover the heater block or shroud.
- Supporting telemetry: fan RPM dropping, current rising, or buddy-camera spaghetti appearing at the same time raises confidence that plastic is accumulating on the hotend, not just at the nozzle tip.

**Root causes (by likelihood):**
1. The part detached or curled into the nozzle and plastic started wrapping the hotend.
2. A leak or severe ooze path is depositing material on the heater block exterior.
3. Overextrusion or a partial clog is feeding a growing hotend-local mass.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Freeze motion before the mass grows | `pause_print()` | 2 | High | Yes |
| Move the head clear for inspection | `set_position(x=x_real, y=y_real, z=z_real + 20)` | 2 | Medium | Yes |
| Abandon the print if the blob is already large or wiring is threatened | `cancel_print()` | 2 | High | No |
| Request physical cleanup | `call_human(message="Nozzle blob detected. Remove hot plastic carefully and inspect heater block, wiring, and nozzle seal before resuming.", severity="critical")` | 4 | High | No |

**Decision ladder:**
1. Pause immediately → observe one cycle only.
2. Lift clear and reassess; if the blob is still small and stable, wait for human cleanup.
3. If the blob is already large, still growing, or near wiring → cancel and call human.

**False positives:**
- A tiny droplet at the nozzle tip during warmup is normal.
- Purge material can briefly look like a blob before the first real extrusion path begins.

**Material-specific notes:**
- PLA: Often starts from first-layer adhesion loss rather than slow ooze.
- PETG: Sticky ooze can grow a blob quickly once it starts.
- ASA: Hot chamber makes exterior buildup harder to self-clear.
- TPU: Soft strings can wrap the nozzle earlier than rigid materials.

## Adhesion

### First layer failure

**Detection:**
- Vision: monitor when `nozzle_camera.bed_adhesion_ok` or `buddy_camera.bed_adhesion_ok` drifts into roughly `0.45-0.60`; intervene when it falls around `0.30-0.45`; escalate if spaghetti appears in the first few layers or adhesion confidence falls near `0.25`.
- Supporting telemetry: a bed running roughly `5°C` under target, or a first layer that still looks round instead of slightly flattened after 2-3 cycles, raises confidence.

**Root causes (by likelihood):**
1. The bed surface is not giving the filament enough grip.
2. The bed is a little too cool for the material and local geometry.
3. First-layer speed or flow is not giving the material enough time to wet the surface.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Raise bed temperature one step | `set_temperature(target=temp_bed_target + 5, heater="bed")` | 1 | High | Yes |
| Slow the first layer | `set_speed_factor(percent=70)` | 1 | Medium | Yes |
| Abort once first-layer spaghetti is clear | `cancel_print()` | 2 | High | No |
| Request cleaning / recalibration | `call_human(message="First layer is failing. Clean the build surface and verify first-layer setup before restarting.", severity="warning")` | 4 | High | No |

**Decision ladder:**
1. Raise bed temperature → observe 2-3 cycles.
2. If adhesion is still weak, slow the print → observe 2-3 cycles.
3. If early spaghetti or clear non-bonding persists → cancel and call human.

**False positives:**
- Purge lines and skirts can peel while the real part still bonds correctly.
- The first few camera frames often arrive before the bead has fully flattened.

**Material-specific notes:**
- PLA: Usually recovers with small bed and speed changes if the sheet is clean.
- PETG: Higher bed heat helps, but contamination or the wrong sheet matters more.
- ASA: Treat drafts and chamber conditions as part of first-layer failure, not just later warping.
- TPU: Act sooner on low adhesion scores; flexible first layers can look acceptable right before they let go.

### Warping

**Detection:**
- Vision: monitor corner lift or `warping` scores around `0.35-0.50`; intervene around `0.50-0.70`; escalate around `0.75+` or when the nozzle begins to interact with raised edges.
- Supporting telemetry: an open door, a cool chamber for ASA, or a bed running a few degrees low raises confidence that the defect is thermal shrink rather than a camera artifact.

**Root causes (by likelihood):**
1. Thermal contraction is beating bed adhesion at corners and edges.
2. Chamber and draft conditions are wrong for the material.
3. The part geometry concentrates shrink stress more than the current setup can tolerate.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Raise bed temperature slightly | `set_temperature(target=temp_bed_target + 5, heater="bed")` | 1 | High | Yes |
| Reduce print speed to lower peel and impact forces | `set_speed_factor(percent=80)` | 1 | Medium | Yes |
| Pause before corners become collision points | `pause_print()` | 2 | High | Yes |
| Request environmental correction | `call_human(message="Warping is growing. Check drafts, enclosure state, and restart strategy if corners keep lifting.", severity="warning")` | 4 | High | No |

**Decision ladder:**
1. Raise bed temperature one step → observe 2-3 cycles.
2. If the lift still grows, slow the print → observe 2-3 cycles.
3. If corners continue rising or nozzle interaction becomes likely → pause and call human.

**False positives:**
- Minor curl on unsupported overhangs is a cooling issue, not bed warping.
- Wide-angle views can exaggerate edge curvature; trust persistence more than a single frame.

**Material-specific notes:**
- PLA: Usually a moderate problem unless there is a strong draft.
- PETG: Corner lift is more likely on large flat parts than on small geometry.
- ASA: Act earlier; chamber and draft control are usually the dominant variables.
- TPU: True warping is uncommon; rethink the diagnosis if TPU edges lift.

### Mid-print detachment

**Detection:**
- Vision: monitor spaghetti or detachment patterns around `0.45-0.60`; intervene around `0.60-0.75`; escalate around `0.80+`, especially when the print loses clear layered structure.
- Supporting telemetry: confidence rises when extrusion continues normally but the camera no longer sees the model where it should be.

**Root causes (by likelihood):**
1. Earlier adhesion loss or warping has finally broken the model free.
2. The nozzle or moving gantry has struck the part hard enough to dislodge it.
3. A support or tall feature failed and pulled the rest of the print into free air.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Stop motion immediately | `pause_print()` | 2 | High | Yes |
| Lift away from loose filament | `set_position(x=x_real, y=y_real, z=z_real + 30)` | 2 | Medium | Yes |
| End the job once detachment is confirmed | `cancel_print()` | 2 | High | No |
| Request cleanup and restart prep | `call_human(message="Mid-print detachment confirmed. Clear loose filament, inspect the nozzle, and restart with stronger adhesion.", severity="critical")` | 4 | High | No |

**Decision ladder:**
1. Pause immediately → observe one cycle only.
2. If the model is clearly no longer anchored, lift clear and cancel.
3. Call human for cleanup before any further printing.

**False positives:**
- Thin support trees can look chaotic without the main part being lost.
- Mild stringing can briefly resemble spaghetti in a single frame.

**Material-specific notes:**
- PLA: Often detaches from a weak first layer rather than late thermal stress.
- PETG: A detached PETG part can smear and stick to the nozzle quickly.
- ASA: Detachment is often the endpoint of unchecked warping.
- TPU: If TPU detaches, think more about first-layer grip than thermal shrink.

### Elephant's foot

**Detection:**
- Vision: monitor when first-layer overextrusion sits around `0.35-0.50`; intervene around `0.50-0.65` only if the widening is confined to the first few layers; escalate when base flare remains obvious after layer 3.
- Supporting telemetry: a bed running a little hotter than planned and a very slow first layer both raise confidence.

**Root causes (by likelihood):**
1. The bed is keeping the base too soft for too long.
2. First-layer flow is slightly too high for the current geometry.
3. The first layer is simply being laid down too slowly and spending too long under heat.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Lower bed temperature one step after adhesion is secure | `set_temperature(target=temp_bed_target - 5, heater="bed")` | 1 | Medium | Yes |
| Trim first-layer flow slightly | `set_flow_factor(percent=97)` | 1 | Medium | Yes |
| Record the need for a profile-level correction | `remember(observation="Elephant's foot suggests first-layer heat/flow is slightly high for this setup.")` | 1 | Medium | Yes |

**Decision ladder:**
1. Lower bed temperature one step once the part is clearly adhering → observe 2-3 cycles.
2. If the base is still widening, trim flow slightly → observe 2-3 cycles.
3. If the part is dimensionally critical and the flare remains obvious → keep the note and correct the next run rather than stacking more live changes.

**False positives:**
- A wider first layer is partly intentional on many profiles.
- Purge lines and skirts are more squashed than the real part and should be ignored.

**Material-specific notes:**
- PLA: Often improves with a small bed reduction alone.
- PETG: Avoid overreacting; PETG benefits from a strong first layer and can look fat before it is truly problematic.
- ASA: Bed reductions must be more conservative because adhesion margin is tighter.
- TPU: First-layer flare is usually less important than simply keeping the part attached.

## Mechanical

### Layer shift

**Detection:**
- Vision: monitor `layer_shift` around `0.40-0.55`; intervene around `0.60-0.75`; escalate around `0.80+` or whenever the whole model appears stepped relative to earlier layers.
- Supporting telemetry: an X or Y real-vs-interpolated jump of roughly `0.5 mm` or more, especially with rising `stepper_stall_counter`, strongly confirms a real mechanical shift.

**Root causes (by likelihood):**
1. Belt or pulley slip has let the carriage lose position.
2. The nozzle hit the part or another obstruction hard enough to skip motion.
3. The machine is running faster or harsher than the current mechanics can hold.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Reduce dynamic load | `set_speed_factor(percent=70)` | 1 | Medium | Yes |
| Stop before more layers are misplaced | `pause_print()` | 2 | High | Yes |
| End the job if the geometry is already lost | `cancel_print()` | 2 | High | No |
| Request mechanical inspection | `call_human(message="Layer shift detected. Inspect belts, pulleys, and possible collision causes before continuing.", severity="critical")` | 4 | High | No |

**Decision ladder:**
1. Reduce speed immediately → observe one cycle.
2. If position error or shift confidence remains high, pause.
3. If the model is already visibly displaced, cancel and call human.

**False positives:**
- Vision can confuse intentionally stepped geometry with a shift.
- Homing or recovery moves should not count; this entry is for PRINTING state only.

**Material-specific notes:**
- PLA: High-speed PLA prints reveal marginal mechanics fastest.
- PETG: A shifted PETG print may also start dragging because the surface stays soft.
- ASA: Warped edges often create the collision that causes the shift.
- TPU: Shifts are usually mechanical; TPU itself is rarely the root cause.

### Stepper stall

**Detection:**
- Telemetry: monitor when `stepper_stall_counter` increments occasionally; intervene when it rises repeatedly within 2-3 cycles; escalate when the counter keeps climbing or position error begins to appear.
- Vision: mild layer-shift symptoms or repeated nozzle hesitation add confidence but are not required.

**Root causes (by likelihood):**
1. Speed or acceleration is too aggressive for the current mechanical load.
2. A partial obstruction or part collision is loading the axis.
3. Binding, friction, or cooling issues in the motion system are reducing step margin.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Reduce mechanical load | `set_speed_factor(percent=65)` | 1 | High | Yes |
| Pause before the stall becomes a shift | `pause_print()` | 2 | High | Yes |
| Request inspection for obstruction or binding | `call_human(message="Repeated stepper stalls suggest collision, binding, or marginal belt/driver behavior. Inspect mechanics.", severity="warning")` | 4 | High | No |

**Decision ladder:**
1. Reduce speed → observe 2-3 cycles.
2. If stall counts keep rising, pause.
3. If the cause is not obvious and the pattern persists → call human.

**False positives:**
- Ignore stall behavior outside PRINTING, especially during homing or preparation.
- One isolated increment on a fast, complex path is not the same as a trend.

**Material-specific notes:**
- PLA: Fast PLA exposes motion limits first.
- PETG: Sticky surfaces can make nozzle contact more likely after a stall.
- ASA: Warm, warped edges are a common stall trigger.
- TPU: Extruder-related stalls are more likely than X/Y stalls.

### Ghosting / ringing

**Detection:**
- Vision: monitor when `buddy_camera.normal` softens without a stronger defect signal; intervene when repeated ripples or echoes are visible on flat walls and `normal` stays depressed over several cycles.
- Supporting telemetry: high speed or a recent speed increase raises confidence that the texture is vibration, not extrusion.

**Root causes (by likelihood):**
1. The printer is exciting a mechanical resonance at the current speed.
2. Belt tension, frame rigidity, or surface stability is marginal.
3. Geometry with sharp corners is amplifying dynamic ringing.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Reduce speed modestly | `set_speed_factor(percent=80)` | 1 | High | Yes |
| Reduce speed further if the first drop barely helps | `set_speed_factor(percent=65)` | 1 | High | Yes |
| Record the resonance pattern for later tuning | `remember(observation="Ringing improved only after speed reduction; likely resonance-limited setup.")` | 1 | Medium | Yes |
| Request mechanical / profile follow-up | `call_human(message="Ringing persists. Check belt tension, printer support surface, and future resonance tuning.", severity="info")` | 4 | High | No |

**Decision ladder:**
1. Reduce speed modestly → observe 2-3 cycles.
2. If ripples remain obvious, reduce further → observe 2-3 cycles.
3. If the part still rings at slower speed, remember the pattern and call human.

**False positives:**
- Matte or textured filaments can hide or mimic ringing.
- Curved surfaces often look noisy even when flat walls are acceptable.

**Material-specific notes:**
- PLA: Most likely to ring because it is often printed fastest.
- PETG: Moderate speeds reduce the issue naturally.
- ASA: Usually less ringing-prone because speeds are often lower.
- TPU: Rare; TPU speed limits usually suppress the effect already.

### Z-banding

**Detection:**
- Vision: monitor when horizontal bands start repeating at a regular interval; intervene when the spacing becomes obvious over several cycles; escalate when the pattern stays periodic rather than random.
- Supporting telemetry: repeated `z_real - z_interpolated` error, even if small, raises confidence that the cause is truly mechanical.

**Root causes (by likelihood):**
1. The Z drive is introducing a repeating geometric error.
2. Temperature or extrusion variation is coupling into an otherwise minor Z imperfection.
3. A component in the Z stack is slightly misaligned or dirty and repeats the same mistake.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Reduce speed slightly to equalize layer timing | `set_speed_factor(percent=85)` | 1 | Medium | Yes |
| Record the repeat interval for diagnosis | `remember(observation="Z-banding appears periodic rather than random; likely a mechanical Z source.")` | 1 | High | Yes |
| Request Z-system inspection | `call_human(message="Periodic Z-banding detected. Inspect lead screw, coupler, lubrication, and alignment.", severity="warning")` | 4 | High | No |

**Decision ladder:**
1. Reduce speed slightly → observe 2-3 cycles.
2. If the pattern remains periodic, record it.
3. Call human if the part quality matters or the bands are growing more obvious.

**False positives:**
- Layer-time changes can create isolated surface bands without a true periodic Z fault.
- Lighting changes across tall prints can exaggerate harmless layer variation.

**Material-specific notes:**
- PLA: Surface finish makes Z-banding easy to see, even when it is mild.
- PETG: Gloss changes can exaggerate the visual severity.
- ASA: Thermal variation can stack on top of a small mechanical Z pattern.
- TPU: TPU hides some banding visually; trust telemetry more than appearance alone.

## Thermal

### Heat creep

**Detection:**
- Telemetry: monitor when `temp_heatbreak` starts running above its normal material baseline; intervene when PLA drifts roughly above `45-50°C` or PETG/ASA above roughly `55-60°C` for several cycles; escalate when heatbreak temperature and underextrusion rise together.
- Vision: a rising underextrusion trend late in a print strengthens the diagnosis.

**Root causes (by likelihood):**
1. Too much heat is migrating into the cold side because cooling is inadequate.
2. The chamber is too warm for the current material, especially PLA.
3. The print is moving material slowly enough that the filament sits and softens too far upstream.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Lower nozzle temperature one step | `set_temperature(target=temp_nozzle_target - 5, heater="nozzle")` | 1 | High | Yes |
| Increase throughput only if the fan looks healthy and the job is very slow | `set_speed_factor(percent=110)` | 1 | Medium | Yes |
| Pause before a soft jam becomes a hard jam | `pause_print()` | 2 | High | Yes |
| Request chamber/fan intervention | `call_human(message="Heat creep is likely. Check heatbreak fan, chamber heat, and cooling path before resuming.", severity="critical")` | 4 | High | No |

**Decision ladder:**
1. Lower nozzle temperature one step → observe 2-3 cycles.
2. If heatbreak temperature is still high but the fan looks healthy and the print is very slow, try a small speed increase → observe 2-3 cycles.
3. If flow still worsens or fan behavior looks abnormal → pause and call human.

**False positives:**
- High ASA chamber temperatures raise heatbreak temperature without always causing a jam.
- A single warm spike matters less than a rising trend paired with falling flow.

**Material-specific notes:**
- PLA: Lowest threshold; act early because PLA softens first.
- PETG: Higher heatbreak temperatures are tolerable, but moisture can complicate the picture.
- ASA: Chamber heat is expected, so look for underextrusion trend before acting.
- TPU: Softening shows up as compression and feed instability before a classic rigid plug forms.

### Nozzle not reaching temperature

**Detection:**
- Telemetry: monitor if the nozzle is still a few degrees below target but climbing steadily; intervene when it stays roughly `10-15°C` low after around `4-5 minutes`, or when the heating slope becomes unusually flat; escalate when the printer errors or the temperature plateaus.
- Supporting telemetry: low `nozzle_voltage` or very weak temperature rise increases confidence that this is a heater or power problem, not just a slow warmup.

**Root causes (by likelihood):**
1. The heater path is losing power through a cartridge, connector, or wire problem.
2. Cooling or airflow is pulling more heat away than expected.
3. The sensor is reading incorrectly enough that control logic cannot settle the hotend.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Pause rather than printing through a heating fault | `pause_print()` | 2 | High | Yes |
| Cancel if the print is already active and the nozzle cannot recover | `cancel_print()` | 2 | High | No |
| Request heater-path inspection | `call_human(message="Nozzle is not reaching target temperature. Inspect heater cartridge, thermistor, cooling path, and connector health.", severity="critical")` | 5 | High | No |

**Decision ladder:**
1. If the nozzle is merely slow but still climbing, give it the allowed warmup window.
2. If it plateaus or the print is already active → pause.
3. If recovery is weak or an error appears → cancel and call human.

**False positives:**
- Large purge or cold filament loading can pull the nozzle briefly below target.
- High-temperature materials legitimately take longer to stabilize, but should still show a clear upward slope.

**Material-specific notes:**
- PLA: Small deficits matter less, but a flat plateau is still a fault.
- PETG: Slightly slower stabilization is common after purge-heavy sections.
- ASA: Allow more warmup time, but do not ignore a stalled rise.
- TPU: Usually low-flow; a failure to heat is more likely hardware than demand-related.

### Bed not reaching temperature

**Detection:**
- Telemetry: monitor if the bed is a few degrees low but still rising; intervene when it remains roughly `10°C` or more below target after around `5-10 minutes`; escalate when the rise stalls or the bed begins to cool under demand.
- Supporting telemetry: low `bed_voltage`, simultaneous rail sag, or heater demand without meaningful temperature gain increases confidence.

**Root causes (by likelihood):**
1. Bed power delivery is weak because of a cable, connector, or supply problem.
2. The bed sensor path is misreading or poorly coupled.
3. Ambient loss is so large that the bed cannot build enough heat, especially on high-heat jobs.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Pause before adhesion fails invisibly | `pause_print()` | 2 | High | Yes |
| Cancel if the bed is clearly not recovering or warping risk is high | `cancel_print()` | 2 | High | No |
| Request bed-heating inspection | `call_human(message="Bed is not reaching target temperature. Inspect bed heater, thermistor, power delivery, and ambient conditions.", severity="critical")` | 5 | High | No |

**Decision ladder:**
1. Allow the normal soak window if the bed is still rising.
2. If the rise flattens or the print is active and adhesion risk is increasing → pause.
3. If the bed still cannot recover → cancel and call human.

**False positives:**
- Large beds warm slowly; slow is not the same as stalled.
- Chamber heating on high-temp jobs can make the bed appear inefficient even when it is still climbing normally.

**Material-specific notes:**
- PLA: Small bed deficits are often tolerable briefly.
- PETG: Bed deficits show up faster as corner-lift or first-layer weakness.
- ASA: Treat bed heating weakness as a print-critical fault early.
- TPU: TPU is least demanding on bed temperature, so rethink the diagnosis if adhesion is otherwise good.

### Thermal runaway

**Detection:**
- Telemetry: intervene immediately if nozzle or bed temperature rises in a way that no normal control response explains, or drops implausibly fast while power remains applied; firmware thermal errors are automatic escalate conditions.
- Confidence is highest when a large jump or drop is paired with an electrical or sensor anomaly rather than a simple overshoot.

**Root causes (by likelihood):**
1. The temperature sensor path is lying to the controller.
2. A heater control element is stuck on or otherwise behaving outside feedback control.
3. A connector or wire fault is making normal temperature control impossible.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Shut off nozzle heat immediately | `set_temperature(target=0, heater="nozzle")` | 1 | High | No |
| Shut off bed heat immediately | `set_temperature(target=0, heater="bed")` | 1 | High | No |
| End the print immediately | `cancel_print()` | 1 | High | No |
| Escalate as a safety event | `call_human(message="Thermal runaway or a runaway-like sensor fault detected. Do not resume until the heater and sensor path are inspected.", severity="critical")` | 5 | High | No |

**Decision ladder:**
1. Shut off heaters immediately.
2. Cancel the print immediately.
3. Call human immediately and do not attempt recovery.

**False positives:**
- Small warmup overshoot is normal; runaway is about behavior that stops making physical sense.
- A commanded cooldown or preheat transition is not runaway if the signal is smooth and explained.

**Material-specific notes:**
- PLA: Lower absolute temperatures make implausible jumps easier to spot.
- PETG: Do not excuse abnormal heating just because PETG runs hotter.
- ASA: High setpoints still need stable control; hot jobs are not a reason to tolerate sensor nonsense.
- TPU: Lower flow means thermal instability is even less likely to be demand-driven.

### Temperature oscillation

**Detection:**
- Telemetry: monitor when nozzle or bed temperature wiggles slightly around target; intervene when oscillation grows to roughly `±3-5°C` and repeats over several cycles; escalate when the oscillation is clearly periodic and begins affecting surface quality or extrusion stability.
- Vision: inconsistent extrusion or repeating surface texture raises confidence that the thermal signal matters.

**Root causes (by likelihood):**
1. Control tuning is too aggressive or mismatched to the current hardware state.
2. Airflow or fan behavior is disturbing the sensor more than usual.
3. A wiring or sensor coupling issue is injecting false movement into the loop.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Reduce dynamic load slightly | `set_speed_factor(percent=85)` | 1 | Medium | Yes |
| Pause if oscillation is clearly degrading print quality | `pause_print()` | 2 | Medium | Yes |
| Record the pattern for later tuning | `remember(observation="Temperature oscillation appears periodic enough to suggest a control or sensor issue.")` | 1 | High | Yes |
| Request thermal-control maintenance | `call_human(message="Sustained temperature oscillation suggests tuning or sensor-path work. Inspect before the next critical print.", severity="warning")` | 4 | High | No |

**Decision ladder:**
1. Reduce dynamic load → observe 2-3 cycles.
2. If oscillation still grows or extrusion visibly suffers, pause.
3. Record the pattern and call human if the instability persists.

**False positives:**
- Small, irregular fluctuations are normal on a closed-loop heater.
- Short perturbations after fan changes do not matter unless they persist.

**Material-specific notes:**
- PLA: Surface quality shows the effect quickly.
- PETG: Slight oscillation is less visually obvious but can still affect seam quality.
- ASA: High setpoints magnify the cost of poor tuning over long prints.
- TPU: Since speeds are low, repeated oscillation points even more strongly to control-path issues.

## Filament

### Partial clog

**Detection:**
- Vision: monitor when underextrusion rises gently from baseline; intervene when it reaches roughly `0.45-0.70` and keeps trending upward; escalate when recovery after a small change is only temporary.
- Supporting telemetry: a flow decline of roughly `10-30%`, with filament rotation still present, is the classic partial-clog pattern.

**Root causes (by likelihood):**
1. Debris or degraded polymer is narrowing the nozzle path.
2. Heat creep is softening material upstream and creating intermittent restriction.
3. Recent material history or moisture has left residue that the nozzle cannot pass cleanly.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Raise nozzle temperature one step | `set_temperature(target=temp_nozzle_target + 10, heater="nozzle")` | 1 | High | Yes |
| Reduce pressure demand | `set_speed_factor(percent=75)` | 1 | High | Yes |
| Pause and attempt a short purge | `pause_print()` | 2 | High | Yes |
| Probe recovery with a purge | `extrude(length_mm=20)` | 2 | Medium | Yes |
| Escalate when the restriction returns quickly | `call_human(message="Partial clog likely. Inspect nozzle cleanliness, material history, and feed path before continuing.", severity="warning")` | 4 | High | No |

**Decision ladder:**
1. Raise nozzle temperature one step → observe 2-3 cycles.
2. If improvement is weak, reduce speed → observe 2-3 cycles.
3. If the pattern still trends worse, pause, purge, and call human if flow only recovers briefly.

**False positives:**
- Spool drag can mimic a partial clog if you only watch underextrusion.
- A purge-heavy transition can temporarily dirty the nozzle without creating a true clog.

**Material-specific notes:**
- PLA: Partial clogs often have a heat-creep component on long prints.
- PETG: Residue and moisture are especially common contributors.
- ASA: High-temperature residue can build quietly before becoming obvious.
- TPU: True clogs are less common than compression or feed-path drag, so confirm with rotation.

### Full clog

**Detection:**
- Vision: intervene when underextrusion is already severe; escalate when it sits around `0.80+` and the part is clearly no longer receiving material.
- Supporting telemetry: a near-zero `filament_sensor.flow_rate`, near-zero `filament_sensor.rotation`, and rising extruder-related resistance together make this a high-confidence hard clog.

**Root causes (by likelihood):**
1. A solid plug or foreign particle has blocked the nozzle path.
2. Heat creep has let material seize upstream into a harder jam.
3. The filament or feed path has failed in a way that leaves the extruder unable to push.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Stop the print before grinding worsens | `pause_print()` | 2 | High | Yes |
| Try a single soften-and-purge attempt | `set_temperature(target=temp_nozzle_target + 15, heater="nozzle")` | 2 | Medium | Yes |
| Test whether any path remains open | `extrude(length_mm=10)` | 2 | Low | Yes |
| End the job if the path is still blocked | `cancel_print()` | 2 | High | No |
| Request manual clearing | `call_human(message="Full clog likely. Manual cold-pull, nozzle clearing, or nozzle swap is required.", severity="critical")` | 4 | High | No |

**Decision ladder:**
1. Pause immediately.
2. Make one soften-and-purge attempt.
3. If flow does not return clearly, cancel and call human.

**False positives:**
- Long travel moves can make flow look zero briefly, but rotation should recover as soon as extrusion restarts.
- Runout can look similar unless the loaded/unloaded state is checked.

**Material-specific notes:**
- PLA: Often recoverable if the problem is caught before the plug hardens.
- PETG: Sticky residue makes hard clogs more stubborn.
- ASA: High-temperature residue can make the single recovery attempt less likely to work.
- TPU: Distinguish a real clog from a compressed filament path before forcing more heat.

### Wet filament

**Detection:**
- Vision: monitor when stringing and small zits rise together; intervene when stringing is around `0.60+` and blob/zit behavior is also present across multiple cycles; escalate when the surface starts looking both hairy and inconsistent.
- Supporting telemetry: unusually jumpy flow with stable motion and temperature supports a moisture diagnosis better than a pure tuning diagnosis.

**Root causes (by likelihood):**
1. The spool has absorbed enough moisture to disturb melt behavior.
2. Storage and ambient humidity are driving continued moisture pickup during printing.
3. The material is naturally hygroscopic and has not been dried for the current job.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Slow the print slightly | `set_speed_factor(percent=85)` | 1 | Medium | Yes |
| Lower nozzle temperature one step if stringing dominates | `set_temperature(target=temp_nozzle_target - 5, heater="nozzle")` | 1 | Medium | Yes |
| Request drying and storage correction | `call_human(message="Wet filament is likely. Dry the spool and improve storage before expecting cleaner output.", severity="warning")` | 4 | High | No |

**Decision ladder:**
1. Slow the print slightly → observe 2-3 cycles.
2. If hairy ooze still dominates, lower nozzle temperature one step → observe 2-3 cycles.
3. If the pattern stays moisture-like, call human instead of stacking more live tuning.

**False positives:**
- PETG and TPU can string even when dry.
- Overextrusion can roughen a surface without creating the same hair + zit combination.

**Material-specific notes:**
- PLA: Mild moisture often stays cosmetic longer than on PETG.
- PETG: Moisture shows up aggressively and earlier.
- ASA: Moisture can also show up as darkened, degraded surface if ignored.
- TPU: Moisture and softness combine, so slow down sooner.

### Filament tangle

**Detection:**
- Telemetry: monitor when a loaded filament path shows repeated short drops in flow and rotation; intervene when the pattern repeats across 2-3 cycles; escalate when the feed stops, then recovers, then stops again without any clear temperature explanation.
- Vision: intermittent underextrusion spikes help, but the stop-go feed pattern is the stronger signal.

**Root causes (by likelihood):**
1. A crossed loop on the spool is tightening under tension.
2. The spool holder or path is adding enough friction to mimic a tangle.
3. The free end was mishandled earlier and is now feeding underneath another loop.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Reduce pull force while confidence is still low | `set_speed_factor(percent=70)` | 1 | Medium | Yes |
| Stop before the extruder grinds the filament | `pause_print()` | 2 | High | Yes |
| Request physical untangling | `call_human(message="Filament tangle or severe spool-path drag detected. Free the spool and check the feed path before resuming.", severity="warning")` | 4 | High | No |
| Continue only after the path is physically clear | `resume_print()` | 3 | High | Yes |

**Decision ladder:**
1. If the pattern is mild, reduce speed → observe 2-3 cycles.
2. If the stop-go feed repeats, pause.
3. Call human to clear the spool path, then resume only after confirmation.

**False positives:**
- Complex geometry can create sharp but normal demand changes.
- A partial clog usually reduces flow more continuously than a tangle does.

**Material-specific notes:**
- PLA: Brittle PLA can snap if a tangle is ignored too long.
- PETG: Stronger filament may keep pulling until the extruder begins to grind.
- ASA: High chamber heat does not cause tangles, so keep the diagnosis mechanical.
- TPU: Flexible filament can mask a tangle briefly by compressing instead of stopping.

### Filament runout

**Detection:**
- Telemetry: monitor any flicker in `filament_sensor.state`; intervene when it reads unloaded during PRINTING and flow/rotation trend toward zero; escalate when the state is stably unloaded and the printer pauses or begins underextruding.
- Vision: underextrusion appearing immediately after an unload signal increases confidence.

**Root causes (by likelihood):**
1. The spool is empty.
2. The filament snapped upstream of the sensor.
3. The sensor state is false and needs human validation before trusting it again.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Pause if firmware has not already done so | `pause_print()` | 2 | High | Yes |
| Request a new spool | `call_human(message="Filament runout detected. Load fresh filament and confirm the feed path is clear.", severity="warning")` | 4 | High | No |
| Re-prime after reload | `extrude(length_mm=20)` | 3 | High | Yes |
| Continue the job after prime looks healthy | `resume_print()` | 3 | High | Yes |

**Decision ladder:**
1. Pause if needed.
2. Call human for reload.
3. Prime briefly and resume once flow looks normal again.

**False positives:**
- Some sensor flicker can occur near the end of a spool before true exhaustion.
- A sensor fault is more likely if state says unloaded while flow and rotation still look normal.

**Material-specific notes:**
- PLA: Usually resumes cleanly after a short prime.
- PETG: Prime a little more carefully to avoid restart ooze.
- ASA: Longer pauses can cool the chamber enough to matter on thin parts.
- TPU: Keep the restart slow so the feed path does not buckle during reprime.

## Electrical

### Overcurrent

**Detection:**
- Telemetry: monitor current that is running notably above its normal baseline; intervene immediately on `overcurrent_nozzle` or `overcurrent_input`; escalate without waiting for persistence because the protection flag itself is the signal.
- Supporting telemetry: simultaneous voltage sag raises confidence that this is a real electrical fault rather than a noisy reading.

**Root causes (by likelihood):**
1. Heater or power wiring is damaged, pinched, or shorting intermittently.
2. A cartridge, PSU, or board component is failing electrically.
3. A connector has degraded enough to arc, heat, or partially short.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Shut off nozzle heat immediately | `set_temperature(target=0, heater="nozzle")` | 1 | High | No |
| Shut off bed heat immediately | `set_temperature(target=0, heater="bed")` | 1 | High | No |
| End the job immediately | `cancel_print()` | 1 | High | No |
| Escalate as an electrical safety issue | `call_human(message="Overcurrent detected. Inspect heater and power wiring before any restart.", severity="critical")` | 5 | High | No |

**Decision ladder:**
1. Shut off the affected heater immediately; if the source is ambiguous, shut off both.
2. Cancel the job immediately.
3. Call human immediately and do not resume.

**False positives:**
- Treat hardware overcurrent flags as real unless a human later proves a sensor fault.
- A startup surge matters much less than a flagged fault during active printing.

**Material-specific notes:**
- PLA: Material does not change the electrical logic.
- PETG: Material does not change the electrical logic.
- ASA: Material does not change the electrical logic.
- TPU: Material does not change the electrical logic.

### Fan stall

**Detection:**
- Telemetry: monitor when fan RPM is low but nonzero under PWM; intervene when a fan is roughly at zero RPM while PWM stays on for several checks; escalate immediately if the stalled fan is the heatbreak fan.
- Vision: rising heat creep, weak overhangs, or surface roughness can support the diagnosis, but the RPM/PWM mismatch is primary.

**Root causes (by likelihood):**
1. Debris or wear has stopped the fan mechanically.
2. The fan is commanded on but not receiving power correctly.
3. The RPM signal or connector path has failed and the printer can no longer trust cooling feedback.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Stop motion immediately if the heatbreak fan has stalled | `pause_print()` | 2 | High | Yes |
| Kill nozzle heat if the heatbreak fan is truly gone | `set_temperature(target=0, heater="nozzle")` | 2 | High | No |
| Slow the print if only the print fan is affected | `set_speed_factor(percent=70)` | 1 | Medium | Yes |
| Request fan inspection | `call_human(message="Fan stall detected. Inspect for blockage, connector faults, and true fan failure before continuing.", severity="critical")` | 4 | High | No |

**Decision ladder:**
1. If the heatbreak fan stalls, pause immediately.
2. If heatbreak cooling is really gone, shut off nozzle heat and call human.
3. If only the print fan is stalled, slow the print and call human if RPM does not recover.

**False positives:**
- Very low PWM can make RPM reporting unreliable.
- Some sense-line failures report zero RPM even when the fan still spins, but the printer still needs human validation before trusting it.

**Material-specific notes:**
- PLA: Heatbreak fan failure is especially urgent because PLA heat-creeps fastest.
- PETG: Print-fan loss is often tolerable longer than on PLA, but heatbreak-fan loss is not.
- ASA: Print-fan loss may matter little; heatbreak-fan loss still matters a lot.
- TPU: Heatbreak cooling matters; print-fan loss is usually less important than feed stability.

### Voltage drop

**Detection:**
- Telemetry: monitor small voltage sag under heater switching; intervene when `bed_voltage` or `nozzle_voltage` sags materially below nominal under sustained heater demand; escalate when the sag repeats and temperatures can no longer hold target.
- Supporting telemetry: simultaneous heater weakness, overcurrent hints, or both rails sagging together raise confidence.

**Root causes (by likelihood):**
1. The power source or PSU is sagging under load.
2. A connector or wire has enough resistance to waste voltage as heat.
3. Multiple high-load systems are peaking together beyond what the supply comfortably supports.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Reduce total machine load slightly | `set_speed_factor(percent=80)` | 1 | Medium | Yes |
| Ease bed demand one step | `set_temperature(target=temp_bed_target - 5, heater="bed")` | 1 | Medium | Yes |
| Pause if rails still sag and thermal performance is degrading | `pause_print()` | 2 | High | Yes |
| Cancel if the sag remains severe | `cancel_print()` | 2 | High | No |
| Request power-path inspection | `call_human(message="Sustained voltage sag detected under load. Inspect PSU health, mains quality, and heater wiring.", severity="critical")` | 4 | High | No |

**Decision ladder:**
1. Reduce speed and ease bed demand → observe 2-3 cycles.
2. If voltage and temperature hold recover, continue cautiously.
3. If sag persists, pause or cancel depending on severity, then call human.

**False positives:**
- Brief switching-edge dips are normal.
- A single rail moving a little under warmup is less important than repeated sag that causes heater underperformance.

**Material-specific notes:**
- PLA: Lower heater demand can mask mild voltage issues longer.
- PETG: Bed-heavy PETG jobs reveal power weakness sooner.
- ASA: High bed and chamber demand make voltage weakness more consequential.
- TPU: Since TPU runs cooler and slower, strong sag usually points to hardware, not demand.

## Surface quality

### Burn marks

**Detection:**
- Vision: monitor burn or discoloration signals around `0.35-0.50`; intervene around `0.55-0.70`; escalate when the marks keep appearing on fresh surfaces rather than one isolated speck.
- Supporting telemetry: confidence rises when nozzle temperature is running high for the material or when nozzle-local blob / contamination signals rise with the marks.

**Root causes (by likelihood):**
1. Burnt residue on the nozzle or heater block is dropping onto the print.
2. The nozzle is running hot enough to degrade the polymer locally.
3. Material is dwelling too long in a hot zone because of slow movement or repeated ooze.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Lower nozzle temperature clearly | `set_temperature(target=temp_nozzle_target - 10, heater="nozzle")` | 1 | High | Yes |
| Increase throughput if the print is moving very slowly | `set_speed_factor(percent=110)` | 1 | Medium | Yes |
| Request nozzle cleaning or leak inspection | `call_human(message="Burn marks suggest nozzle contamination or overheating. Clean the nozzle and inspect for leaks.", severity="info")` | 4 | High | No |

**Decision ladder:**
1. Lower nozzle temperature → observe 2-3 cycles.
2. If marks still appear and the print is very slow, increase throughput modestly → observe 2-3 cycles.
3. If new marks continue to form, call human for physical cleaning or leak inspection.

**False positives:**
- Dark filaments and strong shadows can look like localized burning.
- One old speck carried along from a previous print is less important than repeated fresh discoloration.

**Material-specific notes:**
- PLA: Shows burn sensitivity earliest.
- PETG: Sticky residue on the nozzle often matters more than pure temperature.
- ASA: High temperatures make true degradation plausible if residue is present.
- TPU: Burn-like darkening may also indicate residence time that is too long for a soft polymer.

### Top surface gaps

**Detection:**
- Vision: monitor mild openings on late top layers; intervene when top-surface gaps sit around `0.55+` and exposed infill or poor closure remains visible across cycles; escalate when the final skin clearly will not close.
- Supporting telemetry: a mild late-stage underextrusion pattern or overly high top-layer speed increases confidence.

**Root causes (by likelihood):**
1. Top skin is not receiving enough material or enough time to close.
2. A mild restriction or low melt capacity is showing up first on the final solid layers.
3. The profile simply did not leave enough margin for the top surface to bridge cleanly.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Slow top layers substantially | `set_speed_factor(percent=70)` | 1 | High | Yes |
| Add a small flow increase | `set_flow_factor(percent=103)` | 1 | Medium | Yes |
| Raise nozzle temperature one step if the surface still looks starved | `set_temperature(target=temp_nozzle_target + 5, heater="nozzle")` | 1 | Medium | Yes |
| Request future-profile correction | `call_human(message="Top surface gaps persist. Adjust top-layer strategy or flow margin for future runs.", severity="info")` | 4 | High | No |

**Decision ladder:**
1. Slow the top layers → observe 2-3 cycles.
2. If closure is still weak, add a small flow increase → observe 2-3 cycles.
3. If the surface still cannot close, add a small temperature increase or call human for future profile correction.

**False positives:**
- Intentional textured or patterned top surfaces can look open from some angles.
- One sparse region above unusual infill is less meaningful than a repeated closure failure across the top skin.

**Material-specific notes:**
- PLA: Usually responds quickly to slowing the top layers.
- PETG: Slightly more flow can help, but too much can turn gaps into roughness.
- ASA: High chamber heat can keep the top soft, so speed reduction often matters more than large temperature jumps.
- TPU: Top closure on TPU needs slow movement more than aggressive flow.

### Top surface roughness

**Detection:**
- Vision: monitor mild tearing or ploughing on late top layers; intervene when the surface stays visibly rough across several cycles without true holes; escalate when the nozzle appears to be piling or dragging material rather than laying it flat.
- Supporting telemetry: slight overextrusion or drag-related motion load raises confidence.

**Root causes (by likelihood):**
1. There is slightly too much material on the top skin.
2. The nozzle is dragging through still-soft top layers.
3. The top surface is being asked to finish too aggressively for the current material state.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Trim flow slightly | `set_flow_factor(percent=95)` | 1 | High | Yes |
| Slow the print modestly | `set_speed_factor(percent=80)` | 1 | Medium | Yes |
| Request profile cleanup when appearance matters | `call_human(message="Top surface remains rough. Review top-skin strategy, cooling assumptions, and flow margin for future runs.", severity="info")` | 4 | Medium | No |

**Decision ladder:**
1. Trim flow slightly → observe 2-3 cycles.
2. If the surface still looks ploughed or torn, slow the print → observe 2-3 cycles.
3. If roughness remains but the part is otherwise sound, call human only if finish quality matters.

**False positives:**
- Matte materials can look rough even when geometry is fine.
- Intentional textured top finishes should not be corrected away.

**Material-specific notes:**
- PLA: Flow trims usually show up clearly.
- PETG: Too much correction can make the top glossy but still smeared.
- ASA: Warm surfaces stay soft longer, so speed reduction can matter more than it first appears.
- TPU: Surface finish is often secondary to keeping the feed path stable.

### Scarring

**Detection:**
- Vision: monitor faint drag marks when they appear only occasionally; intervene when drag marks recur across cycles and surface-normal confidence keeps dropping; escalate when marks coincide with rising overextrusion, rising warp confidence, or motion load.
- Supporting telemetry: a small rise in `stepper_stall_counter` or persistent surface contact cues increases confidence that the nozzle is grazing real geometry, not just visual noise.

**Root causes (by likelihood):**
1. Raised plastic from slight overextrusion is being hit on travel or skin moves.
2. Warping or lifted features are entering the nozzle path.
3. The profile would really prefer Z-hop or different restart behavior, which the live tool set cannot add.

**Interventions:**
| Option | Tool call | Independence | Effectiveness | Reversible |
|--------|-----------|-------------|---------------|------------|
| Reduce the amount of raised material | `set_flow_factor(percent=95)` | 1 | High | Yes |
| Reduce collision energy | `set_speed_factor(percent=75)` | 1 | Medium | Yes |
| Pause if the marks are becoming impacts rather than cosmetics | `pause_print()` | 2 | High | Yes |
| Request profile-level correction | `call_human(message="Scarring suggests nozzle drag. Check for warp, raised material, and whether the profile needs Z-hop or gentler restart behavior.", severity="warning")` | 4 | High | No |

**Decision ladder:**
1. Trim flow slightly → observe 2-3 cycles.
2. If marks remain or get longer, reduce speed → observe 2-3 cycles.
3. If the nozzle is now clearly hitting the part rather than merely grazing it, pause and call human.

**False positives:**
- Infill crossings can produce harmless light rubs that never affect the outer surface.
- One isolated scrape is less important than repeated surface damage in the same phase of the print.

**Material-specific notes:**
- PLA: Scarring usually points to a real geometry or flow problem, not normal softness.
- PETG: Soft, sticky top layers make scarring more likely once the nozzle touches them.
- ASA: Warping-driven scarring is common enough that chamber logic should stay in the diagnosis.
- TPU: Surface scarring is often less important than preserving feed stability, so avoid over-correcting.