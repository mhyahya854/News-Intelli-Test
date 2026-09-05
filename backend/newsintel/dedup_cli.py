from __future__ import annotations

import argparse
import json
from pathlib import Path

from .deduplication import benchmark_deduplication, deduplication_doctor, load_dedup_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 6 canonical-story deduplication diagnostics")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--load-model", action="store_true")

    benchmark = sub.add_parser("benchmark")
    benchmark.add_argument("--manifest", type=Path, required=True)
    benchmark.add_argument("--output", type=Path)

    args = parser.parse_args()
    if args.command == "doctor":
        report = deduplication_doctor(load_model=args.load_model)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ready" else 1

    steps = load_dedup_manifest(args.manifest)
    report = benchmark_deduplication(steps).serializable()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
