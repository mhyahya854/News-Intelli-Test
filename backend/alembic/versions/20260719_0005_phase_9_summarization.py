"""Phase 9 additive bilingual summary fact ledger and run audit.

Revision ID: 20260719_0005
Revises: 20260719_0004
Create Date: 2026-07-19
"""
from __future__ import annotations

from alembic import op

revision = "20260719_0005"
down_revision = "20260719_0004"
branch_labels = None
depends_on = None

UPGRADE_SQL = [
    "ALTER TABLE summary_facts ADD COLUMN fact_order INTEGER",
    "WITH ranked AS (SELECT id, row_number() OVER (PARTITION BY daily_summary_id ORDER BY created_at, id) AS n FROM summary_facts) UPDATE summary_facts SET fact_order = ranked.n FROM ranked WHERE summary_facts.id = ranked.id",
    "ALTER TABLE summary_facts ALTER COLUMN fact_order SET NOT NULL",
    "ALTER TABLE summary_facts ADD CONSTRAINT uq_summary_fact_order UNIQUE (daily_summary_id, fact_order)",
    "ALTER TABLE summary_facts ADD COLUMN fact_state VARCHAR(16) DEFAULT 'active' NOT NULL",
    "ALTER TABLE summary_facts ADD COLUMN fact_kind VARCHAR(16) DEFAULT 'story' NOT NULL",
    "ALTER TABLE summary_facts ADD COLUMN priority SMALLINT DEFAULT 50 NOT NULL",
    "ALTER TABLE summary_facts ADD COLUMN source_first_seen_at TIMESTAMP WITH TIME ZONE",
    "UPDATE summary_facts SET source_first_seen_at = created_at WHERE source_first_seen_at IS NULL",
    "ALTER TABLE summary_facts ALTER COLUMN source_first_seen_at SET NOT NULL",
    "ALTER TABLE summary_facts ADD COLUMN source_last_seen_at TIMESTAMP WITH TIME ZONE",
    "UPDATE summary_facts SET source_last_seen_at = created_at WHERE source_last_seen_at IS NULL",
    "ALTER TABLE summary_facts ALTER COLUMN source_last_seen_at SET NOT NULL",
    "ALTER TABLE summary_facts ADD COLUMN factual_signature JSONB DEFAULT '{}'::jsonb NOT NULL",
    "ALTER TABLE summary_facts ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL",
    "ALTER TABLE summary_facts ADD CONSTRAINT ck_summary_facts_priority_range CHECK (priority BETWEEN 0 AND 100)",
    "ALTER TABLE summary_facts ADD CONSTRAINT ck_summary_facts_state_allowed CHECK (fact_state IN ('active','superseded','withheld'))",
    "ALTER TABLE summary_facts ADD CONSTRAINT ck_summary_facts_kind_allowed CHECK (fact_kind IN ('story','update'))",
    "CREATE INDEX ix_summary_facts_summary_state_order ON summary_facts (daily_summary_id, fact_state, fact_order)",
    """CREATE TABLE summary_runs (
        id UUID NOT NULL,
        daily_summary_id UUID,
        category_id VARCHAR(64) NOT NULL,
        calendar_date DATE NOT NULL,
        run_key VARCHAR(240) NOT NULL,
        status VARCHAR(16) DEFAULT 'running' NOT NULL,
        started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        completed_at TIMESTAMP WITH TIME ZONE,
        stories_considered INTEGER DEFAULT 0 NOT NULL,
        facts_appended INTEGER DEFAULT 0 NOT NULL,
        duplicate_facts_skipped INTEGER DEFAULT 0 NOT NULL,
        pending_translation_count INTEGER DEFAULT 0 NOT NULL,
        review_count INTEGER DEFAULT 0 NOT NULL,
        diagnostics JSONB DEFAULT '{}'::jsonb NOT NULL,
        CONSTRAINT pk_summary_runs PRIMARY KEY (id),
        CONSTRAINT uq_summary_run_key UNIQUE (run_key),
        CONSTRAINT ck_summary_runs_status_allowed CHECK (status IN ('running','succeeded','failed')),
        CONSTRAINT fk_summary_runs_daily_summary_id_daily_summaries FOREIGN KEY(daily_summary_id) REFERENCES daily_summaries (id) ON DELETE SET NULL,
        CONSTRAINT fk_summary_runs_category_id_categories FOREIGN KEY(category_id) REFERENCES categories (id) ON DELETE RESTRICT
    )""",
    "CREATE INDEX ix_summary_runs_category_date_started ON summary_runs (category_id, calendar_date, started_at)",
    "CREATE INDEX ix_summary_runs_status_started ON summary_runs (status, started_at)",
    """CREATE TABLE summary_review_cases (
        id UUID NOT NULL,
        daily_summary_id UUID NOT NULL,
        sentence_id UUID NOT NULL,
        candidate_fact_id UUID,
        semantic_similarity FLOAT NOT NULL,
        evidence JSONB NOT NULL,
        status VARCHAR(16) DEFAULT 'pending' NOT NULL,
        resolved_at TIMESTAMP WITH TIME ZONE,
        resolved_by VARCHAR(120),
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_summary_review_cases PRIMARY KEY (id),
        CONSTRAINT uq_summary_review_sentence UNIQUE (daily_summary_id, sentence_id),
        CONSTRAINT ck_summary_review_cases_status_allowed CHECK (status IN ('pending','append','duplicate')),
        CONSTRAINT ck_summary_review_cases_semantic_range CHECK (semantic_similarity BETWEEN -1 AND 1),
        CONSTRAINT fk_summary_review_cases_daily_summary_id_daily_summaries FOREIGN KEY(daily_summary_id) REFERENCES daily_summaries (id) ON DELETE CASCADE,
        CONSTRAINT fk_summary_review_cases_sentence_id_sentences FOREIGN KEY(sentence_id) REFERENCES sentences (id) ON DELETE CASCADE,
        CONSTRAINT fk_summary_review_cases_candidate_fact_id_summary_facts FOREIGN KEY(candidate_fact_id) REFERENCES summary_facts (id) ON DELETE SET NULL
    )""",
    "CREATE INDEX ix_summary_review_pending_created ON summary_review_cases (status, created_at)",
]

