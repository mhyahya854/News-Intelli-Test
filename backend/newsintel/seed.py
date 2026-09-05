from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import build_engine, build_session_factory
from .models import Category, Keyword, ModelRegistry, ScheduledJob, SystemSetting
from .taxonomy import category_seed_rows, keyword_seed_rows

TAXONOMY_VERSION = "2026-07-19.phase-5-context-exact"


def seed_categories(session: Session) -> int:
    count = 0
    for row in category_seed_rows():
        category = session.get(Category, row["id"])
        if category is None:
            category = Category(**row)
            session.add(category)
        else:
            for key, value in row.items():
                if key != "id":
                    setattr(category, key, value)
        count += 1
    session.flush()
    return count


def seed_keywords(session: Session) -> int:
    count = 0
    for row in keyword_seed_rows():
        keyword = session.scalar(
            select(Keyword).where(
                Keyword.category_id == row["category_id"],
                Keyword.language == row["language"],
                Keyword.normalized_term == row["normalized_term"],
            )
        )
        if keyword is None:
            session.add(Keyword(**row))
        else:
            for key, value in row.items():
                if key not in {"category_id", "language", "normalized_term"}:
                    setattr(keyword, key, value)
        count += 1
    session.flush()
    return count


def seed_schedules(session: Session) -> int:
    schedules = [
        {
            "name": "category-summary-refresh",
            "job_type": "refresh_category_summaries",
            "payload": {},
            "interval_seconds": 300,
        },
        {
            "name": "raw-ocr-retention-cleanup",
            "job_type": "purge_expired_raw_ocr",
            "payload": {"retention_days": 7},
            "interval_seconds": 3600,
        },
        {
            "name": "stream-health-snapshot",
            "job_type": "capture_stream_health",
            "payload": {},
            "interval_seconds": 60,
        },
    ]
    count = 0
    for row in schedules:
        existing = session.scalar(select(ScheduledJob).where(ScheduledJob.name == row["name"]))
        if existing is None:
            # The scheduler sets the first concrete due time when it starts.
            session.add(ScheduledJob(**row, next_run_at=datetime.now(timezone.utc)))
        else:
            for key, value in row.items():
                if key != "name":
                    setattr(existing, key, value)
        count += 1
    session.flush()
    return count



def seed_model_candidates(session: Session) -> int:
    candidates = [
        {
            "purpose": "ocr_detection",
            "model_name": "PP-OCRv6_small_det",
            "revision": "paddleocr-3.7.0",
            "runtime": "Paddle Static CPU",
            "license_name": "Apache-2.0",
            "config": {"device": "cpu", "shared_across_scripts": True, "status": "benchmark_required"},
            "benchmark": {"acceptance": "labelled_line_recall=1.0_on_project_test_set"},
        },
        {
            "purpose": "ocr_recognition",
            "model_name": "arabic_PP-OCRv5_mobile_rec",
            "revision": "paddleocr-3.7.0",
            "runtime": "Paddle Static CPU",
            "license_name": "Apache-2.0",
            "config": {"script": "Arabic/Urdu", "device": "cpu", "status": "benchmark_required"},
            "benchmark": {"acceptance": "word_accuracy>=0.95_and_line_recall=1.0"},
        },
        {
            "purpose": "ocr_recognition",
            "model_name": "en_PP-OCRv5_mobile_rec",
            "revision": "paddleocr-3.7.0",
            "runtime": "Paddle Static CPU",
            "license_name": "Apache-2.0",
            "config": {"script": "English", "device": "cpu", "status": "benchmark_required"},
            "benchmark": {"acceptance": "word_accuracy>=0.95_and_line_recall=1.0"},
        },
        {
            "purpose": "ocr_recognition",
            "model_name": "EasyOCR ur+en fallback",
            "revision": "easyocr-1.7.2",
            "runtime": "PyTorch CPU",
            "license_name": "Apache-2.0",
            "config": {"role": "fallback_and_periodic_audit", "device": "cpu", "status": "benchmark_required"},
            "benchmark": {"acceptance": "must_recover_primary_misses_without_lowering_precision"},
        },
        {
            "purpose": "translation",
            "model_name": "Helsinki-NLP/opus-mt-ur-en",
            "revision": "7be1b539f1396ec91378efec4ca6ae1b9e5da6bd",
            "runtime": "CTranslate2 4.8.1 CPU INT8",
            "license_name": "Apache-2.0",
            "config": {"direction": "ur-en", "status": "benchmark_required", "role": "low_latency_baseline", "challenger": "ai4bharat/indictrans2-indic-en-dist-200M"},
            "benchmark": {"acceptance": "human_reviewed_news_domain"},
        },
        {
            "purpose": "translation",
            "model_name": "Helsinki-NLP/opus-mt-en-ur",
            "revision": "4642e030400759ebc20834837cd3ed4c9ca526b5",
            "runtime": "CTranslate2 4.8.1 CPU INT8",
            "license_name": "Apache-2.0",
            "config": {"direction": "en-ur", "status": "benchmark_required", "role": "low_latency_baseline", "challenger": "ai4bharat/indictrans2-en-indic-dist-200M"},
            "benchmark": {"acceptance": "human_reviewed_news_domain"},
        },
        {
            "purpose": "summarization",
            "model_name": "authoritative-extractive-fact-ledger",
            "revision": "phase9-v1",
            "runtime": "Python 3.12 + PostgreSQL",
            "license_name": "Project runtime",
            "is_active": True,
            "config": {
                "status": "production_default",
                "additive_only": True,
                "abstractive_generation": False,
                "complete_bilingual_facts": True,
                "source": "accepted canonical stories and verified translations",
            },
            "benchmark": {
                "acceptance": "all_unique_story_facts_included_once_and_no_unsupported_claims"
            },
        },
        {
            "purpose": "summarization",
            "model_name": "google/mt5-small",
            "revision": "research-candidate-not-activated",
            "runtime": "Transformers CPU",
            "license_name": "Apache-2.0",
            "is_active": False,
            "config": {
                "status": "research_only_requires_news_finetuning",
                "role": "optional_future_abstractive_digest",
                "never_authoritative": True,
            },
            "benchmark": {
                "acceptance": "zero_hallucination_zero_fact_omission_and_cpu_budget_pass"
            },
        },
        {
            "purpose": "embedding",
            "model_name": "intfloat/multilingual-e5-small",
            "revision": "fd1525a9fd15316a2d503bf26ab031a61d056e98",
            "runtime": "SentenceTransformers 5.6 CPU",
            "license_name": "MIT",
            "config": {
                "status": "benchmark_required",
                "target": "Urdu-English canonical-story deduplication",
                "dimensions": 384,
                "same_day_auto_merge": 0.93,
                "cross_channel_window_merge": 0.90,
                "cross_language_auto_merge": 0.94,
                "review_gate": 0.84,
            },
            "benchmark": {
                "acceptance": "duplicate_recall_and_distinct_story_precision_must_both_equal_1.0_on_labelled_project_set"
            },
        },
    ]
    count = 0
    for row in candidates:
        existing = session.scalar(
            select(ModelRegistry).where(
                ModelRegistry.purpose == row["purpose"],
                ModelRegistry.model_name == row["model_name"],
                ModelRegistry.revision == row["revision"],
            )
        )
        active = bool(row.get("is_active", False))
        values = {key: value for key, value in row.items() if key != "is_active"}
        if existing is None:
            session.add(ModelRegistry(**values, is_active=active))
        else:
            existing.runtime = row["runtime"]
            existing.license_name = row["license_name"]
            existing.config = row["config"]
            existing.benchmark = row["benchmark"]
            existing.is_active = active
        count += 1
    session.flush()
    return count

