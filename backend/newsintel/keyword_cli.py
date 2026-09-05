from __future__ import annotations

import argparse
import json
from pathlib import Path

from .keyword_matching import (
    KeywordMatcherService,
    benchmark_keyword_matching,
    keyword_doctor,
    load_keyword_manifest,
    seeded_keyword_provider,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 5 exact keyword matching tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="Inspect exact-matching policy and seed index")
    benchmark = subparsers.add_parser("benchmark", help="Run labeled keyword/category cases")
    benchmark.add_argument("--manifest", type=Path, required=True)
    benchmark.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "doctor":
        print(json.dumps(keyword_doctor(), ensure_ascii=False, indent=2))
        return 0
    matcher = KeywordMatcherService(seeded_keyword_provider())
    cases = load_keyword_manifest(args.manifest)
    report = benchmark_keyword_matching(matcher, cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report.serializable(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report.serializable(), ensure_ascii=False, indent=2))
    return 0 if report.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
