"""Pi cameras device pack — nozzle camera and thermal camera via local HTTP snapshots."""

PACK_META = {
    "name": "pi_cameras",
    "description": "Camera feeds from 3DO nozzle camera and Waveshare thermal camera",
    "bus": "network",
    "discovery_match": {"type": "http", "path": "/snapshot", "host": "localhost:8080"},
}
