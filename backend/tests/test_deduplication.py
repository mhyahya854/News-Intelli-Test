from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from newsintel.deduplication import (
    DeduplicationConfig,
    DeduplicationService,
    FixedEmbeddingProvider,
    InMemoryStoryRepository,
    benchmark_deduplication,
    exact_dedup_key,
    load_dedup_manifest,
    pakistan_calendar_date,
    story_features,
)
from newsintel.keyword_matching import CategoryDecision, DetectionObservation, KeywordHit, normalize_match_text

BASE = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def observation(
    text: str,
    *,
    observation_id: str,
    stream_id: str = "stream-geo",
    channel_name: str = "Geo News",
    observed_at: datetime = BASE,
    language: str = "en",
    categories: tuple[str, ...] = ("judiciary",),
    confidence: float = 0.96,
) -> DetectionObservation:
    hits = tuple(
        KeywordHit(
            keyword_id=f"kw-{category}",
            category_id=category,
            term=category,
            normalized_term=category,
            language=language if language in {"en", "ur"} else "en",
            priority="normal",
            requires_context=False,
            matched_text=category,
            normalized_span=(0, len(category)),
            original_span=(0, len(category)),
        )
        for category in categories
    )
    decisions = tuple(
        CategoryDecision(
            category_id=category,
            label_en=category.title(),
            label_ur=category,
            color="#7C3AED",
            score=1.0,
            accepted=True,
            reason="test",
            keyword_ids=(f"kw-{category}",),
            supporting_context=(),
            excluded_context=(),
        )
        for category in categories
    )
    return DetectionObservation(
        observation_id=observation_id,
        unit_id=f"unit-{observation_id}",
        stream_id=stream_id,
        channel_name=channel_name,
        observed_at=observed_at,
        text=text,
        normalized_text=normalize_match_text(text),
        language=language,
        confidence=confidence,
        review_required=False,
        keyword_hits=hits,
        category_decisions=decisions,
        urgency="normal",
        emit_immediately=True,
        canonical_story_status="pending_phase_6_deduplication",
        summary_status="blocked_until_canonical_story_deduplication",
        snapshot_version="test",
    )


def service(vectors: dict[str, tuple[float, ...]], config: DeduplicationConfig | None = None):
    repository = InMemoryStoryRepository()
    return DeduplicationService(repository, FixedEmbeddingProvider(vectors), config), repository


def test_exact_hash_ignores_case_whitespace_and_punctuation_but_not_words() -> None:
    assert exact_dedup_key("  Supreme Court: granted bail! ") == "supreme court granted bail"
    assert exact_dedup_key("Supreme Court denied bail") != exact_dedup_key("Supreme Court granted bail")


def test_every_repeat_emits_live_but_only_first_story_is_summary_eligible() -> None:
    text = "Supreme Court granted bail to Imran Khan after the hearing."
    dedup, repository = service({text: (1.0, 0.0, 0.0, 0.0)})
    first = dedup.process(observation(text, observation_id="1"))
    second = dedup.process(observation(text, observation_id="2", observed_at=BASE + timedelta(hours=3)))

    assert first.action == "created"
    assert first.summary_eligible is True
    assert second.action == "merged"
    assert second.dedup_layer == "exact_hash"
    assert second.summary_eligible is False
    assert second.emitted_events[0]["event"] == "detection_observed"
    assert second.emitted_events[1]["event"] == "canonical_story_updated"
    assert len(repository.occurrences) == 2
    assert second.story is not None and second.story.occurrence_count == 2


def test_same_story_reworded_merges_semantically() -> None:
    first_text = "Supreme Court granted bail to Imran Khan after the hearing."
    second_text = "After the hearing, Supreme Court granted Imran Khan bail."
    dedup, _ = service({
        first_text: (1.0, 0.0, 0.0, 0.0),
        second_text: (0.98, 0.199, 0.0, 0.0),
    })
    first = dedup.process(observation(first_text, observation_id="1"))
    second = dedup.process(observation(second_text, observation_id="2", observed_at=BASE + timedelta(hours=2)))
    assert first.action == "created"
    assert second.action == "merged"
    assert second.dedup_layer == "semantic_same_day"
    assert second.best_candidate is not None
    assert second.best_candidate.semantic_similarity >= 0.93


