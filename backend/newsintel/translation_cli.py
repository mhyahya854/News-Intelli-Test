from __future__ import annotations

import argparse
import json
from pathlib import Path

from .translation import (
    CTranslate2MarianEngine,
    TranslationConfig,
    TranslationService,
    benchmark_records,
    translation_doctor,
)


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 7 translation diagnostics and benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--load-models", action="store_true")
    benchmark = sub.add_parser("benchmark")
    benchmark.add_argument("--manifest", type=Path, required=True)
    benchmark.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "doctor":
        report = translation_doctor(load_models=args.load_models)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ready" else 1

    config = TranslationConfig.from_env()
    service = TranslationService(CTranslate2MarianEngine(config), config=config)
    report = benchmark_records(_load_jsonl(args.manifest), service)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
