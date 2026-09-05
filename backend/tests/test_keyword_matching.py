from __future__ import annotations

import asyncio
from pathlib import Path
from datetime import datetime, timezone

import pytest

from newsintel.keyword_matching import (
    BoundedSegmentedUnitBus,
    CategoryRecord,
    InMemoryKeywordObservationSink,
    InMemoryKeywordProvider,
    KeywordBackpressureError,
    KeywordBusWorker,
    KeywordMatcherService,
    KeywordRecord,
    benchmark_keyword_matching,
    build_text_view,
    load_keyword_manifest,
    normalize_match_text,
    seeded_keyword_provider,
)
from newsintel.segmentation import SegmentedTextUnit

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _keyword(
    keyword_id: str,
    category_id: str,
    term: str,
    *,
    requires_context: bool = False,
    context_terms: tuple[str, ...] = (),
    priority: str = "normal",
) -> KeywordRecord:
    return KeywordRecord(
        keyword_id=keyword_id,
        category_id=category_id,
        term=term,
        normalized_term=normalize_match_text(term),
        language="ur" if any("\u0600" <= char <= "\u06ff" for char in term) else "en",
        priority=priority,
        match_mode="phrase",
        requires_context=requires_context,
        context_terms=tuple(normalize_match_text(item) for item in context_terms),
        excluded_terms=(),
    )


def _provider(keywords: list[KeywordRecord]) -> InMemoryKeywordProvider:
    category_ids = sorted({item.category_id for item in keywords})
    categories = [
        CategoryRecord(
            category_id=category_id,
            label_en=category_id.title(),
            label_ur=category_id,
            color="#123456",
            priority=50,
            positive_terms=(),
            multi_label_allowed=True,
        )
        for category_id in category_ids
    ]
    return InMemoryKeywordProvider(categories, keywords)


def _match(matcher: KeywordMatcherService, text: str):
    return matcher.match_text(
        text=text,
        language="mixed",
        unit_id="unit-1",
        stream_id="stream-1",
        channel_name="Test News",
        observed_at=NOW,
        confidence=0.99,
        review_required=False,
    )


def _unit(text: str = "The Supreme Court granted bail after the hearing.") -> SegmentedTextUnit:
    return SegmentedTextUnit(
        unit_id="unit-1",
        stream_id="stream-1",
        channel_name="Test News",
        track_id="track-1",
        text=text,
        normalized_text=normalize_match_text(text),
        language="en",
        unit_type="sentence",
        completion_reason="terminal_punctuation",
        confidence=0.99,
        review_required=False,
        first_frame_sequence=1,
        last_frame_sequence=2,
        first_seen_at=NOW,
        last_seen_at=NOW,
        source_frame_sequences=(1, 2),
        source_line_indexes=(0,),
    )


def test_exact_whole_word_and_phrase_matching() -> None:
    matcher = KeywordMatcherService(_provider([_keyword("1", "legal", "court"), _keyword("2", "legal", "supreme court")]))
    result = _match(matcher, "The Supreme Court issued an order.")
    assert result.matched is True
    assert {item.normalized_term for item in result.keyword_hits} == {"court", "supreme court"}
    assert {item.category_id for item in result.accepted_categories} == {"legal"}


def test_substring_is_not_a_word_match() -> None:
    matcher = KeywordMatcherService(_provider([_keyword("1", "legal", "court")]))
    result = _match(matcher, "The courthouse opened today.")
    assert result.matched is False
    assert result.keyword_hits == ()


def test_no_stemming_and_no_misspelling_recovery() -> None:
    matcher = KeywordMatcherService(_provider([_keyword("1", "weather", "flood")]))
    assert _match(matcher, "Flooding affected roads.").matched is False
    assert _match(matcher, "A fl00d affected roads.").matched is False
    assert _match(matcher, "A flood affected roads.").matched is True


def test_unicode_equivalent_urdu_normalization_is_not_spelling_correction() -> None:
    matcher = KeywordMatcherService(_provider([_keyword("1", "legal", "قانون")]))
    # Arabic kaf and Urdu kaf are Unicode variants of the same correctly spelled word.
    result = _match(matcher, "یہ قانونی حکم ہے اور قانون نافذ ہوگا۔")
    assert result.matched is True
    assert result.keyword_hits[0].matched_text == "قانون"


def test_original_span_mapping_is_preserved() -> None:
    view = build_text_view("  سپریم کورٹ نے فیصلہ دیا۔ ")
    assert view.normalized.startswith("سپریم کورٹ")
    start, end = view.original_span(0, len("سپریم کورٹ"))
    assert view.original[start:end] == "سپریم کورٹ"


def test_ambiguous_keyword_requires_full_sentence_support() -> None:
    matcher = KeywordMatcherService(
        _provider(
            [
                _keyword("case", "judiciary", "case", requires_context=True, context_terms=("court", "judge")),
                _keyword("court", "judiciary", "court"),
                _keyword("dengue", "health", "dengue"),
            ]
        )
    )
    health = _match(matcher, "This is a case of dengue.")
    assert {item.category_id for item in health.accepted_categories} == {"health"}
    legal = _match(matcher, "The court heard the case.")
    assert {item.category_id for item in legal.accepted_categories} == {"judiciary"}


