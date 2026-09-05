from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from newsintel.ocr import ocr_doctor, ocr_runtime_config_from_env
from newsintel.segmentation import segmentation_doctor, segmentation_runtime_config_from_env
from newsintel.keyword_matching import (
    KeywordMatcherService,
    keyword_doctor,
    keyword_runtime_config_from_env,
    seeded_keyword_provider,
)
from newsintel.streaming import ingestion_doctor, runtime_compatibility
from newsintel.deduplication import DeduplicationConfig, deduplication_doctor
from newsintel.translation import TranslationConfig, translation_doctor
from newsintel.persistence import PersistenceConfig, persistence_doctor
from newsintel.summarization import summarization_doctor, summarization_runtime_config
from newsintel.api import (
    ApiError,
    OutboxWebSocketPump,
    api_session_factory,
    dispose_api_engine,
    router as phase10_router,
    ws_router as phase10_ws_router,
    websocket_hub,
)
from newsintel.persistence import OutboxDispatcher

ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

APP_NAME = os.getenv("APP_NAME", "Pakistani News Stream Intelligence")
APP_ENV = os.getenv("APP_ENV", "development")
DATABASE_URL = os.getenv("DATABASE_URL", "")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://127.0.0.1:5173")


class KeywordTestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    language: str = Field(default="mixed", pattern="^(en|ur|mixed|unknown)$")


_seed_keyword_matcher = KeywordMatcherService(
    seeded_keyword_provider(),
    refresh_interval_seconds=keyword_runtime_config_from_env()["refresh_interval_seconds"],
)


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Database migrations and seeds remain explicit operator actions. The only
    # startup worker here publishes already-committed transactional outbox rows.
    pump: OutboxWebSocketPump | None = None
    pump_task: asyncio.Task[None] | None = None
    if DATABASE_URL and os.getenv("API_OUTBOX_ENABLED", "true").lower() in {"1", "true", "yes"}:
        try:
            pump = OutboxWebSocketPump(
                websocket_hub,
                OutboxDispatcher(session_factory=api_session_factory()),
                poll_seconds=float(os.getenv("API_OUTBOX_POLL_SECONDS", "0.25")),
            )
            pump_task = asyncio.create_task(pump.run(), name="newsintel-outbox-websocket-pump")
        except Exception:
            # Readiness will expose database failure; the API remains available for
            # diagnostics rather than crashing before the operator can inspect it.
            pump = None
            pump_task = None
    application.state.outbox_pump = pump
    application.state.outbox_pump_task = pump_task
    try:
        yield
    finally:
        if pump is not None:
            await pump.stop()
        if pump_task is not None:
            pump_task.cancel()
            with suppress(asyncio.CancelledError):
                await pump_task
        dispose_api_engine()


app = FastAPI(
    title=APP_NAME,
    version="0.12.0-phase.12",
    description="Docker-free PostgreSQL news intelligence platform with a production React frontend, complete sharing controls, runtime administration, REST APIs, and durable outbox-backed WebSocket delivery.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(phase10_router)
app.include_router(phase10_ws_router)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "detail": exc.extra_detail}},
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "The request did not pass validation.",
                "detail": exc.errors(),
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "not_found" if exc.status_code == 404 else "http_error"
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": str(exc.detail), "detail": None}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "An unexpected backend error occurred.",
                "detail": str(exc) if APP_ENV == "development" else None,
            }
        },
    )


def _connect() -> psycopg.Connection[Any]:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, connect_timeout=3)


@app.get("/api/v1/health", tags=["system"])
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": APP_NAME,
        "version": app.version,
        "phase": 12,
        "deployment": "windows-native-no-docker",
        "database": "postgresql",
        "python_runtime": runtime_compatibility(),
    }


@app.get("/api/v1/ready", tags=["system"])
def readiness() -> JSONResponse:
    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        message = "DATABASE_URL is not configured" if not DATABASE_URL else f"unavailable: {exc.__class__.__name__}"
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "checks": {"postgresql": message}},
        )
    return JSONResponse(status_code=200, content={"status": "ready", "checks": {"postgresql": "ok"}})


