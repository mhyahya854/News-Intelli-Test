from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from .deduplication import BoundedObservationBus, DeduplicationResult
from .keyword_matching import BoundedSegmentedUnitBus, DetectionObservation, KeywordBusWorker, KeywordMatcherService
from .ocr import OCRBusWorker, OCRFrameResult, OCRPipeline
from .persistence import PersistenceReceipt, ResilientPersistencePipeline
from .segmentation import (
    BoundedOCRResultBus,
    SegmentationBusWorker,
    SegmentationFrameResult,
    SentenceReconstructor,
)
from .translation import TranslationCoordinator, TranslationRequest, TranslationService


@dataclass(slots=True)
class PersistenceHandoffMetrics:
    ocr_persisted: int = 0
    ocr_spooled: int = 0
    frames_forwarded: int = 0
    duplicate_frames_stopped: int = 0
    units_persisted: int = 0
    units_spooled: int = 0
    units_forwarded: int = 0
    observations_processed: int = 0
    observations_spooled: int = 0
    translations_queued: int = 0
    failures: int = 0
    last_error: str | None = None


def _count_receipt(metrics: PersistenceHandoffMetrics, receipt: PersistenceReceipt, persisted: str, spooled: str) -> None:
    if receipt.status == "spooled":
        setattr(metrics, spooled, getattr(metrics, spooled) + 1)
    else:
        setattr(metrics, persisted, getattr(metrics, persisted) + 1)


class PersistentOCRForwardSink:
    """Persists or spools every OCR result before allowing sentence reconstruction."""

    def __init__(
        self,
        persistence: ResilientPersistencePipeline,
        downstream: BoundedOCRResultBus,
        metrics: PersistenceHandoffMetrics,
    ) -> None:
        self.persistence = persistence
        self.downstream = downstream
        self.metrics = metrics

    async def write(self, result: OCRFrameResult) -> None:
        try:
            receipt = await asyncio.to_thread(self.persistence.persist_ocr, result)
            _count_receipt(self.metrics, receipt, "ocr_persisted", "ocr_spooled")
            if result.skipped_downstream:
                self.metrics.duplicate_frames_stopped += 1
                return
            await self.downstream.publish(result)
            self.metrics.frames_forwarded += 1
        except Exception as exc:
            self.metrics.failures += 1
            self.metrics.last_error = f"{exc.__class__.__name__}: {exc}"
            raise


class PersistentSegmentationForwardSink:
    """Persists emitted complete units before keyword matching; fragments stay in OCR audit data."""

    def __init__(
        self,
        persistence: ResilientPersistencePipeline,
        downstream: BoundedSegmentedUnitBus,
        metrics: PersistenceHandoffMetrics,
    ) -> None:
        self.persistence = persistence
        self.downstream = downstream
        self.metrics = metrics

    async def write(self, result: SegmentationFrameResult) -> None:
        try:
            for unit in result.units:
                receipt = await asyncio.to_thread(self.persistence.persist_segmented_unit, unit)
                _count_receipt(self.metrics, receipt, "units_persisted", "units_spooled")
                await self.downstream.publish(unit)
                self.metrics.units_forwarded += 1
        except Exception as exc:
            self.metrics.failures += 1
            self.metrics.last_error = f"{exc.__class__.__name__}: {exc}"
            raise


class ObservationBusSink:
    def __init__(self, bus: BoundedObservationBus) -> None:
        self.bus = bus

    async def write(self, observation: DetectionObservation) -> None:
        await self.bus.publish(observation)


