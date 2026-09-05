from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .summarization import (
    ExistingFact,
    PostgresSummarizationService,
    PostgresSummaryScheduler,
    SummaryFactCandidate,
    SummaryFactDecider,
    SummarizationConfig,
    summarization_doctor,
)


def _fixed_candidate(identifier: str, en: str, ur: str, *, day: str = "2026-07-19") -> SummaryFactCandidate:
    return SummaryFactCandidate(
        sentence_id=identifier,
        category_id="politics",
        calendar_date=date.fromisoformat(day),
        fact_text_en=en,
        fact_text_ur=ur,
        source_first_seen_at=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        source_last_seen_at=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        priority=50,
        fact_hash=__import__("hashlib").sha256(f"{en}|{ur}".encode()).hexdigest(),
        factual_signature={},
    )


def synthetic_benchmark() -> dict[str, Any]:
    config = SummarizationConfig()
    decider = SummaryFactDecider(config)
    base = _fixed_candidate(
        "00000000-0000-0000-0000-000000000001",
        "The cabinet approved the relief package for five districts.",
        "کابینہ نے پانچ اضلاع کے لیے امدادی پیکیج منظور کر لیا۔",
    )
    base_fact = ExistingFact(
        fact_id="00000000-0000-0000-0000-000000000010",
        fact_text_en=base.fact_text_en,
        fact_text_ur=base.fact_text_ur,
        fact_hash=base.fact_hash,
        embedding_vector=(1.0, 0.0, 0.0),
    )
    cases = [
        {
            "name": "exact_repeat",
            "candidate": base,
            "vector": (1.0, 0.0, 0.0),
            "existing": [base_fact],
            "expected": "duplicate",
        },
        {
            "name": "distinct_fact",
            "candidate": _fixed_candidate(
                "00000000-0000-0000-0000-000000000002",
                "The election commission announced the polling schedule.",
                "الیکشن کمیشن نے پولنگ شیڈول کا اعلان کر دیا۔",
            ),
            "vector": (0.0, 1.0, 0.0),
            "existing": [base_fact],
            "expected": "append",
        },
        {
            "name": "changed_number_with_high_similarity",
            "candidate": _fixed_candidate(
                "00000000-0000-0000-0000-000000000003",
                "The cabinet approved the relief package for six districts.",
                "کابینہ نے چھ اضلاع کے لیے امدادی پیکیج منظور کر لیا۔",
            ),
            "vector": (0.999, 0.001, 0.0),
            "existing": [base_fact],
            "expected": "review",
        },
        {
            "name": "negation_conflict",
            "candidate": _fixed_candidate(
                "00000000-0000-0000-0000-000000000004",
                "The cabinet did not approve the relief package for five districts.",
                "کابینہ نے پانچ اضلاع کے لیے امدادی پیکیج منظور نہیں کیا۔",
            ),
            "vector": (0.999, 0.001, 0.0),
            "existing": [base_fact],
            "expected": "review",
        },
    ]
    results = []
    for case in cases:
        decision = decider.decide(case["candidate"], case["vector"], case["existing"])
        results.append(
            {
                "name": case["name"],
                "expected": case["expected"],
                "actual": decision.action,
                "passed": decision.action == case["expected"],
            }
        )
    return {
        "status": "passed" if all(item["passed"] for item in results) else "failed",
        "cases": results,
        "policy": {
            "additive_only": True,
            "abstractive_generation": False,
            "complete_bilingual_fact_required": True,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 9 additive bilingual summarization")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--load-model", action="store_true")
    run_once = sub.add_parser("run-once")
    run_once.add_argument("--force", action="store_true")
    worker = sub.add_parser("worker")
    worker.add_argument("--poll-seconds", type=int, default=10)
    benchmark = sub.add_parser("benchmark")
    benchmark.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.command == "doctor":
        report = summarization_doctor(load_model=args.load_model)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report["status"] == "ready" else 1)
    if args.command == "benchmark":
        report = synthetic_benchmark()
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report["status"] == "passed" else 1)

    service = PostgresSummarizationService()
    scheduler = PostgresSummaryScheduler(service)
    if args.command == "run-once":
        scheduler.ensure_schedule()
        print(json.dumps(scheduler.tick(force=args.force), ensure_ascii=False, indent=2))
        return
    while True:
        try:
            scheduler.ensure_schedule()
            report = scheduler.tick()
        except Exception as exc:
            report = {
                "status": "database_unavailable",
                "error": f"{exc.__class__.__name__}: {exc}",
                "retrying": True,
            }
        print(json.dumps(report, ensure_ascii=False))
        time.sleep(max(1, args.poll_seconds))


if __name__ == "__main__":
    main()
