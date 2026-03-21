# Prusa Link Device Pack Setup

This guide configures the `prusa_link` device pack used by the reference deployment.

## Requirements

- A Prusa machine reachable on the same network as the Wallee host
- PrusaLink enabled on the machine
- A valid local API key
- Wallee host able to reach the machine over HTTP

## Environment variables

Set these in `.env` when `DEVICE_PACKS` includes `prusa_link`:

```bash
DEVICE_PACKS=host_pi,prusa_link,prusa_metrics,prusa_serial,pi_cameras
PRUSALINK_HOST=
PRUSALINK_API_KEY=
QUEUE_AUTOSTART_READY_TEMP_C=35
```

Notes:

- `PRUSALINK_HOST` may be a bare host/IP or a full `http://` URL
- `PRUSALINK_API_KEY` is sent as `X-Api-Key`
- `QUEUE_AUTOSTART_READY_TEMP_C` controls when the idle queue callback is allowed to auto-propose `start_print`

## Machine configuration

1. Enable PrusaLink on the machine.
2. Generate or copy the local API key from the machine UI or PrusaLink settings.
3. Confirm the Wallee host can reach `http://$PRUSALINK_HOST/api/version`.
4. Confirm the API key works by calling an authenticated endpoint such as `/api/v1/status`.

## What this pack publishes

Once running, this pack should publish:

- machine state: `printer.state`, `printer.job_state`, `printer.job_progress`
- thermal and rate data: `printer.temp_nozzle`, `printer.target_nozzle`, `printer.temp_bed`, `printer.target_bed`, `printer.speed`, `printer.flow`
- identity: `printer.firmware`, `printer.model`, `printer.serial`, `printer.nozzle_diameter`
- file inventory: `printer.files`
- generic job metadata: `job.filename`, `job.material`, `job.phase`, `job.phase_detail`, `job.time_in_phase_s`

## Expected interactions with other packs

- `prusa_metrics` fills in higher-rate telemetry and electrical measurements
- `prusa_serial` provides diagnostic serial tools
- `pi_cameras` provides image and vision keys used in the prompt
- `host_pi` provides local host health

## Troubleshooting

- Missing state keys:
  check `PRUSALINK_HOST`, API-key validity, and HTTP reachability
- `job.material` stays `unknown`:
  the material is inferred from file metadata or filename patterns
- `start_print` rejects while idle queue is populated:
  check `printer.job_state`, `printer.state`, and the queue callback readiness threshold

## Related docs

- [README.md](README.md)
- [CAPABILITIES.md](CAPABILITIES.md)
