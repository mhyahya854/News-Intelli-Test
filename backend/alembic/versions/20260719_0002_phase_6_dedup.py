"""Phase 6 occurrence ledger and deduplication review gate.

Revision ID: 20260719_0002
Revises: 20260719_0001
Create Date: 2026-07-19
"""
from __future__ import annotations

from alembic import op

revision = "20260719_0002"
down_revision = "20260719_0001"
branch_labels = None
depends_on = None

UPGRADE_SQL = [
    """CREATE TABLE sentence_occurrences (
        id UUID NOT NULL,
        observation_id VARCHAR(160) NOT NULL,
        sentence_id UUID,
        stream_id UUID NOT NULL,
        raw_ocr_id UUID,
        observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
        calendar_date DATE NOT NULL,
        source_text TEXT NOT NULL,
        normalized_text TEXT NOT NULL,
        language VARCHAR(12) NOT NULL,
        confidence_score FLOAT NOT NULL,
        category_ids TEXT[] DEFAULT ARRAY[]::text[] NOT NULL,
        keyword_ids TEXT[] DEFAULT ARRAY[]::text[] NOT NULL,
        exact_hash VARCHAR(64) NOT NULL,
        decision VARCHAR(24) NOT NULL,
        dedup_layer VARCHAR(40) NOT NULL,
        semantic_similarity FLOAT,
        lexical_overlap FLOAT,
        decision_metadata JSONB DEFAULT '{}'::jsonb NOT NULL,
        emitted_live BOOLEAN DEFAULT true NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_sentence_occurrences PRIMARY KEY (id),
        CONSTRAINT uq_sentence_occurrence_observation UNIQUE (observation_id),
        CONSTRAINT ck_sentence_occurrences_confidence_range CHECK (confidence_score BETWEEN 0 AND 1),
        CONSTRAINT ck_sentence_occurrences_decision_allowed CHECK (decision IN ('created','merged','pending_review','resolved_merged','resolved_separate')),
        CONSTRAINT fk_sentence_occurrences_sentence_id_sentences FOREIGN KEY(sentence_id) REFERENCES sentences (id) ON DELETE SET NULL,
        CONSTRAINT fk_sentence_occurrences_stream_id_streams FOREIGN KEY(stream_id) REFERENCES streams (id) ON DELETE RESTRICT,
        CONSTRAINT fk_sentence_occurrences_raw_ocr_id_raw_ocr_text FOREIGN KEY(raw_ocr_id) REFERENCES raw_ocr_text (id) ON DELETE SET NULL
    )""",
    "CREATE INDEX ix_sentence_occurrences_story_observed ON sentence_occurrences (sentence_id, observed_at)",
    "CREATE INDEX ix_sentence_occurrences_stream_observed ON sentence_occurrences (stream_id, observed_at)",
    "CREATE INDEX ix_sentence_occurrences_date_decision ON sentence_occurrences (calendar_date, decision)",
    """CREATE TABLE dedup_review_cases (
        id UUID NOT NULL,
        observation_id VARCHAR(160) NOT NULL,
        occurrence_id UUID,
        candidate_sentence_id UUID NOT NULL,
        resolved_sentence_id UUID,
        calendar_date DATE NOT NULL,
        semantic_similarity FLOAT NOT NULL,
        lexical_overlap FLOAT NOT NULL,
        evidence JSONB NOT NULL,
        status VARCHAR(16) DEFAULT 'pending' NOT NULL,
        resolved_at TIMESTAMP WITH TIME ZONE,
        resolved_by VARCHAR(120),
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_dedup_review_cases PRIMARY KEY (id),
        CONSTRAINT uq_dedup_review_observation UNIQUE (observation_id),
        CONSTRAINT ck_dedup_review_cases_status_allowed CHECK (status IN ('pending','merged','separate')),
        CONSTRAINT ck_dedup_review_cases_semantic_range CHECK (semantic_similarity BETWEEN -1 AND 1),
        CONSTRAINT ck_dedup_review_cases_lexical_range CHECK (lexical_overlap BETWEEN 0 AND 1),
        CONSTRAINT fk_dedup_review_cases_occurrence_id_sentence_occurrences FOREIGN KEY(occurrence_id) REFERENCES sentence_occurrences (id) ON DELETE SET NULL,
        CONSTRAINT fk_dedup_review_cases_candidate_sentence_id_sentences FOREIGN KEY(candidate_sentence_id) REFERENCES sentences (id) ON DELETE CASCADE,
        CONSTRAINT fk_dedup_review_cases_resolved_sentence_id_sentences FOREIGN KEY(resolved_sentence_id) REFERENCES sentences (id) ON DELETE SET NULL
    )""",
    "CREATE INDEX ix_dedup_review_pending_created ON dedup_review_cases (status, created_at)",
    "CREATE INDEX ix_dedup_review_candidate ON dedup_review_cases (candidate_sentence_id, calendar_date)",
]

DOWNGRADE_SQL = [
    'DROP TABLE IF EXISTS "dedup_review_cases" CASCADE',
    'DROP TABLE IF EXISTS "sentence_occurrences" CASCADE',
]


def upgrade() -> None:
    for statement in UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_SQL:
        op.execute(statement)
