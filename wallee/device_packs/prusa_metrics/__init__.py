"""Prusa metrics device pack — UDP listener for InfluxDB line protocol telemetry."""

PACK_META = {
    "name": "prusa_metrics",
    "description": "Prusa printer telemetry via UDP metrics stream (port 8514)",
    "bus": "udp",
    "discovery_match": {"type": "udp", "port": 8514},
}
