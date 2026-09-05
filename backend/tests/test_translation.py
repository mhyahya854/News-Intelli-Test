from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from newsintel.translation import (
    MemoryTranslationCache,
    RollingLiveFeed,
    StaticTranslationEngine,
    TranslationBackpressureError,
    TranslationConfig,
    TranslationCoordinator,
    TranslationError,
    TranslationRequest,
    TranslationService,
    translation_memory_key,
    validate_complete_sentence_translation,
    benchmark_records,
)

NOW = datetime(2026, 7, 19, 13, 0, tzinfo=timezone.utc)


def config(**overrides):
    values = dict(
        model_root=__import__("pathlib").Path("models/translation"),
        queue_capacity=4,
        publish_timeout_seconds=0.01,
        cache_capacity=20,
        live_window_minutes=30,
    )
    values.update(overrides)
    return TranslationConfig(**values)


def test_opposite_language_only_and_complete_sentence() -> None:
    request = TranslationRequest(
        observation_id="obs-1",
        source_text="وزیر اعظم نے اجلاس طلب کر لیا۔",
        source_language="ur",
        observed_at=NOW,
        channel_name="Geo News",
    )
    assert request.target_language == "en"
    with pytest.raises(TranslationError):
        TranslationService(StaticTranslationEngine({}), config=config()).translate(
            TranslationRequest(
                observation_id="fragment",
                source_text="وزیر اعظم نے",
                source_language="ur",
                observed_at=NOW,
                channel_name="Geo News",
                is_complete_sentence=False,
            )
        )


def test_urdu_to_english_translation_preserves_original_and_numbers() -> None:
    source = "عدالت نے سماعت 22 جولائی تک ملتوی کر دی۔"
    translated = "The court adjourned the hearing until 22 July."
    engine = StaticTranslationEngine({("ur", "en", source): translated})
    service = TranslationService(engine, config=config())
    result = service.translate(
        TranslationRequest("obs-1", source, "ur", NOW, "Geo News")
    )
    assert result.source_text == source
    assert result.translated_text == translated
    assert result.target_language == "en"
    assert result.status == "complete"
    assert result.quality.checks["numbers_preserved"] is True


def test_english_to_urdu_translation_rejects_roman_urdu_output() -> None:
    quality = validate_complete_sentence_translation(
        "Heavy rain is expected tonight.",
        "Aaj raat shadeed barish ka imkaan hai.",
        source_language="en",
        target_language="ur",
    )
    assert quality.accepted is False
    assert quality.checks["target_script_present"] is False
    assert quality.checks["no_roman_urdu_output"] is False


def test_english_to_urdu_complete_sentence_is_accepted() -> None:
    source = "Heavy rain is expected in Punjab tonight."
    translated = "آج رات پنجاب میں شدید بارش کا امکان ہے۔"
    engine = StaticTranslationEngine({("en", "ur", source): translated})
    result = TranslationService(engine, config=config()).translate(
        TranslationRequest("obs-2", source, "en", NOW, "ARY News")
    )
    assert result.status == "complete"
    assert result.target_language == "ur"
    assert result.quality.checks["no_roman_urdu_output"] is True


def test_changed_number_is_not_accepted_as_quality_translation() -> None:
    quality = validate_complete_sentence_translation(
        "Five people were injured and 2 remain critical.",
        "پانچ افراد زخمی ہوئے اور 3 کی حالت تشویشناک ہے۔",
        source_language="en",
        target_language="ur",
    )
    assert quality.accepted is False
    assert quality.checks["numbers_preserved"] is False


def test_exact_repeat_translation_memory_avoids_second_inference() -> None:
    source = "The cabinet meeting will be held tomorrow."
    translated = "کابینہ کا اجلاس کل ہوگا۔"
    engine = StaticTranslationEngine({("en", "ur", source): translated})
    service = TranslationService(engine, config=config(), cache=MemoryTranslationCache(5))
    first = service.translate(TranslationRequest("obs-a", source, "en", NOW, "Geo News"))
    second = service.translate(TranslationRequest("obs-b", source, "en", NOW, "ARY News"))
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert len(engine.calls) == 1
    assert translation_memory_key(source, "en", "ur") == translation_memory_key(source, "en", "ur")



def test_micro_batch_groups_same_direction_and_collapses_exact_repeats() -> None:
    source_a = "The court adjourned the hearing."
    source_b = "Heavy rain is expected tonight."
    engine = StaticTranslationEngine(
        {
            ("en", "ur", source_a): "عدالت نے سماعت ملتوی کر دی۔",
            ("en", "ur", source_b): "آج رات شدید بارش کا امکان ہے۔",
        }
    )
    service = TranslationService(engine, config=config(max_batch_size=8))
    results = service.translate_many(
        [
            TranslationRequest("obs-1", source_a, "en", NOW, "Geo News"),
            TranslationRequest("obs-2", source_b, "en", NOW, "ARY News"),
            TranslationRequest("obs-3", source_a, "en", NOW, "Dunya News"),
        ]
    )
    assert [result.observation_id for result in results] == ["obs-1", "obs-2", "obs-3"]
    assert len(engine.calls) == 1
    assert engine.calls[0][3] == (source_a, source_b)
    assert results[0].cache_hit is False
    assert results[2].cache_hit is True


