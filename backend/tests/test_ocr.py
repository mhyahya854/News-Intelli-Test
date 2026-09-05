from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from newsintel.ocr import (
    FrameTextDeduplicator,
    OCRBenchmarkSample,
    OCRConfig,
    OCRLine,
    OCRPipeline,
    benchmark_engine,
    contains_arabic,
    load_benchmark_manifest,
    merge_ocr_lines,
    normalize_ocr_text,
    script_profile,
    similarity,
)
from newsintel.streaming import FrameEnvelope


BOX = ((10.0, 10.0), (500.0, 10.0), (500.0, 80.0), (10.0, 80.0))
BOX_2 = ((10.0, 100.0), (500.0, 100.0), (500.0, 170.0), (10.0, 170.0))


def make_jpeg(path: Path | None = None) -> bytes:
    image = Image.new("RGB", (640, 360), "white")
    if path:
        image.save(path, format="JPEG", quality=95)
        return path.read_bytes()
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def make_frame(sequence: int, payload: bytes | None = None) -> FrameEnvelope:
    jpeg = payload or make_jpeg()
    return FrameEnvelope(
        stream_id="geo-news",
        channel_name="Geo News",
        sequence=sequence,
        captured_at=datetime.now(timezone.utc),
        received_monotonic_ns=time.monotonic_ns(),
        jpeg_bytes=jpeg,
        sha256=hashlib.sha256(jpeg).hexdigest(),
        width=640,
        height=360,
        source_video_id="video",
        source_format_id="format",
    )


def line(text: str, confidence: float, box=BOX, engine="fake-primary") -> OCRLine:
    return OCRLine(
        text=text,
        confidence=confidence,
        polygon=box,
        engine=engine,
        model="fake",
        script=script_profile(text),
        accepted=confidence >= 0.95,
        needs_review=confidence < 0.95,
    )


class FakeEngine:
    def __init__(self, name: str, outputs: list[list[OCRLine]]) -> None:
        self.name = name
        self.outputs = list(outputs)
        self.calls = 0

    def recognize(self, image_bytes: bytes) -> list[OCRLine]:
        assert image_bytes.startswith(b"\xff\xd8")
        self.calls += 1
        if self.outputs:
            return self.outputs.pop(0)
        return []


def test_urdu_normalization_preserves_urdu_and_normalizes_variants() -> None:
    text = "إيك ـ خبر يہاں\u200cہے"
    normalized = normalize_ocr_text(text)
    assert normalized == "ایک خبر یہاں ہے"
    assert contains_arabic(normalized)
    assert script_profile(normalized) == "ur"


def test_script_profile_handles_mixed_text() -> None:
    assert script_profile("Geo News خبر") == "mixed"
    assert script_profile("BREAKING NEWS") == "en"
    assert script_profile("1234") == "unknown"


def test_similarity_tolerates_spacing_and_punctuation() -> None:
    assert similarity("وزیرِ اعظم، کا اعلان", "وزیر اعظم کا اعلان") > 0.95
    assert similarity("Petrol price is Rs 272", "Petrol price is Rs 280") < 0.97


def test_frame_dedup_requires_same_number_signature() -> None:
    dedup = FrameTextDeduplicator(threshold=0.90, capacity=4)
    duplicate, _, _ = dedup.compare_and_remember("geo", "Petrol price Rs 272", 1)
    assert not duplicate
    duplicate, score, reference = dedup.compare_and_remember("geo", "Petrol price Rs 272", 2)
    assert duplicate and score == 1 and reference == 1
    duplicate, score, reference = dedup.compare_and_remember("geo", "Petrol price Rs 280", 3)
    assert not duplicate
    assert score > 0.8 and reference in {1, 2}


def test_merge_prefers_better_same_text_candidate() -> None:
    primary = [line("BREAKING NEWS", 0.72)]
    fallback = [line("BREAKING NEWS", 0.98, engine="fallback")]
    merged = merge_ocr_lines(primary, fallback, 0.95)
    assert len(merged) == 1
    assert merged[0].engine == "fallback"
    assert merged[0].accepted
    assert merged[0].alternatives


