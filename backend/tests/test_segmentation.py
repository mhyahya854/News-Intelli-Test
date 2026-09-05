from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from newsintel.ocr import OCRFrameResult, OCRLine, script_profile
from newsintel.segmentation import (
    BoundedOCRResultBus,
    InMemorySegmentationResultSink,
    SegmentationBackpressureError,
    SegmentationBenchmarkCase,
    SegmentationBusWorker,
    SegmentationConfig,
    SentenceReconstructor,
    benchmark_segmentation,
    extract_region_observations,
    merge_exact_overlap,
    split_complete_sentences,
)


def polygon(x1: float, y1: float, x2: float, y2: float):
    return ((x1, y1), (x2, y1), (x2, y2), (x1, y2))


def ocr_line(text: str, box, confidence: float = 0.99) -> OCRLine:
    return OCRLine(
        text=text,
        confidence=confidence,
        polygon=box,
        engine="fixture",
        model="fixture",
        script=script_profile(text),
        accepted=confidence >= 0.95,
        needs_review=confidence < 0.95,
    )


def frame(sequence: int, lines: list[OCRLine], stream_id: str = "geo") -> OCRFrameResult:
    timestamp = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=sequence / 2)
    text = "\n".join(line.text for line in lines)
    return OCRFrameResult(
        stream_id=stream_id,
        channel_name="Geo News",
        frame_sequence=sequence,
        frame_sha256=f"sha-{sequence}",
        frame_timestamp=timestamp,
        raw_text=text,
        normalized_text=text,
        confidence=sum(line.confidence for line in lines) / len(lines) if lines else 0,
        lines=tuple(lines),
        engine_chain=("fixture",),
        processing_ms=1.0,
        fallback_used=False,
        duplicate_of_recent_frame=False,
        duplicate_similarity=0.0,
        review_required=any(line.needs_review for line in lines),
        skipped_downstream=False,
        diagnostics={"frame_width": 1920, "frame_height": 1080},
    )


def emitted_texts(reconstructor: SentenceReconstructor, frames: list[OCRFrameResult]) -> list[str]:
    values: list[str] = []
    for item in frames:
        values.extend(unit.text for unit in reconstructor.process_frame(item).units)
    return values


def test_exact_overlap_reconstructs_urdu_ticker_sentence() -> None:
    box = polygon(100, 900, 1800, 1000)
    values = emitted_texts(
        SentenceReconstructor(),
        [
            frame(1, [ocr_line("وزیر اعظم نے آج", box)]),
            frame(2, [ocr_line("آج کابینہ کا اجلاس طلب کیا۔", box)]),
        ],
    )
    assert values == ["وزیر اعظم نے آج کابینہ کا اجلاس طلب کیا۔"]


def test_exact_overlap_reconstructs_english_sentence() -> None:
    box = polygon(100, 900, 1800, 1000)
    values = emitted_texts(
        SentenceReconstructor(),
        [
            frame(1, [ocr_line("The prime minister has", box)]),
            frame(2, [ocr_line("has called a cabinet meeting.", box)]),
        ],
    )
    assert values == ["The prime minister has called a cabinet meeting."]


def test_one_character_difference_is_not_treated_as_overlap() -> None:
    merged = merge_exact_overlap("The governor announced", "governor announced reforms")
    assert merged.overlap_tokens == 2
    changed = merge_exact_overlap("The governor announced", "governr announced reforms")
    assert changed.overlap_tokens == 0
    assert changed.relationship == "no_overlap"


def test_no_overlap_never_invents_a_join() -> None:
    box = polygon(100, 900, 1800, 1000)
    reconstructor = SentenceReconstructor()
    first = reconstructor.process_frame(frame(1, [ocr_line("Cabinet meeting begins", box)]))
    second = reconstructor.process_frame(frame(2, [ocr_line("Petrol prices increased", box)]))
    assert not first.units
    assert not second.units
    assert second.incomplete_fragments
    assert "Cabinet meeting begins Petrol prices increased" not in [
        fragment.text for fragment in second.incomplete_fragments
    ]