def test_live_feed_keeps_repeated_observations_and_expires_after_30_minutes() -> None:
    feed = RollingLiveFeed(window_minutes=30)
    source = "وزیر اعظم نے اجلاس طلب کر لیا۔"
    for index, minutes in enumerate((0, 8, 25), start=1):
        feed.add_pending(
            TranslationRequest(
                f"obs-{index}", source, "ur", NOW + timedelta(minutes=minutes), "Geo News"
            ),
            repeat=index > 1,
        )
    assert len(feed.snapshot(now=NOW + timedelta(minutes=29))) == 3
    remaining = feed.snapshot(now=NOW + timedelta(minutes=39))
    assert [item.observation_id for item in remaining] == ["obs-3"]
    assert remaining[0].repeat is True


def test_coordinator_emits_original_before_translation_update() -> None:
    source = "The court adjourned the hearing."
    translated = "عدالت نے سماعت ملتوی کر دی۔"
    events = []
    service = TranslationService(
        StaticTranslationEngine({("en", "ur", source): translated}), config=config()
    )
    coordinator = TranslationCoordinator(service, event_sink=events.append)
    request = TranslationRequest("obs-live", source, "en", NOW, "Dunya News")
    pending = coordinator.submit(request)
    assert pending.translation_status == "pending"
    assert events[0]["event"] == "live_observation"
    result = coordinator.process_one(coordinator._queue.get_nowait())
    coordinator._queue.task_done()
    assert result.status == "complete"
    assert events[1]["event"] == "translation_ready"
    assert events[1]["data"]["translation_text"] == translated


def test_translation_queue_saturation_is_explicit_not_silent() -> None:
    source = "The court adjourned the hearing."
    service = TranslationService(
        StaticTranslationEngine({("en", "ur", source): "عدالت نے سماعت ملتوی کر دی۔"}),
        config=config(queue_capacity=1),
    )
    coordinator = TranslationCoordinator(service)
    coordinator.submit(TranslationRequest("obs-1", source, "en", NOW, "Geo News"))
    with pytest.raises(TranslationBackpressureError):
        coordinator.submit(TranslationRequest("obs-2", source, "en", NOW, "Geo News"))


def test_unsupported_source_language_is_rejected() -> None:
    service = TranslationService(StaticTranslationEngine({}), config=config())
    with pytest.raises(TranslationError):
        service.translate(TranslationRequest("obs", "خبر", "mixed", NOW, "Geo News"))


def test_translation_benchmark_requires_approved_complete_reference() -> None:
    source = "The court adjourned the hearing until 22 July."
    output = "عدالت نے سماعت 22 جولائی تک ملتوی کر دی۔"
    service = TranslationService(
        StaticTranslationEngine({("en", "ur", source): output}), config=config()
    )
    report = benchmark_records(
        [
            {
                "id": "legal-1",
                "source_text": source,
                "source_language": "en",
                "acceptable_translations": [output],
                "required_terms": ["22", "عدالت"],
                "forbidden_terms": ["court"],
            }
        ],
        service,
    )
    assert report["pass_rate"] == 1.0


def test_translation_benchmark_rejects_unreviewed_alternative() -> None:
    source = "The cabinet will meet tomorrow."
    output = "کابینہ کل ملاقات کرے گی۔"
    service = TranslationService(
        StaticTranslationEngine({("en", "ur", source): output}), config=config()
    )
    report = benchmark_records(
        [
            {
                "source_text": source,
                "source_language": "en",
                "acceptable_translations": ["کابینہ کا اجلاس کل ہوگا۔"],
            }
        ],
        service,
    )
    assert report["pass_rate"] == 0.0


def test_number_words_are_compared_across_english_and_urdu() -> None:
    accepted = validate_complete_sentence_translation(
        "Five people were injured.",
        "پانچ افراد زخمی ہوئے۔",
        source_language="en",
        target_language="ur",
    )
    rejected = validate_complete_sentence_translation(
        "Five people were injured.",
        "چھ افراد زخمی ہوئے۔",
        source_language="en",
        target_language="ur",
    )
    assert accepted.checks["numbers_preserved"] is True
    assert rejected.checks["numbers_preserved"] is False


def test_negation_must_be_preserved() -> None:
    quality = validate_complete_sentence_translation(
        "The court did not grant bail.",
        "عدالت نے ضمانت منظور کر دی۔",
        source_language="en",
        target_language="ur",
    )
    assert quality.accepted is False
    assert quality.checks["negation_preserved"] is False


def test_coordinator_persists_translation_result_before_update_event() -> None:
    source = "The court adjourned the hearing."
    translated = "عدالت نے سماعت ملتوی کر دی۔"
    order = []
    service = TranslationService(
        StaticTranslationEngine({("en", "ur", source): translated}), config=config()
    )
    coordinator = TranslationCoordinator(
        service,
        result_sink=lambda result: order.append(("persist", result.observation_id)),
        event_sink=lambda event: order.append((event["event"], event["data"]["observation_id"])),
    )
    request = TranslationRequest("obs-persist", source, "en", NOW, "Geo News")
    coordinator.submit(request)
    queued = coordinator._queue.get_nowait()
    coordinator._queue.task_done()
    coordinator.process_one(queued)
    assert order == [
        ("live_observation", "obs-persist"),
        ("persist", "obs-persist"),
        ("translation_ready", "obs-persist"),
    ]