def test_merge_preserves_distinct_scripts_in_same_region() -> None:
    primary = [line("اہم خبر", 0.96)]
    fallback = [line("BREAKING NEWS", 0.97, engine="fallback")]
    merged = merge_ocr_lines(primary, fallback, 0.95)
    assert len(merged) == 1
    assert merged[0].script == "mixed"
    assert "اہم خبر" in merged[0].text
    assert "BREAKING NEWS" in merged[0].text


def test_pipeline_runs_fallback_below_95_percent_and_keeps_review_state() -> None:
    config = OCRConfig(fallback_audit_interval_frames=10)
    primary = FakeEngine("primary", [[line("اہم خبر", 0.82)]])
    fallback = FakeEngine("fallback", [[line("اہم خبر", 0.91, engine="fallback")]])
    result = OCRPipeline(primary=primary, fallback=fallback, config=config).process_frame(make_frame(1))
    assert primary.calls == 1
    assert fallback.calls == 1
    assert result.fallback_used
    assert result.review_required
    assert result.raw_text == "اہم خبر"
    assert not result.skipped_downstream


def test_pipeline_periodically_audits_even_high_confidence_frames() -> None:
    config = OCRConfig(fallback_audit_interval_frames=2)
    primary = FakeEngine(
        "primary",
        [[line("خبر ایک", 0.99)], [line("خبر دو", 0.99)]],
    )
    fallback = FakeEngine("fallback", [[line("اضافی خبر", 0.96, BOX_2, engine="fallback")]])
    pipeline = OCRPipeline(primary=primary, fallback=fallback, config=config)
    first = pipeline.process_frame(make_frame(1))
    second = pipeline.process_frame(make_frame(2))
    assert not first.fallback_used
    assert second.fallback_used
    assert "اضافی خبر" in second.raw_text


def test_pipeline_marks_text_duplicate_but_not_changed_number() -> None:
    config = OCRConfig(frame_text_duplicate_threshold=0.90)
    primary = FakeEngine(
        "primary",
        [
            [line("Petrol price Rs 272", 0.99)],
            [line("Petrol price Rs 272", 0.99)],
            [line("Petrol price Rs 280", 0.99)],
        ],
    )
    fallback = FakeEngine("fallback", [])
    pipeline = OCRPipeline(primary=primary, fallback=fallback, config=config)
    assert not pipeline.process_frame(make_frame(1)).skipped_downstream
    assert pipeline.process_frame(make_frame(2)).skipped_downstream
    assert not pipeline.process_frame(make_frame(3)).skipped_downstream


