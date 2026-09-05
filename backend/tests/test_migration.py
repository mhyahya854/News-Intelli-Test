from pathlib import Path


def test_initial_migration_is_frozen_and_postgres_native() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "20260719_0001_phase_1_schema.py"
    text = path.read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in text
    assert "CREATE TABLE sentences" in text
    assert "CREATE TABLE sentence_categories" in text
    assert "CREATE TABLE processing_jobs" in text
    assert "gin_trgm_ops" in text
    assert "redis" not in text.casefold()
    assert "newsintel.models" not in text


def test_initial_migration_contains_no_fuzzy_or_misspelling_schema() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "20260719_0001_phase_1_schema.py"
    text = path.read_text(encoding="utf-8").casefold()
    assert "min_fuzzy_score" not in text
    assert "ocr_variants" not in text
    assert "is_variant_of" not in text
    assert "'fuzzy'" not in text
    assert "'ocr_variant'" not in text
    assert "'stemmed'" not in text


def test_phase6_migration_adds_occurrence_ledger_and_review_gate() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "20260719_0002_phase_6_dedup.py"
    text = path.read_text(encoding="utf-8")
    assert 'down_revision = "20260719_0001"' in text
    assert "CREATE TABLE sentence_occurrences" in text
    assert "CREATE TABLE dedup_review_cases" in text
    assert "emitted_live BOOLEAN DEFAULT true" in text
    assert "pending_review" in text
    assert "redis" not in text.casefold()


def test_phase7_migration_adds_exact_occurrence_translation_and_memory() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "20260719_0003_phase_7_translation.py"
    text = path.read_text(encoding="utf-8")
    assert 'down_revision = "20260719_0002"' in text
    assert "CREATE TABLE occurrence_translations" in text
    assert "CREATE TABLE translation_memory" in text
    assert "source_language <> target_language" in text
    assert "roman" not in text.casefold()
    assert "redis" not in text.casefold()


def test_phase8_migration_adds_transactional_outbox_leases_and_pipeline_traces() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "20260719_0004_phase_8_persistence.py"
    text = path.read_text(encoding="utf-8")
    assert 'down_revision = "20260719_0003"' in text
    assert "CREATE TABLE pipeline_traces" in text
    assert "event_key VARCHAR(300)" in text
    assert "leased_until TIMESTAMP WITH TIME ZONE" in text
    assert "uq_processing_jobs_active_dedup_key" in text
    assert "redis" not in text.casefold()


def test_phase9_migration_adds_additive_summary_audit_and_review_gate() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "20260719_0005_phase_9_summarization.py"
    text = path.read_text(encoding="utf-8")
    assert 'down_revision = "20260719_0004"' in text
    assert "CREATE TABLE summary_runs" in text
    assert "CREATE TABLE summary_review_cases" in text
    assert "fact_order INTEGER" in text
    assert "fact_state IN ('active','superseded','withheld')" in text
    assert "redis" not in text.casefold()


def test_phase10_migration_adds_cursor_and_live_filter_indexes() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "20260720_0006_phase_10_api_indexes.py"
    text = path.read_text(encoding="utf-8")
    assert 'down_revision = "20260719_0005"' in text
    assert "ix_sentence_occurrences_api_cursor" in text
    assert "ix_sentence_occurrences_categories_gin" in text
    assert "ix_sentence_occurrences_keywords_gin" in text
    assert "ix_sentences_api_cursor" in text
    assert "redis" not in text.casefold()