class PersistentObservationWorker:
    """Canonicalizes and commits/spools an observation before translation is queued."""

    def __init__(
        self,
        bus: BoundedObservationBus,
        persistence: ResilientPersistencePipeline,
        translation: TranslationCoordinator,
        metrics: PersistenceHandoffMetrics,
    ) -> None:
        self.bus = bus
        self.persistence = persistence
        self.translation = translation
        self.metrics = metrics
        self.processed_observations = 0
        self.failed_observations = 0
        self.last_error: str | None = None

    async def process(self, observation: DetectionObservation) -> tuple[DeduplicationResult, PersistenceReceipt]:
        result, receipt = await asyncio.to_thread(self.persistence.process, observation)
        self.metrics.observations_processed += 1
        self.processed_observations += 1
        if receipt.status == "spooled":
            self.metrics.observations_spooled += 1
        request = TranslationRequest(
            observation_id=observation.observation_id,
            source_text=observation.text,
            source_language=observation.language,
            observed_at=observation.observed_at,
            channel_name=observation.channel_name,
            category_ids=tuple(item.category_id for item in observation.accepted_categories),
            canonical_story_id=result.story.story_id if result.story else None,
            is_complete_sentence=True,
        )
        await asyncio.to_thread(
            self.translation.submit,
            request,
            repeat=result.action != "created",
        )
        self.metrics.translations_queued += 1
        return result, receipt

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() or self.bus.size:
            try:
                observation = await asyncio.wait_for(self.bus.consume(), timeout=0.25)
            except TimeoutError:
                continue
            try:
                await self.process(observation)
            except Exception as exc:
                self.metrics.failures += 1
                self.failed_observations += 1
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                self.metrics.last_error = self.last_error
                raise
            finally:
                self.bus.task_done()


@dataclass(slots=True)
class Phase8Runtime:
    """Wired frame→OCR→unit→keyword→canonical story→translation persistence runtime."""

    ocr_worker: OCRBusWorker
    segmentation_worker: SegmentationBusWorker
    keyword_worker: KeywordBusWorker
    observation_worker: PersistentObservationWorker
    translation: TranslationCoordinator
    ocr_bus: BoundedOCRResultBus
    unit_bus: BoundedSegmentedUnitBus
    observation_bus: BoundedObservationBus
    metrics: PersistenceHandoffMetrics

    async def run(self, stop_event: asyncio.Event) -> None:
        self.translation.start()
        tasks = [
            asyncio.create_task(self.ocr_worker.run(stop_event), name="ocr-worker"),
            asyncio.create_task(self.segmentation_worker.run(stop_event), name="segmentation-worker"),
            asyncio.create_task(self.keyword_worker.run(stop_event), name="keyword-worker"),
            asyncio.create_task(self.observation_worker.run(stop_event), name="observation-worker"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            self.translation.stop()


def build_phase8_runtime(
    *,
    frame_bus: Any,
    ocr_pipeline: OCRPipeline,
    reconstructor: SentenceReconstructor,
    matcher: KeywordMatcherService,
    persistence: ResilientPersistencePipeline,
    translation_service: TranslationService,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    ocr_bus_capacity: int = 32,
    unit_bus_capacity: int = 128,
    observation_bus_capacity: int = 128,
) -> Phase8Runtime:
    metrics = PersistenceHandoffMetrics()
    ocr_bus = BoundedOCRResultBus(ocr_bus_capacity)
    unit_bus = BoundedSegmentedUnitBus(unit_bus_capacity)
    observation_bus = BoundedObservationBus(observation_bus_capacity)
    translation = TranslationCoordinator(
        translation_service,
        event_sink=event_sink,
        result_sink=persistence.persist_translation,
    )
    ocr_sink = PersistentOCRForwardSink(persistence, ocr_bus, metrics)
    segmentation_sink = PersistentSegmentationForwardSink(persistence, unit_bus, metrics)
    observation_worker = PersistentObservationWorker(
        observation_bus, persistence, translation, metrics
    )
    return Phase8Runtime(
        ocr_worker=OCRBusWorker(frame_bus, ocr_pipeline, ocr_sink),
        segmentation_worker=SegmentationBusWorker(ocr_bus, reconstructor, segmentation_sink),
        keyword_worker=KeywordBusWorker(unit_bus, matcher, ObservationBusSink(observation_bus)),
        observation_worker=observation_worker,
        translation=translation,
        ocr_bus=ocr_bus,
        unit_bus=unit_bus,
        observation_bus=observation_bus,
        metrics=metrics,
    )
