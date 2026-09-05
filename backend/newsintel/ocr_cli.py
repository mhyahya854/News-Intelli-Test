from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ocr import (
    PaddleDualScriptEngine,
    benchmark_engine,
    load_benchmark_manifest,
    ocr_doctor,
    ocr_runtime_config_from_env,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 3 OCR diagnostics and benchmark harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check OCR dependencies and optional model loading")
    doctor.add_argument("--load-models", action="store_true")

    benchmark = subparsers.add_parser("benchmark", help="Run the manually-labelled OCR benchmark")
    benchmark.add_argument("--manifest", type=Path, required=True)
    benchmark.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "doctor":
        report = ocr_doctor(load_models=args.load_models)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ready" else 1

    samples = load_benchmark_manifest(args.manifest)
    engine = PaddleDualScriptEngine(ocr_runtime_config_from_env())
    report = benchmark_engine(engine, samples)
    payload = report.serializable()
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