@app.get("/api/v1/schema", tags=["system"])
def schema_status() -> JSONResponse:
    try:
        with _connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version LIMIT 1")
            revision = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM categories")
            categories = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM keywords")
            keywords = cursor.fetchone()[0]
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "schema_not_ready",
                "expected_revision": "20260720_0006",
                "detail": str(exc) if APP_ENV == "development" else exc.__class__.__name__,
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": "schema_ready",
            "revision": revision,
            "categories": categories,
            "keywords": keywords,
        },
    )


@app.get("/api/v1/ingestion/doctor", tags=["ingestion"])
def stream_ingestion_doctor() -> JSONResponse:
    report = ingestion_doctor()
    return JSONResponse(status_code=200 if report["status"] == "ready" else 503, content=report)


@app.get("/api/v1/ocr/doctor", tags=["ocr"])
def ocr_dependency_doctor(load_models: bool = Query(default=False)) -> JSONResponse:
    report = ocr_doctor(load_models=load_models)
    return JSONResponse(status_code=200 if report["status"] == "ready" else 503, content=report)


@app.get("/api/v1/ocr/config", tags=["ocr"])
def ocr_configuration() -> dict[str, Any]:
    config = ocr_runtime_config_from_env()
    return {
        "device": "cpu",
        "models": {
            "detection": config.detection_model,
            "urdu_recognition": config.urdu_recognition_model,
            "english_recognition": config.english_recognition_model,
            "fallback": "EasyOCR Urdu+English",
        },
        "thresholds": {
            "high_confidence": config.high_confidence_threshold,
            "fallback_trigger": config.fallback_trigger_threshold,
            "minimum_candidate": config.minimum_candidate_confidence,
            "frame_duplicate": config.frame_text_duplicate_threshold,
        },
        "policy": {
            "low_confidence_text_is_retained": config.preserve_low_confidence_candidates,
            "accuracy_gate": "word_accuracy>=0.95 AND line_recall=1.0 AND numeric_token_recall=1.0",
            "confidence_is_not_accuracy": True,
        },
    }


@app.get("/api/v1/segmentation/doctor", tags=["segmentation"])
def sentence_segmentation_doctor() -> dict[str, Any]:
    return segmentation_doctor()


@app.get("/api/v1/segmentation/config", tags=["segmentation"])
def sentence_segmentation_configuration() -> dict[str, Any]:
    config = segmentation_runtime_config_from_env()
    return {
        "rolling_frame_count": config.rolling_frame_count,
        "screen_region_isolation": True,
        "language_region_isolation": True,
        "completion": {
            "terminal_punctuation": True,
            "stable_unpunctuated_headlines": config.stable_headline_frames,
            "incomplete_fragments_are_held": True,
        },
        "matching_policy": {
            "exact_normalized_overlap_only": True,
            "fuzzy_overlap": False,
            "misspelling_variants": False,
            "autocorrection": False,
        },
        "limits": {
            "minimum_characters": config.minimum_unit_characters,
            "minimum_words": config.minimum_unit_words,
            "maximum_pending_characters": config.maximum_pending_characters,
        },
    }




@app.get("/api/v1/keyword-matching/doctor", tags=["keyword-matching"])
def keyword_matching_doctor() -> dict[str, Any]:
    return keyword_doctor()


@app.get("/api/v1/keyword-matching/config", tags=["keyword-matching"])
def keyword_matching_configuration() -> dict[str, Any]:
    config = keyword_runtime_config_from_env()
    return {
        "engine": "Aho-Corasick exact multi-pattern search",
        "production_source": "PostgreSQL active categories and keywords",
        "cache_refresh_seconds": config["refresh_interval_seconds"],
        "matching_policy": {
            "whole_word_or_phrase": True,
            "unicode_normalization_only": True,
            "full_sentence_context_classification": True,
            "multi_label_categories": True,
            "fuzzy_matching": False,
            "stemming": False,
            "misspelling_variants": False,
            "autocorrection": False,
        },
        "delivery_policy": {
            "matched_observations_emit_immediately": True,
            "repeated_story_occurrences_update_live_state": True,
            "canonical_story_deduplication": "Phase 6",
            "summary_input": "canonical non-duplicate stories only",
        },
    }


