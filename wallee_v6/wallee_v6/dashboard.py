"""Read-only dashboard API.

The dashboard is intentionally read-only by default.  That prevents a useful
observability surface from quietly becoming an uncontrolled actuation path.
"""

from __future__ import annotations

from fastapi import FastAPI

from .planning_context import WorldCompiler
from .runtime_db import RuntimeDB


def create_app(world_compiler: WorldCompiler, runtime_db: RuntimeDB, goal: str) -> FastAPI:
    """Create a small read-only FastAPI application."""
    app = FastAPI(title="Wallee v6.5 Dashboard", version="0.6.5")

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/world")
    def world() -> dict:
        return world_compiler.compile(goal).prompt_view()

    @app.get("/last_result")
    def last_result() -> dict:
        return runtime_db.latest_completed_action()

    @app.get("/events")
    def events(limit: int = 50) -> list[dict]:
        rows = runtime_db._conn.execute(
            "SELECT ts_ms, component, level, message, context_json FROM events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "ts_ms": row["ts_ms"],
                "component": row["component"],
                "level": row["level"],
                "message": row["message"],
                "context_json": row["context_json"],
            }
            for row in rows
        ]

    return app
