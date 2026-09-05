from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy.dialects import postgresql

from newsintel.models import Sentence, Translation
from newsintel.summarization import (
    AUTHORITATIVE_ENGINE,
    ExistingFact,
    InMemoryAdditiveSummaryLedger,
    PostgresSummarizationService,
    SummaryFactCandidate,
    SummaryFactDecider,
    SummarizationConfig,
    candidate_from_story,
    summarization_doctor,
    summarization_runtime_config,
)


def candidate(identifier: int, en: str, ur: str, *, day: str = "2026-07-19") -> SummaryFactCandidate:
    import hashlib

    return SummaryFactCandidate(
        sentence_id=str(uuid.UUID(int=identifier)),
        category_id="politics",
        calendar_date=date.fromisoformat(day),
        fact_text_en=en,
        fact_text_ur=ur,
        source_first_seen_at=datetime(2026, 7, 19, 12, identifier, tzinfo=timezone.utc),
        source_last_seen_at=datetime(2026, 7, 19, 12, identifier, tzinfo=timezone.utc),
        priority=50,
        fact_hash=hashlib.sha256(f"{en}|{ur}".encode()).hexdigest(),
        factual_signature={},
    )


def test_summary_policy_is_authoritative_additive_and_non_abstractive() -> None:
    config = summarization_runtime_config()
    assert config["engine"] == AUTHORITATIVE_ENGINE
    assert config["output_policy"]["additive_only"] is True
    assert config["output_policy"]["regenerate_from_scratch"] is False
    assert config["output_policy"]["optional_abstractive_digest_enabled"] is False


def test_doctor_does_not_require_generative_model() -> None:
    report = summarization_doctor(load_model=False)
    assert report["status"] == "ready"
    assert report["checks"]["abstractive_model_required"] is False


def test_candidate_uses_complete_verified_bilingual_sentence_without_paraphrase() -> None:
    sentence = Sentence(
        id=uuid.UUID(int=1),
        original_text="The cabinet approved the relief package.",
        normalized_text="the cabinet approved the relief package",
        original_language="en",
        confidence_score=0.99,
        exact_hash="a" * 64,
        calendar_date=date(2026, 7, 19),
        first_seen_at=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        last_seen_at=datetime(2026, 7, 19, 12, 1, tzinfo=timezone.utc),
        occurrence_count=1,
        dedup_decision={"summary_state": "eligible"},
        extracted_entities={},
        review_status="accepted",
    )
    translation = Translation(
        sentence_id=sentence.id,
        english_text=sentence.original_text,
        urdu_text="کابینہ نے امدادی پیکیج منظور کر لیا۔",
        translation_status="complete",
        source_language="en",
    )
    result = candidate_from_story(sentence, translation, category_id="politics")
    assert result is not None
    assert result.fact_text_en == sentence.original_text
    assert result.fact_text_ur == translation.urdu_text


def test_incomplete_translation_is_not_inserted_into_bilingual_summary() -> None:
    sentence = Sentence(
        id=uuid.UUID(int=2), original_text="News", normalized_text="news", original_language="en",
        confidence_score=1.0, exact_hash="b"*64, calendar_date=date(2026,7,19),
        first_seen_at=datetime.now(timezone.utc), last_seen_at=datetime.now(timezone.utc),
        occurrence_count=1, dedup_decision={"summary_state":"eligible"}, extracted_entities={}, review_status="accepted"
    )
    translation = Translation(
        sentence_id=sentence.id, english_text="News", urdu_text=None,
        translation_status="failed", source_language="en"
    )
    assert candidate_from_story(sentence, translation, category_id="politics") is None


def test_exact_repeat_does_not_change_additive_summary() -> None:
    ledger = InMemoryAdditiveSummaryLedger()
    item = candidate(3, "The cabinet approved the package.", "کابینہ نے پیکیج منظور کر لیا۔")
    first = ledger.apply(item, [1.0, 0.0, 0.0])
    before = (ledger.summary_text_en, ledger.summary_text_ur)
    second = ledger.apply(item, [1.0, 0.0, 0.0])
    assert first.action == "append"
    assert second.action == "duplicate"
    assert (ledger.summary_text_en, ledger.summary_text_ur) == before
    assert len(ledger.facts) == 1


def test_distinct_fact_appends_once_in_order() -> None:
    ledger = InMemoryAdditiveSummaryLedger()
    one = candidate(4, "The cabinet approved the package.", "کابینہ نے پیکیج منظور کر لیا۔")
    two = candidate(5, "The election commission announced the schedule.", "الیکشن کمیشن نے شیڈول کا اعلان کیا۔")
    assert ledger.apply(one, [1.0, 0.0, 0.0]).action == "append"
    assert ledger.apply(two, [0.0, 1.0, 0.0]).action == "append"
    assert ledger.summary_text_en.count("•") == 2
    assert ledger.summary_text_ur.count("•") == 2


def test_high_similarity_with_changed_number_is_withheld_for_review() -> None:
    decider = SummaryFactDecider(SummarizationConfig())
    old = candidate(6, "Five people were injured in the blast.", "دھماکے میں پانچ افراد زخمی ہوئے۔")
    new = candidate(7, "Six people were injured in the blast.", "دھماکے میں چھ افراد زخمی ہوئے۔")
    existing = ExistingFact(
        fact_id=str(uuid.UUID(int=20)), fact_text_en=old.fact_text_en,
        fact_text_ur=old.fact_text_ur, fact_hash=old.fact_hash,
        embedding_vector=(1.0,0.0,0.0)
    )
    decision = decider.decide(new, [0.999,0.001,0.0], [existing])
    assert decision.action == "review"
    assert decision.evidence["numbers_compatible"] is False


def test_low_similarity_distinct_story_appends() -> None:
    decider = SummaryFactDecider()
    old = candidate(8, "The court adjourned the hearing.", "عدالت نے سماعت ملتوی کر دی۔")
    new = candidate(9, "Pakistan announced its cricket squad.", "پاکستان نے کرکٹ اسکواڈ کا اعلان کیا۔")
    existing = ExistingFact(str(uuid.UUID(int=21)), old.fact_text_en, old.fact_text_ur, old.fact_hash, (1.0,0.0,0.0))
    assert decider.decide(new, [0.0,1.0,0.0], [existing]).action == "append"


def test_eligible_query_is_postgresql_native_and_excludes_folded_and_reviewed() -> None:
    statement = PostgresSummarizationService._eligible_query("politics", date(2026,7,19), 100)
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "daily_summary_sentences" in sql
    assert "summary_review_cases" in sql
    assert "summary_state" in sql
    assert "accepted" in sql
