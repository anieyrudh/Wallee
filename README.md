# Wallee

Autonomous 3D printer operator. An LLM proposes actions. Deterministic code decides if they're safe. A safety kernel watches independently.

See `CLAUDE.md` for the full specification.

## Known Limitations

- **ESTOP is software-only.** The emergency stop sends M25 via HTTP to the printer.
  If the network or printer firmware is unresponsive, ESTOP cannot physically stop
  the machine. A hardware relay ESTOP is recommended for production use.
- **Safety kernel requires Redis.** If Redis goes down, the safety kernel loses
  visibility into printer state. The kernel retries on connection loss but cannot
  monitor during the outage.
- **Vision sensor depends on OpenRouter.** If the API is unreachable or credits
  are exhausted, the vision sensor produces no scores. The agent operates on
  telemetry alone until vision recovers.
