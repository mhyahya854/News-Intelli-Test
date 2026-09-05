from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    REAL,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Stream(Base, TimestampMixin):
    __tablename__ = "streams"
    __table_args__ = (
        CheckConstraint(
            "status IN ('live','reconnecting','offline','error','disabled')",
            name="status_allowed",
        ),
        CheckConstraint("frame_rate_fps > 0 AND frame_rate_fps <= 10", name="fps_range"),
        UniqueConstraint("youtube_url", name="uq_streams_youtube_url"),
        Index("ix_streams_active_status", "is_active", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    channel_name: Mapped[str] = mapped_column(String(160), nullable=False)
    youtube_url: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="offline")
    frame_rate_fps: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.5")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_frame_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconnect_count_today: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    raw_ocr_records: Mapped[list[RawOCRText]] = relationship(back_populates="stream")
    source_records: Mapped[list[SentenceSource]] = relationship(back_populates="stream")


class Category(Base, TimestampMixin):
    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint("priority BETWEEN 0 AND 100", name="priority_range"),
        CheckConstraint("color ~ '^#[0-9A-Fa-f]{6}$'", name="hex_color"),
        Index("ix_categories_active_priority", "is_active", "priority"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label_en: Mapped[str] = mapped_column(String(160), nullable=False)
    label_ur: Mapped[str] = mapped_column(String(160), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="50")
    description_en: Mapped[str | None] = mapped_column(Text)
    description_ur: Mapped[str | None] = mapped_column(Text)
    context_profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    keywords: Mapped[list[Keyword]] = relationship(back_populates="category")
    sentence_links: Mapped[list[SentenceCategory]] = relationship(back_populates="category")
    summaries: Mapped[list[DailySummary]] = relationship(back_populates="category")


class Keyword(Base, TimestampMixin):
    __tablename__ = "keywords"
    __table_args__ = (
        CheckConstraint("language IN ('en','ur')", name="language_allowed"),
        CheckConstraint("priority IN ('normal','high','critical')", name="priority_allowed"),
        CheckConstraint(
            "match_mode IN ('exact','phrase')", name="match_mode_allowed"
        ),
        UniqueConstraint(
            "category_id", "language", "normalized_term", name="uq_keyword_normalized"
        ),
        Index("ix_keywords_category_active_language", "category_id", "is_active", "language"),
        Index("ix_keywords_normalized_term", "normalized_term"),
        Index("ix_keywords_admin_cursor", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    term: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_term: Mapped[str] = mapped_column(String(300), nullable=False)
    language: Mapped[str] = mapped_column(String(2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    priority: Mapped[str] = mapped_column(String(16), nullable=False, server_default="normal")
    match_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="phrase")
    requires_context: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    context_terms: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")
    )
    excluded_terms: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")
    )
    added_by: Mapped[str] = mapped_column(String(120), nullable=False, server_default="system")

    category: Mapped[Category] = relationship(back_populates="keywords")
    sentence_links: Mapped[list[SentenceKeyword]] = relationship(back_populates="keyword")


class ModelRegistry(Base, TimestampMixin):
    __tablename__ = "model_registry"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('ocr_detection','ocr_recognition','translation','embedding','summarization','language_detection','classification')",
            name="purpose_allowed",
        ),
        UniqueConstraint("purpose", "model_name", "revision", name="uq_model_revision"),
        Index("ix_model_registry_active_purpose", "is_active", "purpose"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    model_name: Mapped[str] = mapped_column(String(300), nullable=False)
    revision: Mapped[str] = mapped_column(String(160), nullable=False)
    runtime: Mapped[str] = mapped_column(String(80), nullable=False)
    license_name: Mapped[str] = mapped_column(String(80), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    benchmark: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class RawOCRText(Base):
    __tablename__ = "raw_ocr_text"
    __table_args__ = (
        CheckConstraint("confidence_score BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint("frame_width > 0 AND frame_height > 0", name="frame_dimensions"),
        Index("ix_raw_ocr_stream_frame_timestamp", "stream_id", "frame_timestamp"),
        Index("ix_raw_ocr_expires_at", "expires_at"),
        Index("ix_raw_ocr_frame_hash", "stream_id", "frame_sha256"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    stream_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("streams.id", ondelete="CASCADE"), nullable=False
    )
    frame_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    ocr_engine: Mapped[str] = mapped_column(String(80), nullable=False)
    ocr_model: Mapped[str] = mapped_column(String(300), nullable=False)
    frame_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    perceptual_hash: Mapped[str | None] = mapped_column(String(64))
    frame_width: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_height: Mapped[int] = mapped_column(Integer, nullable=False)
    regions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    processing_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    stream: Mapped[Stream] = relationship(back_populates="raw_ocr_records")


class Sentence(Base, TimestampMixin):
    __tablename__ = "sentences"
    __table_args__ = (
        CheckConstraint("original_language IN ('en','ur','mixed','unknown')", name="language_allowed"),
        CheckConstraint("confidence_score BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint(
            "review_status IN ('accepted','needs_review','rejected')", name="review_status_allowed"
        ),
        UniqueConstraint("calendar_date", "exact_hash", name="uq_sentence_day_exact_hash"),
        Index("ix_sentences_calendar_date_created", "calendar_date", "created_at"),
        Index("ix_sentences_first_seen", "first_seen_at"),
        Index("ix_sentences_api_cursor", "first_seen_at", "id"),
        Index("ix_sentences_story_fingerprint", "calendar_date", "story_fingerprint"),
        Index(
            "ix_sentences_search_document",
            "search_document",
            postgresql_using="gin",
        ),
        Index(
            "ix_sentences_normalized_trgm",
            "normalized_text",
            postgresql_using="gin",
            postgresql_ops={"normalized_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    original_language: Mapped[str] = mapped_column(String(12), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    exact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    story_fingerprint: Mapped[str | None] = mapped_column(String(128))
    calendar_date: Mapped[date] = mapped_column(Date, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    dedup_similarity_score: Mapped[float | None] = mapped_column(Float)
    dedup_decision: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    extracted_entities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="accepted"
    )
    search_document: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', coalesce(original_text, ''))", persisted=True),
    )

    categories: Mapped[list[SentenceCategory]] = relationship(
        back_populates="sentence", cascade="all, delete-orphan"
    )
    keywords: Mapped[list[SentenceKeyword]] = relationship(
        back_populates="sentence", cascade="all, delete-orphan"
    )
    sources: Mapped[list[SentenceSource]] = relationship(
        back_populates="sentence", cascade="all, delete-orphan"
    )
    translation: Mapped[Translation | None] = relationship(
        back_populates="sentence", cascade="all, delete-orphan", uselist=False
    )
    embeddings: Mapped[list[SentenceEmbedding]] = relationship(
        back_populates="sentence", cascade="all, delete-orphan"
    )


class SentenceCategory(Base):
    __tablename__ = "sentence_categories"
    __table_args__ = (
        CheckConstraint("classification_score BETWEEN 0 AND 1", name="score_range"),
        Index("ix_sentence_categories_category_sentence", "category_id", "sentence_id"),
    )

    sentence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sentences.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), primary_key=True
    )
    classification_score: Mapped[float] = mapped_column(Float, nullable=False)
    classifier_version: Mapped[str] = mapped_column(String(200), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    sentence: Mapped[Sentence] = relationship(back_populates="categories")
    category: Mapped[Category] = relationship(back_populates="sentence_links")


class SentenceKeyword(Base):
    __tablename__ = "sentence_keywords"
    __table_args__ = (
        CheckConstraint("match_score BETWEEN 0 AND 1", name="score_range"),
        CheckConstraint(
            "match_type IN ('exact','normalized','contextual')",
            name="match_type_allowed",
        ),
        Index("ix_sentence_keywords_keyword_sentence", "keyword_id", "sentence_id"),
    )

    sentence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sentences.id", ondelete="CASCADE"), primary_key=True
    )
    keyword_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("keywords.id", ondelete="RESTRICT"), primary_key=True
    )
    match_type: Mapped[str] = mapped_column(String(20), nullable=False)
    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    matched_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_span: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    sentence: Mapped[Sentence] = relationship(back_populates="keywords")
    keyword: Mapped[Keyword] = relationship(back_populates="sentence_links")


class SentenceSource(Base):
    __tablename__ = "sentence_sources"
    __table_args__ = (
        UniqueConstraint("sentence_id", "stream_id", name="uq_sentence_source"),
        Index("ix_sentence_sources_stream_first_seen", "stream_id", "first_seen_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    sentence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sentences.id", ondelete="CASCADE"), nullable=False
    )
    stream_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("streams.id", ondelete="RESTRICT"), nullable=False
    )
    raw_ocr_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("raw_ocr_text.id", ondelete="SET NULL")
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    source_text: Mapped[str] = mapped_column(Text, nullable=False)

    sentence: Mapped[Sentence] = relationship(back_populates="sources")
    stream: Mapped[Stream] = relationship(back_populates="source_records")


class Translation(Base, TimestampMixin):
    __tablename__ = "translations"
    __table_args__ = (
        CheckConstraint(
            "translation_status IN ('complete','partial','failed','not_required')",
            name="status_allowed",
        ),
        CheckConstraint("quality_score IS NULL OR (quality_score BETWEEN 0 AND 1)", name="quality_range"),
        Index("ix_translations_search_document", "search_document", postgresql_using="gin"),
        Index(
            "ix_translations_english_trgm",
            "english_text",
            postgresql_using="gin",
            postgresql_ops={"english_text": "gin_trgm_ops"},
        ),
        Index(
            "ix_translations_urdu_trgm",
            "urdu_text",
            postgresql_using="gin",
            postgresql_ops={"urdu_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    sentence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sentences.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    english_text: Mapped[str | None] = mapped_column(Text)
    urdu_text: Mapped[str | None] = mapped_column(Text)
    translation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_language: Mapped[str] = mapped_column(String(12), nullable=False)
    model_en: Mapped[str | None] = mapped_column(String(300))
    model_ur: Mapped[str | None] = mapped_column(String(300))
    model_revision_en: Mapped[str | None] = mapped_column(String(160))
    model_revision_ur: Mapped[str | None] = mapped_column(String(160))
    quality_score: Mapped[float | None] = mapped_column(Float)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    search_document: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(english_text, '') || ' ' || coalesce(urdu_text, ''))",
            persisted=True,
        ),
    )

    sentence: Mapped[Sentence] = relationship(back_populates="translation")


class SentenceEmbedding(Base):
    __tablename__ = "sentence_embeddings"
    __table_args__ = (
        UniqueConstraint("sentence_id", "model_name", "model_revision", name="uq_sentence_embedding_model"),
        CheckConstraint("dimensions > 0", name="dimensions_positive"),
        Index("ix_sentence_embeddings_model", "model_name", "model_revision"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    sentence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sentences.id", ondelete="CASCADE"), nullable=False
    )
    model_name: Mapped[str] = mapped_column(String(300), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(160), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_vector: Mapped[list[float]] = mapped_column(ARRAY(REAL), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    sentence: Mapped[Sentence] = relationship(back_populates="embeddings")


class DailySummary(Base, TimestampMixin):
    __tablename__ = "daily_summaries"
    __table_args__ = (
        UniqueConstraint("category_id", "calendar_date", name="uq_daily_summary_category_date"),
        Index("ix_daily_summaries_date_category", "calendar_date", "category_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    calendar_date: Mapped[date] = mapped_column(Date, nullable=False)
    summary_text_en: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    summary_text_ur: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    source_sentence_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    summarizer_model: Mapped[str | None] = mapped_column(String(300))
    summarizer_revision: Mapped[str | None] = mapped_column(String(160))
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    category: Mapped[Category] = relationship(back_populates="summaries")
    sentence_links: Mapped[list[DailySummarySentence]] = relationship(
        back_populates="summary", cascade="all, delete-orphan"
    )
    facts: Mapped[list[SummaryFact]] = relationship(
        back_populates="summary", cascade="all, delete-orphan"
    )
    runs: Mapped[list[SummaryRun]] = relationship(
        back_populates="summary", cascade="all, delete-orphan"
    )
    review_cases: Mapped[list[SummaryReviewCase]] = relationship(
        back_populates="summary", cascade="all, delete-orphan"
    )


class DailySummarySentence(Base):
    __tablename__ = "daily_summary_sentences"
    __table_args__ = (Index("ix_daily_summary_sentences_sentence", "sentence_id"),)

    daily_summary_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("daily_summaries.id", ondelete="CASCADE"), primary_key=True
    )
    sentence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sentences.id", ondelete="RESTRICT"), primary_key=True
    )
    folded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    summary: Mapped[DailySummary] = relationship(back_populates="sentence_links")


class SummaryFact(Base):
    __tablename__ = "summary_facts"
    __table_args__ = (
        UniqueConstraint("daily_summary_id", "fact_hash", name="uq_summary_fact_hash"),
        UniqueConstraint("daily_summary_id", "fact_order", name="uq_summary_fact_order"),
        CheckConstraint("dimensions > 0", name="dimensions_positive"),
        CheckConstraint("priority BETWEEN 0 AND 100", name="priority_range"),
        CheckConstraint("fact_state IN ('active','superseded','withheld')", name="state_allowed"),
        CheckConstraint("fact_kind IN ('story','update')", name="kind_allowed"),
        Index("ix_summary_facts_summary_created", "daily_summary_id", "created_at"),
        Index("ix_summary_facts_summary_state_order", "daily_summary_id", "fact_state", "fact_order"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    daily_summary_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("daily_summaries.id", ondelete="CASCADE"), nullable=False
    )
    fact_text_en: Mapped[str] = mapped_column(Text, nullable=False)
    fact_text_ur: Mapped[str] = mapped_column(Text, nullable=False)
    fact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_order: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    fact_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="story")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="50")
    source_first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    factual_signature: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    model_name: Mapped[str] = mapped_column(String(300), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(160), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_vector: Mapped[list[float]] = mapped_column(ARRAY(REAL), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    summary: Mapped[DailySummary] = relationship(back_populates="facts")
    sources: Mapped[list[SummaryFactSource]] = relationship(
        back_populates="fact", cascade="all, delete-orphan"
    )


class SummaryFactSource(Base):
    __tablename__ = "summary_fact_sources"

    summary_fact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("summary_facts.id", ondelete="CASCADE"), primary_key=True
    )
    sentence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sentences.id", ondelete="RESTRICT"), primary_key=True
    )

    fact: Mapped[SummaryFact] = relationship(back_populates="sources")


class SummaryRun(Base):
    """Auditable execution record for one category/day additive summary cycle."""

    __tablename__ = "summary_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running','succeeded','failed')", name="status_allowed"),
        UniqueConstraint("run_key", name="uq_summary_run_key"),
        Index("ix_summary_runs_category_date_started", "category_id", "calendar_date", "started_at"),
        Index("ix_summary_runs_status_started", "status", "started_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    daily_summary_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("daily_summaries.id", ondelete="SET NULL")
    )
    category_id: Mapped[str] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    calendar_date: Mapped[date] = mapped_column(Date, nullable=False)
    run_key: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stories_considered: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    facts_appended: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duplicate_facts_skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pending_translation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    diagnostics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    summary: Mapped[DailySummary | None] = relationship(back_populates="runs")


class SummaryReviewCase(Base, TimestampMixin):
    """Potential summary duplicate withheld when factual safeguards disagree."""

    __tablename__ = "summary_review_cases"
    __table_args__ = (
        CheckConstraint("status IN ('pending','append','duplicate')", name="status_allowed"),
        CheckConstraint("semantic_similarity BETWEEN -1 AND 1", name="semantic_range"),
        UniqueConstraint("daily_summary_id", "sentence_id", name="uq_summary_review_sentence"),
        Index("ix_summary_review_pending_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    daily_summary_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("daily_summaries.id", ondelete="CASCADE"), nullable=False
    )
    sentence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sentences.id", ondelete="CASCADE"), nullable=False
    )
    candidate_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("summary_facts.id", ondelete="SET NULL")
    )
    semantic_similarity: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(120))

    summary: Mapped[DailySummary] = relationship(back_populates="review_cases")


class AdminUser(Base, TimestampMixin):
    __tablename__ = "admin_users"
    __table_args__ = (UniqueConstraint("username", name="uq_admin_username"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertLog(Base):
    __tablename__ = "alerts_log"
    __table_args__ = (
        CheckConstraint("severity IN ('normal','high','critical')", name="severity_allowed"),
        Index("ix_alerts_unacknowledged", "acknowledged", "triggered_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    sentence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sentences.id", ondelete="CASCADE"), nullable=False
    )
    keyword_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("keywords.id", ondelete="SET NULL")
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )


class StreamHealthEvent(Base):
    __tablename__ = "stream_health_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('live','reconnecting','offline','error','recovered')",
            name="status_allowed",
        ),
        Index("ix_stream_health_stream_occurred", "stream_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    stream_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("streams.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    average_ocr_confidence: Mapped[float | None] = mapped_column(Float)
    reconnect_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class ProcessingJob(Base, TimestampMixin):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','dead_letter','cancelled')",
            name="status_allowed",
        ),
        CheckConstraint("priority BETWEEN 0 AND 100", name="priority_range"),
        CheckConstraint("attempts >= 0 AND max_attempts > 0", name="attempts_valid"),
        Index(
            "ix_processing_jobs_poll",
            "status",
            "available_at",
            "priority",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_processing_jobs_lease", "status", "leased_until"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    job_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="50")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    dedup_key: Mapped[str | None] = mapped_column(String(300))
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduledJob(Base, TimestampMixin):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (
        CheckConstraint("interval_seconds > 0", name="interval_positive"),
        UniqueConstraint("name", name="uq_scheduled_job_name"),
        Index("ix_scheduled_jobs_due", "is_enabled", "next_run_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    job_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("attempts >= 0 AND max_attempts > 0", name="attempts_valid"),
        UniqueConstraint("event_key", name="uq_outbox_event_key"),
        Index(
            "ix_outbox_unpublished",
            "available_at",
            "created_at",
            postgresql_where=text("published_at IS NULL AND dead_lettered_at IS NULL"),
        ),
        Index("ix_outbox_lease", "leased_until", "lease_owner"),
        Index("ix_outbox_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    event_key: Mapped[str] = mapped_column(String(300), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="12")
    last_error: Mapped[str | None] = mapped_column(Text)


class SystemSetting(Base, TimestampMixin):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False, server_default="system")


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"
    __table_args__ = (Index("ix_admin_audit_created", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(160))
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SentenceOccurrence(Base):
    """Every accepted broadcast appearance, including repeats of one canonical story."""

    __tablename__ = "sentence_occurrences"
    __table_args__ = (
        CheckConstraint("confidence_score BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint(
            "decision IN ('created','merged','pending_review','resolved_merged','resolved_separate')",
            name="decision_allowed",
        ),
        UniqueConstraint("observation_id", name="uq_sentence_occurrence_observation"),
        Index("ix_sentence_occurrences_story_observed", "sentence_id", "observed_at"),
        Index("ix_sentence_occurrences_stream_observed", "stream_id", "observed_at"),
        Index("ix_sentence_occurrences_date_decision", "calendar_date", "decision"),
        Index("ix_sentence_occurrences_live_window", "emitted_live", "observed_at"),
        Index("ix_sentence_occurrences_api_cursor", "observed_at", "id", postgresql_where=text("emitted_live = true")),
        Index("ix_sentence_occurrences_categories_gin", "category_ids", postgresql_using="gin"),
        Index("ix_sentence_occurrences_keywords_gin", "keyword_ids", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    observation_id: Mapped[str] = mapped_column(String(160), nullable=False)
    unit_id: Mapped[str | None] = mapped_column(String(160))
    snapshot_version: Mapped[str | None] = mapped_column(String(160))
    urgency: Mapped[str] = mapped_column(String(16), nullable=False, server_default="normal")
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    sentence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sentences.id", ondelete="SET NULL")
    )
    stream_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("streams.id", ondelete="RESTRICT"), nullable=False
    )
    raw_ocr_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("raw_ocr_text.id", ondelete="SET NULL")
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    calendar_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(12), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    category_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")
    )
    keyword_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")
    )
    exact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    dedup_layer: Mapped[str] = mapped_column(String(40), nullable=False)
    semantic_similarity: Mapped[float | None] = mapped_column(Float)
    lexical_overlap: Mapped[float | None] = mapped_column(Float)
    decision_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    emitted_live: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OccurrenceTranslation(Base, TimestampMixin):
    """Translation of the exact live observation wording, not only the canonical story."""

    __tablename__ = "occurrence_translations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','complete','needs_review','failed')",
            name="status_allowed",
        ),
        CheckConstraint("source_language IN ('en','ur')", name="source_language_allowed"),
        CheckConstraint("target_language IN ('en','ur')", name="target_language_allowed"),
        CheckConstraint("source_language <> target_language", name="opposite_language_only"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_nonnegative"),
        UniqueConstraint("occurrence_id", name="uq_occurrence_translation_occurrence"),
        Index("ix_occurrence_translations_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sentence_occurrences.id", ondelete="CASCADE"), nullable=False
    )
    source_language: Mapped[str] = mapped_column(String(2), nullable=False)
    target_language: Mapped[str] = mapped_column(String(2), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    translated_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    model_name: Mapped[str] = mapped_column(String(300), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(160), nullable=False)
    engine: Mapped[str] = mapped_column(String(120), nullable=False)
    quality_checks: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    latency_ms: Mapped[float | None] = mapped_column(Float)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class TranslationMemory(Base, TimestampMixin):
    """Permanent exact-text translation memory for immediate repeat delivery."""

    __tablename__ = "translation_memory"
    __table_args__ = (
        CheckConstraint("source_language IN ('en','ur')", name="source_language_allowed"),
        CheckConstraint("target_language IN ('en','ur')", name="target_language_allowed"),
        CheckConstraint("source_language <> target_language", name="opposite_language_only"),
        UniqueConstraint(
            "source_hash", "source_language", "target_language", "model_revision",
            name="uq_translation_memory_identity",
        ),
        Index("ix_translation_memory_source_hash", "source_hash"),
        Index("ix_translation_memory_last_used", "last_used_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_language: Mapped[str] = mapped_column(String(2), nullable=False)
    target_language: Mapped[str] = mapped_column(String(2), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(300), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(160), nullable=False)
    engine: Mapped[str] = mapped_column(String(120), nullable=False)
    quality_checks: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    hit_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DedupReviewCase(Base, TimestampMixin):
    """Ambiguous semantic candidates withheld from summaries until resolved."""

    __tablename__ = "dedup_review_cases"
    __table_args__ = (
        CheckConstraint("status IN ('pending','merged','separate')", name="status_allowed"),
        CheckConstraint("semantic_similarity BETWEEN -1 AND 1", name="semantic_range"),
        CheckConstraint("lexical_overlap BETWEEN 0 AND 1", name="lexical_range"),
        UniqueConstraint("observation_id", name="uq_dedup_review_observation"),
        Index("ix_dedup_review_pending_created", "status", "created_at"),
        Index("ix_dedup_review_candidate", "candidate_sentence_id", "calendar_date"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    observation_id: Mapped[str] = mapped_column(String(160), nullable=False)
    occurrence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sentence_occurrences.id", ondelete="SET NULL")
    )
    candidate_sentence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sentences.id", ondelete="CASCADE"), nullable=False
    )
    resolved_sentence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sentences.id", ondelete="SET NULL")
    )
    calendar_date: Mapped[date] = mapped_column(Date, nullable=False)
    semantic_similarity: Mapped[float] = mapped_column(Float, nullable=False)
    lexical_overlap: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(120))


class PipelineTrace(Base, TimestampMixin):
    """End-to-end audit record for frames, segmented units, and observations."""

    __tablename__ = "pipeline_traces"
    __table_args__ = (
        CheckConstraint("trace_type IN ('frame','unit','observation')", name="trace_type_allowed"),
        CheckConstraint("status IN ('persisted','spooled','replayed','failed')", name="status_allowed"),
        UniqueConstraint("trace_key", name="uq_pipeline_trace_key"),
        Index("ix_pipeline_traces_stream_observed", "stream_id", "observed_at"),
        Index("ix_pipeline_traces_status_created", "status", "created_at"),
        Index("ix_pipeline_traces_observation", "observation_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    trace_key: Mapped[str] = mapped_column(String(320), nullable=False)
    trace_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="persisted")
    stream_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("streams.id", ondelete="SET NULL")
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frame_sequence: Mapped[int | None] = mapped_column(BigInteger)
    unit_id: Mapped[str | None] = mapped_column(String(160))
    observation_id: Mapped[str | None] = mapped_column(String(160))
    raw_ocr_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("raw_ocr_text.id", ondelete="SET NULL")
    )
    occurrence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sentence_occurrences.id", ondelete="SET NULL")
    )
    sentence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sentences.id", ondelete="SET NULL")
    )
    pipeline_version: Mapped[str] = mapped_column(String(80), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