DOWNGRADE_SQL = [
    'DROP TABLE IF EXISTS "summary_review_cases" CASCADE',
    'DROP TABLE IF EXISTS "summary_runs" CASCADE',
    "DROP INDEX IF EXISTS ix_summary_facts_summary_state_order",
    "ALTER TABLE summary_facts DROP CONSTRAINT IF EXISTS ck_summary_facts_kind_allowed",
    "ALTER TABLE summary_facts DROP CONSTRAINT IF EXISTS ck_summary_facts_state_allowed",
    "ALTER TABLE summary_facts DROP CONSTRAINT IF EXISTS ck_summary_facts_priority_range",
    "ALTER TABLE summary_facts DROP CONSTRAINT IF EXISTS uq_summary_fact_order",
    "ALTER TABLE summary_facts DROP COLUMN IF EXISTS updated_at",
    "ALTER TABLE summary_facts DROP COLUMN IF EXISTS factual_signature",
    "ALTER TABLE summary_facts DROP COLUMN IF EXISTS source_last_seen_at",
    "ALTER TABLE summary_facts DROP COLUMN IF EXISTS source_first_seen_at",
    "ALTER TABLE summary_facts DROP COLUMN IF EXISTS priority",
    "ALTER TABLE summary_facts DROP COLUMN IF EXISTS fact_kind",
    "ALTER TABLE summary_facts DROP COLUMN IF EXISTS fact_state",
    "ALTER TABLE summary_facts DROP COLUMN IF EXISTS fact_order",
]


def upgrade() -> None:
    for statement in UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_SQL:
        op.execute(statement)