def test_cross_channel_window_uses_lower_threshold_and_merges() -> None:
    first_text = "Supreme Court granted bail to Imran Khan after the hearing."
    second_text = "After the hearing, Supreme Court granted Imran Khan bail."
    dedup, _ = service({
        first_text: (1.0, 0.0, 0.0, 0.0),
        second_text: (0.91, 0.414, 0.0, 0.0),
    })
    dedup.process(observation(first_text, observation_id="1"))
    result = dedup.process(
        observation(
            second_text,
            observation_id="2",
            stream_id="stream-ary",
            channel_name="ARY News",
            observed_at=BASE + timedelta(minutes=10),
        )
    )
    assert result.action == "merged"
    assert result.dedup_layer == "cross_channel_window"
    assert result.story is not None
    assert result.story.source_channels == {"Geo News", "ARY News"}


def test_cross_day_identical_story_creates_new_canonical_story() -> None:
    text = "Supreme Court granted bail to Imran Khan after the hearing."
    dedup, repository = service({text: (1.0, 0.0, 0.0, 0.0)})
    first = dedup.process(observation(text, observation_id="1", observed_at=datetime(2026, 7, 19, 18, 0, tzinfo=timezone.utc)))
    second = dedup.process(observation(text, observation_id="2", observed_at=datetime(2026, 7, 19, 19, 30, tzinfo=timezone.utc)))
    assert pakistan_calendar_date(first.occurrence.observed_at).isoformat() == "2026-07-19"
    assert pakistan_calendar_date(second.occurrence.observed_at).isoformat() == "2026-07-20"
    assert first.action == "created"
    assert second.action == "created"
    assert len(repository.stories) == 2


def test_different_numbers_never_auto_merge_and_are_withheld_for_review() -> None:
    first_text = "Five people were injured in the Lahore blast."
    second_text = "Six people were injured in the Lahore blast."
    dedup, repository = service({
        first_text: (1.0, 0.0, 0.0, 0.0),
        second_text: (0.99, 0.141, 0.0, 0.0),
    })
    # Words rather than digits do not enter the number signature; use explicit figures.
    first_text = "5 people were injured in the Lahore blast."
    second_text = "6 people were injured in the Lahore blast."
    dedup, repository = service({
        first_text: (1.0, 0.0, 0.0, 0.0),
        second_text: (0.99, 0.141, 0.0, 0.0),
    })
    dedup.process(observation(first_text, observation_id="1", categories=("crime",)))
    result = dedup.process(observation(second_text, observation_id="2", categories=("crime",)))
    assert result.action == "pending_review"
    assert result.summary_eligible is False
    assert result.best_candidate is not None
    assert result.best_candidate.numbers_compatible is False
    assert len(repository.stories) == 1
    assert len(repository.reviews) == 1


def test_different_named_people_never_auto_merge() -> None:
    first_text = "Supreme Court granted bail to Ali Raza after the hearing."
    second_text = "Supreme Court granted bail to Ahmed Khan after the hearing."
    dedup, repository = service({
        first_text: (1.0, 0.0, 0.0, 0.0),
        second_text: (0.99, 0.141, 0.0, 0.0),
    })
    dedup.process(observation(first_text, observation_id="1"))
    result = dedup.process(observation(second_text, observation_id="2"))
    assert result.action == "pending_review"
    assert result.best_candidate is not None
    assert result.best_candidate.named_entities_compatible is False
    assert len(repository.stories) == 1


def test_negation_and_event_state_conflicts_block_merge() -> None:
    first_text = "The court approved the petition."
    second_text = "The court did not approve the petition and rejected it."
    dedup, _ = service({
        first_text: (1.0, 0.0, 0.0, 0.0),
        second_text: (0.99, 0.141, 0.0, 0.0),
    })
    dedup.process(observation(first_text, observation_id="1"))
    result = dedup.process(observation(second_text, observation_id="2"))
    assert result.action == "pending_review"
    assert result.best_candidate is not None
    assert result.best_candidate.negation_compatible is False


