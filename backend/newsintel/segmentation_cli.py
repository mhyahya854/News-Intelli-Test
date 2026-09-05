from __future__ import annotations

import argparse
import json
from pathlib import Path

from .segmentation import (
    benchmark_segmentation,
    load_segmentation_manifest,
    segmentation_doctor,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 4 sentence reconstruction tools")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="Print the active exact-segmentation policy")
    benchmark = subcommands.add_parser("benchmark", help="Run an exact segmentation manifest")
    benchmark.add_argument("--manifest", type=Path, required=True)
    benchmark.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "doctor":
        print(json.dumps(segmentation_doctor(), ensure_ascii=False, indent=2))
        return 0
    cases = load_segmentation_manifest(args.manifest.resolve())
    report = benchmark_segmentation(cases)
    payload = report.serializable()
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
