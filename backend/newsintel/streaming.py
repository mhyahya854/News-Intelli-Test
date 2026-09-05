from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import platform
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Mapping, Protocol, Sequence

import psutil
import yt_dlp

LOGGER = logging.getLogger(__name__)

DEFAULT_GEO_NEWS_URL = "https://www.youtube.com/@geonews/live"
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


class StreamIngestionError(RuntimeError):
    """Base class for stream-ingestion failures."""


class DependencyError(StreamIngestionError):
    """A required executable or library is unavailable."""


class StreamResolutionError(StreamIngestionError):
    """yt-dlp could not resolve a usable live video stream."""


class StreamNotLiveError(StreamResolutionError):
    """The configured source exists but is not currently live."""


class StreamStalledError(StreamIngestionError):
    """FFmpeg stopped delivering frame bytes within the allowed interval."""


class FrameBackpressureError(StreamIngestionError):
    """The downstream pipeline could not accept frames without data loss."""


class InvalidJpegStreamError(StreamIngestionError):
    """FFmpeg emitted malformed or unbounded JPEG data."""


@dataclass(slots=True, frozen=True)
class StreamSpec:
    stream_id: str
    channel_name: str
    youtube_url: str
    capture_fps: float = 2.0
    target_height: int = 1080
    jpeg_quality: int = 2
    first_frame_timeout_seconds: float = 45.0
    read_stall_timeout_seconds: float = 25.0
    queue_put_timeout_seconds: float = 10.0
    session_refresh_seconds: float = 10_800.0
    max_frame_bytes: int = 15 * 1024 * 1024
    retry_delays_seconds: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0, 30.0)
    alert_after_failures: int = 5
    degraded_retry_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.stream_id.strip():
            raise ValueError("stream_id cannot be blank")
        if not self.channel_name.strip():
            raise ValueError("channel_name cannot be blank")
        if not self.youtube_url.startswith(("https://", "http://")):
            raise ValueError("youtube_url must be HTTP(S)")
        if not 0.1 <= self.capture_fps <= 10:
            raise ValueError("capture_fps must be between 0.1 and 10")
        if self.target_height not in {360, 480, 540, 720, 900, 1080, 1440, 2160}:
            raise ValueError("target_height must be a supported video height")
        if not 1 <= self.jpeg_quality <= 31:
            raise ValueError("jpeg_quality must be between 1 and 31")
        if self.max_frame_bytes < 256 * 1024:
            raise ValueError("max_frame_bytes is too small for news frames")


@dataclass(slots=True, frozen=True)
class ResolvedStream:
    source_page_url: str
    media_url: str
    title: str
    video_id: str
    format_id: str
    protocol: str
    width: int | None
    height: int | None
    fps: float | None
    is_live: bool
    live_status: str | None
    http_headers: dict[str, str]
    resolved_at: datetime

    def safe_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["media_url"] = "<redacted-direct-media-url>"
        data["http_headers"] = {
            key: "<redacted>" if key.lower() in {"cookie", "authorization"} else value
            for key, value in self.http_headers.items()
        }
        data["resolved_at"] = self.resolved_at.isoformat()
        return data


@dataclass(slots=True, frozen=True)
class FrameEnvelope:
    stream_id: str
    channel_name: str
    sequence: int
    captured_at: datetime
    received_monotonic_ns: int
    jpeg_bytes: bytes
    sha256: str
    width: int
    height: int
    source_video_id: str
    source_format_id: str

    @property
    def size_bytes(self) -> int:
        return len(self.jpeg_bytes)

    def metadata(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "channel_name": self.channel_name,
            "sequence": self.sequence,
            "captured_at": self.captured_at.isoformat(),
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "source_video_id": self.source_video_id,
            "source_format_id": self.source_format_id,
        }


