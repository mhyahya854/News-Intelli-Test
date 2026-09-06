from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Iterable, Literal, Protocol, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response, Security, WebSocket, WebSocketDisconnect, status
from fastapi.security import APIKeyHeader
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import String, and_, cast, desc, func, or_, select, true, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload, sessionmaker

from .database import build_session_factory
from .keyword_matching import KeywordMatcherService, SQLAlchemyKeywordProvider, normalize_match_text
from .models import (
    AdminAuditLog,
    Category,
    DailySummary,
    Keyword,
    OccurrenceTranslation,
    RawOCRText,
    Sentence,
    SentenceCategory,
    SentenceKeyword,
    SentenceOccurrence,
    SentenceSource,
    Stream,
    PipelineTrace,
    SummaryFact,
    Translation,
)
from .persistence import OutboxDelivery, OutboxDispatcher

PKT = ZoneInfo("Asia/Karachi")
API_PREFIX = "/api/v1"
DEFAULT_LIVE_WINDOW_MINUTES = 30
MAX_PAGE_SIZE = 100


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, *, detail: Any | None = None):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        self.extra_detail = detail


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class PageMeta(BaseModel):
    next_cursor: str | None = None
    has_more: bool = False
    limit: int


class StreamResponse(BaseModel):
    id: str
    channel_name: str
    youtube_url: str
    is_active: bool
    status: str
    frame_rate_fps: float
    last_seen_at: datetime | None = None
    last_frame_at: datetime | None = None
    reconnect_count_today: int
    consecutive_failures: int


class StreamListResponse(BaseModel):
    items: list[StreamResponse]