def test_urdu_and_english_screen_regions_remain_separate() -> None:
    urdu_box = polygon(900, 850, 1850, 930)
    english_box = polygon(50, 200, 900, 280)
    result = frame(
        1,
        [
            ocr_line("وزیر اعظم نے اجلاس طلب کیا۔", urdu_box),
            ocr_line("Heavy rain is expected tonight.", english_box),
        ],
    )
    units = SentenceReconstructor().process_frame(result).units
    assert {unit.text for unit in units} == {
        "وزیر اعظم نے اجلاس طلب کیا۔",
        "Heavy rain is expected tonight.",
    }
    assert len({unit.track_id for unit in units}) == 2


def test_explicit_phase3_mixed_engine_marker_is_split_by_language() -> None:
    merged_box = polygon(100, 850, 1800, 930)
    observations = extract_region_observations(
        frame(1, [ocr_line("اہم خبر | BREAKING NEWS", merged_box)]),
        SegmentationConfig(),
    )
    assert {item.text for item in observations} == {"اہم خبر", "BREAKING NEWS"}
    assert {item.language for item in observations} == {"ur", "en"}


def test_stable_unpunctuated_headline_is_emitted_once() -> None:
    box = polygon(100, 100, 1500, 200)
    reconstructor = SentenceReconstructor()
    frames = [
        frame(1, [ocr_line("Prime Minister chairs federal cabinet meeting", box)]),
        frame(2, [ocr_line("Prime Minister chairs federal cabinet meeting", box)]),
        frame(3, [ocr_line("Prime Minister chairs federal cabinet meeting", box)]),
    ]
    units = []
    for item in frames:
        units.extend(reconstructor.process_frame(item).units)
    assert len(units) == 1
    assert units[0].unit_type == "headline"
    assert units[0].completion_reason == "stable_text"


def test_sentence_split_does_not_break_decimal_or_abbreviation() -> None:
    sentences, remainder = split_complete_sentences(
        "Dr. Khan said inflation reached 3.5 percent. The meeting continues"
    )
    assert sentences == ["Dr. Khan said inflation reached 3.5 percent."]
    assert remainder == "The meeting continues"


def test_multiple_sentences_are_emitted_and_remainder_is_held() -> None:
    box = polygon(100, 850, 1800, 930)
    result = SentenceReconstructor().process_frame(
        frame(1, [ocr_line("Rain has started. Roads are wet! More updates", box)])
    )
    assert [unit.text for unit in result.units] == ["Rain has started.", "Roads are wet!"]
    assert result.active_track_count == 1


def test_rolling_track_keeps_exactly_three_observations() -> None:
    box = polygon(100, 850, 1800, 930)
    reconstructor = SentenceReconstructor()
    last_result = None
    for sequence, text in enumerate(
        ["one two three", "two three four", "three four five", "four five six"], start=1
    ):
        last_result = reconstructor.process_frame(frame(sequence, [ocr_line(text, box)]))
    assert last_result is not None
    track_info = last_result.diagnostics["tracks"][0]
    assert track_info["rolling_observation_count"] == 3


def test_numeric_change_is_preserved_as_a_distinct_sentence() -> None:
    box = polygon(100, 850, 1800, 930)
    reconstructor = SentenceReconstructor()
    first = reconstructor.process_frame(frame(1, [ocr_line("Petrol price is Rs 272.", box)]))
    second = reconstructor.process_frame(frame(2, [ocr_line("Petrol price is Rs 280.", box)]))
    assert [unit.text for unit in first.units] == ["Petrol price is Rs 272."]
    assert [unit.text for unit in second.units] == ["Petrol price is Rs 280."]


def test_low_confidence_provenance_marks_unit_for_review() -> None:
    box = polygon(100, 850, 1800, 930)
    result = SentenceReconstructor().process_frame(
        frame(1, [ocr_line("Heavy rain is expected tonight.", box, confidence=0.80)])
    )
    assert result.units[0].review_required
    assert result.units[0].confidence == 0.8


