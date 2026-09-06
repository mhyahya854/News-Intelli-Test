from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from newsintel.database import build_engine, build_session_factory
from newsintel.models import Stream, Sentence, SentenceOccurrence, OutboxEvent, Category
from newsintel.streaming import FrameEnvelope, BoundedFrameBus
from newsintel.ocr import OCRPipeline, OCRConfig, OCRLine, script_profile
from newsintel.segmentation import SentenceReconstructor, SegmentationConfig
from newsintel.keyword_matching import KeywordMatcherService, SQLAlchemyKeywordProvider
from newsintel.deduplication import DeduplicationConfig, FixedEmbeddingProvider
from newsintel.persistence import (
    PersistenceConfig,
    PostgresPersistenceService,
    ResilientPersistencePipeline,
    OutboxDispatcher,
)
from newsintel.translation import TranslationConfig, TranslationService, StaticTranslationEngine
from newsintel.runtime_pipeline import build_phase8_runtime, Phase8Runtime
from app import app


class DeterministicProductionFixtureEngine:
    """Deterministic OCR fixture engine simulating production OCR inference."""
    def __init__(self, text: str) -> None:
        self.name = "deterministic-production-fixture"
        self.text = text

    def recognize(self, image_bytes: bytes) -> list[OCRLine]:
        box = ((10.0, 10.0), (500.0, 10.0), (500.0, 80.0), (10.0, 80.0))
        return [
            OCRLine(
                text=self.text,
                confidence=0.99,
                polygon=box,
                engine=self.name,
                model="synthetic",
                script=script_profile(self.text),
                accepted=True,
                needs_review=False,
            )
        ]