class CreateStreamRequest(BaseModel):
    channel_name: str = Field(min_length=2, max_length=160)
    youtube_url: AnyHttpUrl
    frame_rate_fps: float = Field(default=0.5, gt=0, le=10)

    @field_validator("channel_name")
    @classmethod
    def clean_channel_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("channel_name must not be blank")
        return cleaned

    @field_validator("youtube_url")
    @classmethod
    def require_youtube_host(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        host = (value.host or "").casefold()
        if not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")):
            raise ValueError("youtube_url must use youtube.com or youtu.be")
        return value


class AdminTokenRequest(BaseModel):
    password: SecretStr


class AdminTokenResponse(BaseModel):
    token: str
    header: str = "X-Admin-Token"


class CategoryBrief(BaseModel):
    id: str
    label_en: str
    label_ur: str
    color: str


class TranslationBrief(BaseModel):
    status: str
    source_language: str | None = None
    target_language: str | None = None
    translated_text: str | None = None


class FeedItem(BaseModel):
    occurrence_id: str
    observation_id: str
    canonical_story_id: str | None = None
    stream_id: str
    channel_name: str
    observed_at: datetime
    original_text: str
    original_language: str
    confidence: float
    category_ids: list[str]
    keyword_ids: list[str]
    urgency: str
    review_required: bool
    dedup_decision: str
    dedup_layer: str
    translation: TranslationBrief


class FeedResponse(BaseModel):
    items: list[FeedItem]
    page: PageMeta
    window_minutes: int
    repeats_are_preserved: bool = True


class KeywordBrief(BaseModel):
    id: str
    category_id: str
    term: str
    language: str
    priority: str
    matched_text: str | None = None


class SourceBrief(BaseModel):
    stream_id: str
    channel_name: str
    first_seen_at: datetime
    last_seen_at: datetime
    occurrence_count: int


class SentenceDetail(BaseModel):
    id: str
    original_text: str
    original_language: str
    english_text: str | None = None
    urdu_text: str | None = None
    translation_status: str
    confidence: float
    categories: list[CategoryBrief]
    keywords: list[KeywordBrief]
    sources: list[SourceBrief]
    first_seen_at: datetime
    last_seen_at: datetime
    occurrence_count: int
    calendar_date: date
    review_status: str
    share_text: str | None = None


class StoryListItem(BaseModel):
    id: str
    original_text: str
    original_language: str
    english_text: str | None = None
    urdu_text: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    occurrence_count: int
    confidence: float
    category_ids: list[str]
    channel_names: list[str]
    review_status: str


class StoryListResponse(BaseModel):
    items: list[StoryListItem]
    page: PageMeta


class CategoryResponse(CategoryBrief):
    is_active: bool
    priority: int
    today_story_count: int
    today_observation_count: int


class CategoryListResponse(BaseModel):
    items: list[CategoryResponse]
    pakistan_date: date


class SummaryFactResponse(BaseModel):
    id: str
    order: int
    text_en: str
    text_ur: str
    kind: str
    priority: int
    source_first_seen_at: datetime
    source_last_seen_at: datetime


class SummaryResponse(BaseModel):
    id: str | None = None
    category: CategoryBrief
    calendar_date: date
    summary_text_en: str
    summary_text_ur: str
    source_sentence_count: int
    last_updated_at: datetime | None = None
    finalized_at: datetime | None = None
    facts: list[SummaryFactResponse]


class HistoryDatesResponse(BaseModel):
    dates: list[date]


class SearchResponse(BaseModel):
    items: list[StoryListItem]
    page: PageMeta
    query: str
    matching_policy: str = "exact-normalized substring and PostgreSQL simple text search; no typo correction"


class KeywordResponse(BaseModel):
    id: str
    category_id: str
    term: str
    normalized_term: str
    language: str
    is_active: bool
    priority: str
    match_mode: str
    requires_context: bool
    context_terms: list[str]
    excluded_terms: list[str]
    added_by: str


class KeywordListResponse(BaseModel):
    items: list[KeywordResponse]
    page: PageMeta


class CreateKeywordRequest(BaseModel):
    category_id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    term: str = Field(min_length=1, max_length=300)
    language: Literal["en", "ur"]
    priority: Literal["normal", "high", "critical"] = "normal"
    requires_context: bool = False
    context_terms: list[str] = Field(default_factory=list, max_length=100)
    excluded_terms: list[str] = Field(default_factory=list, max_length=100)


class UpdateKeywordRequest(BaseModel):
    term: str | None = Field(default=None, min_length=1, max_length=300)
    is_active: bool | None = None
    priority: Literal["normal", "high", "critical"] | None = None
    requires_context: bool | None = None
    context_terms: list[str] | None = Field(default=None, max_length=100)
    excluded_terms: list[str] | None = Field(default=None, max_length=100)


class CreateCategoryRequest(BaseModel):
    id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    label_en: str = Field(min_length=2, max_length=160)
    label_ur: str = Field(min_length=2, max_length=160)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    priority: int = Field(default=50, ge=0, le=100)
    description_en: str | None = None
    description_ur: str | None = None


class AdminKeywordTestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    language: Literal["en", "ur", "mixed", "unknown"] = "mixed"


class OverviewStats(BaseModel):
    pakistan_date: date
    unique_stories: int
    observations: int
    active_streams: int
    average_ocr_confidence: float | None = None
    average_end_to_end_latency_ms: float | None = None
    per_category: list[dict[str, Any]]
    per_stream: list[dict[str, Any]]


class KeywordFrequencyResponse(BaseModel):
    items: list[dict[str, Any]]


class ShareResponse(BaseModel):
    text: str
    whatsapp_url: str
    email_url: str
    web_share: dict[str, str]


class WebSocketEnvelope(BaseModel):
    event: str
    data: Any
    event_id: str | None = None
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class CursorValue:
    timestamp: datetime
    identifier: str


class CursorCodec:
    def __init__(self, secret: str | bytes):
        self.secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        if not self.secret:
            raise ValueError("cursor secret must not be empty")

    def encode(self, timestamp: datetime, identifier: str) -> str:
        aware = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        body = json.dumps(
            {"t": aware.astimezone(timezone.utc).isoformat(), "i": identifier},
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self.secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + b"." + signature).rstrip(b"=").decode("ascii")

    def decode(self, value: str) -> CursorValue:
        try:
            padded = value + "=" * (-len(value) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            if len(raw) < 34 or raw[-33:-32] != b".":
                raise ValueError("malformed cursor")
            body, signature = raw[:-33], raw[-32:]
            expected = hmac.new(self.secret, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature mismatch")
            parsed = json.loads(body)
            timestamp = datetime.fromisoformat(parsed["t"])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            identifier = str(parsed["i"])
            uuid.UUID(identifier)
            return CursorValue(timestamp=timestamp.astimezone(timezone.utc), identifier=identifier)
        except Exception as exc:
            raise ApiError(400, "invalid_cursor", "The pagination cursor is invalid or expired.") from exc


class ApiRepository(Protocol):
    def list_streams(self) -> list[dict[str, Any]]: ...
    def create_stream(self, request: CreateStreamRequest) -> dict[str, Any]: ...
    def deactivate_stream(self, stream_id: str) -> None: ...
    def list_feed(self, **kwargs: Any) -> tuple[list[dict[str, Any]], str | None, bool]: ...
    def get_sentence(self, sentence_id: str) -> dict[str, Any] | None: ...
    def get_occurrence(self, occurrence_id: str) -> dict[str, Any] | None: ...
    def list_stories(self, **kwargs: Any) -> tuple[list[dict[str, Any]], str | None, bool]: ...
    def list_categories(self, pakistan_date: date) -> list[dict[str, Any]]: ...
    def get_summary(self, category_id: str, calendar_date: date) -> dict[str, Any] | None: ...
    def history_dates(self, category_id: str | None) -> list[date]: ...
    def search(self, **kwargs: Any) -> tuple[list[dict[str, Any]], str | None, bool]: ...
    def list_keywords(self, **kwargs: Any) -> tuple[list[dict[str, Any]], str | None, bool]: ...
    def create_keyword(self, request: CreateKeywordRequest) -> dict[str, Any]: ...
    def update_keyword(self, keyword_id: str, request: UpdateKeywordRequest) -> dict[str, Any]: ...
    def deactivate_keyword(self, keyword_id: str) -> None: ...
    def test_keyword_match(self, request: AdminKeywordTestRequest) -> dict[str, Any]: ...
    def create_category(self, request: CreateCategoryRequest) -> dict[str, Any]: ...
    def overview_stats(self, pakistan_date: date) -> dict[str, Any]: ...
    def keyword_frequency(self, **kwargs: Any) -> list[dict[str, Any]]: ...


def _cursor_secret() -> str:
    return (
        os.getenv("API_CURSOR_SECRET", "").strip()
        or os.getenv("ADMIN_TOKEN", "").strip()
        or os.getenv("ADMIN_PASSWORD", "").strip()
        or "pak-news-development-cursor-secret"
    )


def cursor_codec() -> CursorCodec:
    return CursorCodec(_cursor_secret())


def _expected_admin_token() -> str | None:
    explicit = os.getenv("ADMIN_TOKEN", "").strip()
    if explicit:
        return explicit
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not password:
        return None
    secret = os.getenv("ADMIN_TOKEN_SECRET", "pak-news-local-admin-token-v1").encode("utf-8")
    return hmac.new(secret, password.encode("utf-8"), hashlib.sha256).hexdigest()


admin_header = APIKeyHeader(name="X-Admin-Token", auto_error=False, scheme_name="AdminToken")


def require_admin(token: str | None = Security(admin_header)) -> str:
    expected = _expected_admin_token()
    if expected is None:
        raise ApiError(503, "admin_not_configured", "ADMIN_PASSWORD or ADMIN_TOKEN is not configured.")
    if token is None or not secrets.compare_digest(token, expected):
        raise ApiError(401, "invalid_admin_token", "A valid X-Admin-Token header is required.")
    return token


@lru_cache(maxsize=1)
def api_session_factory() -> sessionmaker[Session]:
    """One shared SQLAlchemy engine/pool for all API requests in this process."""
    return build_session_factory()


def get_repository() -> ApiRepository:
    return PostgresApiRepository(api_session_factory())


def dispose_api_engine() -> None:
    if api_session_factory.cache_info().currsize:
        factory = api_session_factory()
        bind = factory.kw.get("bind")
        if bind is not None:
            bind.dispose()
        api_session_factory.cache_clear()


def _pakistan_today() -> date:
    return datetime.now(timezone.utc).astimezone(PKT).date()


def _serialize_stream(row: Stream) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "channel_name": row.channel_name,
        "youtube_url": row.youtube_url,
        "is_active": row.is_active,
        "status": row.status,
        "frame_rate_fps": row.frame_rate_fps,
        "last_seen_at": row.last_seen_at,
        "last_frame_at": row.last_frame_at,
        "reconnect_count_today": row.reconnect_count_today,
        "consecutive_failures": row.consecutive_failures,
    }


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _translated_texts(original: str, language: str, translation: Translation | OccurrenceTranslation | None) -> tuple[str | None, str | None, str]:
    if isinstance(translation, Translation):
        return translation.english_text, translation.urdu_text, translation.translation_status
    status_value = translation.status if translation else "pending"
    translated = translation.translated_text if translation else None
    if language == "en":
        return original, translated, status_value
    if language == "ur":
        return translated, original, status_value
    return None, None, status_value


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def build_sentence_share_text(
    *, channel: str, observed_at: datetime, original: str, english: str, urdu: str, app_name: str
) -> str:
    timestamp = observed_at.astimezone(PKT).strftime("%Y-%m-%d %I:%M:%S %p PKT")
    return f"[{channel}] [{timestamp}] — {original} | English: {english} | اردو: {urdu} | Source: {app_name}"


def build_summary_share_text(
    *, category_label: str, calendar_date: date, english: str, urdu: str, app_name: str
) -> str:
    return f"[{category_label}] [{calendar_date.isoformat()}] — English: {english} | اردو: {urdu} | Source: {app_name}"


def share_payload(text: str, *, subject: str) -> dict[str, Any]:
    return {
        "text": text,
        "whatsapp_url": f"https://wa.me/?text={quote(text)}",
        "email_url": f"mailto:?subject={quote(subject)}&body={quote(text)}",
        "web_share": {"title": subject, "text": text},
    }


class PostgresApiRepository:
    def __init__(self, session_factory: sessionmaker[Session] | None = None, codec: CursorCodec | None = None):
        self.session_factory = session_factory or build_session_factory()
        self.codec = codec or cursor_codec()

    def list_streams(self) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(select(Stream).order_by(Stream.channel_name.asc())).all()
            return [_serialize_stream(row) for row in rows]

    def create_stream(self, request: CreateStreamRequest) -> dict[str, Any]:
        row = Stream(
            channel_name=request.channel_name,
            youtube_url=str(request.youtube_url),
            frame_rate_fps=request.frame_rate_fps,
            status="offline",
            is_active=True,
        )
        with self.session_factory() as session, session.begin():
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ApiError(409, "stream_already_exists", "This YouTube stream is already configured.") from exc
            self._audit(session, "create", "stream", str(row.id), None, _serialize_stream(row))
        return _serialize_stream(row)

    def deactivate_stream(self, stream_id: str) -> None:
        try:
            parsed = uuid.UUID(stream_id)
        except ValueError as exc:
            raise ApiError(404, "stream_not_found", "Stream was not found.") from exc
        with self.session_factory() as session, session.begin():
            row = session.get(Stream, parsed, with_for_update=True)
            if row is None:
                raise ApiError(404, "stream_not_found", "Stream was not found.")
            before = _serialize_stream(row)
            row.is_active = False
            row.status = "disabled"
            self._audit(session, "deactivate", "stream", stream_id, before, _serialize_stream(row))

    @staticmethod
    def feed_statement(
        *, cutoff: datetime, category: str | None = None, language: str | None = None,
        stream_id: uuid.UUID | None = None, cursor: CursorValue | None = None, limit: int = 20,
    ):
        statement = (
            select(SentenceOccurrence, Stream, OccurrenceTranslation)
            .join(Stream, Stream.id == SentenceOccurrence.stream_id)
            .outerjoin(OccurrenceTranslation, OccurrenceTranslation.occurrence_id == SentenceOccurrence.id)
            .where(SentenceOccurrence.emitted_live.is_(True), SentenceOccurrence.observed_at >= cutoff)
        )
        if category:
            statement = statement.where(SentenceOccurrence.category_ids.any(category))
        if language:
            statement = statement.where(SentenceOccurrence.language == language)
        if stream_id:
            statement = statement.where(SentenceOccurrence.stream_id == stream_id)
        if cursor:
            statement = statement.where(
                tuple_(SentenceOccurrence.observed_at, SentenceOccurrence.id)
                < tuple_(cursor.timestamp, uuid.UUID(cursor.identifier))
            )
        return statement.order_by(SentenceOccurrence.observed_at.desc(), SentenceOccurrence.id.desc()).limit(limit + 1)

    def list_feed(
        self, *, category: str | None, language: str | None, stream_id: str | None,
        limit: int, cursor: str | None, window_minutes: int = DEFAULT_LIVE_WINDOW_MINUTES,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        parsed_stream = None
        if stream_id:
            try:
                parsed_stream = uuid.UUID(stream_id)
            except ValueError as exc:
                raise ApiError(400, "invalid_stream_id", "stream_id must be a UUID.") from exc
        cursor_value = self.codec.decode(cursor) if cursor else None
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, min(window_minutes, 120)))
        with self.session_factory() as session:
            rows = session.execute(
                self.feed_statement(
                    cutoff=cutoff, category=category, language=language,
                    stream_id=parsed_stream, cursor=cursor_value, limit=limit,
                )
            ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items: list[dict[str, Any]] = []
        for occurrence, stream, translation in rows:
            items.append({
                "occurrence_id": str(occurrence.id),
                "observation_id": occurrence.observation_id,
                "canonical_story_id": str(occurrence.sentence_id) if occurrence.sentence_id else None,
                "stream_id": str(occurrence.stream_id),
                "channel_name": stream.channel_name,
                "observed_at": occurrence.observed_at,
                "original_text": occurrence.source_text,
                "original_language": occurrence.language,
                "confidence": occurrence.confidence_score,
                "category_ids": list(occurrence.category_ids or []),
                "keyword_ids": list(occurrence.keyword_ids or []),
                "urgency": occurrence.urgency,
                "review_required": occurrence.review_required,
                "dedup_decision": occurrence.decision,
                "dedup_layer": occurrence.dedup_layer,
                "translation": {
                    "status": translation.status if translation else "pending",
                    "source_language": translation.source_language if translation else occurrence.language,
                    "target_language": translation.target_language if translation else ("ur" if occurrence.language == "en" else "en"),
                    "translated_text": translation.translated_text if translation else None,
                },
            })
        next_cursor = None
        if has_more and rows:
            last_occurrence = rows[-1][0]
            next_cursor = self.codec.encode(last_occurrence.observed_at, str(last_occurrence.id))
        return items, next_cursor, has_more

    def get_occurrence(self, occurrence_id: str) -> dict[str, Any] | None:
        try:
            parsed = uuid.UUID(occurrence_id)
        except ValueError:
            return None
        with self.session_factory() as session:
            row = session.execute(
                select(SentenceOccurrence, Stream, OccurrenceTranslation)
                .join(Stream, Stream.id == SentenceOccurrence.stream_id)
                .outerjoin(OccurrenceTranslation, OccurrenceTranslation.occurrence_id == SentenceOccurrence.id)
                .where(SentenceOccurrence.id == parsed)
            ).one_or_none()
            if row is None:
                return None
            occurrence, stream, translation = row
            if translation is None or translation.status != "complete" or not translation.translated_text:
                return {"share_text": None, "calendar_date": occurrence.calendar_date, "channel_name": stream.channel_name}
            if occurrence.language == "ur":
                english, urdu = translation.translated_text, occurrence.source_text
            elif occurrence.language == "en":
                english, urdu = occurrence.source_text, translation.translated_text
            else:
                return {"share_text": None, "calendar_date": occurrence.calendar_date, "channel_name": stream.channel_name}
            return {
                "share_text": build_sentence_share_text(
                    channel=stream.channel_name, observed_at=occurrence.observed_at,
                    original=occurrence.source_text, english=english, urdu=urdu,
                    app_name=os.getenv("APP_NAME", "Pakistani News Stream Intelligence"),
                ),
                "calendar_date": occurrence.calendar_date,
                "channel_name": stream.channel_name,
            }

    def get_sentence(self, sentence_id: str) -> dict[str, Any] | None:
        try:
            parsed = uuid.UUID(sentence_id)
        except ValueError:
            return None
        with self.session_factory() as session:
            sentence = session.scalar(
                select(Sentence)
                .where(Sentence.id == parsed)
                .options(
                    joinedload(Sentence.translation),
                    selectinload(Sentence.categories).joinedload(SentenceCategory.category),
                    selectinload(Sentence.keywords).joinedload(SentenceKeyword.keyword),
                    selectinload(Sentence.sources).joinedload(SentenceSource.stream),
                )
            )
            if sentence is None:
                return None
            english, urdu, translation_status = _translated_texts(
                sentence.original_text, sentence.original_language, sentence.translation
            )
            categories = [
                {"id": link.category.id, "label_en": link.category.label_en, "label_ur": link.category.label_ur, "color": link.category.color}
                for link in sentence.categories
            ]
            keywords = [
                {
                    "id": str(link.keyword.id), "category_id": link.keyword.category_id,
                    "term": link.keyword.term, "language": link.keyword.language,
                    "priority": link.keyword.priority, "matched_text": link.matched_text,
                }
                for link in sentence.keywords
            ]
            sources = [
                {
                    "stream_id": str(link.stream_id), "channel_name": link.stream.channel_name,
                    "first_seen_at": link.first_seen_at, "last_seen_at": link.last_seen_at,
                    "occurrence_count": link.occurrence_count,
                }
                for link in sentence.sources
            ]
            share_text = None
            if english and urdu and sources:
                share_text = build_sentence_share_text(
                    channel=sources[0]["channel_name"], observed_at=sentence.first_seen_at,
                    original=sentence.original_text, english=english, urdu=urdu,
                    app_name=os.getenv("APP_NAME", "Pakistani News Stream Intelligence"),
                )
            return {
                "id": str(sentence.id), "original_text": sentence.original_text,
                "original_language": sentence.original_language, "english_text": english,
                "urdu_text": urdu, "translation_status": translation_status,
                "confidence": sentence.confidence_score, "categories": categories,
                "keywords": keywords, "sources": sources, "first_seen_at": sentence.first_seen_at,
                "last_seen_at": sentence.last_seen_at, "occurrence_count": sentence.occurrence_count,
                "calendar_date": sentence.calendar_date, "review_status": sentence.review_status,
                "share_text": share_text,
            }

    @staticmethod
    def stories_statement(
        *, category: str | None = None, calendar_date: date | None = None,
        cursor: CursorValue | None = None, limit: int = 20,
    ):
        statement = select(Sentence).where(Sentence.review_status != "rejected")
        if category:
            statement = statement.join(SentenceCategory).where(SentenceCategory.category_id == category)
        if calendar_date:
            statement = statement.where(Sentence.calendar_date == calendar_date)
        if cursor:
            statement = statement.where(
                tuple_(Sentence.first_seen_at, Sentence.id)
                < tuple_(cursor.timestamp, uuid.UUID(cursor.identifier))
            )
        return (
            statement.options(joinedload(Sentence.translation), selectinload(Sentence.categories), selectinload(Sentence.sources).joinedload(SentenceSource.stream))
            .order_by(Sentence.first_seen_at.desc(), Sentence.id.desc())
            .limit(limit + 1)
        )

    def list_stories(
        self, *, category: str | None, calendar_date: date | None,
        limit: int, cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        cursor_value = self.codec.decode(cursor) if cursor else None
        with self.session_factory() as session:
            rows = list(session.scalars(self.stories_statement(category=category, calendar_date=calendar_date, cursor=cursor_value, limit=limit)).unique())
            has_more = len(rows) > limit
            rows = rows[:limit]
            items = [self._story_item(row) for row in rows]
        next_cursor = self.codec.encode(rows[-1].first_seen_at, str(rows[-1].id)) if has_more and rows else None
        return items, next_cursor, has_more

    def list_categories(self, pakistan_date: date) -> list[dict[str, Any]]:
        story_counts = (
            select(SentenceCategory.category_id, func.count(func.distinct(SentenceCategory.sentence_id)).label("count"))
            .join(Sentence, Sentence.id == SentenceCategory.sentence_id)
            .where(Sentence.calendar_date == pakistan_date, Sentence.review_status != "rejected")
            .group_by(SentenceCategory.category_id)
            .subquery()
        )
        unnested_sub = (
            select(func.unnest(SentenceOccurrence.category_ids).label("category_id"))
            .where(
                SentenceOccurrence.calendar_date == pakistan_date,
                SentenceOccurrence.emitted_live.is_(True),
            )
            .subquery()
        )
        observation_counts = (
            select(unnested_sub.c.category_id, func.count().label("count"))
            .group_by(unnested_sub.c.category_id)
            .subquery()
        )
        statement = (
            select(Category, func.coalesce(story_counts.c.count, 0), func.coalesce(observation_counts.c.count, 0))
            .outerjoin(story_counts, story_counts.c.category_id == Category.id)
            .outerjoin(observation_counts, observation_counts.c.category_id == Category.id)
            .where(Category.is_active.is_(True))
            .order_by(Category.priority.desc(), Category.label_en.asc())
        )
        with self.session_factory() as session:
            rows = session.execute(statement).all()
        return [
            {
                "id": category.id, "label_en": category.label_en, "label_ur": category.label_ur,
                "color": category.color, "is_active": category.is_active, "priority": category.priority,
                "today_story_count": int(stories), "today_observation_count": int(observations),
            }
            for category, stories, observations in rows
        ]

    def get_summary(self, category_id: str, calendar_date: date) -> dict[str, Any] | None:
        with self.session_factory() as session:
            category = session.get(Category, category_id)
            if category is None:
                return None
            summary = session.scalar(
                select(DailySummary)
                .where(DailySummary.category_id == category_id, DailySummary.calendar_date == calendar_date)
                .options(selectinload(DailySummary.facts))
            )
            category_payload = {"id": category.id, "label_en": category.label_en, "label_ur": category.label_ur, "color": category.color}
            if summary is None:
                return {
                    "id": None, "category": category_payload, "calendar_date": calendar_date,
                    "summary_text_en": "", "summary_text_ur": "", "source_sentence_count": 0,
                    "last_updated_at": None, "finalized_at": None, "facts": [],
                }
            facts = sorted((fact for fact in summary.facts if fact.fact_state == "active"), key=lambda fact: fact.fact_order)
            return {
                "id": str(summary.id), "category": category_payload, "calendar_date": summary.calendar_date,
                "summary_text_en": summary.summary_text_en, "summary_text_ur": summary.summary_text_ur,
                "source_sentence_count": summary.source_sentence_count,
                "last_updated_at": summary.last_updated_at, "finalized_at": summary.finalized_at,
                "facts": [
                    {
                        "id": str(fact.id), "order": fact.fact_order, "text_en": fact.fact_text_en,
                        "text_ur": fact.fact_text_ur, "kind": fact.fact_kind,
                        "priority": fact.priority, "source_first_seen_at": fact.source_first_seen_at,
                        "source_last_seen_at": fact.source_last_seen_at,
                    }
                    for fact in facts
                ],
            }

    def history_dates(self, category_id: str | None) -> list[date]:
        statement = select(DailySummary.calendar_date).distinct().order_by(DailySummary.calendar_date.desc())
        if category_id:
            statement = statement.where(DailySummary.category_id == category_id)
        with self.session_factory() as session:
            return list(session.scalars(statement))

    def search(
        self, *, query: str, language: str | None, category: str | None,
        date_from: date | None, date_to: date | None, limit: int, cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        normalized = normalize_match_text(query)
        if not normalized:
            raise ApiError(400, "empty_search", "Search query must contain visible text.")
        cursor_value = self.codec.decode(cursor) if cursor else None
        pattern = f"%{_escape_like(normalized)}%"
        statement = select(Sentence).outerjoin(Translation, Translation.sentence_id == Sentence.id)
        search_conditions = [
            Sentence.search_document.op("@@")(func.plainto_tsquery("simple", normalized)),
            Translation.search_document.op("@@")(func.plainto_tsquery("simple", normalized)),
        ]
        if language in {None, "en", "mixed"}:
            search_conditions.extend([func.lower(Sentence.original_text).like(pattern.lower(), escape="\\"), func.lower(Translation.english_text).like(pattern.lower(), escape="\\")])
        if language in {None, "ur", "mixed"}:
            search_conditions.extend([Sentence.normalized_text.like(pattern, escape="\\"), Translation.urdu_text.like(pattern, escape="\\")])
        statement = statement.where(or_(*search_conditions), Sentence.review_status != "rejected")
        if category:
            statement = statement.join(SentenceCategory, SentenceCategory.sentence_id == Sentence.id).where(SentenceCategory.category_id == category)
        if date_from:
            statement = statement.where(Sentence.calendar_date >= date_from)
        if date_to:
            statement = statement.where(Sentence.calendar_date <= date_to)
        if cursor_value:
            statement = statement.where(tuple_(Sentence.first_seen_at, Sentence.id) < tuple_(cursor_value.timestamp, uuid.UUID(cursor_value.identifier)))
        statement = statement.options(joinedload(Sentence.translation), selectinload(Sentence.categories), selectinload(Sentence.sources).joinedload(SentenceSource.stream)).order_by(Sentence.first_seen_at.desc(), Sentence.id.desc()).limit(limit + 1)
        with self.session_factory() as session:
            rows = list(session.scalars(statement).unique())
            has_more = len(rows) > limit
            rows = rows[:limit]
            items = [self._story_item(row) for row in rows]
        next_cursor = self.codec.encode(rows[-1].first_seen_at, str(rows[-1].id)) if has_more and rows else None
        return items, next_cursor, has_more

    def list_keywords(self, *, category: str | None, limit: int, cursor: str | None) -> tuple[list[dict[str, Any]], str | None, bool]:
        cursor_value = self.codec.decode(cursor) if cursor else None
        statement = select(Keyword)
        if category:
            statement = statement.where(Keyword.category_id == category)
        if cursor_value:
            statement = statement.where(tuple_(Keyword.created_at, Keyword.id) > tuple_(cursor_value.timestamp, uuid.UUID(cursor_value.identifier)))
        statement = statement.order_by(Keyword.created_at.asc(), Keyword.id.asc()).limit(limit + 1)
        with self.session_factory() as session:
            rows = list(session.scalars(statement))
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self._keyword_item(row) for row in rows]
        next_cursor = self.codec.encode(rows[-1].created_at, str(rows[-1].id)) if has_more and rows else None
        return items, next_cursor, has_more

    def create_keyword(self, request: CreateKeywordRequest) -> dict[str, Any]:
        normalized = normalize_match_text(request.term)
        if not normalized:
            raise ApiError(400, "invalid_keyword", "Keyword must contain letters or numbers after normalization.")
        row = Keyword(
            category_id=request.category_id, term=" ".join(request.term.split()), normalized_term=normalized,
            language=request.language, priority=request.priority, match_mode="phrase",
            requires_context=request.requires_context,
            context_terms=[normalize_match_text(value) for value in request.context_terms if normalize_match_text(value)],
            excluded_terms=[normalize_match_text(value) for value in request.excluded_terms if normalize_match_text(value)],
            added_by="admin-api",
        )
        with self.session_factory() as session, session.begin():
            if session.get(Category, request.category_id) is None:
                raise ApiError(404, "category_not_found", "Category was not found.")
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ApiError(409, "keyword_already_exists", "This exact normalized keyword already exists in the category.") from exc
            self._audit(session, "create", "keyword", str(row.id), None, self._keyword_item(row))
        return self._keyword_item(row)

    def update_keyword(self, keyword_id: str, request: UpdateKeywordRequest) -> dict[str, Any]:
        try:
            parsed = uuid.UUID(keyword_id)
        except ValueError as exc:
            raise ApiError(404, "keyword_not_found", "Keyword was not found.") from exc
        with self.session_factory() as session, session.begin():
            row = session.get(Keyword, parsed, with_for_update=True)
            if row is None:
                raise ApiError(404, "keyword_not_found", "Keyword was not found.")
            before = self._keyword_item(row)
            updates = request.model_dump(exclude_unset=True)
            if not updates:
                raise ApiError(400, "empty_update", "At least one keyword field must be supplied.")
            if "term" in updates:
                row.term = " ".join(updates.pop("term").split())
                row.normalized_term = normalize_match_text(row.term)
                if not row.normalized_term:
                    raise ApiError(400, "invalid_keyword", "Keyword must contain letters or numbers after normalization.")
            for name, value in updates.items():
                if name in {"context_terms", "excluded_terms"} and value is not None:
                    value = [normalize_match_text(item) for item in value if normalize_match_text(item)]
                setattr(row, name, value)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ApiError(409, "keyword_already_exists", "The edited normalized keyword duplicates another keyword.") from exc
            after = self._keyword_item(row)
            self._audit(session, "update", "keyword", keyword_id, before, after)
        return after

    def deactivate_keyword(self, keyword_id: str) -> None:
        try:
            parsed = uuid.UUID(keyword_id)
        except ValueError as exc:
            raise ApiError(404, "keyword_not_found", "Keyword was not found.") from exc
        with self.session_factory() as session, session.begin():
            row = session.get(Keyword, parsed, with_for_update=True)
            if row is None:
                raise ApiError(404, "keyword_not_found", "Keyword was not found.")
            before = self._keyword_item(row)
            row.is_active = False
            after = self._keyword_item(row)
            self._audit(session, "deactivate", "keyword", keyword_id, before, after)

    def test_keyword_match(self, request: AdminKeywordTestRequest) -> dict[str, Any]:
        matcher = KeywordMatcherService(
            SQLAlchemyKeywordProvider(self.session_factory),
            refresh_interval_seconds=0,
        )
        observation = matcher.match_text(
            text=request.text, language=request.language, unit_id="admin-preview",
            stream_id="admin-preview", channel_name="Admin Keyword Preview",
            observed_at=datetime.now(timezone.utc), confidence=1.0, review_required=False,
        )
        payload = observation.serializable()
        payload["diagnostic_source"] = "postgresql-active-taxonomy"
        return payload

    def create_category(self, request: CreateCategoryRequest) -> dict[str, Any]:
        row = Category(**request.model_dump(), is_active=True, context_profile={})
        with self.session_factory() as session, session.begin():
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ApiError(409, "category_already_exists", "A category with this ID already exists.") from exc
            payload = {"id": row.id, "label_en": row.label_en, "label_ur": row.label_ur, "color": row.color}
            self._audit(session, "create", "category", row.id, None, payload)
        return payload

    def overview_stats(self, pakistan_date: date) -> dict[str, Any]:
        with self.session_factory() as session:
            unique_stories = session.scalar(select(func.count()).select_from(Sentence).where(Sentence.calendar_date == pakistan_date, Sentence.review_status != "rejected")) or 0
            observations = session.scalar(select(func.count()).select_from(SentenceOccurrence).where(SentenceOccurrence.calendar_date == pakistan_date, SentenceOccurrence.emitted_live.is_(True))) or 0
            active_streams = session.scalar(select(func.count()).select_from(Stream).where(Stream.is_active.is_(True), Stream.status.in_(["live", "reconnecting"]))) or 0
            average_ocr = session.scalar(select(func.avg(RawOCRText.confidence_score)).where(func.date(RawOCRText.frame_timestamp.op("AT TIME ZONE")("Asia/Karachi")) == pakistan_date))
            average_latency = session.scalar(
                select(
                    func.avg(
                        func.extract("epoch", PipelineTrace.created_at - PipelineTrace.observed_at) * 1000
                    )
                ).where(
                    PipelineTrace.trace_type == "observation",
                    func.date(PipelineTrace.observed_at.op("AT TIME ZONE")("Asia/Karachi")) == pakistan_date,
                )
            )
            unnested_sub = (
                select(func.unnest(SentenceOccurrence.category_ids).label("category_id"))
                .where(SentenceOccurrence.calendar_date == pakistan_date)
                .subquery()
            )
            category_rows = session.execute(
                select(unnested_sub.c.category_id, func.count().label("count"))
                .group_by(unnested_sub.c.category_id)
                .order_by(func.count().desc())
            ).all()
            stream_rows = session.execute(
                select(Stream.id, Stream.channel_name, func.count(SentenceOccurrence.id))
                .outerjoin(SentenceOccurrence, and_(SentenceOccurrence.stream_id == Stream.id, SentenceOccurrence.calendar_date == pakistan_date))
                .group_by(Stream.id, Stream.channel_name).order_by(func.count(SentenceOccurrence.id).desc())
            ).all()
        return {
            "pakistan_date": pakistan_date, "unique_stories": int(unique_stories), "observations": int(observations),
            "active_streams": int(active_streams), "average_ocr_confidence": float(average_ocr) if average_ocr is not None else None,
            "average_end_to_end_latency_ms": float(average_latency) if average_latency is not None else None,
            "per_category": [{"category_id": row.category_id, "observations": int(row.count)} for row in category_rows],
            "per_stream": [{"stream_id": str(stream_id), "channel_name": name, "observations": int(count)} for stream_id, name, count in stream_rows],
        }

    def keyword_frequency(self, *, category: str | None, date_from: date | None, date_to: date | None) -> list[dict[str, Any]]:
        statement = (
            select(Sentence.calendar_date, Keyword.id, Keyword.term, Keyword.language, Keyword.category_id, func.count().label("count"))
            .join(SentenceKeyword, SentenceKeyword.sentence_id == Sentence.id)
            .join(Keyword, Keyword.id == SentenceKeyword.keyword_id)
        )
        if category:
            statement = statement.where(Keyword.category_id == category)
        if date_from:
            statement = statement.where(Sentence.calendar_date >= date_from)
        if date_to:
            statement = statement.where(Sentence.calendar_date <= date_to)
        statement = statement.group_by(Sentence.calendar_date, Keyword.id, Keyword.term, Keyword.language, Keyword.category_id).order_by(Sentence.calendar_date.asc(), func.count().desc())
        with self.session_factory() as session:
            rows = session.execute(statement).all()
        return [
            {"date": row.calendar_date, "keyword_id": str(row.id), "term": row.term, "language": row.language, "category_id": row.category_id, "count": int(row.count)}
            for row in rows
        ]

    @staticmethod
    def _story_item(row: Sentence) -> dict[str, Any]:
        english, urdu, _ = _translated_texts(row.original_text, row.original_language, row.translation)
        return {
            "id": str(row.id), "original_text": row.original_text, "original_language": row.original_language,
            "english_text": english, "urdu_text": urdu, "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at, "occurrence_count": row.occurrence_count,
            "confidence": row.confidence_score, "category_ids": [link.category_id for link in row.categories],
            "channel_names": sorted({link.stream.channel_name for link in row.sources}),
            "review_status": row.review_status,
        }

    @staticmethod
    def _keyword_item(row: Keyword) -> dict[str, Any]:
        return {
            "id": str(row.id), "category_id": row.category_id, "term": row.term,
            "normalized_term": row.normalized_term, "language": row.language,
            "is_active": row.is_active, "priority": row.priority, "match_mode": row.match_mode,
            "requires_context": row.requires_context, "context_terms": list(row.context_terms or []),
            "excluded_terms": list(row.excluded_terms or []), "added_by": row.added_by,
        }

    @staticmethod
    def _audit(session: Session, action: str, entity_type: str, entity_id: str, before: Any, after: Any) -> None:
        session.add(AdminAuditLog(action=action, entity_type=entity_type, entity_id=entity_id, before_state=_json_safe(before), after_state=_json_safe(after)))


class WebSocketHub:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, envelope: dict[str, Any]) -> int:
        async with self._lock:
            connections = list(self._connections)
        failed: list[WebSocket] = []
        delivered = 0
        for connection in connections:
            try:
                await connection.send_json(envelope)
                delivered += 1
            except Exception:
                failed.append(connection)
        if failed:
            async with self._lock:
                for connection in failed:
                    self._connections.discard(connection)
        return delivered


class OutboxWebSocketPump:
    def __init__(self, hub: WebSocketHub, dispatcher: OutboxDispatcher | None = None, poll_seconds: float = 0.25):
        self.hub = hub
        self.dispatcher = dispatcher
        self.poll_seconds = max(0.05, poll_seconds)
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def envelope(delivery: OutboxDelivery) -> dict[str, Any]:
        event_name = delivery.event_type
        if event_name == "detection_observed":
            event_name = "new_sentence"
        return WebSocketEnvelope(
            event=event_name,
            data=delivery.payload,
            event_id=delivery.event_id,
        ).model_dump(mode="json")

    async def run(self) -> None:
        if self.dispatcher is None:
            return
        while not self._stop.is_set():
            deliveries = await asyncio.to_thread(self.dispatcher.claim)
            if not deliveries:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    continue
                break
            for delivery in deliveries:
                try:
                    await self.hub.broadcast(self.envelope(delivery))
                except Exception as exc:
                    await asyncio.to_thread(self.dispatcher.mark_failed, delivery.event_id, exc)
                else:
                    await asyncio.to_thread(self.dispatcher.mark_published, delivery.event_id)


router = APIRouter(prefix=API_PREFIX)
ws_router = APIRouter()
websocket_hub = WebSocketHub()


@router.post("/admin/auth/token", response_model=AdminTokenResponse, tags=["admin"])
def admin_token(request: AdminTokenRequest) -> AdminTokenResponse:
    configured = os.getenv("ADMIN_PASSWORD", "")
    if not configured:
        raise ApiError(503, "admin_not_configured", "ADMIN_PASSWORD is not configured.")
    if not secrets.compare_digest(request.password.get_secret_value(), configured):
        raise ApiError(401, "invalid_admin_password", "The admin password is incorrect.")
    expected = _expected_admin_token()
    assert expected is not None
    return AdminTokenResponse(token=expected)


@router.get("/streams", response_model=StreamListResponse, tags=["streams"])
def list_streams(repo: ApiRepository = Depends(get_repository)) -> StreamListResponse:
    return StreamListResponse(items=repo.list_streams())


@router.post("/admin/streams", response_model=StreamResponse, status_code=201, tags=["admin", "streams"])
def add_stream(request: CreateStreamRequest, _: str = Depends(require_admin), repo: ApiRepository = Depends(get_repository)) -> StreamResponse:
    return StreamResponse.model_validate(repo.create_stream(request))


@router.delete("/admin/streams/{stream_id}", status_code=204, response_class=Response, tags=["admin", "streams"])
def remove_stream(stream_id: str, _: str = Depends(require_admin), repo: ApiRepository = Depends(get_repository)) -> Response:
    repo.deactivate_stream(stream_id)
    return Response(status_code=204)


@router.get("/feed", response_model=FeedResponse, tags=["feed"])
def feed(
    category: str | None = None,
    lang: Literal["en", "ur", "mixed", "unknown"] | None = None,
    stream_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = None,
    window_minutes: int = Query(default=DEFAULT_LIVE_WINDOW_MINUTES, ge=1, le=120),
    repo: ApiRepository = Depends(get_repository),
) -> FeedResponse:
    items, next_cursor, has_more = repo.list_feed(
        category=category, language=lang, stream_id=stream_id, limit=limit,
        cursor=cursor, window_minutes=window_minutes,
    )
    return FeedResponse(items=items, page=PageMeta(next_cursor=next_cursor, has_more=has_more, limit=limit), window_minutes=window_minutes)


@router.get("/sentence/{sentence_id}", response_model=SentenceDetail, tags=["stories"])
def sentence_detail(sentence_id: str, repo: ApiRepository = Depends(get_repository)) -> SentenceDetail:
    result = repo.get_sentence(sentence_id)
    if result is None:
        raise ApiError(404, "sentence_not_found", "Sentence was not found.")
    return SentenceDetail.model_validate(result)


@router.get("/stories", response_model=StoryListResponse, tags=["stories"])
def stories(
    category: str | None = None,
    date_value: date | None = Query(default=None, alias="date"),
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = None,
    repo: ApiRepository = Depends(get_repository),
) -> StoryListResponse:
    items, next_cursor, has_more = repo.list_stories(category=category, calendar_date=date_value, limit=limit, cursor=cursor)
    return StoryListResponse(items=items, page=PageMeta(next_cursor=next_cursor, has_more=has_more, limit=limit))


@router.get("/category/{category_id}/sentences", response_model=StoryListResponse, tags=["categories"])
def category_sentences(
    category_id: str,
    date_value: date = Query(alias="date"),
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = None,
    repo: ApiRepository = Depends(get_repository),
) -> StoryListResponse:
    items, next_cursor, has_more = repo.list_stories(category=category_id, calendar_date=date_value, limit=limit, cursor=cursor)
    return StoryListResponse(items=items, page=PageMeta(next_cursor=next_cursor, has_more=has_more, limit=limit))


@router.get("/categories", response_model=CategoryListResponse, tags=["categories"])
def categories(repo: ApiRepository = Depends(get_repository)) -> CategoryListResponse:
    current = _pakistan_today()
    return CategoryListResponse(items=repo.list_categories(current), pakistan_date=current)


@router.get("/category/{category_id}/summary", response_model=SummaryResponse, tags=["summaries"])
def category_summary(
    category_id: str,
    date_value: date = Query(alias="date"),
    repo: ApiRepository = Depends(get_repository),
) -> SummaryResponse:
    result = repo.get_summary(category_id, date_value)
    if result is None:
        raise ApiError(404, "category_not_found", "Category was not found.")
    return SummaryResponse.model_validate(result)


@router.get("/history/dates", response_model=HistoryDatesResponse, tags=["summaries"])
def history_dates(category: str | None = None, repo: ApiRepository = Depends(get_repository)) -> HistoryDatesResponse:
    return HistoryDatesResponse(dates=repo.history_dates(category))


@router.get("/search", response_model=SearchResponse, tags=["search"])
def search(
    q: str = Query(min_length=1, max_length=500),
    lang: Literal["en", "ur", "mixed"] | None = None,
    category: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = None,
    repo: ApiRepository = Depends(get_repository),
) -> SearchResponse:
    if date_from and date_to and date_from > date_to:
        raise ApiError(400, "invalid_date_range", "date_from must be before or equal to date_to.")
    items, next_cursor, has_more = repo.search(
        query=q, language=lang, category=category, date_from=date_from, date_to=date_to,
        limit=limit, cursor=cursor,
    )
    return SearchResponse(items=items, page=PageMeta(next_cursor=next_cursor, has_more=has_more, limit=limit), query=q)


@router.get("/admin/keywords", response_model=KeywordListResponse, tags=["admin", "keywords"])
def admin_keywords(
    category: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = None,
    _: str = Depends(require_admin),
    repo: ApiRepository = Depends(get_repository),
) -> KeywordListResponse:
    items, next_cursor, has_more = repo.list_keywords(category=category, limit=limit, cursor=cursor)
    return KeywordListResponse(items=items, page=PageMeta(next_cursor=next_cursor, has_more=has_more, limit=limit))


@router.post("/admin/keywords", response_model=KeywordResponse, status_code=201, tags=["admin", "keywords"])
def add_keyword(request: CreateKeywordRequest, _: str = Depends(require_admin), repo: ApiRepository = Depends(get_repository)) -> KeywordResponse:
    return KeywordResponse.model_validate(repo.create_keyword(request))


@router.patch("/admin/keywords/{keyword_id}", response_model=KeywordResponse, tags=["admin", "keywords"])
def edit_keyword(keyword_id: str, request: UpdateKeywordRequest, _: str = Depends(require_admin), repo: ApiRepository = Depends(get_repository)) -> KeywordResponse:
    return KeywordResponse.model_validate(repo.update_keyword(keyword_id, request))


@router.delete("/admin/keywords/{keyword_id}", status_code=204, response_class=Response, tags=["admin", "keywords"])
def remove_keyword(keyword_id: str, _: str = Depends(require_admin), repo: ApiRepository = Depends(get_repository)) -> Response:
    repo.deactivate_keyword(keyword_id)
    return Response(status_code=204)


@router.post("/admin/keywords/test", tags=["admin", "keywords"])
def test_admin_keyword_match(request: AdminKeywordTestRequest, _: str = Depends(require_admin), repo: ApiRepository = Depends(get_repository)) -> dict[str, Any]:
    return repo.test_keyword_match(request)


@router.post("/admin/categories", response_model=CategoryBrief, status_code=201, tags=["admin", "categories"])
def add_category(request: CreateCategoryRequest, _: str = Depends(require_admin), repo: ApiRepository = Depends(get_repository)) -> CategoryBrief:
    return CategoryBrief.model_validate(repo.create_category(request))


@router.get("/stats/overview", response_model=OverviewStats, tags=["statistics"])
def stats_overview(repo: ApiRepository = Depends(get_repository)) -> OverviewStats:
    return OverviewStats.model_validate(repo.overview_stats(_pakistan_today()))


@router.get("/stats/keyword-frequency", response_model=KeywordFrequencyResponse, tags=["statistics"])
def stats_keyword_frequency(
    category: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    repo: ApiRepository = Depends(get_repository),
) -> KeywordFrequencyResponse:
    if date_from and date_to and date_from > date_to:
        raise ApiError(400, "invalid_date_range", "date_from must be before or equal to date_to.")
    return KeywordFrequencyResponse(items=repo.keyword_frequency(category=category, date_from=date_from, date_to=date_to))


@router.get("/share/occurrence/{occurrence_id}", response_model=ShareResponse, tags=["sharing"])
def share_occurrence(occurrence_id: str, repo: ApiRepository = Depends(get_repository)) -> ShareResponse:
    item = repo.get_occurrence(occurrence_id)
    if item is None:
        raise ApiError(404, "occurrence_not_found", "Live occurrence was not found.")
    if not item.get("share_text"):
        raise ApiError(409, "translation_not_ready", "The opposite-language translation is required before sharing.")
    subject = f"News — {item['calendar_date']}"
    return ShareResponse.model_validate(share_payload(item["share_text"], subject=subject))


@router.get("/share/{sentence_id}", response_model=ShareResponse, tags=["sharing"])
def share_sentence(sentence_id: str, repo: ApiRepository = Depends(get_repository)) -> ShareResponse:
    item = repo.get_sentence(sentence_id)
    if item is None:
        raise ApiError(404, "sentence_not_found", "Sentence was not found.")
    if not item.get("share_text"):
        raise ApiError(409, "translation_not_ready", "Both English and Urdu text are required before sharing.")
    subject = f"News — {item['calendar_date']}"
    return ShareResponse.model_validate(share_payload(item["share_text"], subject=subject))


@router.get("/share/summary/{category_id}", response_model=ShareResponse, tags=["sharing"])
def share_summary(
    category_id: str,
    date_value: date = Query(alias="date"),
    repo: ApiRepository = Depends(get_repository),
) -> ShareResponse:
    item = repo.get_summary(category_id, date_value)
    if item is None:
        raise ApiError(404, "category_not_found", "Category was not found.")
    if not item["summary_text_en"] or not item["summary_text_ur"]:
        raise ApiError(409, "summary_not_ready", "The bilingual summary is not ready for this date.")
    text = build_summary_share_text(
        category_label=item["category"]["label_en"], calendar_date=date_value,
        english=item["summary_text_en"], urdu=item["summary_text_ur"],
        app_name=os.getenv("APP_NAME", "Pakistani News Stream Intelligence"),
    )
    return ShareResponse.model_validate(share_payload(text, subject=f"{item['category']['label_en']} — {date_value}"))


@ws_router.websocket("/api/v1/ws/live")
@ws_router.websocket("/ws/live")
async def websocket_live(
    websocket: WebSocket,
    repo: ApiRepository = Depends(get_repository),
) -> None:
    await websocket_hub.connect(websocket)
    try:
        await websocket.send_json(
            WebSocketEnvelope(
                event="connected",
                data={
                    "window_minutes": DEFAULT_LIVE_WINDOW_MINUTES,
                    "repeats_are_preserved": True,
                    "bootstrap_event": "snapshot",
                },
            ).model_dump(mode="json")
        )
        try:
            items, next_cursor, has_more = await asyncio.to_thread(
                repo.list_feed,
                category=None,
                language=None,
                stream_id=None,
                limit=MAX_PAGE_SIZE,
                cursor=None,
                window_minutes=DEFAULT_LIVE_WINDOW_MINUTES,
            )
            await websocket.send_json(
                WebSocketEnvelope(
                    event="snapshot",
                    data={
                        "items": items,
                        "next_cursor": next_cursor,
                        "has_more": has_more,
                        "window_minutes": DEFAULT_LIVE_WINDOW_MINUTES,
                    },
                ).model_dump(mode="json")
            )
        except Exception:
            await websocket.send_json(
                WebSocketEnvelope(
                    event="snapshot_unavailable",
                    data={"message": "Use GET /api/v1/feed to retry the live-window bootstrap."},
                ).model_dump(mode="json")
            )
        while True:
            message = await websocket.receive_text()
            if message.strip().lower() in {"ping", '{"event":"ping"}'}:
                await websocket.send_json(WebSocketEnvelope(event="pong", data={}).model_dump(mode="json"))
    except WebSocketDisconnect:
        pass
    finally:
        await websocket_hub.disconnect(websocket)