@dataclass(slots=True)
class ContinuityMetrics:
    target_fps: float
    frame_count: int = 0
    first_frame_at: datetime | None = None
    last_frame_at: datetime | None = None
    first_monotonic_ns: int | None = None
    last_monotonic_ns: int | None = None
    gaps_seconds: list[float] = field(default_factory=list)
    continuity_violations: int = 0
    duplicate_sequence_violations: int = 0
    last_sequence: int | None = None
    total_bytes: int = 0

    def observe(self, frame: FrameEnvelope) -> None:
        self.frame_count += 1
        self.total_bytes += frame.size_bytes
        if self.first_frame_at is None:
            self.first_frame_at = frame.captured_at
            self.first_monotonic_ns = frame.received_monotonic_ns
        if self.last_monotonic_ns is not None:
            gap = (frame.received_monotonic_ns - self.last_monotonic_ns) / 1_000_000_000
            self.gaps_seconds.append(gap)
            expected = 1.0 / self.target_fps
            if gap > max(expected * 1.75, expected + 0.35):
                self.continuity_violations += 1
        if self.last_sequence is not None and frame.sequence != self.last_sequence + 1:
            self.duplicate_sequence_violations += 1
        self.last_sequence = frame.sequence
        self.last_frame_at = frame.captured_at
        self.last_monotonic_ns = frame.received_monotonic_ns

    def snapshot(self) -> dict[str, Any]:
        duration = 0.0
        if self.first_monotonic_ns is not None and self.last_monotonic_ns is not None:
            duration = max(0.0, (self.last_monotonic_ns - self.first_monotonic_ns) / 1e9)
        effective_fps = (self.frame_count - 1) / duration if duration > 0 and self.frame_count > 1 else 0.0
        sorted_gaps = sorted(self.gaps_seconds)
        p95_gap = percentile(sorted_gaps, 95) if sorted_gaps else 0.0
        return {
            "target_fps": self.target_fps,
            "frame_count": self.frame_count,
            "first_frame_at": self.first_frame_at.isoformat() if self.first_frame_at else None,
            "last_frame_at": self.last_frame_at.isoformat() if self.last_frame_at else None,
            "duration_seconds": round(duration, 3),
            "effective_fps": round(effective_fps, 3),
            "average_gap_seconds": round(statistics.fmean(self.gaps_seconds), 3)
            if self.gaps_seconds
            else 0.0,
            "p95_gap_seconds": round(p95_gap, 3),
            "max_gap_seconds": round(max(self.gaps_seconds), 3) if self.gaps_seconds else 0.0,
            "continuity_violations": self.continuity_violations,
            "sequence_violations": self.duplicate_sequence_violations,
            "total_megabytes": round(self.total_bytes / (1024 * 1024), 3),
        }


@dataclass(slots=True)
class ResourceMetrics:
    cpu_samples: list[float] = field(default_factory=list)
    rss_samples_bytes: list[int] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "cpu_percent_average_one_core": round(statistics.fmean(self.cpu_samples), 2)
            if self.cpu_samples
            else 0.0,
            "cpu_percent_peak_one_core": round(max(self.cpu_samples), 2)
            if self.cpu_samples
            else 0.0,
            "rss_megabytes_average": round(statistics.fmean(self.rss_samples_bytes) / (1024 * 1024), 2)
            if self.rss_samples_bytes
            else 0.0,
            "rss_megabytes_peak": round(max(self.rss_samples_bytes) / (1024 * 1024), 2)
            if self.rss_samples_bytes
            else 0.0,
            "samples": len(self.cpu_samples),
        }


