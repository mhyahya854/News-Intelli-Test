from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import pytest

from newsintel.streaming import (
    BoundedFrameBus,
    ContinuityMetrics,
    FfmpegFrameReader,
    FrameBackpressureError,
    FrameEnvelope,
    JpegPipeParser,
    ResolvedStream,
    ResourceMetrics,
    RuntimeState,
    StreamManager,
    StreamSpec,
    ingestion_doctor,
    jpeg_dimensions,
    retry_delay,
    run_live_probe,
    runtime_compatibility,
    select_best_video_format,
)


def resolved(**overrides: Any) -> ResolvedStream:
    values: dict[str, Any] = {
        "source_page_url": "https://www.youtube.com/@geonews/live",
        "media_url": "https://example.test/live/index.m3u8",
        "title": "Geo News Live",
        "video_id": "abc123",
        "format_id": "301",
        "protocol": "m3u8_native",
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "is_live": True,
        "live_status": "is_live",
        "http_headers": {"User-Agent": "test-agent", "Referer": "https://youtube.com/"},
        "resolved_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return ResolvedStream(**values)


def frame(sequence: int, timestamp_ns: int) -> FrameEnvelope:
    return FrameEnvelope(
        stream_id="geo-news",
        channel_name="Geo News",
        sequence=sequence,
        captured_at=datetime.now(timezone.utc),
        received_monotonic_ns=timestamp_ns,
        jpeg_bytes=b"\xff\xd8payload\xff\xd9",
        sha256="a" * 64,
        width=1280,
        height=720,
        source_video_id="abc",
        source_format_id="301",
    )


def test_select_best_video_format_prefers_highest_hls_video_only_within_limit() -> None:
    info = {
        "formats": [
            {
                "format_id": "audio",
                "url": "https://example/audio.m3u8",
                "vcodec": "none",
                "acodec": "mp4a",
                "protocol": "m3u8_native",
            },
            {
                "format_id": "720",
                "url": "https://example/720.m3u8",
                "vcodec": "avc1",
                "acodec": "none",
                "protocol": "m3u8_native",
                "height": 720,
                "fps": 30,
            },
            {
                "format_id": "1080",
                "url": "https://example/1080.m3u8",
                "vcodec": "avc1",
                "acodec": "none",
                "protocol": "m3u8_native",
                "height": 1080,
                "fps": 30,
            },
            {
                "format_id": "1440",
                "url": "https://example/1440.m3u8",
                "vcodec": "av01",
                "acodec": "none",
                "protocol": "m3u8_native",
                "height": 1440,
                "fps": 30,
            },
        ]
    }
    selected = select_best_video_format(info, target_height=1080)
    assert selected["format_id"] == "1080"


def test_ffmpeg_command_is_cpu_only_video_only_and_piped() -> None:
    reader = FfmpegFrameReader("ffmpeg")
    spec = StreamSpec(
        stream_id="geo-news",
        channel_name="Geo News",
        youtube_url="https://www.youtube.com/@geonews/live",
        capture_fps=2,
    )
    command = reader.build_command(spec, resolved())
    joined = " ".join(command)
    assert "-hwaccel none" in joined
    assert "-an" in command
    assert "-sn" in command
    assert "-dn" in command
    assert "fps=2" in command
    assert "image2pipe" in command
    assert "pipe:1" in command
    assert "-reconnect" in command
    assert "test-agent" in command
    assert "https://example.test/live/index.m3u8" in command


def test_jpeg_parser_reassembles_split_frames_without_silent_loss() -> None:
    parser = JpegPipeParser(max_frame_bytes=1024)
    jpeg1 = b"\xff\xd8first\xff\xd9"
    jpeg2 = b"\xff\xd8second\xff\xd9"
    assert parser.feed(b"noise" + jpeg1[:4]) == []
    assert parser.feed(jpeg1[4:] + jpeg2[:3]) == [jpeg1]
    assert parser.feed(jpeg2[3:]) == [jpeg2]


def test_bounded_frame_bus_raises_instead_of_dropping() -> None:
    async def scenario() -> None:
        bus = BoundedFrameBus(capacity=1)
        await bus.publish(frame(1, 1), timeout_seconds=0.1)
        with pytest.raises(FrameBackpressureError):
            await bus.publish(frame(2, 2), timeout_seconds=0.01)
        retrieved = await bus.consume()
        assert retrieved.sequence == 1
        bus.task_done()

    asyncio.run(scenario())


def test_continuity_metrics_detects_gap_and_sequence_violation() -> None:
    metrics = ContinuityMetrics(target_fps=2)
    metrics.observe(frame(1, 0))
    metrics.observe(frame(2, 500_000_000))
    metrics.observe(frame(4, 2_000_000_000))
    snapshot = metrics.snapshot()
    assert snapshot["frame_count"] == 3
    assert snapshot["continuity_violations"] == 1
    assert snapshot["sequence_violations"] == 1


def test_retry_policy_alert_burst_then_degraded_retry() -> None:
    spec = StreamSpec(
        stream_id="geo-news",
        channel_name="Geo News",
        youtube_url="https://www.youtube.com/@geonews/live",
    )
    assert [retry_delay(spec, attempt) for attempt in range(1, 6)] == [2, 5, 10, 20, 30]
    assert retry_delay(spec, 6) == 60


def test_doctor_reports_gpu_disabled() -> None:
    report = ingestion_doctor()
    assert report["checks"]["yt_dlp"]["ok"] is True
    assert report["checks"]["hardware_acceleration"]["mode"] == "disabled"


def test_runtime_is_strictly_python_312_x64() -> None:
    assert runtime_compatibility((3, 12), 64)["ok"] is True
    assert runtime_compatibility((3, 9), 64)["ok"] is False
    assert runtime_compatibility((3, 11), 64)["ok"] is False
    assert runtime_compatibility((3, 13), 64)["ok"] is False
    assert runtime_compatibility((3, 12), 32)["ok"] is False


def test_live_probe_fails_fast_when_capture_process_dies() -> None:
    class FakeResolver:
        async def resolve(self, spec: StreamSpec) -> ResolvedStream:
            del spec
            return resolved()

    class FailingReader:
        async def capture(self, **_: Any) -> int:
            raise RuntimeError("ffmpeg died")

    async def scenario() -> None:
        spec = StreamSpec(
            stream_id="geo-news",
            channel_name="Geo News",
            youtube_url="https://www.youtube.com/@geonews/live",
        )
        started = time.monotonic()
        report = await run_live_probe(
            spec,
            duration_seconds=5,
            resolver=FakeResolver(),  # type: ignore[arg-type]
            reader=FailingReader(),  # type: ignore[arg-type]
        )
        assert time.monotonic() - started < 1
        assert report["status"] == "failed"
        assert "ffmpeg died" in report["error"]

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
def test_real_ffmpeg_synthetic_capture_cadence_and_jpeg_dimensions() -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=30",
        "-t",
        "3",
        "-vf",
        "fps=2",
        "-fps_mode",
        "vfr",
        "-c:v",
        "mjpeg",
        "-q:v",
        "2",
        "-f",
        "image2pipe",
        "pipe:1",
    ]
    completed = subprocess.run(command, capture_output=True, check=True, timeout=20)
    parser = JpegPipeParser(max_frame_bytes=2 * 1024 * 1024)
    frames = []
    payload = completed.stdout
    for index in range(0, len(payload), 997):
        frames.extend(parser.feed(payload[index : index + 997]))
    assert 5 <= len(frames) <= 7
    assert all(jpeg_dimensions(item) == (640, 360) for item in frames)


