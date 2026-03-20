# Print Issue Reference

Consulted on-demand via the lookup_issue tool. NOT loaded into every prompt cycle.

### Stringing

**Detection:**
- vision.stringing scores around 0.6-0.7 or higher
- Thin strings visible between travel moves in vision description
- More common at higher nozzle temperatures

**Root causes (by likelihood):**
1. Nozzle temperature too high for material
2. Retraction settings insufficient (slicer-side, not adjustable mid-print)
3. Travel speed too low

**Interventions:**
| Option | Tool call | Effectiveness | Reversible |
|--------|-----------|---------------|------------|
| Reduce nozzle temp 5°C | `set_temperature(target=current-5, heater="nozzle")` | High | Yes |
| Reduce speed 10% | `set_speed_factor(percent=90)` | Medium | Yes |
| Call human for retraction check | `call_human("Stringing persists after temp and speed reduction. May need slicer retraction adjustment.")` | High | N/A |

**Decision ladder:**
1. Reduce nozzle temp roughly 5°C → observe 2-3 cycles
2. If no improvement → reduce speed roughly 10% → observe 2-3 cycles
3. If still no improvement → call human for retraction/slicer check

**False positives:**
- Minor stringing during PREPARING phase purge is normal
- PETG naturally strings more than PLA — raise your threshold for PETG

**Material-specific:**
- PLA: action threshold roughly around vision score 0.6
- PETG: action threshold roughly around vision score 0.8 (strings by nature)
- TPU: reduce speed before reducing temp (temp reduction risks underextrusion)

### Spaghetti / Print Detachment

**Detection:**
- vision.spaghetti scores around 0.6 or higher (lower threshold — high cost of missing this)
- vision.bed_adhesion_ok dropping below roughly 0.3
- Buddy camera showing empty bed area where print should be

**Root causes (by likelihood):**
1. Poor first layer adhesion (bed temp, Z-offset, dirty bed)
2. Part warped and peeled off during print
3. Mechanical knock (bump, vibration loosened part)

**Interventions:**
| Option | Tool call | Effectiveness | Reversible |
|--------|-----------|---------------|------------|
| Pause + call human | `pause_print()` then `call_human("Print detached from bed. Spaghetti forming. Need manual cleanup.", "critical")` | High | No (print likely failed) |
| Cancel if clearly unrecoverable | `cancel_print()` | High | No |

**Decision ladder:**
1. Pause immediately — don't wait for more evidence on spaghetti
2. Call human in the same chain — pausing without calling leaves the printer frozen
3. If human confirms failure → cancel and clean

**False positives:**
- Support material can look like loose filament to vision — check progress % (supports appear early)
- Thin wispy stringing is not spaghetti — spaghetti means structural failure

### Overextrusion

**Detection:**
- vision.overextrusion scores around 0.6 or higher
- Rough/bumpy top surface, excess material at corners
- Lines look wider than expected

**Root causes (by likelihood):**
1. Flow rate too high for this filament
2. Nozzle temperature too high (filament too fluid)
3. Filament diameter slightly oversized

**Interventions:**
| Option | Tool call | Effectiveness | Reversible |
|--------|-----------|---------------|------------|
| Reduce flow 3-5% | `set_flow_factor(percent=current-3)` | High | Yes |
| Reduce nozzle temp 5°C | `set_temperature(target=current-5, heater="nozzle")` | Medium | Yes |

**Decision ladder:**
1. Reduce flow 3-5% → observe 2-3 cycles
2. If persists → also reduce nozzle temp 5°C
3. If same adjustment needed on multiple prints → record pattern with remember tool

### Underextrusion

**Detection:**
- vision.underextrusion scores around 0.6 or higher
- Gaps in top surface, thin/sparse infill visible
- Lines look thinner than expected

**Root causes (by likelihood):**
1. Flow rate too low
2. Nozzle temperature too low (filament too viscous)
3. Partial clog developing (check filament sensor flow count trend)

**Interventions:**
| Option | Tool call | Effectiveness | Reversible |
|--------|-----------|---------------|------------|
| Increase flow 3-5% | `set_flow_factor(percent=current+3)` | High | Yes |
| Increase nozzle temp 5°C | `set_temperature(target=current+5, heater="nozzle")` | Medium | Yes |
| Call human if clog suspected | `call_human("Underextrusion persisting despite flow/temp increase. Possible partial clog.")` | High | N/A |

**Decision ladder:**
1. Increase flow 3-5% → observe 2-3 cycles
2. If persists → increase nozzle temp 5°C
3. If filament sensor shows declining flow rate → likely clog, call human

### Warping

**Detection:**
- vision.warping scores around 0.5 or higher
- Corners lifting from bed visible in camera
- More common with ABS/ASA and large flat parts

**Root causes (by likelihood):**
1. Bed temperature too low for material
2. Chamber temperature too low (ABS/ASA)
3. Print speed too high (rapid cooling causes differential contraction)

**Interventions:**
| Option | Tool call | Effectiveness | Reversible |
|--------|-----------|---------------|------------|
| Increase bed temp 5°C | `set_temperature(target=current+5, heater="bed")` | High | Yes |
| Reduce speed 10% | `set_speed_factor(percent=current-10)` | Medium | Yes |

**Decision ladder:**
1. Increase bed temp 5°C → observe 2-3 cycles
2. If persists → reduce speed 10%
3. If corners fully detach → pause and call human (bed may need cleaning/adhesion help)
