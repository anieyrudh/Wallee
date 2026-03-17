"""Prusa printer device pack via PrusaLink local HTTP API."""

PACK_META = {
    "name": "prusa_link",
    "description": "Prusa printer via PrusaLink local HTTP API",
    "bus": "network",
    "discovery_match": {"type": "http", "path": "/api/version", "expect_key": "api"},
}
