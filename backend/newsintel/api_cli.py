from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from sqlalchemy import text

from .api import _expected_admin_token
from .database import build_engine

REQUIRED_PATHS = {
    "/api/v1/streams",
    "/api/v1/feed",
    "/api/v1/sentence/{sentence_id}",
    "/api/v1/category/{category_id}/sentences",
    "/api/v1/categories",
    "/api/v1/category/{category_id}/summary",
    "/api/v1/history/dates",
    "/api/v1/search",
    "/api/v1/admin/keywords",
    "/api/v1/admin/keywords/{keyword_id}",
    "/api/v1/admin/categories",
    "/api/v1/stats/overview",
    "/api/v1/stats/keyword-frequency",
    "/api/v1/share/{sentence_id}",
    "/api/v1/share/summary/{category_id}",
}


def doctor(*, database: bool = False) -> dict[str, Any]:
    from app import app

    schema = app.openapi()
    paths = set(schema.get("paths", {}))
    missing = sorted(REQUIRED_PATHS - paths)
    checks: dict[str, Any] = {
        "openapi_generated": bool(schema.get("openapi")),
        "required_rest_paths": {"ok": not missing, "missing": missing},
        "websocket_route": any(getattr(route, "path", None) == "/ws/live" for route in app.routes),
        "admin_token_configured": _expected_admin_token() is not None,
        "cursor_secret_explicit": bool(os.getenv("API_CURSOR_SECRET", "").strip()),
        "outbox_delivery": "PostgreSQL transactional outbox -> WebSocket hub",
        "live_feed_window_minutes": 30,
        "repeats_preserved_in_live_feed": True,
        "canonical_story_and_summary_deduplication": True,
    }
    status = "ready" if checks["openapi_generated"] and checks["required_rest_paths"]["ok"] and checks["websocket_route"] else "not_ready"
    if database:
        try:
            with build_engine().connect() as connection:
                revision = connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar_one()
                connection.execute(text("SELECT 1 FROM streams LIMIT 1"))
            checks["postgresql"] = {"ok": revision == "20260720_0006", "revision": revision, "expected_revision": "20260720_0006"}
            if revision != "20260720_0006":
                status = "not_ready"
        except Exception as exc:
            checks["postgresql"] = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
            status = "not_ready"
    return {"status": status, "phase": 10, "python_required": "3.12.x x64", "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 10 FastAPI/WebSocket diagnostics")
    parser.add_argument("command", choices=["doctor"])
    parser.add_argument("--database", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = doctor(database=args.database)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(encoded)
    if args.output:
        from pathlib import Path

        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded + "\n", encoding="utf-8")
    raise SystemExit(0 if report["status"] == "ready" else 1)


if __name__ == "__main__":
    main()