@pytest.mark.asyncio
async def test_real_production_pipeline_e2e():
    """
    Phase 3 Hard Gate 6: Complete End-to-End test through the real production pipeline components:
    - Ingestion: BoundedFrameBus & FrameEnvelope
    - OCR Subsystem: OCRPipeline & OCRBusWorker
    - Segmentation: SentenceReconstructor & SegmentationBusWorker
    - Taxonomy/Keywords: KeywordMatcherService backed by real PostgreSQL taxonomy
    - Deduplication: DeduplicationService & PersistentObservationWorker
    - Persistence: ResilientPersistencePipeline & PostgresPersistenceService
    - Outbox/Events: OutboxDispatcher claiming transactional outbox events
    - API Exposure: FastAPI feed and category endpoints querying real PostgreSQL state
    """
    engine = build_engine()
    session_factory = build_session_factory(engine)
    
    # 1. Setup persistent fixture stream in PostgreSQL
    stream_id = uuid.uuid4()
    with session_factory() as session:
        stream = session.query(Stream).filter_by(channel_name="E2E Pipeline Channel").first()
        if not stream:
            stream = Stream(
                id=stream_id,
                channel_name="E2E Pipeline Channel",
                youtube_url="https://youtube.com/watch?v=e2e_real_pipeline",
                is_active=True,
                status="live",
                frame_rate_fps=1.0,
                config={"e2e_test": True},
            )
            session.add(stream)
            session.commit()
        else:
            stream_id = stream.id
            
    # 2. Configure production pipeline components
    frame_bus = BoundedFrameBus(capacity=16)
    
    e2e_text = "The Supreme Court granted bail after the hearing."
    ocr_config = OCRConfig()
    ocr_engine = DeterministicProductionFixtureEngine(text=e2e_text)
    ocr_pipeline = OCRPipeline(config=ocr_config, primary=ocr_engine, fallback=None)
    
    seg_config = SegmentationConfig()
    reconstructor = SentenceReconstructor(config=seg_config)
    
    # KeywordMatcherService backed by real PostgreSQL taxonomy
    keyword_provider = SQLAlchemyKeywordProvider(session_factory)
    matcher = KeywordMatcherService(keyword_provider)
    
    # Persistence & Deduplication
    persist_config = PersistenceConfig.from_env()
    persist_service = PostgresPersistenceService(config=persist_config, session_factory=session_factory)
    embedding_provider = FixedEmbeddingProvider({e2e_text: (1.0, 0.0, 0.0, 0.0)}, dimensions=4)
    dedup_config = DeduplicationConfig.from_env()
    persistence_pipeline = ResilientPersistencePipeline(
        persist_service,
        embedding_provider,
        dedup_config=dedup_config,
    )
    
    # Translation
    trans_config = TranslationConfig(
        model_root=Path("models/translation"),
        queue_capacity=16,
        publish_timeout_seconds=0.1,
        cache_capacity=100,
        live_window_minutes=30,
    )
    translation_engine = StaticTranslationEngine(
        {e2e_text: "عدالت عظمیٰ نے سماعت کے بعد ضمانت منظور کر لی۔"}
    )
    translation_service = TranslationService(translation_engine, config=trans_config)
    
    # 3. Assemble full Phase 8 production runtime
    events_captured = []
    runtime = build_phase8_runtime(
        frame_bus=frame_bus,
        ocr_pipeline=ocr_pipeline,
        reconstructor=reconstructor,
        matcher=matcher,
        persistence=persistence_pipeline,
        translation_service=translation_service,
        event_sink=lambda ev: events_captured.append(ev),
    )
    
    # 4. Run pipeline in background task and publish test frames
    stop_event = asyncio.Event()
    runtime_task = asyncio.create_task(runtime.run(stop_event))
    
    now = datetime.now(timezone.utc)
    jpeg_payload = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    
    # Publish 1 complete frame
    frame = FrameEnvelope(
        stream_id=str(stream_id),
        channel_name="E2E Pipeline Channel",
        sequence=1,
        captured_at=now,
        received_monotonic_ns=time.monotonic_ns(),
        jpeg_bytes=jpeg_payload,
        sha256=hashlib.sha256(jpeg_payload).hexdigest(),
        width=640,
        height=360,
        source_video_id="e2e-vid",
        source_format_id="e2e-fmt",
    )
    await frame_bus.publish(frame, timeout_seconds=5.0)
        
    # Wait for processing across pipeline workers
    await asyncio.sleep(2.0)
    stop_event.set()
    await runtime_task
    
    # 5. Verify metrics and data flow through production stages
    metrics = runtime.metrics
    assert metrics.ocr_persisted >= 1, "OCR frame results should be persisted"
    assert metrics.units_persisted >= 1, "Segmented unit should be persisted"
    assert metrics.observations_processed >= 1, "Observation should be processed"
    assert metrics.translations_queued >= 1, "Translation should be queued"
    assert metrics.failures == 0, f"No pipeline stage failures allowed: {metrics.last_error}"
    
    # 6. Verify PostgreSQL persistence directly in database
    with session_factory() as session:
        # Check occurrence exists and has judiciary category
        occ = (
            session.query(SentenceOccurrence)
            .filter(SentenceOccurrence.stream_id == stream_id)
            .order_by(SentenceOccurrence.observed_at.desc())
            .first()
        )
        assert occ is not None, "SentenceOccurrence must be written to PostgreSQL"
        assert "judiciary" in occ.category_ids, "Should match judiciary category from taxonomy"
        
        # Check sentence record exists
        sentence = session.query(Sentence).filter_by(id=occ.sentence_id).first()
        assert sentence is not None, "Sentence record must exist"
        assert e2e_text in sentence.original_text
        
        # Check outbox event was generated
        outbox = session.query(OutboxEvent).filter_by(aggregate_id=occ.observation_id).first()
        assert outbox is not None, "OutboxEvent must be written to PostgreSQL"
        
    # 7. Verify Outbox claim
    dispatcher = OutboxDispatcher(session_factory=session_factory)
    deliveries = dispatcher.claim(limit=10)
    assert len(deliveries) >= 1, "OutboxDispatcher must claim unhandled events"
    
    # 8. Verify API exposure via FastAPI client
    client = TestClient(app)
    feed_resp = client.get("/api/v1/feed")
    assert feed_resp.status_code == 200
    feed_data = feed_resp.json()
    assert "items" in feed_data
    feed_ids = [item["occurrence_id"] for item in feed_data["items"]]
    assert str(occ.id) in feed_ids, "Persisted occurrence must appear in /api/v1/feed"
    
    cat_resp = client.get("/api/v1/categories")
    assert cat_resp.status_code == 200
    cat_data = cat_resp.json()
    judiciary_cat = next((c for c in cat_data.get("items", []) if c["id"] == "judiciary"), None)
    assert judiciary_cat is not None
    assert judiciary_cat["today_observation_count"] >= 1