def test_manifest_loader_resolves_relative_paths(tmp_path: Path) -> None:
    image_path = tmp_path / "frame.jpg"
    make_jpeg(image_path)
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "id": "sample",
                "image": "frame.jpg",
                "language": "mixed",
                "ground_truth_lines": ["اہم خبر", "BREAKING NEWS"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    samples = load_benchmark_manifest(manifest)
    assert samples[0].image_path == image_path.resolve()
    assert samples[0].ground_truth_lines[0] == "اہم خبر"


def test_benchmark_accepts_exact_output(tmp_path: Path) -> None:
    image_path = tmp_path / "frame.jpg"
    make_jpeg(image_path)
    engine = FakeEngine(
        "benchmark",
        [[line("وزیر اعظم کا اعلان", 0.99), line("Petrol Rs 272", 0.99, BOX_2)]],
    )
    samples = [
        OCRBenchmarkSample(
            sample_id="exact",
            image_path=image_path,
            ground_truth_lines=("وزیر اعظم کا اعلان", "Petrol Rs 272"),
            language="mixed",
        )
    ]
    report = benchmark_engine(engine, samples)
    assert report.accepted
    assert report.word_accuracy == 1
    assert report.line_recall == 1
    assert report.numeric_token_recall == 1


def test_benchmark_rejects_missing_line_even_if_other_text_is_exact(tmp_path: Path) -> None:
    image_path = tmp_path / "frame.jpg"
    make_jpeg(image_path)
    engine = FakeEngine("benchmark", [[line("وزیر اعظم کا اعلان", 0.99)]])
    report = benchmark_engine(
        engine,
        [
            OCRBenchmarkSample(
                sample_id="missing",
                image_path=image_path,
                ground_truth_lines=("وزیر اعظم کا اعلان", "Petrol Rs 272"),
                language="mixed",
            )
        ],
    )
    assert not report.accepted
    assert report.line_recall == 0.5
    assert report.numeric_token_recall == 0


def test_ocr_bus_worker_connects_phase_2_bus_to_phase_3_pipeline() -> None:
    import asyncio

    from newsintel.ocr import InMemoryOCRResultSink, OCRBusWorker
    from newsintel.streaming import BoundedFrameBus

    async def scenario() -> None:
        bus = BoundedFrameBus(capacity=2)
        primary = FakeEngine("primary", [[line("Geo News خبر", 0.99)]])
        fallback = FakeEngine("fallback", [])
        pipeline = OCRPipeline(primary=primary, fallback=fallback, config=OCRConfig())
        sink = InMemoryOCRResultSink()
        worker = OCRBusWorker(bus, pipeline, sink)
        stop = asyncio.Event()
        await bus.publish(make_frame(1), timeout_seconds=1)
        stop.set()
        await worker.run(stop)
        await bus.join()
        assert worker.processed_frames == 1
        assert worker.failed_frames == 0
        assert sink.results[0].raw_text == "Geo News خبر"

    asyncio.run(scenario())


def test_paddle_adapter_uses_shared_detector_and_two_script_recognizers(monkeypatch) -> None:
    import sys
    import types

    from newsintel.ocr import PaddleDualScriptEngine

    created: list[tuple[str, dict]] = []

    class FakeDetection:
        def __init__(self, **kwargs):
            created.append(("detector", kwargs))

        def predict(self, **kwargs):
            assert kwargs["batch_size"] == 1
            return [
                {
                    "res": {
                        "dt_polys": [
                            [[10, 10], [500, 10], [500, 80], [10, 80]],
                        ],
                        "dt_scores": [0.99],
                    }
                }
            ]

    class FakeRecognition:
        def __init__(self, **kwargs):
            self.model_name = kwargs["model_name"]
            created.append(("recognizer", kwargs))

        def predict(self, **kwargs):
            assert kwargs["batch_size"] == 1
            if self.model_name.startswith("arabic"):
                return [{"res": {"rec_text": "اہم خبر", "rec_score": 0.98}}]
            return [{"res": {"rec_text": "BREAKING NEWS", "rec_score": 0.97}}]

    monkeypatch.setitem(
        sys.modules,
        "paddleocr",
        types.SimpleNamespace(TextDetection=FakeDetection, TextRecognition=FakeRecognition),
    )
    config = OCRConfig(
        detection_threshold=0.22,
        detection_box_threshold=0.44,
        detection_unclip_ratio=1.7,
        cpu_threads=3,
    )
    lines = PaddleDualScriptEngine(config).recognize(make_jpeg())
    assert len(lines) == 1
    assert lines[0].script == "mixed"
    assert "اہم خبر" in lines[0].text
    assert "BREAKING NEWS" in lines[0].text
    detector_config = created[0][1]
    assert detector_config["model_name"] == "PP-OCRv6_small_det"
    assert detector_config["thresh"] == 0.22
    assert detector_config["box_thresh"] == 0.44
    assert detector_config["unclip_ratio"] == 1.7
    assert detector_config["device"] == "cpu"
    assert detector_config["cpu_threads"] == 3
    assert [item[1]["model_name"] for item in created[1:]] == [
        "arabic_PP-OCRv5_mobile_rec",
        "en_PP-OCRv5_mobile_rec",
    ]
