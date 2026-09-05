from __future__ import annotations

import argparse
import json

from .persistence import (
    DurableCommandSpool,
    PostgresPersistenceService,
    PersistenceConfig,
    persistence_doctor,
    replay_spool,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 8 persistence diagnostics and recovery")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--database", action="store_true", help="Connect to PostgreSQL and verify revision")

    replay = sub.add_parser("replay")
    replay.add_argument("--maximum", type=int, default=None)

    sub.add_parser("stats")
    sub.add_parser("purge-expired-ocr")

    args = parser.parse_args()
    config = PersistenceConfig.from_env()
    if args.command == "doctor":
        report = persistence_doctor(check_database=args.database)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ready" else 1
    if args.command == "stats":
        print(json.dumps(DurableCommandSpool(config).stats(), ensure_ascii=False, indent=2))
        return 0
    service = PostgresPersistenceService(config=config)
    if args.command == "replay":
        report = replay_spool(service, DurableCommandSpool(config), maximum=args.maximum)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["failed"] == 0 and report["corrupt"] == 0 else 2
    if args.command == "purge-expired-ocr":
        deleted = service.purge_expired_ocr()
        print(json.dumps({"deleted": deleted}, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