@dataclass(slots=True)
class RuntimeState:
    stream_id: str
    channel_name: str
    status: str = "offline"
    detail: str | None = None
    started_at: datetime | None = None
    last_frame_at: datetime | None = None
    last_error_at: datetime | None = None
    reconnect_count: int = 0
    consecutive_failures: int = 0
    alert_active: bool = False
    frames_published: int = 0
    queue_high_watermark: int = 0
    resolved: dict[str, Any] | None = None
    ffmpeg_pid: int | None = None

    def snapshot(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("started_at", "last_frame_at", "last_error_at"):
            value = data[key]
            data[key] = value.isoformat() if value else None
        return data


class FrameConsumer(Protocol):
    async def __call__(self, frame: FrameEnvelope) -> None: ...


class StreamStateSink(Protocol):
    async def status_changed(self, state: RuntimeState) -> None: ...


class NullStateSink:
    async def status_changed(self, state: RuntimeState) -> None:
        del state


class BoundedFrameBus:
    """A no-silent-drop frame handoff.

    Queue saturation raises an error instead of discarding old or new frames. This
    makes overload observable and prevents a false claim that capture is complete.
    """

    def __init__(self, capacity: int = 32) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._queue: asyncio.Queue[FrameEnvelope] = asyncio.Queue(maxsize=capacity)
        self.high_watermark = 0
        self.publish_count = 0

    @property
    def capacity(self) -> int:
        return self._queue.maxsize

    @property
    def size(self) -> int:
        return self._queue.qsize()

    async def publish(self, frame: FrameEnvelope, timeout_seconds: float) -> None:
        try:
            await asyncio.wait_for(self._queue.put(frame), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise FrameBackpressureError(
                f"frame queue remained full for {timeout_seconds:.1f}s; no frame was silently dropped"
            ) from exc
        self.publish_count += 1
        self.high_watermark = max(self.high_watermark, self._queue.qsize())

    async def consume(self) -> FrameEnvelope:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()


class JpegPipeParser:
    """Incrementally extracts complete JPEG files from FFmpeg image2pipe output."""

    def __init__(self, max_frame_bytes: int) -> None:
        self._buffer = bytearray()
        self._max_frame_bytes = max_frame_bytes

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        self._buffer.extend(chunk)
        frames: list[bytes] = []
        while True:
            start = self._buffer.find(JPEG_SOI)
            if start < 0:
                if len(self._buffer) > 2:
                    del self._buffer[:-2]
                return frames
            if start > 0:
                del self._buffer[:start]
            end = self._buffer.find(JPEG_EOI, 2)
            if end < 0:
                if len(self._buffer) > self._max_frame_bytes:
                    self._buffer.clear()
                    raise InvalidJpegStreamError(
                        f"JPEG frame exceeded safety limit of {self._max_frame_bytes} bytes"
                    )
                return frames
            end += 2
            frame = bytes(self._buffer[:end])
            del self._buffer[:end]
            if len(frame) > self._max_frame_bytes:
                raise InvalidJpegStreamError(
                    f"JPEG frame exceeded safety limit of {self._max_frame_bytes} bytes"
                )
            frames.append(frame)


class YtDlpResolver:
    def __init__(
        self,
        *,
        socket_timeout_seconds: float = 20.0,
        extractor_retries: int = 2,
        cookies_from_browser: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.socket_timeout_seconds = socket_timeout_seconds
        self.extractor_retries = extractor_retries
        self.cookies_from_browser = cookies_from_browser
        self.user_agent = user_agent

    async def resolve(self, spec: StreamSpec) -> ResolvedStream:
        return await asyncio.to_thread(self._resolve_sync, spec)

    def _resolve_sync(self, spec: StreamSpec) -> ResolvedStream:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": self.socket_timeout_seconds,
            "retries": self.extractor_retries,
            "extractor_retries": self.extractor_retries,
            "fragment_retries": self.extractor_retries,
            "live_from_start": False,
            "format": f"bestvideo[height<={spec.target_height}]/best[height<={spec.target_height}]",
        }
        if self.cookies_from_browser:
            options["cookiesfrombrowser"] = (self.cookies_from_browser,)
        if self.user_agent:
            options["user_agent"] = self.user_agent
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(spec.youtube_url, download=False)
        except Exception as exc:
            raise StreamResolutionError(f"yt-dlp resolution failed: {exc}") from exc
        if not isinstance(info, Mapping):
            raise StreamResolutionError("yt-dlp returned no stream metadata")
        info = unwrap_single_entry(info)
        live_status = str(info.get("live_status") or "") or None
        is_live = bool(info.get("is_live")) or live_status == "is_live"
        if live_status in {"not_live", "was_live", "post_live"} and not is_live:
            raise StreamNotLiveError(
                f"source is not live (live_status={live_status!r}, title={info.get('title')!r})"
            )
        selected = select_best_video_format(info, target_height=spec.target_height)
        media_url = selected.get("url") or info.get("url")
        if not isinstance(media_url, str) or not media_url.startswith(("http://", "https://")):
            raise StreamResolutionError("yt-dlp did not expose a usable HTTP(S) media URL")
        headers: dict[str, str] = {}
        for source in (info.get("http_headers"), selected.get("http_headers")):
            if isinstance(source, Mapping):
                for key, value in source.items():
                    if isinstance(key, str) and isinstance(value, str):
                        headers[key] = value
        return ResolvedStream(
            source_page_url=spec.youtube_url,
            media_url=media_url,
            title=str(info.get("title") or spec.channel_name),
            video_id=str(info.get("id") or "unknown"),
            format_id=str(selected.get("format_id") or info.get("format_id") or "unknown"),
            protocol=str(selected.get("protocol") or info.get("protocol") or "unknown"),
            width=to_int(selected.get("width") or info.get("width")),
            height=to_int(selected.get("height") or info.get("height")),
            fps=to_float(selected.get("fps") or info.get("fps")),
            is_live=is_live,
            live_status=live_status,
            http_headers=headers,
            resolved_at=datetime.now(timezone.utc),
        )


class FfmpegFrameReader:
    def __init__(self, executable: str = "ffmpeg") -> None:
        self.executable = executable

    def build_command(self, spec: StreamSpec, resolved: ResolvedStream) -> list[str]:
        command = [
            self.executable,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-hwaccel",
            "none",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_at_eof",
            "1",
            "-reconnect_delay_max",
            "10",
            "-rw_timeout",
            str(int(spec.read_stall_timeout_seconds * 1_000_000)),
        ]
        user_agent = pop_header_case_insensitive(resolved.http_headers, "User-Agent")
        referer = pop_header_case_insensitive(resolved.http_headers, "Referer")
        if user_agent:
            command.extend(["-user_agent", user_agent])
        if referer:
            command.extend(["-referer", referer])
        if resolved.http_headers:
            header_blob = "".join(f"{key}: {value}\r\n" for key, value in resolved.http_headers.items())
            command.extend(["-headers", header_blob])
        command.extend(
            [
                "-i",
                resolved.media_url,
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                f"fps={format_fps(spec.capture_fps)}",
                "-fps_mode",
                "vfr",
                "-c:v",
                "mjpeg",
                "-q:v",
                str(spec.jpeg_quality),
                "-f",
                "image2pipe",
                "pipe:1",
            ]
        )
        return command

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
        command = self.build_command(spec, clone_resolved(resolved))
        LOGGER.info(
            "starting FFmpeg stream=%s format=%s height=%s fps=%s",
            spec.stream_id,
            resolved.format_id,
            resolved.height,
            spec.capture_fps,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_creation_kwargs(),
        )
        state.ffmpeg_pid = process.pid
        stderr_lines: deque[str] = deque(maxlen=40)
        stderr_task = asyncio.create_task(drain_stderr(process, stderr_lines))
        sampler_task = asyncio.create_task(sample_process_tree(process.pid, stop_event, resource_metrics))
        parser = JpegPipeParser(spec.max_frame_bytes)
        sequence = sequence_start
        first_frame_deadline = time.monotonic() + spec.first_frame_timeout_seconds
        session_deadline = time.monotonic() + spec.session_refresh_seconds
        received_any = False
        try:
            if process.stdout is None:
                raise StreamIngestionError("FFmpeg stdout pipe was not created")
            while not stop_event.is_set():
                now = time.monotonic()
                if now >= session_deadline:
                    LOGGER.info("rotating expiring media URL for stream=%s", spec.stream_id)
                    return sequence
                timeout = (
                    max(0.1, first_frame_deadline - now)
                    if not received_any
                    else spec.read_stall_timeout_seconds
                )
                try:
                    chunk = await asyncio.wait_for(process.stdout.read(64 * 1024), timeout=timeout)
                except TimeoutError as exc:
                    stage = "first frame" if not received_any else "frame data"
                    raise StreamStalledError(f"timed out waiting for {stage}") from exc
                if not chunk:
                    return_code = await process.wait()
                    detail = " | ".join(stderr_lines)[-2000:]
                    raise StreamIngestionError(
                        f"FFmpeg exited with code {return_code}; stderr={detail or '<empty>'}"
                    )
                for jpeg_bytes in parser.feed(chunk):
                    received_any = True
                    sequence += 1
                    width, height = jpeg_dimensions(jpeg_bytes)
                    captured_at = datetime.now(timezone.utc)
                    frame = FrameEnvelope(
                        stream_id=spec.stream_id,
                        channel_name=spec.channel_name,
                        sequence=sequence,
                        captured_at=captured_at,
                        received_monotonic_ns=time.monotonic_ns(),
                        jpeg_bytes=jpeg_bytes,
                        sha256=hashlib.sha256(jpeg_bytes).hexdigest(),
                        width=width,
                        height=height,
                        source_video_id=resolved.video_id,
                        source_format_id=resolved.format_id,
                    )
                    await frame_bus.publish(frame, spec.queue_put_timeout_seconds)
                    state.frames_published += 1
                    state.last_frame_at = captured_at
                    state.queue_high_watermark = max(
                        state.queue_high_watermark, frame_bus.high_watermark
                    )
            return sequence
        finally:
            await terminate_process(process)
            stderr_task.cancel()
            sampler_task.cancel()
            await gather_cancelled(stderr_task, sampler_task)
            state.ffmpeg_pid = None


class StreamManager:
    def __init__(
        self,
        *,
        frame_bus: BoundedFrameBus,
        resolver: YtDlpResolver | None = None,
        reader: FfmpegFrameReader | None = None,
        state_sink: StreamStateSink | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.frame_bus = frame_bus
        self.resolver = resolver or YtDlpResolver()
        self.reader = reader or FfmpegFrameReader()
        self.state_sink = state_sink or NullStateSink()
        self.sleep = sleep
        self.states: dict[str, RuntimeState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stops: dict[str, asyncio.Event] = {}

    async def start(self, spec: StreamSpec) -> None:
        existing = self._tasks.get(spec.stream_id)
        if existing and not existing.done():
            raise ValueError(f"stream {spec.stream_id!r} is already running")
        stop_event = asyncio.Event()
        state = RuntimeState(
            stream_id=spec.stream_id,
            channel_name=spec.channel_name,
            status="starting",
            started_at=datetime.now(timezone.utc),
        )
        self.states[spec.stream_id] = state
        self._stops[spec.stream_id] = stop_event
        self._tasks[spec.stream_id] = asyncio.create_task(
            self._supervise(spec, state, stop_event),
            name=f"stream-supervisor:{spec.stream_id}",
        )
        await self.state_sink.status_changed(state)

    async def stop(self, stream_id: str) -> None:
        stop_event = self._stops.get(stream_id)
        task = self._tasks.get(stream_id)
        if stop_event:
            stop_event.set()
        if task:
            try:
                await asyncio.wait_for(task, timeout=10)
            except TimeoutError:
                task.cancel()
                await gather_cancelled(task)
        state = self.states.get(stream_id)
        if state:
            state.status = "offline"
            state.detail = "stopped"
            await self.state_sink.status_changed(state)

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(stream_id) for stream_id in list(self._tasks)))

    def snapshot(self) -> list[dict[str, Any]]:
        return [self.states[key].snapshot() for key in sorted(self.states)]

    async def _supervise(
        self, spec: StreamSpec, state: RuntimeState, stop_event: asyncio.Event
    ) -> None:
        sequence = 0
        while not stop_event.is_set():
            try:
                state.status = "resolving"
                state.detail = "resolving live media URL"
                await self.state_sink.status_changed(state)
                resolved = await self.resolver.resolve(spec)
                state.resolved = resolved.safe_dict()
                state.status = "live"
                state.detail = "capturing frames"
                await self.state_sink.status_changed(state)
                resource_metrics = ResourceMetrics()
                sequence = await self.reader.capture(
                    spec=spec,
                    resolved=resolved,
                    frame_bus=self.frame_bus,
                    state=state,
                    stop_event=stop_event,
                    sequence_start=state.frames_published,
                    resource_metrics=resource_metrics,
                )
                if stop_event.is_set():
                    break
                # Normal return means proactive media-URL rotation; reconnect immediately.
                state.status = "reconnecting"
                state.detail = "refreshing expiring live media URL"
                state.consecutive_failures = 0
                await self.state_sink.status_changed(state)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.consecutive_failures += 1
                state.reconnect_count += 1
                state.last_error_at = datetime.now(timezone.utc)
                state.status = "reconnecting"
                state.detail = f"{exc.__class__.__name__}: {exc}"
                if state.consecutive_failures >= spec.alert_after_failures:
                    state.alert_active = True
                    state.status = "error"
                await self.state_sink.status_changed(state)
                delay = retry_delay(spec, state.consecutive_failures)
                LOGGER.warning(
                    "stream failure stream=%s attempt=%s delay=%ss error=%s",
                    spec.stream_id,
                    state.consecutive_failures,
                    delay,
                    exc,
                )
                await interruptible_sleep(delay, stop_event, self.sleep)
        state.status = "offline"
        state.detail = "stopped"
        await self.state_sink.status_changed(state)


class ProbeFrameConsumer:
    def __init__(self, bus: BoundedFrameBus, target_fps: float) -> None:
        self.bus = bus
        self.metrics = ContinuityMetrics(target_fps=target_fps)
        self.last_frame_metadata: dict[str, Any] | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() or self.bus.size:
            try:
                frame = await asyncio.wait_for(self.bus.consume(), timeout=0.25)
            except TimeoutError:
                continue
            try:
                self.metrics.observe(frame)
                self.last_frame_metadata = frame.metadata()
            finally:
                self.bus.task_done()


async def run_live_probe(
    spec: StreamSpec,
    *,
    duration_seconds: float,
    queue_capacity: int = 32,
    resolver: YtDlpResolver | None = None,
    reader: FfmpegFrameReader | None = None,
) -> dict[str, Any]:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    bus = BoundedFrameBus(queue_capacity)
    stop_event = asyncio.Event()
    state = RuntimeState(
        stream_id=spec.stream_id,
        channel_name=spec.channel_name,
        status="resolving",
        started_at=datetime.now(timezone.utc),
    )
    resolver = resolver or YtDlpResolver(
        cookies_from_browser=os.getenv("YTDLP_COOKIES_FROM_BROWSER") or None
    )
    reader = reader or FfmpegFrameReader(os.getenv("FFMPEG_PATH", "ffmpeg"))
    consumer = ProbeFrameConsumer(bus, spec.capture_fps)
    resource_metrics = ResourceMetrics()
    resolved = await resolver.resolve(spec)
    state.resolved = resolved.safe_dict()
    state.status = "live"
    consumer_task = asyncio.create_task(consumer.run(stop_event))
    capture_task = asyncio.create_task(
        reader.capture(
            spec=spec,
            resolved=resolved,
            frame_bus=bus,
            state=state,
            stop_event=stop_event,
            sequence_start=0,
            resource_metrics=resource_metrics,
        )
    )
    timer_task = asyncio.create_task(asyncio.sleep(duration_seconds))
    error: str | None = None
    started = time.monotonic()
    started_ns = time.monotonic_ns()
    try:
        done, _ = await asyncio.wait(
            {capture_task, timer_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if capture_task in done and not timer_task.done():
            try:
                capture_task.result()
            except Exception as exc:
                error = f"{exc.__class__.__name__}: {exc}"
            else:
                error = "StreamIngestionError: capture ended before the requested probe duration"
    except asyncio.CancelledError:
        raise
    finally:
        stop_event.set()
        timer_task.cancel()
        await gather_cancelled(timer_task)
        if not capture_task.done():
            try:
                await asyncio.wait_for(capture_task, timeout=10)
            except Exception as exc:
                error = error or f"{exc.__class__.__name__}: {exc}"
                capture_task.cancel()
                await gather_cancelled(capture_task)
        else:
            try:
                capture_task.result()
            except Exception as exc:
                error = error or f"{exc.__class__.__name__}: {exc}"
        await bus.join()
        await asyncio.wait_for(consumer_task, timeout=5)
    elapsed = time.monotonic() - started
    continuity = consumer.metrics.snapshot()
    first_ns = consumer.metrics.first_monotonic_ns
    startup_latency = ((first_ns - started_ns) / 1e9) if first_ns is not None else elapsed
    active_window = max(0.0, elapsed - startup_latency)
    target_frames = max(1, math.floor(active_window * spec.capture_fps) + (1 if first_ns else 0))
    capture_ratio = continuity["frame_count"] / target_frames
    cadence_ok = (
        continuity["frame_count"] >= 2
        and continuity["effective_fps"] >= spec.capture_fps * 0.9
    )
    continuity_ok = (
        continuity["sequence_violations"] == 0
        and continuity["continuity_violations"] <= max(1, math.ceil(continuity["frame_count"] * 0.02))
    )
    queue_ok = bus.high_watermark < bus.capacity
    passed = error is None and capture_ratio >= 0.9 and cadence_ok and continuity_ok and queue_ok
    return {
        "status": "passed" if passed else "failed",
        "error": error,
        "stream": {
            "stream_id": spec.stream_id,
            "channel_name": spec.channel_name,
            "youtube_url": spec.youtube_url,
            "resolved": resolved.safe_dict(),
        },
        "configuration": {
            "duration_seconds": duration_seconds,
            "capture_fps": spec.capture_fps,
            "target_height": spec.target_height,
            "queue_capacity": queue_capacity,
            "no_silent_drop_policy": True,
            "hardware_acceleration": "none",
        },
        "continuity": continuity,
        "startup_latency_seconds": round(startup_latency, 3),
        "active_capture_window_seconds": round(active_window, 3),
        "target_frame_count": target_frames,
        "capture_ratio": round(capture_ratio, 4),
        "acceptance": {
            "capture_ratio_at_least_0_90": capture_ratio >= 0.9,
            "effective_fps_at_least_0_90_target": cadence_ok,
            "sequence_integrity": continuity["sequence_violations"] == 0,
            "continuity_within_tolerance": continuity_ok,
            "queue_never_saturated": queue_ok,
        },
        "queue": {
            "high_watermark": bus.high_watermark,
            "capacity": bus.capacity,
            "published": bus.publish_count,
        },
        "resources": resource_metrics.snapshot(),
        "last_frame": consumer.last_frame_metadata,
        "elapsed_seconds": round(elapsed, 3),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def select_best_video_format(info: Mapping[str, Any], target_height: int) -> Mapping[str, Any]:
    formats = info.get("formats")
    candidates: list[Mapping[str, Any]] = []
    if isinstance(formats, Sequence):
        for item in formats:
            if not isinstance(item, Mapping):
                continue
            if item.get("vcodec") in {None, "none"}:
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            height = to_int(item.get("height"))
            if height and height > target_height:
                continue
            protocol = str(item.get("protocol") or "")
            if protocol in {"mhtml", "images"}:
                continue
            candidates.append(item)
    if not candidates:
        if isinstance(info.get("url"), str):
            return info
        raise StreamResolutionError("no usable video format was found")

    def score(item: Mapping[str, Any]) -> tuple[int, int, int, float, float]:
        protocol = str(item.get("protocol") or "")
        protocol_score = 4 if protocol in {"m3u8_native", "m3u8"} else 2 if "https" in protocol else 1
        video_only = 1 if item.get("acodec") in {None, "none"} else 0
        height = to_int(item.get("height")) or 0
        fps = to_float(item.get("fps")) or 0.0
        bitrate = to_float(item.get("tbr")) or 0.0
        return protocol_score, video_only, height, fps, bitrate

    return max(candidates, key=score)


def unwrap_single_entry(info: Mapping[str, Any]) -> Mapping[str, Any]:
    entries = info.get("entries")
    if isinstance(entries, Sequence):
        usable = [entry for entry in entries if isinstance(entry, Mapping)]
        if len(usable) == 1:
            return usable[0]
    return info


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if not data.startswith(JPEG_SOI):
        raise InvalidJpegStreamError("frame does not start with JPEG SOI marker")
    index = 2
    length = len(data)
    while index + 4 <= length:
        if data[index] != 0xFF:
            index += 1
            continue
        while index < length and data[index] == 0xFF:
            index += 1
        if index >= length:
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > length:
            break
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > length:
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if segment_length < 7:
                break
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            if width > 0 and height > 0:
                return width, height
        index += segment_length
    raise InvalidJpegStreamError("JPEG dimensions could not be parsed")


def format_fps(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def retry_delay(spec: StreamSpec, consecutive_failures: int) -> float:
    if consecutive_failures <= len(spec.retry_delays_seconds):
        return spec.retry_delays_seconds[consecutive_failures - 1]
    return spec.degraded_retry_seconds


async def interruptible_sleep(
    seconds: float, stop_event: asyncio.Event, sleep: Callable[[float], Any]
) -> None:
    if seconds <= 0:
        return
    sleeper = asyncio.create_task(sleep(seconds))
    stopper = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await gather_cancelled(*pending)
    for task in done:
        if task is sleeper:
            await task


async def drain_stderr(
    process: asyncio.subprocess.Process, target: deque[str]
) -> None:
    if process.stderr is None:
        return
    while True:
        line = await process.stderr.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            target.append(text)
            LOGGER.debug("ffmpeg[%s]: %s", process.pid, text)


async def sample_process_tree(
    pid: int, stop_event: asyncio.Event, metrics: ResourceMetrics, interval_seconds: float = 1.0
) -> None:
    try:
        root = psutil.Process(pid)
        root.cpu_percent(None)
    except psutil.Error:
        return
    while not stop_event.is_set():
        await asyncio.sleep(interval_seconds)
        processes: list[psutil.Process] = [root]
        try:
            processes.extend(root.children(recursive=True))
        except psutil.Error:
            pass
        cpu = 0.0
        rss = 0
        for process in processes:
            try:
                cpu += process.cpu_percent(None)
                rss += process.memory_info().rss
            except psutil.Error:
                continue
        metrics.cpu_samples.append(cpu)
        metrics.rss_samples_bytes.append(rss)


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if platform.system() == "Windows":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        await asyncio.wait_for(process.wait(), timeout=4)
        return
    except (ProcessLookupError, TimeoutError, ValueError):
        pass
    try:
        process.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except TimeoutError:
        LOGGER.error("unable to reap FFmpeg process pid=%s", process.pid)


async def gather_cancelled(*tasks: asyncio.Task[Any]) -> None:
    if not tasks:
        return
    await asyncio.gather(*tasks, return_exceptions=True)


def subprocess_creation_kwargs() -> dict[str, Any]:
    if platform.system() == "Windows":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def pop_header_case_insensitive(headers: dict[str, str], name: str) -> str | None:
    for key in list(headers):
        if key.lower() == name.lower():
            return headers.pop(key)
    return None


def clone_resolved(value: ResolvedStream) -> ResolvedStream:
    return ResolvedStream(
        source_page_url=value.source_page_url,
        media_url=value.media_url,
        title=value.title,
        video_id=value.video_id,
        format_id=value.format_id,
        protocol=value.protocol,
        width=value.width,
        height=value.height,
        fps=value.fps,
        is_live=value.is_live,
        live_status=value.live_status,
        http_headers=dict(value.http_headers),
        resolved_at=value.resolved_at,
    )


def popen_version(command: Sequence[str], timeout_seconds: float = 10.0) -> str | None:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (completed.stdout or completed.stderr).strip()
    return text.splitlines()[0] if text else None


def runtime_compatibility(
    version_info: tuple[int, int] | None = None,
    pointer_bits: int | None = None,
) -> dict[str, Any]:
    version = version_info or (sys.version_info.major, sys.version_info.minor)
    bits = pointer_bits or struct.calcsize("P") * 8
    return {
        "ok": version == (3, 12) and bits == 64,
        "version": f"{version[0]}.{version[1]}",
        "architecture_bits": bits,
        "target": "CPython 3.12.x x64",
    }


def ingestion_doctor() -> dict[str, Any]:
    ffmpeg_path = shutil.which(os.getenv("FFMPEG_PATH", "ffmpeg"))
    yt_dlp_path = shutil.which("yt-dlp")
    checks = {
        "python": runtime_compatibility(),
        "ffmpeg": {
            "ok": bool(ffmpeg_path),
            "path": ffmpeg_path,
            "version": popen_version([ffmpeg_path, "-version"]) if ffmpeg_path else None,
        },
        "yt_dlp": {
            "ok": True,
            "python_package_version": getattr(yt_dlp.version, "__version__", "unknown"),
            "cli_path": yt_dlp_path,
        },
        "hardware_acceleration": {
            "ok": True,
            "mode": "disabled",
            "detail": "Phase 2 explicitly invokes FFmpeg with -hwaccel none.",
        },
    }
    return {
        "status": "ready" if all(item["ok"] for item in checks.values()) else "not_ready",
        "checks": checks,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def percentile(sorted_values: Sequence[float], percentile_value: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * percentile_value / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    fraction = rank - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def write_json_report(report: Mapping[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path