def test_stream_manager_retries_then_recovers_without_giving_up() -> None:
    class FakeResolver:
        calls = 0

        async def resolve(self, spec: StreamSpec) -> ResolvedStream:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary resolver failure")
            return resolved()

    class FakeReader:
        calls = 0

        async def capture(
            self,
            *,
            spec: StreamSpec,
            resolved: ResolvedStream,
            frame_bus: BoundedFrameBus,
            state: RuntimeState,
            stop_event: asyncio.Event,
            sequence_start: int,
            resource_metrics: ResourceMetrics,
        ) -> int:
            del spec, resolved, frame_bus, state, resource_metrics
            self.calls += 1
            stop_event.set()
            return sequence_start

    class StateSink:
        def __init__(self) -> None:
            self.statuses: list[str] = []

        async def status_changed(self, state: RuntimeState) -> None:
            self.statuses.append(state.status)

    async def no_sleep(_: float) -> None:
        await asyncio.sleep(0)

    async def scenario() -> None:
        sink = StateSink()
        resolver_instance = FakeResolver()
        reader_instance = FakeReader()
        manager = StreamManager(
            frame_bus=BoundedFrameBus(2),
            resolver=resolver_instance,  # type: ignore[arg-type]
            reader=reader_instance,  # type: ignore[arg-type]
            state_sink=sink,
            sleep=no_sleep,
        )
        spec = StreamSpec(
            stream_id="geo-news",
            channel_name="Geo News",
            youtube_url="https://www.youtube.com/@geonews/live",
        )
        await manager.start(spec)
        await asyncio.wait_for(manager._tasks[spec.stream_id], timeout=2)
        state = manager.states[spec.stream_id]
        assert resolver_instance.calls == 2
        assert reader_instance.calls == 1
        assert state.reconnect_count == 1
        assert state.status == "offline"
        assert "reconnecting" in sink.statuses
        assert "live" in sink.statuses

    asyncio.run(scenario())
