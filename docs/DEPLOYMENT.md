# Deployment — Raspberry Pi cutover runbook

This runbook installs the v6 runtime on the Raspberry Pi and cuts over from a
legacy (v5) deployment if one is present. It is deliberately conservative: the
legacy services are **stopped and disabled but not removed** until the v6
release is signed off, so rollback is always one command away.

The golden rule for the cutover window: **never run two watchdogs at once.** Two
processes both empowered to stop the machine can fight over the interlock latch.
Stop the legacy stack completely before enabling the v6 safety service.

## 0. Prerequisites

- A Pi reachable over SSH with `sudo`, on the **same isolated network segment**
  as the printer (see [../SECURITY.md](../SECURITY.md), LAN exposure).
- Python 3.11+ and `git`.
- The printer's PrusaLink host/IP and API key.
- An `OPENROUTER_API_KEY` if you run the LLM planner (omit to use the heuristic
  planner).

## 1. Install the v6 code and virtualenv

```bash
sudo mkdir -p /opt/wallee
sudo chown "$USER" /opt/wallee
git clone <your-fork-url> /opt/wallee/src
python3 -m venv /opt/wallee/venv
/opt/wallee/venv/bin/pip install -e "/opt/wallee/src"
```

## 2. Provision service users and data dir

```bash
sudo useradd --system --no-create-home wallee || true
sudo useradd --system --no-create-home wallee-safety || true
sudo mkdir -p /var/lib/wallee
sudo chown wallee:wallee /var/lib/wallee
# The safety watchdog and main runtime share the safety dir under the data dir.
sudo chmod 2775 /var/lib/wallee
sudo usermod -aG wallee wallee-safety
```

## 3. Provision environment files (secrets stay out of the repo)

```bash
sudo mkdir -p /etc/wallee
sudo cp /opt/wallee/src/deploy/systemd/main.env.example /etc/wallee/main.env
sudo cp /opt/wallee/src/deploy/systemd/safety.env.example /etc/wallee/safety.env
sudo chmod 640 /etc/wallee/main.env /etc/wallee/safety.env
sudo chown root:wallee /etc/wallee/main.env
sudo chown root:wallee-safety /etc/wallee/safety.env
```

Edit `/etc/wallee/main.env`: set `PRUSA_CORE_ONE_HOST`, `PRUSA_CORE_ONE_API_KEY`,
`OPENROUTER_API_KEY`, and the `WALLEE_GOAL`. Keep `WALLEE_SIMULATION=0` for real
hardware. Mirror `PRUSA_CORE_ONE_HOST`/`PRUSA_CORE_ONE_API_KEY` into
`/etc/wallee/safety.env` if the pack stop profile resolves its host/key by env
var, so the watchdog can stop the machine on its own.

## 4. Quiesce the legacy stack (if present)

Stop and disable — do **not** uninstall — every legacy service, especially any
legacy watchdog:

```bash
sudo systemctl stop  'wallee-*.service' || true
sudo systemctl disable 'wallee-*.service' || true
sudo systemctl status 'wallee-*.service' || true   # confirm nothing is active
```

Confirm no legacy process still holds the machine (no leftover stop-transport or
control loop). Only proceed once the machine is idle and unmanaged.

## 5. Dry-run in simulation before touching hardware

```bash
sudo -u wallee env WALLEE_DATA_DIR=/var/lib/wallee WALLEE_SIMULATION=1 \
  WALLEE_ENABLED_PACKS=sim_printer,sim_arm \
  /opt/wallee/venv/bin/python -m wallee.main --once --goal "smoke test"
```

Expect a clean cycle and artifacts under `/var/lib/wallee/current_run`.

## 6. Install and start the v6 services

```bash
sudo cp /opt/wallee/src/deploy/systemd/wallee-safety.service /etc/systemd/system/
sudo cp /opt/wallee/src/deploy/systemd/wallee-main.service   /etc/systemd/system/
sudo systemctl daemon-reload
# Start the watchdog first; the main unit Requires= it and starts After= it.
sudo systemctl enable --now wallee-safety.service
sudo systemctl enable --now wallee-main.service
sudo systemctl status wallee-safety.service wallee-main.service
```

## 7. Hardware safety checklist (attended)

Run these with a hand on the physical ESTOP:

- [ ] Start a real print; confirm the runtime observes it (`journalctl -u wallee-main`).
- [ ] Trip via the loop: `sudo -u wallee /opt/wallee/venv/bin/wallee-operator estop`.
      Confirm the machine stops within budget and `estop.latch.json` appears
      under `/var/lib/wallee/safety/`.
- [ ] Confirm dispatch stays blocked while latched (no further actions run).
- [ ] Clear (attended): `sudo -u wallee /opt/wallee/venv/bin/wallee-operator clear-estop`.
      Confirm the latch file is gone and the runtime resumes on the next cycle.
- [ ] Kill the runtime mid-print (`sudo systemctl kill -s SIGKILL wallee-main`)
      and confirm the **watchdog** stops the machine on the stale heartbeat.
- [ ] Cold start: with the machine already stopped, confirm the watchdog comes
      up and the operator ESTOP path works before the main loop is healthy.

Do not leave the machine unattended until every box is checked.

## 8. Sign-off and legacy removal

Only **after** the v6 release is signed off on hardware, uninstall the legacy
tree. Keep a `legacy/v5` checkout on the Pi so rollback remains possible.

## Rollback

If v6 misbehaves during the window:

```bash
sudo systemctl disable --now wallee-main.service wallee-safety.service
# Re-enable the legacy units from the retained legacy/v5 checkout.
sudo systemctl enable --now <legacy-units>
```

Because legacy services were disabled (not removed) in step 4, rollback is a
re-enable, not a reinstall.