def seed_settings(session: Session) -> None:
    values = {
        "taxonomy.version": {
            "value": {"version": TAXONOMY_VERSION},
            "description": "Version of the built-in bilingual taxonomy seed.",
        },
        "pipeline.timezone": {
            "value": {"name": "Asia/Karachi"},
            "description": "Calendar-day boundary used by deduplication and summaries.",
        },
        "pipeline.raw_ocr_retention_days": {
            "value": {"days": 7},
            "description": "Raw OCR text retention; frame images are never stored.",
        },
        "pipeline.keyword_cache_seconds": {
            "value": {"seconds": 60},
            "description": "Maximum delay before runtime keyword edits become active.",
        },
        "pipeline.keyword_match_policy": {
            "value": {
                "mode": "exact_normalized_phrase_only",
                "fuzzy_matching": False,
                "misspelling_variants": False,
                "autocorrection": False,
            },
            "description": "Fixed user-selected keyword policy; context classification is separate from spelling recovery.",
        },
        "pipeline.deduplication_policy": {
            "value": {
                "calendar_timezone": "Asia/Karachi",
                "every_observation_emits_live": True,
                "summary_accepts_only_new_canonical_stories": True,
                "uncertain_matches_require_review": True,
                "number_conflicts_block_merge": True,
                "named_entity_conflicts_block_merge": True,
                "negation_conflicts_block_merge": True,
            },
            "description": "Phase 6 canonical-story and duplicate-free summary eligibility policy.",
        },
        "pipeline.translation_policy": {
            "value": {
                "source_languages": ["en", "ur"],
                "opposite_language_only": True,
                "complete_sentence_only": True,
                "original_emits_before_translation": True,
                "live_window_minutes": 30,
                "live_repeats_visible": True,
                "roman_urdu_output_allowed": False,
                "model_activation_requires_human_reviewed_benchmark": True,
            },
            "description": "Phase 7 original-first translation and rolling live-feed policy.",
        },
        "pipeline.summarization_policy": {
            "value": {
                "engine": "authoritative-extractive-fact-ledger",
                "interval_seconds": 300,
                "additive_only": True,
                "canonical_stories_only": True,
                "complete_bilingual_fact_required": True,
                "semantic_duplicate_guard": True,
                "factual_conflicts_require_review": True,
                "abstractive_digest_enabled": False,
            },
            "description": "Phase 9 duplicate-free bilingual daily summary policy.",
        },
    }
    for key, row in values.items():
        existing = session.get(SystemSetting, key)
        if existing is None:
            session.add(SystemSetting(key=key, **row))
        else:
            existing.value = row["value"]
            existing.description = row["description"]


def run_seed(session: Session) -> dict[str, int | str]:
    categories = seed_categories(session)
    keywords = seed_keywords(session)
    schedules = seed_schedules(session)
    models = seed_model_candidates(session)
    seed_settings(session)
    session.commit()
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "categories": categories,
        "keywords": keywords,
        "schedules": schedules,
        "model_candidates": models,
    }


def main() -> None:
    engine = build_engine()
    factory = build_session_factory(engine)
    with factory() as session:
        result = run_seed(session)
    print(
        "Seed complete: "
        f"{result['categories']} categories, {result['keywords']} keywords, "
        f"{result['schedules']} schedules, {result['model_candidates']} model candidates "
        f"({result['taxonomy_version']})."
    )


if __name__ == "__main__":
    main()