def test_benchmark_requires_exact_precision_and_recall() -> None:
    box = polygon(100, 850, 1800, 930)
    case = SegmentationBenchmarkCase(
        case_id="urdu-overlap",
        frames=(
            frame(1, [ocr_line("وزیر اعظم نے آج", box)]),
            frame(2, [ocr_line("آج کابینہ کا اجلاس طلب کیا۔", box)]),
        ),
        expected_units=("وزیر اعظم نے آج کابینہ کا اجلاس طلب کیا۔",),
    )
    report = benchmark_segmentation([case])
    assert report.accepted
    assert report.precision == 1
    assert report.recall == 1


@pytest.mark.asyncio
async def test_bounded_ocr_bus_never_silently_drops() -> None:
    bus = BoundedOCRResultBus(capacity=1)
    item = frame(1, [])
    await bus.publish(item, timeout_seconds=0.1)
    with pytest.raises(SegmentationBackpressureError):
        await bus.publish(item, timeout_seconds=0.01)
    assert bus.size == 1


@pytest.mark.asyncio
async def test_segmentation_worker_processes_ocr_bus() -> None:
    bus = BoundedOCRResultBus(capacity=2)
    sink = InMemorySegmentationResultSink()
    worker = SegmentationBusWorker(bus, SentenceReconstructor(), sink)
    stop = asyncio.Event()
    box = polygon(100, 850, 1800, 930)
    await bus.publish(frame(1, [ocr_line("Heavy rain is expected tonight.", box)]))
    stop.set()
    await worker.run(stop)
    assert worker.processed_frames == 1
    assert sink.results[0].units[0].text == "Heavy rain is expected tonight."


def test_manifest_loader_and_synthetic_fixture() -> None:
    from pathlib import Path
    from newsintel.segmentation import load_segmentation_manifest

    manifest = Path(__file__).resolve().parents[1] / "fixtures" / "segmentation" / "manifest.synthetic.jsonl"
    cases = load_segmentation_manifest(manifest)
    report = benchmark_segmentation(cases)
    assert len(cases) == 2
    assert report.accepted


def test_abbreviation_can_end_a_sentence_at_end_of_text() -> None:
    sentences, remainder = split_complete_sentences("The delegation arrived from the U.S.")
    assert sentences == ["The delegation arrived from the U.S."]
    assert remainder == ""


def test_exact_emission_suppression_resets_on_new_pkt_day() -> None:
    box = polygon(100, 850, 1800, 930)
    reconstructor = SentenceReconstructor()
    first = frame(1, [ocr_line("Heavy rain is expected tonight.", box)])
    second = frame(2, [ocr_line("Heavy rain is expected tonight.", box)])
    second = OCRFrameResult(
        stream_id=second.stream_id,
        channel_name=second.channel_name,
        frame_sequence=second.frame_sequence,
        frame_sha256=second.frame_sha256,
        frame_timestamp=first.frame_timestamp + timedelta(days=1),
        raw_text=second.raw_text,
        normalized_text=second.normalized_text,
        confidence=second.confidence,
        lines=second.lines,
        engine_chain=second.engine_chain,
        processing_ms=second.processing_ms,
        fallback_used=second.fallback_used,
        duplicate_of_recent_frame=second.duplicate_of_recent_frame,
        duplicate_similarity=second.duplicate_similarity,
        review_required=second.review_required,
        skipped_downstream=second.skipped_downstream,
        diagnostics=second.diagnostics,
    )
    assert len(reconstructor.process_frame(first).units) == 1
    assert len(reconstructor.process_frame(second).units) == 1


def test_stable_headline_is_flushed_when_replaced() -> None:
    box = polygon(100, 100, 1500, 200)
    reconstructor = SentenceReconstructor()
    reconstructor.process_frame(frame(1, [ocr_line("Prime Minister chairs cabinet meeting", box)]))
    second = reconstructor.process_frame(frame(2, [ocr_line("Prime Minister chairs cabinet meeting", box)]))
    assert [unit.text for unit in second.units] == ["Prime Minister chairs cabinet meeting"]
    third = reconstructor.process_frame(frame(3, [ocr_line("Heavy rain warning issued", box)]))
    # It was already emitted on stability, so replacement must not duplicate it.
    assert not third.units
