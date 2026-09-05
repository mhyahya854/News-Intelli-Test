"""Phase 7 exact occurrence translation and permanent translation memory.

Revision ID: 20260719_0003
Revises: 20260719_0002
Create Date: 2026-07-19
"""
from __future__ import annotations

from alembic import op

revision = "20260719_0003"
down_revision = "20260719_0002"
branch_labels = None
depends_on = None

UPGRADE_SQL = [
    """CREATE TABLE occurrence_translations (
        id UUID NOT NULL,
        occurrence_id UUID NOT NULL,
        source_language VARCHAR(2) NOT NULL,
        target_language VARCHAR(2) NOT NULL,
        source_text TEXT NOT NULL,
        translated_text TEXT,
        status VARCHAR(20) DEFAULT 'pending' NOT NULL,
        model_name VARCHAR(300) NOT NULL,
        model_revision VARCHAR(160) NOT NULL,
        engine VARCHAR(120) NOT NULL,
        quality_checks JSONB DEFAULT '{}'::jsonb NOT NULL,
        latency_ms FLOAT,
        cache_hit BOOLEAN DEFAULT false NOT NULL,
        failure_reason TEXT,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_occurrence_translations PRIMARY KEY (id),
        CONSTRAINT uq_occurrence_translation_occurrence UNIQUE (occurrence_id),
        CONSTRAINT ck_occurrence_translations_status_allowed CHECK (status IN ('pending','complete','needs_review','failed')),
        CONSTRAINT ck_occurrence_translations_source_language_allowed CHECK (source_language IN ('en','ur')),
        CONSTRAINT ck_occurrence_translations_target_language_allowed CHECK (target_language IN ('en','ur')),
        CONSTRAINT ck_occurrence_translations_opposite_language_only CHECK (source_language <> target_language),
        CONSTRAINT ck_occurrence_translations_latency_nonnegative CHECK (latency_ms IS NULL OR latency_ms >= 0),
        CONSTRAINT fk_occurrence_translations_occurrence_id_sentence_occurrences FOREIGN KEY(occurrence_id) REFERENCES sentence_occurrences (id) ON DELETE CASCADE
    )""",
    "CREATE INDEX ix_occurrence_translations_status_created ON occurrence_translations (status, created_at)",
    """CREATE TABLE translation_memory (
        id UUID NOT NULL,
        source_hash VARCHAR(64) NOT NULL,
        source_language VARCHAR(2) NOT NULL,
        target_language VARCHAR(2) NOT NULL,
        source_text TEXT NOT NULL,
        translated_text TEXT NOT NULL,
        model_name VARCHAR(300) NOT NULL,
        model_revision VARCHAR(160) NOT NULL,
        engine VARCHAR(120) NOT NULL,
        quality_checks JSONB DEFAULT '{}'::jsonb NOT NULL,
        hit_count BIGINT DEFAULT 0 NOT NULL,
        last_used_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_translation_memory PRIMARY KEY (id),
        CONSTRAINT uq_translation_memory_identity UNIQUE (source_hash, source_language, target_language, model_revision),
        CONSTRAINT ck_translation_memory_source_language_allowed CHECK (source_language IN ('en','ur')),
        CONSTRAINT ck_translation_memory_target_language_allowed CHECK (target_language IN ('en','ur')),
        CONSTRAINT ck_translation_memory_opposite_language_only CHECK (source_language <> target_language)
    )""",
    "CREATE INDEX ix_translation_memory_source_hash ON translation_memory (source_hash)",
    "CREATE INDEX ix_translation_memory_last_used ON translation_memory (last_used_at)",
]

DOWNGRADE_SQL = [
    'DROP TABLE IF EXISTS "translation_memory" CASCADE',
    'DROP TABLE IF EXISTS "occurrence_translations" CASCADE',
]


def upgrade() -> None:
    for statement in UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_SQL:
        op.execute(statement)
