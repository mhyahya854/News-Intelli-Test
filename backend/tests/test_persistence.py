from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from newsintel.deduplication import DeduplicationService, FixedEmbeddingProvider, InMemoryStoryRepository
from newsintel.keyword_matching import CategoryDecision, DetectionObservation, KeywordHit, normalize_match_text
from newsintel.persistence import (
    CorruptSpoolItemError,
    DurableCommandSpool,
    PersistenceCommand,
    PersistenceConfig,
    observation_persistence_command,
    persistence_doctor,
    replay_spool,
    translation_persistence_command,
)
from newsintel.translation import TranslationQuality, TranslationResult


BASE = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def make_observation(text: str = "Supreme Court granted bail after the hearing.") -> DetectionObservation:
    stream_id = str(uuid.uuid4())
    keyword_id = str(uuid.uuid4())
    hit = KeywordHit(
        keyword_id=keyword_id,
        category_id="judiciary",
        term="court",
        normalized_term="court",
        language="en",
        priority="normal",
        requires_context=False,
        matched_text="Court",
        normalized_span=(8, 13),
        original_span=(8, 13),
    )
    decision = CategoryDecision(
        category_id="judiciary",
        label_en="Judiciary",
        label_ur="عدلیہ",
        color="#7C3AED",
        score=1.0,
        accepted=True,
        reason="exact_keyword",
        keyword_ids=(keyword_id,),
        supporting_context=("hearing",),
        excluded_context=(),
    )
    return DetectionObservation(
        observation_id=str(uuid.uuid4()),
        unit_id=str(uuid.uuid4()),
        stream_id=stream_id,
        channel_name="Geo News",
        observed_at=BASE,
        text=text,
        normalized_text=normalize_match_text(text),
        language="en",
        confidence=0.97,
        review_required=False,
        keyword_hits=(hit,),
        category_decisions=(decision,),
        urgency="normal",
        emit_immediately=True,
        canonical_story_status="pending",
        summary_status="blocked",
        snapshot_version="test-taxonomy-v1",
    )


def make_observation_command() -> PersistenceCommand:
    observation = make_observation()
    provider = FixedEmbeddingProvider({observation.text: (1.0, 0.0, 0.0, 0.0)})
    result = DeduplicationService(InMemoryStoryRepository(), provider).process(observation)
    return observation_persistence_command(observation, result)


def config(tmp_path: Path, *, max_attempts: int = 3) -> PersistenceConfig:
    return PersistenceConfig(
        spool_root=tmp_path / "spool",
        spool_max_bytes=5 * 1024 * 1024,
        spool_max_attempts=max_attempts,
        outbox_batch_size=10,
        outbox_lease_seconds=30,
        outbox_max_attempts=5,
        job_lease_seconds=60,
        ocr_retention_days=7,
    )


def test_observation_command_preserves_full_story_evidence_and_urdu() -> None:
    command = make_observation_command()
    encoded = json.dumps(command.serializable(), ensure_ascii=False)
    restored = PersistenceCommand.from_dict(json.loads(encoded))
    assert restored.kind == "observation"
    assert restored.payload["observation"]["channel_name"] == "Geo News"
    assert restored.payload["observation"]["category_decisions"][0]["label_ur"] == "عدلیہ"
    assert restored.payload["result"]["story"]["embedding"] == [1.0, 0.0, 0.0, 0.0]
    assert restored.payload["result"]["summary_eligible"] is True


def test_spool_enqueue_claim_and_acknowledge_is_lossless(tmp_path: Path) -> None:
    spool = DurableCommandSpool(config(tmp_path))
    command = make_observation_command()
    path = spool.enqueue(command)
    assert path.exists()
    assert spool.stats()["pending"] == 1
    item = spool.claim_next()
    assert item is not None
    assert item.command.command_id == command.command_id
    assert json.loads(json.dumps(item.command.payload, ensure_ascii=False)) == json.loads(json.dumps(command.payload, ensure_ascii=False))
    assert spool.stats()["processing"] == 1
    spool.acknowledge(item)
    assert spool.stats()["pending"] == 0
    assert spool.stats()["processing"] == 0


def test_spool_reject_retries_then_moves_to_dead_letter(tmp_path: Path) -> None:
    spool = DurableCommandSpool(config(tmp_path, max_attempts=2))
    spool.enqueue(make_observation_command())
    first = spool.claim_next()
    assert first is not None
    spool.reject(first, "database offline")
    assert spool.stats()["pending"] == 1
    second = spool.claim_next()
    assert second is not None and second.attempts == 1
    destination = spool.reject(second, "still offline")
    assert destination.parent.name == "dead"
    assert spool.stats()["dead"] == 1