def test_different_category_is_not_considered_a_candidate() -> None:
    first_text = "The government announced the federal budget."
    second_text = "The cricket board announced the team."
    dedup, repository = service({
        first_text: (1.0, 0.0, 0.0, 0.0),
        second_text: (0.99, 0.141, 0.0, 0.0),
    })
    dedup.process(observation(first_text, observation_id="1", categories=("politics",)))
    result = dedup.process(observation(second_text, observation_id="2", categories=("sports",)))
    assert result.action == "created"
    assert len(repository.stories) == 2


def test_higher_confidence_repeat_replaces_display_text_without_new_story() -> None:
    first_text = "Supreme Court granted bail to Imran Khan."
    second_text = "Supreme Court granted bail to Imran Khan after the hearing."
    dedup, _ = service({
        first_text: (1.0, 0.0, 0.0, 0.0),
        second_text: (0.99, 0.141, 0.0, 0.0),
    })
    first = dedup.process(observation(first_text, observation_id="1", confidence=0.90))
    second = dedup.process(observation(second_text, observation_id="2", confidence=0.99))
    assert first.story is not None and second.story is not None
    assert first.story.story_id == second.story.story_id
    assert second.story.original_text == second_text
    assert second.story.confidence == 0.99


def test_exact_urdu_repeat_merges_without_spelling_guessing() -> None:
    text = "سپریم کورٹ نے عمران خان کی ضمانت منظور کر لی۔"
    dedup, repository = service({text: (1.0, 0.0, 0.0, 0.0)})
    first = dedup.process(observation(text, observation_id="1", language="ur"))
    second = dedup.process(observation(text, observation_id="2", language="ur", observed_at=BASE + timedelta(hours=5)))
    assert first.action == "created"
    assert second.action == "merged"
    assert second.dedup_layer == "exact_hash"
    assert len(repository.occurrences) == 2


def test_only_accepted_keyword_observations_can_enter_dedup() -> None:
    invalid = observation("No matched news.", observation_id="x")
    invalid = replace(invalid, keyword_hits=(), category_decisions=())
    dedup, _ = service({"No matched news.": (1.0, 0.0, 0.0, 0.0)})
    with pytest.raises(ValueError):
        dedup.process(invalid)


def test_synthetic_manifest_passes() -> None:
    steps = load_dedup_manifest(Path(__file__).parents[1] / "fixtures" / "dedup" / "manifest.synthetic.jsonl")
    report = benchmark_deduplication(steps)
    assert report.accepted is True, report.failures
    assert report.exact_pass_rate == 1.0


def test_bounded_dedup_bus_never_silently_drops() -> None:
    import asyncio
    from newsintel.deduplication import BoundedObservationBus, DedupBackpressureError

    async def scenario() -> None:
        bus = BoundedObservationBus(capacity=1)
        await bus.publish(observation("Supreme Court granted bail.", observation_id="1"), timeout_seconds=0.1)
        with pytest.raises(DedupBackpressureError):
            await bus.publish(observation("Supreme Court heard an appeal.", observation_id="2"), timeout_seconds=0.01)
        assert bus.size == 1
        assert bus.publish_count == 1

    asyncio.run(scenario())


def test_dedup_worker_connects_live_observations_to_canonical_results() -> None:
    import asyncio
    from newsintel.deduplication import BoundedObservationBus, DedupBusWorker, InMemoryDedupResultSink

    async def scenario() -> None:
        text = "Supreme Court granted bail to Imran Khan after the hearing."
        dedup, _ = service({text: (1.0, 0.0, 0.0, 0.0)})
        bus = BoundedObservationBus(capacity=4)
        sink = InMemoryDedupResultSink()
        worker = DedupBusWorker(bus, dedup, sink)
        await bus.publish(observation(text, observation_id="1"))
        await bus.publish(observation(text, observation_id="2", observed_at=BASE + timedelta(minutes=5)))
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        await bus.join()
        stop.set()
        await task
        assert worker.processed_observations == 2
        assert worker.created_stories == 1
        assert worker.merged_observations == 1
        assert worker.pending_reviews == 0
        assert [item.action for item in sink.results] == ["created", "merged"]
        assert all(item.emitted_events[0]["event"] == "detection_observed" for item in sink.results)

    asyncio.run(scenario())