@app.post("/api/v1/keyword-matching/test", tags=["keyword-matching"])
def keyword_matching_test(request: KeywordTestRequest) -> dict[str, Any]:
    observation = _seed_keyword_matcher.match_text(
        text=request.text,
        language=request.language,
        unit_id="api-preview",
        stream_id="api-preview",
        channel_name="Keyword Preview",
        observed_at=datetime.now(timezone.utc),
        confidence=1.0,
        review_required=False,
    )
    payload = observation.serializable()
    payload["diagnostic_source"] = "seed-preview; production uses PostgreSQL"
    return payload


@app.get("/api/v1/deduplication/doctor", tags=["deduplication"])
def canonical_deduplication_doctor(load_model: bool = Query(default=False)) -> JSONResponse:
    report = deduplication_doctor(load_model=load_model)
    return JSONResponse(status_code=200 if report["status"] == "ready" else 503, content=report)


@app.get("/api/v1/deduplication/config", tags=["deduplication"])
def canonical_deduplication_configuration() -> dict[str, Any]:
    config = DeduplicationConfig.from_env()
    return {
        "calendar_boundary": "Asia/Karachi midnight-to-midnight",
        "embedding": {
            "model": config.model_name,
            "revision": config.model_revision,
            "dimensions": config.embedding_dimensions,
            "device": "cpu",
            "production_acceptance": "labelled bilingual duplicate/non-duplicate benchmark required",
        },
        "thresholds": {
            "same_day_auto_merge": config.same_day_auto_merge_similarity,
            "cross_channel_window_auto_merge": config.cross_channel_window_similarity,
            "cross_language_auto_merge": config.cross_language_auto_merge_similarity,
            "review_gate": config.review_similarity,
            "minimum_exact_token_overlap": config.minimum_lexical_overlap,
            "cross_channel_window_minutes": config.cross_channel_window_minutes,
        },
        "guards": {
            "category_overlap": True,
            "numbers": "exactly compatible or review",
            "named_entities": "exact anchor set or review",
            "negation": "must agree",
            "event_state": "contradictory outcomes block merge",
            "spelling_fuzziness": False,
        },
        "delivery": {
            "every_observation_emits_immediately": True,
            "repeats_update_one_canonical_story": True,
            "only_new_canonical_story_enters_summary": True,
            "uncertain_candidate_is_visible_live_but_withheld_from_summary": True,
        },
    }


@app.get("/api/v1/translation/doctor", tags=["translation"])
def complete_sentence_translation_doctor(load_models: bool = Query(default=False)) -> JSONResponse:
    report = translation_doctor(load_models=load_models)
    return JSONResponse(status_code=200 if report["status"] == "ready" else 503, content=report)


@app.get("/api/v1/translation/config", tags=["translation"])
def complete_sentence_translation_configuration() -> dict[str, Any]:
    config = TranslationConfig.from_env()
    return {
        "runtime": "CTranslate2 MarianMT INT8 CPU",
        "source_languages": ["en", "ur"],
        "output_policy": {
            "preserve_original": True,
            "translate_only_opposite_language": True,
            "complete_sentence_units_only": True,
            "word_by_word_translation": False,
            "roman_urdu_output_allowed": False,
            "failed_translation_never_drops_original": True,
        },
        "live_delivery": {
            "original_observation_emits_immediately": True,
            "translation_attaches_as_update_event": True,
            "window_minutes": config.live_window_minutes,
            "repeated_observations_visible": True,
            "canonical_story_and_summary_remain_deduplicated": True,
        },
        "performance": {
            "compute_type": config.compute_type,
            "beam_size": config.beam_size,
            "quality_retry_beam_size": config.retry_beam_size,
            "micro_batch_wait_ms": config.micro_batch_wait_ms,
            "maximum_batch_size": config.max_batch_size,
            "exact_repeat_translation_memory": True,
        },
    }


