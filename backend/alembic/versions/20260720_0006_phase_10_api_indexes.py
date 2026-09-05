"""Phase 10 API cursor and live-filter indexes.

Revision ID: 20260720_0006
Revises: 20260719_0005
Create Date: 2026-07-20
"""
from __future__ import annotations

from alembic import op

revision = "20260720_0006"
down_revision = "20260719_0005"
branch_labels = None
depends_on = None

UPGRADE_SQL = [
    "CREATE INDEX IF NOT EXISTS ix_sentences_api_cursor ON sentences (first_seen_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_keywords_admin_cursor ON keywords (created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_sentence_occurrences_api_cursor ON sentence_occurrences (observed_at, id) WHERE emitted_live = true",
    "CREATE INDEX IF NOT EXISTS ix_sentence_occurrences_categories_gin ON sentence_occurrences USING gin (category_ids)",
    "CREATE INDEX IF NOT EXISTS ix_sentence_occurrences_keywords_gin ON sentence_occurrences USING gin (keyword_ids)",
]

DOWNGRADE_SQL = [
    "DROP INDEX IF EXISTS ix_sentence_occurrences_keywords_gin",
    "DROP INDEX IF EXISTS ix_sentence_occurrences_categories_gin",
    "DROP INDEX IF EXISTS ix_sentence_occurrences_api_cursor",
    "DROP INDEX IF EXISTS ix_keywords_admin_cursor",
    "DROP INDEX IF EXISTS ix_sentences_api_cursor",
]


def upgrade() -> None:
    for statement in UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_SQL:
        op.execute(statement)