def test_spool_checksum_corruption_is_quarantined(tmp_path: Path) -> None:
    spool = DurableCommandSpool(config(tmp_path))
    path = spool.enqueue(make_observation_command())
    data = json.loads(path.read_text(encoding="utf-8"))
    data["body"]["payload"]["observation"]["text"] = "tampered"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CorruptSpoolItemError):
        spool.claim_next()
    assert spool.stats()["dead"] == 1


def test_orphan_processing_file_is_recovered_on_restart(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    spool = DurableCommandSpool(cfg)
    spool.enqueue(make_observation_command())
    item = spool.claim_next()
    assert item is not None
    assert spool.stats()["processing"] == 1
    restarted = DurableCommandSpool(cfg)
    assert restarted.stats()["processing"] == 0
    assert restarted.stats()["pending"] == 1


def test_replay_is_ordered_and_acknowledges_success(tmp_path: Path) -> None:
    spool = DurableCommandSpool(config(tmp_path))
    first = make_observation_command()
    second = PersistenceCommand(
        command_id=str(uuid.uuid4()),
        kind="segmented_unit",
        created_at=first.created_at + timedelta(microseconds=1),
        payload={"unit_id": "second"},
    )
    spool.enqueue(first)
    spool.enqueue(second)

    class Recorder:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def persist_command(self, command: PersistenceCommand, *, replayed: bool = False):
            assert replayed is True
            self.commands.append(command.command_id)

    recorder = Recorder()
    report = replay_spool(recorder, spool)  # type: ignore[arg-type]
    assert report == {"replayed": 2, "failed": 0, "corrupt": 0}
    assert recorder.commands == [first.command_id, second.command_id]
    assert spool.stats()["pending"] == 0


def test_translation_command_preserves_quality_and_no_roman_urdu_contract() -> None:
    result = TranslationResult(
        observation_id=str(uuid.uuid4()),
        source_text="The court did not grant bail.",
        translated_text="عدالت نے ضمانت منظور نہیں کی۔",
        source_language="en",
        target_language="ur",
        status="complete",
        model_name="fixture",
        model_revision="1",
        engine="fixture-cpu",
        latency_ms=12.5,
        cache_hit=False,
        quality=TranslationQuality(
            accepted=True,
            score=1.0,
            checks={"negation_preserved": True, "no_roman_urdu_output": True},
            issues=(),
        ),
    )
    command = translation_persistence_command(result)
    assert command.payload["quality"]["accepted"] is True
    assert command.payload["quality"]["checks"]["no_roman_urdu_output"] is True
    assert command.payload["translated_text"].endswith("۔")


def test_persistence_doctor_validates_writable_spool_without_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PERSISTENCE_SPOOL_ROOT", str(tmp_path / "doctor-spool"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    report = persistence_doctor(check_database=False)
    assert report["status"] == "ready"
    assert report["checks"]["spool_writable"] is True
    assert report["durability"]["transactional_outbox"] is True
    assert report["redis"] is False


def test_observation_command_pins_embedding_identity_for_restart_replay() -> None:
    observation = make_observation()
    provider = FixedEmbeddingProvider({observation.text: (1.0, 0.0, 0.0, 0.0)})
    result = DeduplicationService(InMemoryStoryRepository(), provider).process(observation)
    command = observation_persistence_command(
        observation,
        result,
        embedding_model=provider.model_name,
        embedding_revision=provider.model_revision,
        embedding_dimensions=provider.dimensions,
    )
    assert command.payload["result"]["embedding_model"] == "test/fixed-embedding"
    assert command.payload["result"]["embedding_revision"] == "1"
    assert command.payload["result"]["embedding_dimensions"] == 4


def test_live_window_query_preserves_repeated_occurrences_and_uses_cutoff() -> None:
    from sqlalchemy.dialects import postgresql

    from newsintel.persistence import PostgresReadRepository

    cutoff = BASE - timedelta(minutes=30)
    statement = PostgresReadRepository.live_window_statement(cutoff=cutoff, limit=5000)
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "sentence_occurrences.emitted_live IS true" in sql
    assert "sentence_occurrences.observed_at >=" in sql
    assert "ORDER BY sentence_occurrences.observed_at DESC" in sql
    assert "DISTINCT" not in sql.upper()
    assert "LIMIT 5000" in sql