@app.get("/api/v1", tags=["system"])
def api_root() -> dict[str, Any]:
    return {
        "name": APP_NAME,
        "message": "Phase 12 sharing and administration are implemented: public news surfaces use server-generated share payloads, while protected runtime stream, category, and exact-keyword controls remain inside Admin.",
        "docs": "/docs",
        "health": "/api/v1/health",
        "readiness": "/api/v1/ready",
        "schema": "/api/v1/schema",
        "streams": "/api/v1/streams",
        "feed": "/api/v1/feed",
        "stories": "/api/v1/stories",
        "categories": "/api/v1/categories",
        "history_dates": "/api/v1/history/dates",
        "search": "/api/v1/search",
        "statistics": "/api/v1/stats/overview",
        "share_occurrence": "/api/v1/share/occurrence/{occurrence_id}",
        "share_sentence": "/api/v1/share/{sentence_id}",
        "share_summary": "/api/v1/share/summary/{category_id}?date=YYYY-MM-DD",
        "admin_keywords": "/api/v1/admin/keywords",
        "admin_keyword_test": "/api/v1/admin/keywords/test",
        "admin_streams": "/api/v1/admin/streams",
        "websocket": "/api/v1/ws/live",
        "websocket_compatibility": "/ws/live",
        "ingestion_doctor": "/api/v1/ingestion/doctor",
        "ocr_doctor": "/api/v1/ocr/doctor",
        "ocr_config": "/api/v1/ocr/config",
        "segmentation_doctor": "/api/v1/segmentation/doctor",
        "segmentation_config": "/api/v1/segmentation/config",
        "keyword_matching_doctor": "/api/v1/keyword-matching/doctor",
        "keyword_matching_config": "/api/v1/keyword-matching/config",
        "keyword_matching_test": "/api/v1/keyword-matching/test",
        "deduplication_doctor": "/api/v1/deduplication/doctor",
        "deduplication_config": "/api/v1/deduplication/config",
        "translation_doctor": "/api/v1/translation/doctor",
        "translation_config": "/api/v1/translation/config",
        "persistence_doctor": "/api/v1/persistence/doctor",
        "persistence_config": "/api/v1/persistence/config",
        "summarization_doctor": "/api/v1/summarization/doctor",
        "summarization_config": "/api/v1/summarization/config",
    }


@app.get("/api/v1/persistence/doctor", tags=["persistence"])
def storage_persistence_doctor(check_database: bool = Query(default=False)) -> JSONResponse:
    report = persistence_doctor(check_database=check_database)
    return JSONResponse(status_code=200 if report["status"] == "ready" else 503, content=report)


@app.get("/api/v1/persistence/config", tags=["persistence"])
def storage_persistence_configuration() -> dict[str, Any]:
    config = PersistenceConfig.from_env()
    return {
        "authoritative_store": "PostgreSQL",
        "database_failure_buffer": "checksum-verified append-only local spool",
        "redis": False,
        "transactions": {
            "canonical_story_and_occurrence": "single transaction",
            "translation_and_translation_memory": "single transaction",
            "outbox_event": "same transaction as domain write",
        },
        "outbox": {
            "delivery": "at-least-once with idempotent event keys",
            "batch_size": config.outbox_batch_size,
            "lease_seconds": config.outbox_lease_seconds,
            "maximum_attempts": config.outbox_max_attempts,
        },
        "retention": {
            "raw_frames": "never stored",
            "raw_ocr_days": config.ocr_retention_days,
            "observations": "permanent",
            "canonical_stories": "permanent",
            "translations": "permanent",
        },
        "live_feed_storage": {
            "window_minutes": 30,
            "repeated_occurrences_retained": True,
            "query_order": "observed_at descending",
            "translation_attached_to_exact_occurrence": True,
        },
        "recovery": {
            "spool_root": str(config.spool_root),
            "spool_max_bytes": config.spool_max_bytes,
            "spool_max_attempts": config.spool_max_attempts,
            "ordered_replay": True,
        },
    }


@app.get("/api/v1/summarization/config", tags=["summarization"])
def summarization_configuration() -> dict[str, Any]:
    return summarization_runtime_config()


@app.get("/api/v1/summarization/doctor", tags=["summarization"])
def summary_engine_doctor(load_model: bool = Query(default=False)) -> JSONResponse:
    report = summarization_doctor(load_model=load_model)
    return JSONResponse(status_code=200 if report["status"] == "ready" else 503, content=report)