def test_seeded_context_rejects_power_play_as_energy() -> None:
    matcher = KeywordMatcherService(seeded_keyword_provider())
    result = _match(matcher, "The power play helped Pakistan win the cricket match.")
    assert {item.category_id for item in result.accepted_categories} == {"sports"}


def test_seeded_context_rejects_lahore_as_sports() -> None:
    matcher = KeywordMatcherService(seeded_keyword_provider())
    result = _match(matcher, "A power outage affected Lahore overnight.")
    assert {item.category_id for item in result.accepted_categories} == {"energy"}


def test_cross_category_duplicate_term_requires_context() -> None:
    matcher = KeywordMatcherService(seeded_keyword_provider())
    assert {item.category_id for item in _match(matcher, "The government announced the budget.").accepted_categories} == {"politics"}
    assert {item.category_id for item in _match(matcher, "Tax measures dominate the budget.").accepted_categories} == {"economy"}


def test_multi_label_categories_are_retained() -> None:
    matcher = KeywordMatcherService(seeded_keyword_provider())
    result = _match(matcher, "سپریم کورٹ نے سیلاب سے متعلق درخواست کی سماعت کی۔")
    assert {item.category_id for item in result.accepted_categories} == {"judiciary", "weather"}


def test_every_detection_is_immediate_but_summary_waits_for_dedup() -> None:
    matcher = KeywordMatcherService(seeded_keyword_provider())
    first = _match(matcher, "The Supreme Court granted bail.")
    second = _match(matcher, "The Supreme Court granted bail.")
    assert first.emit_immediately is True
    assert second.emit_immediately is True
    assert first.observation_id != second.observation_id
    assert first.canonical_story_status == "pending_phase_6_deduplication"
    assert first.summary_status == "blocked_until_canonical_story_deduplication"


def test_cache_refreshes_without_restart() -> None:
    now = [0.0]
    provider = _provider([_keyword("1", "legal", "court")])
    matcher = KeywordMatcherService(provider, refresh_interval_seconds=60, monotonic_clock=lambda: now[0])
    assert _match(matcher, "Court order.").matched is True
    provider.categories = [
        CategoryRecord(
            category_id="weather", label_en="Weather", label_ur="موسم", color="#123456",
            priority=50, positive_terms=(), multi_label_allowed=True,
        )
    ]
    provider.keywords = [_keyword("2", "weather", "rain")]
    assert _match(matcher, "Rain warning.").matched is False
    now[0] = 61.0
    assert _match(matcher, "Rain warning.").matched is True
    assert _match(matcher, "Court order.").matched is False


def test_cache_uses_last_good_snapshot_during_database_failure() -> None:
    provider = _provider([_keyword("1", "legal", "court")])
    matcher = KeywordMatcherService(provider, refresh_interval_seconds=1, monotonic_clock=lambda: 10.0)
    matcher.refresh(force=True)
    provider.failure = RuntimeError("database unavailable")
    matcher.refresh(force=True)
    assert _match(matcher, "Court order.").matched is True
    assert matcher.refresh_failures == 1
    assert "database unavailable" in (matcher.last_refresh_error or "")


def test_bounded_unit_bus_never_silently_drops() -> None:
    async def scenario() -> None:
        bus = BoundedSegmentedUnitBus(capacity=1)
        await bus.publish(_unit(), timeout_seconds=0.1)
        with pytest.raises(KeywordBackpressureError):
            await bus.publish(_unit("The court issued another order."), timeout_seconds=0.01)
        assert bus.size == 1
        assert bus.publish_count == 1

    asyncio.run(scenario())


def test_keyword_worker_emits_only_matched_units_by_default() -> None:
    async def scenario() -> None:
        bus = BoundedSegmentedUnitBus(capacity=4)
        sink = InMemoryKeywordObservationSink()
        matcher = KeywordMatcherService(seeded_keyword_provider())
        worker = KeywordBusWorker(bus, matcher, sink)
        await bus.publish(_unit())
        await bus.publish(_unit("The presenter welcomed viewers."))
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        await bus.join()
        stop.set()
        await task
        assert worker.processed_units == 2
        assert worker.matched_units == 1
        assert worker.unmatched_units == 1
        assert len(sink.observations) == 1

    asyncio.run(scenario())


def test_synthetic_benchmark_is_exact() -> None:
    cases = load_keyword_manifest(Path(__file__).parents[1] / "fixtures" / "keywords" / "manifest.synthetic.jsonl")
    report = benchmark_keyword_matching(KeywordMatcherService(seeded_keyword_provider()), cases)
    assert report.accepted is True, report.failures
    assert report.category_precision == 1.0
    assert report.category_recall == 1.0
    assert report.exact_case_pass_rate == 1.0
