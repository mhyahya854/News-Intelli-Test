from sqlalchemy.orm import configure_mappers

from newsintel.models import Base

EXPECTED_TABLES = {
    "streams", "categories", "keywords", "model_registry", "raw_ocr_text",
    "sentences", "sentence_categories", "sentence_keywords", "sentence_sources",
    "translations", "sentence_embeddings", "daily_summaries",
    "daily_summary_sentences", "summary_facts", "summary_fact_sources",
    "admin_users", "alerts_log", "stream_health_events", "processing_jobs",
    "scheduled_jobs", "outbox_events", "system_settings", "admin_audit_log",
    "sentence_occurrences", "dedup_review_cases", "occurrence_translations", "translation_memory",
    "pipeline_traces", "summary_runs", "summary_review_cases",
}


def test_all_mappers_configure() -> None:
    configure_mappers()


def test_expected_tables_exist() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_multi_category_and_multi_keyword_schema() -> None:
    assert "sentence_categories" in Base.metadata.tables
    assert "sentence_keywords" in Base.metadata.tables
    sentence_columns = Base.metadata.tables["sentences"].c
    assert "category_id" not in sentence_columns
    assert "matched_keyword_id" not in sentence_columns


def test_durable_postgres_queue_and_outbox_exist() -> None:
    assert "processing_jobs" in Base.metadata.tables
    assert "scheduled_jobs" in Base.metadata.tables
    assert "outbox_events" in Base.metadata.tables


def test_keyword_schema_contains_no_fuzzy_or_misspelling_fields() -> None:
    columns = Base.metadata.tables["keywords"].c
    assert "min_fuzzy_score" not in columns
    assert "ocr_variants" not in columns
    assert "is_variant_of" not in columns


def test_phase6_occurrence_and_review_tables_exist() -> None:
    occurrence = Base.metadata.tables["sentence_occurrences"].c
    assert "observation_id" in occurrence
    assert "sentence_id" in occurrence
    assert "emitted_live" in occurrence
    review = Base.metadata.tables["dedup_review_cases"].c
    assert "candidate_sentence_id" in review
    assert "status" in review


def test_phase7_occurrence_translation_and_memory_tables_exist() -> None:
    occurrence_translation = Base.metadata.tables["occurrence_translations"].c
    assert "occurrence_id" in occurrence_translation
    assert "target_language" in occurrence_translation
    assert "translated_text" in occurrence_translation
    memory = Base.metadata.tables["translation_memory"].c
    assert "source_hash" in memory
    assert "hit_count" in memory


def test_phase8_trace_and_outbox_lease_schema_exist() -> None:
    trace = Base.metadata.tables["pipeline_traces"].c
    assert "trace_key" in trace
    assert "observation_id" in trace
    assert "occurrence_id" in trace
    occurrence = Base.metadata.tables["sentence_occurrences"].c
    assert "unit_id" in occurrence
    assert "snapshot_version" in occurrence
    assert "urgency" in occurrence
    outbox = Base.metadata.tables["outbox_events"].c
    assert "event_key" in outbox
    assert "available_at" in outbox
    assert "leased_until" in outbox
    assert "dead_lettered_at" in outbox


def test_phase9_summary_run_and_review_schema_exist() -> None:
    fact = Base.metadata.tables["summary_facts"].c
    assert "fact_order" in fact
    assert "fact_state" in fact
    assert "factual_signature" in fact
    run = Base.metadata.tables["summary_runs"].c
    assert "run_key" in run
    assert "facts_appended" in run
    review = Base.metadata.tables["summary_review_cases"].c
    assert "candidate_fact_id" in review
    assert "semantic_similarity" in review


def test_phase10_api_indexes_exist_in_metadata() -> None:
    occurrences = Base.metadata.tables["sentence_occurrences"]
    occurrence_indexes = {index.name for index in occurrences.indexes}
    assert "ix_sentence_occurrences_api_cursor" in occurrence_indexes
    assert "ix_sentence_occurrences_categories_gin" in occurrence_indexes
    assert "ix_sentence_occurrences_keywords_gin" in occurrence_indexes
    sentence_indexes = {index.name for index in Base.metadata.tables["sentences"].indexes}
    keyword_indexes = {index.name for index in Base.metadata.tables["keywords"].indexes}
    assert "ix_sentences_api_cursor" in sentence_indexes
    assert "ix_keywords_admin_cursor" in keyword_indexes
