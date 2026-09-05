from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from newsintel.deduplication import DeduplicationService, FixedEmbeddingProvider, InMemoryStoryRepository
from newsintel.keyword_matching import CategoryDecision, DetectionObservation, KeywordHit, normalize_match_text
from newsintel.ocr import OCRFrameResult
from newsintel.persistence import PersistenceReceipt
from newsintel.runtime_pipeline import (
    PersistenceHandoffMetrics,
    PersistentOCRForwardSink,
    PersistentObservationWorker,
    PersistentSegmentationForwardSink,
)
from newsintel.segmentation import SegmentationFrameResult, SegmentedTextUnit


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


class RecordingPersistence:
    def __init__(self, status: str = "persisted") -> None:
        self.status = status
        self.calls: list[tuple[str, object]] = []

    def persist_ocr(self, result):
        self.calls.append(("ocr", result))
        return PersistenceReceipt("ocr-command", "ocr", self.status)

    def persist_segmented_unit(self, unit):
        self.calls.append(("unit", unit))
        return PersistenceReceipt("unit-command", "segmented_unit", self.status)


class RecordingBus:
    def __init__(self) -> None:
        self.items: list[object] = []

    async def publish(self, item) -> None:
        self.items.append(item)


def ocr_result(*, skipped: bool = False) -> OCRFrameResult:
    return OCRFrameResult(
        stream_id=str(uuid.uuid4()),
        channel_name="Geo News",
        frame_sequence=1,
        frame_sha256="abc",
        frame_timestamp=NOW,
        raw_text="The court granted bail.",
        normalized_text="The court granted bail.",
        confidence=0.99,
        lines=(),
        engine_chain=("fixture",),
        processing_ms=1.0,
        fallback_used=False,
        duplicate_of_recent_frame=skipped,
        duplicate_similarity=1.0 if skipped else 0.0,
        review_required=False,
        skipped_downstream=skipped,
        diagnostics={"frame_width": 1920, "frame_height": 1080},
    )


def unit() -> SegmentedTextUnit:
    return SegmentedTextUnit(
        unit_id="unit-1",
        stream_id=str(uuid.uuid4()),
        channel_name="Geo News",
        track_id="track-1",
        text="The court granted bail.",
        normalized_text="the court granted bail.",
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


def observation() -> DetectionObservation:
    keyword_id = str(uuid.uuid4())
    hit = KeywordHit(
        keyword_id=keyword_id,
        category_id="judiciary",
        term="court",
        normalized_term="court",
        language="en",
        priority="normal",
        requires_context=False,
        matched_text="court",
        normalized_span=(4, 9),
        original_span=(4, 9),
    )
    decision = CategoryDecision(
        category_id="judiciary",
        label_en="Judiciary",
        label_ur="عدلیہ",
        color="#7C3AED",
        score=1.0,
        accepted=True,
        reason="exact_keyword",
        keyword_ids=(keyword_id,),
        supporting_context=("bail",),
        excluded_context=(),
    )
    text = "The court granted bail."
    return DetectionObservation(
        observation_id=str(uuid.uuid4()),
        unit_id="unit-1",
        stream_id=str(uuid.uuid4()),
        channel_name="Geo News",
        observed_at=NOW,
        text=text,
        normalized_text=normalize_match_text(text),
        language="en",
        confidence=0.99,
        review_required=False,
        keyword_hits=(hit,),
        category_decisions=(decision,),
        urgency="normal",
        emit_immediately=True,
        canonical_story_status="pending",
        summary_status="blocked",
        snapshot_version="test",
    )


@pytest.mark.asyncio
async def test_ocr_is_persisted_before_forwarding() -> None:
    persistence = RecordingPersistence()
    bus = RecordingBus()
    metrics = PersistenceHandoffMetrics()
    sink = PersistentOCRForwardSink(persistence, bus, metrics)  # type: ignore[arg-type]
    result = ocr_result()
    await sink.write(result)
    assert persistence.calls == [("ocr", result)]
    assert bus.items == [result]
    assert metrics.ocr_persisted == 1
    assert metrics.frames_forwarded == 1


@pytest.mark.asyncio
async def test_duplicate_ocr_is_audited_but_not_forwarded() -> None:
    persistence = RecordingPersistence(status="spooled")
    bus = RecordingBus()
    metrics = PersistenceHandoffMetrics()
    sink = PersistentOCRForwardSink(persistence, bus, metrics)  # type: ignore[arg-type]
    await sink.write(ocr_result(skipped=True))
    assert len(persistence.calls) == 1
    assert bus.items == []
    assert metrics.ocr_spooled == 1
    assert metrics.duplicate_frames_stopped == 1


@pytest.mark.asyncio
async def test_complete_units_are_persisted_before_keyword_bus() -> None:
    persistence = RecordingPersistence()
    bus = RecordingBus()
    metrics = PersistenceHandoffMetrics()
    sink = PersistentSegmentationForwardSink(persistence, bus, metrics)  # type: ignore[arg-type]
    item = unit()
    result = SegmentationFrameResult(
        stream_id=item.stream_id,
        channel_name=item.channel_name,
        frame_sequence=2,
        frame_timestamp=NOW,
        units=(item,),
        incomplete_fragments=(),
        active_track_count=1,
        diagnostics={},
    )
    await sink.write(result)
    assert persistence.calls == [("unit", item)]
    assert bus.items == [item]
    assert metrics.units_persisted == 1
    assert metrics.units_forwarded == 1


@pytest.mark.asyncio
async def test_observation_is_committed_before_translation_queue() -> None:
    item = observation()
    provider = FixedEmbeddingProvider({item.text: (1.0, 0.0, 0.0, 0.0)})
    result = DeduplicationService(InMemoryStoryRepository(), provider).process(item)
    receipt = PersistenceReceipt(
        command_id="observation-command",
        kind="observation",
        status="persisted",
        observation_id=item.observation_id,
        occurrence_id=result.occurrence.occurrence_id,
        sentence_id=result.story.story_id if result.story else None,
    )
    order: list[str] = []

    class FakePipeline:
        def process(self, value):
            assert value is item
            order.append("persist")
            return result, receipt

    class FakeTranslation:
        def submit(self, request, *, repeat=False):
            order.append("translate")
            assert request.observation_id == item.observation_id
            assert request.canonical_story_id == result.story.story_id
            assert repeat is False
            return SimpleNamespace()

    metrics = PersistenceHandoffMetrics()
    worker = PersistentObservationWorker(
        SimpleNamespace(), FakePipeline(), FakeTranslation(), metrics  # type: ignore[arg-type]
    )
    actual_result, actual_receipt = await worker.process(item)
    assert order == ["persist", "translate"]
    assert actual_result is result
    assert actual_receipt is receipt
    assert metrics.observations_processed == 1
    assert metrics.translations_queued == 1
