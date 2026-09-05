"""Phase 8 durable persistence, transactional outbox leasing, and pipeline traceability.

Revision ID: 20260719_0004
Revises: 20260719_0003
Create Date: 2026-07-19
"""
from __future__ import annotations

from alembic import op

revision = "20260719_0004"
down_revision = "20260719_0003"
branch_labels = None
depends_on = None

UPGRADE_SQL = [
    "ALTER TABLE sentence_occurrences ADD COLUMN unit_id VARCHAR(160)",
    "ALTER TABLE sentence_occurrences ADD COLUMN snapshot_version VARCHAR(160)",
    "ALTER TABLE sentence_occurrences ADD COLUMN urgency VARCHAR(16) DEFAULT 'normal' NOT NULL",
    "ALTER TABLE sentence_occurrences ADD COLUMN review_required BOOLEAN DEFAULT false NOT NULL",
    "CREATE INDEX ix_sentence_occurrences_live_window ON sentence_occurrences (emitted_live, observed_at)",
    "ALTER TABLE outbox_events ADD COLUMN event_key VARCHAR(300)",
    "UPDATE outbox_events SET event_key = event_type || ':' || aggregate_type || ':' || aggregate_id || ':' || id::text WHERE event_key IS NULL",
    "ALTER TABLE outbox_events ALTER COLUMN event_key SET NOT NULL",
    "ALTER TABLE outbox_events ADD CONSTRAINT uq_outbox_event_key UNIQUE (event_key)",
    "ALTER TABLE outbox_events ADD COLUMN available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL",
    "ALTER TABLE outbox_events ADD COLUMN leased_until TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE outbox_events ADD COLUMN lease_owner VARCHAR(200)",
    "ALTER TABLE outbox_events ADD COLUMN dead_lettered_at TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE outbox_events ADD COLUMN max_attempts INTEGER DEFAULT 12 NOT NULL",
    "ALTER TABLE outbox_events ADD CONSTRAINT ck_outbox_events_attempts_valid CHECK (attempts >= 0 AND max_attempts > 0)",
    "DROP INDEX IF EXISTS ix_outbox_unpublished",
    "CREATE INDEX ix_outbox_unpublished ON outbox_events (available_at, created_at) WHERE published_at IS NULL AND dead_lettered_at IS NULL",
    "CREATE INDEX ix_outbox_lease ON outbox_events (leased_until, lease_owner)",
    """CREATE TABLE pipeline_traces (
        id UUID NOT NULL,
        trace_key VARCHAR(320) NOT NULL,
        trace_type VARCHAR(20) NOT NULL,
        status VARCHAR(20) DEFAULT 'persisted' NOT NULL,
        stream_id UUID,
        observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
        frame_sequence BIGINT,
        unit_id VARCHAR(160),
        observation_id VARCHAR(160),
        raw_ocr_id UUID,
        occurrence_id UUID,
        sentence_id UUID,
        pipeline_version VARCHAR(80) NOT NULL,
        detail JSONB DEFAULT '{}'::jsonb NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_pipeline_traces PRIMARY KEY (id),
        CONSTRAINT uq_pipeline_trace_key UNIQUE (trace_key),
        CONSTRAINT ck_pipeline_traces_trace_type_allowed CHECK (trace_type IN ('frame','unit','observation')),
        CONSTRAINT ck_pipeline_traces_status_allowed CHECK (status IN ('persisted','spooled','replayed','failed')),
        CONSTRAINT fk_pipeline_traces_stream_id_streams FOREIGN KEY(stream_id) REFERENCES streams (id) ON DELETE SET NULL,
        CONSTRAINT fk_pipeline_traces_raw_ocr_id_raw_ocr_text FOREIGN KEY(raw_ocr_id) REFERENCES raw_ocr_text (id) ON DELETE SET NULL,
        CONSTRAINT fk_pipeline_traces_occurrence_id_sentence_occurrences FOREIGN KEY(occurrence_id) REFERENCES sentence_occurrences (id) ON DELETE SET NULL,
        CONSTRAINT fk_pipeline_traces_sentence_id_sentences FOREIGN KEY(sentence_id) REFERENCES sentences (id) ON DELETE SET NULL
    )""",
    "CREATE INDEX ix_pipeline_traces_stream_observed ON pipeline_traces (stream_id, observed_at)",
    "CREATE INDEX ix_pipeline_traces_status_created ON pipeline_traces (status, created_at)",
    "CREATE INDEX ix_pipeline_traces_observation ON pipeline_traces (observation_id)",
    "CREATE UNIQUE INDEX uq_processing_jobs_active_dedup_key ON processing_jobs (dedup_key) WHERE dedup_key IS NOT NULL AND status IN ('queued','running')",
]

DOWNGRADE_SQL = [
    "DROP INDEX IF EXISTS uq_processing_jobs_active_dedup_key",
    'DROP TABLE IF EXISTS "pipeline_traces" CASCADE',
    "DROP INDEX IF EXISTS ix_outbox_lease",
    "DROP INDEX IF EXISTS ix_outbox_unpublished",
    "ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS ck_outbox_events_attempts_valid",
    "ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS uq_outbox_event_key",
    "ALTER TABLE outbox_events DROP COLUMN IF EXISTS max_attempts",
    "ALTER TABLE outbox_events DROP COLUMN IF EXISTS dead_lettered_at",
    "ALTER TABLE outbox_events DROP COLUMN IF EXISTS lease_owner",
    "ALTER TABLE outbox_events DROP COLUMN IF EXISTS leased_until",
    "ALTER TABLE outbox_events DROP COLUMN IF EXISTS available_at",
    "ALTER TABLE outbox_events DROP COLUMN IF EXISTS event_key",
    "CREATE INDEX ix_outbox_unpublished ON outbox_events (created_at) WHERE published_at IS NULL",
    "DROP INDEX IF EXISTS ix_sentence_occurrences_live_window",
    "ALTER TABLE sentence_occurrences DROP COLUMN IF EXISTS review_required",
    "ALTER TABLE sentence_occurrences DROP COLUMN IF EXISTS urgency",
    "ALTER TABLE sentence_occurrences DROP COLUMN IF EXISTS snapshot_version",
    "ALTER TABLE sentence_occurrences DROP COLUMN IF EXISTS unit_id",
]


def upgrade() -> None:
    for statement in UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_SQL:
        op.execute(statement)
