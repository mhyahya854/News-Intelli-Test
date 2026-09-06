from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .database import build_session_factory, get_database_url
from .deduplication import (
    CanonicalStory,
    DeduplicationConfig,
    DeduplicationResult,
    DeduplicationService,
    EmbeddingProvider,
    InMemoryStoryRepository,
    ReviewCase,
    StoryOccurrence,
    StoryRepository,
    pakistan_calendar_date,
    story_features,
)
from .keyword_matching import CategoryDecision, DetectionObservation, KeywordHit
from .models import (
    AlertLog,
    Category,
    DedupReviewCase,
    Keyword,
    OccurrenceTranslation,
    OutboxEvent,
    PipelineTrace,
    ProcessingJob,
    RawOCRText,
    Sentence,
    SentenceCategory,
    SentenceEmbedding,
    SentenceKeyword,
    SentenceOccurrence,
    SentenceSource,
    Stream,
    Translation,
    TranslationMemory,
)
from .ocr import OCRFrameResult
from .segmentation import SegmentedTextUnit
from .translation import (
    EN_UR_MODEL,
    EN_UR_REVISION,
    UR_EN_MODEL,
    UR_EN_REVISION,
    TranslationResult,
    translation_memory_key,
)

PIPELINE_VERSION = "phase8-persistence-v1"
COMMAND_SCHEMA_VERSION = 1
OUTBOX_CHANNEL = "newsintel_outbox"


class PersistenceError(RuntimeError):
    pass


class SpoolCapacityError(PersistenceError):
    pass


class CorruptSpoolItemError(PersistenceError):
    pass


class PersistencePublisher(Protocol):
    def __call__(self, event_type: str, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class PersistenceConfig:
    spool_root: Path = Path("data/spool/persistence")
    spool_max_bytes: int = 2 * 1024 * 1024 * 1024
    spool_max_attempts: int = 50
    outbox_batch_size: int = 100
    outbox_lease_seconds: int = 30
    outbox_max_attempts: int = 12
    job_lease_seconds: int = 120
    ocr_retention_days: int = 7

    @classmethod
    def from_env(cls) -> "PersistenceConfig":
        defaults = cls()
        return cls(
            spool_root=Path(os.getenv("PERSISTENCE_SPOOL_ROOT", str(defaults.spool_root))),
            spool_max_bytes=max(
                10 * 1024 * 1024,
                int(os.getenv("PERSISTENCE_SPOOL_MAX_BYTES", str(defaults.spool_max_bytes))),
            ),
            spool_max_attempts=max(
                1, int(os.getenv("PERSISTENCE_SPOOL_MAX_ATTEMPTS", str(defaults.spool_max_attempts)))
            ),
            outbox_batch_size=max(
                1, int(os.getenv("OUTBOX_BATCH_SIZE", str(defaults.outbox_batch_size)))
            ),
            outbox_lease_seconds=max(
                5, int(os.getenv("OUTBOX_LEASE_SECONDS", str(defaults.outbox_lease_seconds)))
            ),
            outbox_max_attempts=max(
                1, int(os.getenv("OUTBOX_MAX_ATTEMPTS", str(defaults.outbox_max_attempts)))
            ),
            job_lease_seconds=max(
                10, int(os.getenv("JOB_LEASE_SECONDS", str(defaults.job_lease_seconds)))
            ),
            ocr_retention_days=max(
                1, int(os.getenv("RAW_OCR_RETENTION_DAYS", str(defaults.ocr_retention_days)))
            ),
        )


@dataclass(frozen=True, slots=True)
class PersistenceCommand:
    command_id: str
    kind: str
    created_at: datetime
    payload: dict[str, Any]
    schema_version: int = COMMAND_SCHEMA_VERSION

    def serializable(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "kind": self.kind,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "payload": self.payload,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PersistenceCommand":
        return cls(
            command_id=str(value["command_id"]),
            kind=str(value["kind"]),
            created_at=_datetime(value["created_at"]),
            payload=dict(value["payload"]),
            schema_version=int(value.get("schema_version", 1)),
        )


@dataclass(slots=True)
class SpoolItem:
    path: Path
    command: PersistenceCommand
    attempts: int
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class PersistenceReceipt:
    command_id: str
    kind: str
    status: str
    observation_id: str | None = None
    occurrence_id: str | None = None
    sentence_id: str | None = None
    spooled_path: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxDelivery:
    event_id: str
    event_key: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True, slots=True)
class LiveObservationRecord:
    """One exact broadcast occurrence for the rolling Live Feed; repeats are retained."""

    occurrence_id: str
    observation_id: str
    canonical_story_id: str | None
    stream_id: str
    channel_name: str
    observed_at: datetime
    source_text: str
    source_language: str
    confidence: float
    category_ids: tuple[str, ...]
    keyword_ids: tuple[str, ...]
    urgency: str
    review_required: bool
    decision: str
    dedup_layer: str
    translation_status: str
    target_language: str | None
    translated_text: str | None

    def serializable(self) -> dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id,
            "observation_id": self.observation_id,
            "canonical_story_id": self.canonical_story_id,
            "stream_id": self.stream_id,
            "channel_name": self.channel_name,
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
            "source_text": self.source_text,
            "source_language": self.source_language,
            "confidence": self.confidence,
            "category_ids": list(self.category_ids),
            "keyword_ids": list(self.keyword_ids),
            "urgency": self.urgency,
            "review_required": self.review_required,
            "decision": self.decision,
            "dedup_layer": self.dedup_layer,
            "translation_status": self.translation_status,
            "target_language": self.target_language,
            "translated_text": self.translated_text,
        }


def _uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _transient_database_error(exc: BaseException) -> bool:
    if isinstance(exc, (OperationalError, InterfaceError)):
        return True
    if isinstance(exc, DBAPIError):
        return bool(exc.connection_invalidated)
    return False


class DurableCommandSpool:
    """Append-only local outage buffer with atomic files and SHA-256 verification.

    It is intentionally not another application database. PostgreSQL remains authoritative;
    this directory exists only so a temporary PostgreSQL outage cannot lose observations.
    """

    def __init__(self, config: PersistenceConfig | None = None) -> None:
        self.config = config or PersistenceConfig.from_env()
        self.root = self.config.spool_root.resolve()
        self.pending = self.root / "pending"
        self.processing = self.root / "processing"
        self.dead = self.root / "dead"
        self._lock = threading.Lock()
        for directory in (self.pending, self.processing, self.dead):
            directory.mkdir(parents=True, exist_ok=True)
        self._recover_processing_files()

    def _recover_processing_files(self) -> None:
        for path in sorted(self.processing.glob("*.json")):
            target = self.pending / path.name
            if target.exists():
                path.unlink(missing_ok=True)
            else:
                path.replace(target)

    def total_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for directory in (self.pending, self.processing, self.dead)
            for path in directory.glob("*.json")
            if path.is_file()
        )

    def enqueue(self, command: PersistenceCommand, *, attempts: int = 0, last_error: str | None = None) -> Path:
        body = command.serializable()
        envelope = {
            "body": body,
            "checksum": _checksum(body),
            "attempts": attempts,
            "last_error": last_error,
        }
        encoded_size = len(_canonical_json(envelope))
        with self._lock:
            if self.total_bytes() + encoded_size > self.config.spool_max_bytes:
                raise SpoolCapacityError(
                    "Persistence spool capacity is exhausted; the pipeline stopped rather than dropping data."
                )
            for directory in (self.pending, self.processing, self.dead):
                existing = next(directory.glob(f"*-{command.command_id}.json"), None)
                if existing is not None:
                    return existing
            sequence = time.time_ns()
            path = self.pending / f"{sequence:020d}-{command.command_id}.json"
            _atomic_json_write(path, envelope)
            return path

    def _read(self, path: Path) -> SpoolItem:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            body = envelope["body"]
            if not isinstance(body, dict) or envelope.get("checksum") != _checksum(body):
                raise CorruptSpoolItemError(f"Checksum mismatch: {path.name}")
            return SpoolItem(
                path=path,
                command=PersistenceCommand.from_dict(body),
                attempts=int(envelope.get("attempts", 0)),
                last_error=envelope.get("last_error"),
            )
        except CorruptSpoolItemError:
            raise
        except Exception as exc:
            raise CorruptSpoolItemError(f"Invalid spool item {path.name}: {exc}") from exc

    def claim_next(self) -> SpoolItem | None:
        with self._lock:
            paths = sorted(self.pending.glob("*.json"))
            if not paths:
                return None
            source = paths[0]
            claimed = self.processing / source.name
            source.replace(claimed)
        try:
            return self._read(claimed)
        except CorruptSpoolItemError:
            claimed.replace(self.dead / claimed.name)
            raise

    def acknowledge(self, item: SpoolItem) -> None:
        item.path.unlink(missing_ok=True)

    def reject(self, item: SpoolItem, error: BaseException | str, *, permanent: bool = False) -> Path:
        attempts = item.attempts + 1
        last_error = str(error)
        destination_dir = self.dead if permanent or attempts >= self.config.spool_max_attempts else self.pending
        destination = destination_dir / item.path.name
        body = item.command.serializable()
        _atomic_json_write(
            destination,
            {
                "body": body,
                "checksum": _checksum(body),
                "attempts": attempts,
                "last_error": last_error,
            },
        )
        item.path.unlink(missing_ok=True)
        return destination

    def stats(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "pending": len(list(self.pending.glob("*.json"))),
            "processing": len(list(self.processing.glob("*.json"))),
            "dead": len(list(self.dead.glob("*.json"))),
            "bytes": self.total_bytes(),
            "max_bytes": self.config.spool_max_bytes,
        }


def _keyword_hit_payload(item: KeywordHit) -> dict[str, Any]:
    return item.serializable()


def _category_decision_payload(item: CategoryDecision) -> dict[str, Any]:
    return item.serializable()


def _story_payload(story: CanonicalStory | None) -> dict[str, Any] | None:
    if story is None:
        return None
    return {
        "story_id": story.story_id,
        "calendar_date": story.calendar_date.isoformat(),
        "original_text": story.original_text,
        "normalized_text": story.normalized_text,
        "original_language": story.original_language,
        "category_ids": sorted(story.category_ids),
        "first_seen_at": story.first_seen_at.isoformat(),
        "last_seen_at": story.last_seen_at.isoformat(),
        "occurrence_count": story.occurrence_count,
        "source_channels": sorted(story.source_channels),
        "source_stream_ids": sorted(story.source_stream_ids),
        "exact_hashes": sorted(story.exact_hashes),
        "embedding": list(story.embedding),
        "embedding_count": story.embedding_count,
        "confidence": story.confidence,
        "latest_text": story.latest_text,
        "summary_state": story.summary_state,
        "review_status": story.review_status,
    }


def observation_persistence_command(
    observation: DetectionObservation,
    result: DeduplicationResult,
    *,
    raw_ocr_id: str | None = None,
    embedding_model: str | None = None,
    embedding_revision: str | None = None,
    embedding_dimensions: int | None = None,
) -> PersistenceCommand:
    review = None
    if result.review_case:
        review = {
            "review_id": result.review_case.review_id,
            "observation_id": result.review_case.observation_id,
            "candidate_story_id": result.review_case.candidate_story_id,
            "calendar_date": result.review_case.calendar_date.isoformat(),
            "semantic_similarity": result.review_case.semantic_similarity,
            "lexical_overlap": result.review_case.lexical_overlap,
            "evidence": result.review_case.evidence,
            "status": result.review_case.status,
        }
    return PersistenceCommand(
        command_id=str(uuid.uuid4()),
        kind="observation",
        created_at=datetime.now(timezone.utc),
        payload={
            "observation": {
                "observation_id": observation.observation_id,
                "unit_id": observation.unit_id,
                "stream_id": observation.stream_id,
                "channel_name": observation.channel_name,
                "observed_at": observation.observed_at.isoformat(),
                "text": observation.text,
                "normalized_text": observation.normalized_text,
                "language": observation.language,
                "confidence": observation.confidence,
                "review_required": observation.review_required,
                "urgency": observation.urgency,
                "snapshot_version": observation.snapshot_version,
                "keyword_hits": [_keyword_hit_payload(item) for item in observation.keyword_hits],
                "category_decisions": [
                    _category_decision_payload(item) for item in observation.category_decisions
                ],
                "raw_ocr_id": raw_ocr_id,
            },
            "result": {
                "embedding_model": embedding_model,
                "embedding_revision": embedding_revision,
                "embedding_dimensions": embedding_dimensions,
                "action": result.action,
                "dedup_layer": result.dedup_layer,
                "summary_eligible": result.summary_eligible,
                "story": _story_payload(result.story),
                "occurrence": result.occurrence.serializable(),
                "best_candidate": result.best_candidate.serializable()
                if result.best_candidate
                else None,
                "review_case": review,
                "emitted_events": list(result.emitted_events),
            },
        },
    )


def translation_persistence_command(result: TranslationResult) -> PersistenceCommand:
    return PersistenceCommand(
        command_id=str(uuid.uuid4()),
        kind="translation",
        created_at=datetime.now(timezone.utc),
        payload=result.serializable(),
    )


def ocr_persistence_command(result: OCRFrameResult) -> PersistenceCommand:
    return PersistenceCommand(
        command_id=str(uuid.uuid4()),
        kind="ocr",
        created_at=datetime.now(timezone.utc),
        payload=result.serializable(),
    )


def segmented_unit_persistence_command(unit: SegmentedTextUnit) -> PersistenceCommand:
    return PersistenceCommand(
        command_id=str(uuid.uuid4()),
        kind="segmented_unit",
        created_at=datetime.now(timezone.utc),
        payload=unit.serializable(),
    )


class PostgresStoryRepository(StoryRepository):
    """Deduplication repository bound to the caller's PostgreSQL transaction."""

    def __init__(
        self,
        session: Session,
        *,
        embedding_model: str,
        embedding_revision: str,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.session = session
        self.embedding_model = embedding_model
        self.embedding_revision = embedding_revision
        self.embedding_provider = embedding_provider

    def exact_story(self, calendar_date: date, exact_hash: str) -> CanonicalStory | None:
        sentence_id = self.session.scalar(
            select(SentenceOccurrence.sentence_id)
            .where(
                SentenceOccurrence.calendar_date == calendar_date,
                SentenceOccurrence.exact_hash == exact_hash,
                SentenceOccurrence.sentence_id.is_not(None),
            )
            .order_by(SentenceOccurrence.observed_at.asc())
            .limit(1)
        )
        if sentence_id is None:
            sentence = self.session.scalar(
                select(Sentence).where(
                    Sentence.calendar_date == calendar_date, Sentence.exact_hash == exact_hash
                )
            )
        else:
            sentence = self.session.get(Sentence, sentence_id)
        return self._to_story(sentence) if sentence else None

    def candidates(self, calendar_date: date, category_ids: set[str], limit: int) -> list[CanonicalStory]:
        if not category_ids:
            return []
        sentence_ids = list(
            self.session.scalars(
                select(SentenceCategory.sentence_id)
                .join(Sentence, Sentence.id == SentenceCategory.sentence_id)
                .where(
                    Sentence.calendar_date == calendar_date,
                    SentenceCategory.category_id.in_(sorted(category_ids)),
                    Sentence.review_status != "rejected",
                )
                .group_by(SentenceCategory.sentence_id, Sentence.last_seen_at)
                .order_by(Sentence.last_seen_at.desc())
                .limit(limit)
            )
        )
        stories: list[CanonicalStory] = []
        for sentence_id in sentence_ids:
            sentence = self.session.get(Sentence, sentence_id)
            if sentence:
                stories.append(self._to_story(sentence))
        stories.sort(key=lambda item: item.last_seen_at, reverse=True)
        return stories

    def _to_story(self, sentence: Sentence) -> CanonicalStory:
        category_ids = set(
            self.session.scalars(
                select(SentenceCategory.category_id).where(
                    SentenceCategory.sentence_id == sentence.id
                )
            )
        )
        source_rows = self.session.execute(
            select(SentenceSource.stream_id, Stream.channel_name)
            .join(Stream, Stream.id == SentenceSource.stream_id)
            .where(SentenceSource.sentence_id == sentence.id)
        ).all()
        exact_hashes = set(
            self.session.scalars(
                select(SentenceOccurrence.exact_hash).where(
                    SentenceOccurrence.sentence_id == sentence.id
                )
            )
        )
        exact_hashes.add(sentence.exact_hash)
        embedding = self.session.scalar(
            select(SentenceEmbedding)
            .where(
                SentenceEmbedding.sentence_id == sentence.id,
                SentenceEmbedding.model_name == self.embedding_model,
                SentenceEmbedding.model_revision == self.embedding_revision,
            )
            .order_by(SentenceEmbedding.created_at.desc())
            .limit(1)
        )
        if embedding is None and self.embedding_provider is not None:
            vector = self.embedding_provider.encode([sentence.original_text])[0]
            embedding = SentenceEmbedding(
                sentence_id=sentence.id,
                model_name=self.embedding_model,
                model_revision=self.embedding_revision,
                dimensions=len(vector),
                embedding_vector=list(vector),
            )
            self.session.add(embedding)
        decision = sentence.dedup_decision or {}
        return CanonicalStory(
            story_id=str(sentence.id),
            calendar_date=sentence.calendar_date,
            original_text=sentence.original_text,
            normalized_text=sentence.normalized_text,
            original_language=sentence.original_language,
            category_ids=category_ids,
            first_seen_at=sentence.first_seen_at,
            last_seen_at=sentence.last_seen_at,
            occurrence_count=sentence.occurrence_count,
            source_channels={row.channel_name for row in source_rows},
            source_stream_ids={str(row.stream_id) for row in source_rows},
            exact_hashes=exact_hashes,
            embedding=list(embedding.embedding_vector) if embedding else [],
            embedding_count=int(decision.get("embedding_count", sentence.occurrence_count)),
            confidence=sentence.confidence_score,
            latest_text=str(decision.get("latest_text") or sentence.original_text),
            summary_state=str(decision.get("summary_state") or "eligible"),
            review_status=sentence.review_status,
        )

    def create_story(self, story: CanonicalStory) -> None:
        story_id = _uuid(story.story_id)
        existing_pending = next(
            (obj for obj in self.session.new if isinstance(obj, Sentence) and obj.id == story_id),
            None,
        )
        if existing_pending is not None or self.session.get(Sentence, story_id) is not None:
            self.update_story(story)
            return
        primary_hash = story_features(story.original_text).exact_hash
        sentence = Sentence(
            id=story_id,
            original_text=story.original_text,
            normalized_text=story.normalized_text,
            original_language=story.original_language,
            confidence_score=story.confidence,
            exact_hash=primary_hash,
            story_fingerprint=hashlib.sha256(story.normalized_text.encode("utf-8")).hexdigest(),
            calendar_date=story.calendar_date,
            first_seen_at=story.first_seen_at,
            last_seen_at=story.last_seen_at,
            occurrence_count=story.occurrence_count,
            dedup_similarity_score=None,
            dedup_decision={
                "latest_text": story.latest_text,
                "embedding_count": story.embedding_count,
                "summary_state": story.summary_state,
            },
            extracted_entities={},
            review_status=story.review_status,
        )
        self.session.add(sentence)
        for category_id in sorted(story.category_ids):
            self.session.add(
                SentenceCategory(
                    sentence_id=story_id,
                    category_id=category_id,
                    classification_score=1.0,
                    classifier_version="phase8-replay",
                    evidence={},
                )
            )
        if story.embedding:
            self.session.add(
                SentenceEmbedding(
                    sentence_id=story_id,
                    model_name=self.embedding_model,
                    model_revision=self.embedding_revision,
                    dimensions=len(story.embedding),
                    embedding_vector=list(story.embedding),
                )
            )
        for stream_id in sorted(story.source_stream_ids):
            self.session.add(
                SentenceSource(
                    sentence_id=story_id,
                    stream_id=_uuid(stream_id),
                    first_seen_at=story.first_seen_at,
                    last_seen_at=story.last_seen_at,
                    occurrence_count=1,
                    source_text=story.latest_text,
                )
            )

    def update_story(self, story: CanonicalStory) -> None:
        story_id = _uuid(story.story_id)
        sentence = self.session.get(Sentence, story_id, with_for_update=True)
        if sentence is None:
            sentence = next(
                (obj for obj in self.session.new if isinstance(obj, Sentence) and obj.id == story_id),
                None,
            )
        if sentence is None:
            self.create_story(story)
            return
        sentence.first_seen_at = min(sentence.first_seen_at, story.first_seen_at)
        sentence.last_seen_at = max(sentence.last_seen_at, story.last_seen_at)
        sentence.occurrence_count = max(sentence.occurrence_count, story.occurrence_count)
        sentence.review_status = story.review_status
        if story.confidence >= sentence.confidence_score:
            sentence.original_text = story.original_text
            sentence.normalized_text = story.normalized_text
            sentence.original_language = story.original_language
            sentence.confidence_score = story.confidence
        current_decision = dict(sentence.dedup_decision or {})
        current_count = int(current_decision.get("embedding_count", 0))
        current_decision.update(
            {
                "latest_text": story.latest_text,
                "embedding_count": max(current_count, story.embedding_count),
                "summary_state": story.summary_state,
            }
        )
        sentence.dedup_decision = current_decision
        existing_categories = set(
            self.session.scalars(
                select(SentenceCategory.category_id).where(
                    SentenceCategory.sentence_id == story_id
                )
            )
        )
        existing_categories.update(
            obj.category_id
            for obj in self.session.new
            if isinstance(obj, SentenceCategory) and obj.sentence_id == story_id
        )
        for category_id in sorted(story.category_ids - existing_categories):
            self.session.add(
                SentenceCategory(
                    sentence_id=story_id,
                    category_id=category_id,
                    classification_score=1.0,
                    classifier_version="phase8-replay",
                    evidence={},
                )
            )
        existing_sources = set(
            self.session.scalars(
                select(SentenceSource.stream_id).where(SentenceSource.sentence_id == story_id)
            )
        )
        existing_sources.update(
            obj.stream_id
            for obj in self.session.new
            if isinstance(obj, SentenceSource) and obj.sentence_id == story_id
        )
        for stream_value in sorted(story.source_stream_ids):
            stream_id = _uuid(stream_value)
            source = self.session.scalar(
                select(SentenceSource).where(
                    SentenceSource.sentence_id == story_id,
                    SentenceSource.stream_id == stream_id,
                )
            )
            if source:
                source.last_seen_at = max(source.last_seen_at, story.last_seen_at)
                source.occurrence_count = max(source.occurrence_count, 1)
                source.source_text = story.latest_text
            elif stream_id not in existing_sources:
                self.session.add(
                    SentenceSource(
                        sentence_id=story_id,
                        stream_id=stream_id,
                        first_seen_at=story.first_seen_at,
                        last_seen_at=story.last_seen_at,
                        occurrence_count=1,
                        source_text=story.latest_text,
                    )
                )
        embedding = self.session.scalar(
            select(SentenceEmbedding).where(
                SentenceEmbedding.sentence_id == story_id,
                SentenceEmbedding.model_name == self.embedding_model,
                SentenceEmbedding.model_revision == self.embedding_revision,
            )
        )
        if story.embedding:
            if embedding is None:
                has_pending_embedding = any(
                    isinstance(obj, SentenceEmbedding) and obj.sentence_id == story_id
                    for obj in self.session.new
                )
                if not has_pending_embedding:
                    self.session.add(
                        SentenceEmbedding(
                            sentence_id=story_id,
                            model_name=self.embedding_model,
                            model_revision=self.embedding_revision,
                            dimensions=len(story.embedding),
                            embedding_vector=list(story.embedding),
                        )
                    )
            elif story.embedding_count >= current_count:
                embedding.dimensions = len(story.embedding)
                embedding.embedding_vector = list(story.embedding)

    def add_occurrence(self, occurrence: StoryOccurrence) -> None:
        if any(
            isinstance(obj, SentenceOccurrence)
            and obj.observation_id == occurrence.observation_id
            for obj in self.session.new
        ):
            return
        existing = self.session.scalar(
            select(SentenceOccurrence).where(
                SentenceOccurrence.observation_id == occurrence.observation_id
            )
        )
        if existing:
            return
        self.session.add(
            SentenceOccurrence(
                id=_uuid(occurrence.occurrence_id),
                observation_id=occurrence.observation_id,
                sentence_id=_uuid(occurrence.story_id),
                stream_id=_uuid(occurrence.stream_id),
                observed_at=occurrence.observed_at,
                calendar_date=occurrence.calendar_date,
                source_text=occurrence.source_text,
                normalized_text=occurrence.normalized_text,
                language=occurrence.language,
                confidence_score=occurrence.confidence,
                category_ids=list(occurrence.category_ids),
                keyword_ids=list(occurrence.keyword_ids),
                exact_hash=occurrence.exact_hash,
                decision=occurrence.decision,
                dedup_layer=occurrence.dedup_layer,
                semantic_similarity=occurrence.semantic_similarity,
                lexical_overlap=occurrence.lexical_overlap,
                decision_metadata=occurrence.decision_metadata,
                emitted_live=occurrence.emitted_live,
            )
        )

    def add_review(self, review: ReviewCase) -> None:
        if self.session.scalar(
            select(DedupReviewCase.id).where(
                DedupReviewCase.observation_id == review.observation_id
            )
        ):
            return
        self.session.add(
            DedupReviewCase(
                id=_uuid(review.review_id),
                observation_id=review.observation_id,
                candidate_sentence_id=_uuid(review.candidate_story_id),
                calendar_date=review.calendar_date,
                semantic_similarity=review.semantic_similarity,
                lexical_overlap=review.lexical_overlap,
                evidence=review.evidence,
                status=review.status,
            )
        )


class PostgresPersistenceService:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        config: PersistenceConfig | None = None,
    ) -> None:
        self.session_factory = session_factory or build_session_factory()
        self.config = config or PersistenceConfig.from_env()

    @staticmethod
    def _is_unique_violation(exc: IntegrityError) -> bool:
        return getattr(exc.orig, "sqlstate", None) == "23505"

    def _run_transaction(self, operation: Callable[[Session], Any], *, attempts: int = 4) -> Any:
        for attempt in range(attempts):
            try:
                with self.session_factory() as session, session.begin():
                    return operation(session)
            except IntegrityError as exc:
                if not self._is_unique_violation(exc) or attempt + 1 >= attempts:
                    raise
                time.sleep(0.02 * (2**attempt))
        raise AssertionError("unreachable")

    def process_observation(
        self,
        observation: DetectionObservation,
        embedding_provider: EmbeddingProvider,
        dedup_config: DeduplicationConfig | None = None,
        *,
        raw_ocr_id: str | None = None,
    ) -> tuple[DeduplicationResult, PersistenceReceipt]:
        config = dedup_config or DeduplicationConfig.from_env()
        def operation(session: Session) -> tuple[DeduplicationResult, PersistenceReceipt]:
            repository = PostgresStoryRepository(
                session,
                embedding_model=embedding_provider.model_name,
                embedding_revision=embedding_provider.model_revision,
                embedding_provider=embedding_provider,
            )
            result = DeduplicationService(repository, embedding_provider, config).process(observation)
            command = observation_persistence_command(
                observation,
                result,
                raw_ocr_id=raw_ocr_id,
                embedding_model=embedding_provider.model_name,
                embedding_revision=embedding_provider.model_revision,
                embedding_dimensions=embedding_provider.dimensions,
            )
            receipt = self._apply_observation_command(session, command, replayed=False)
            return result, receipt

        return self._run_transaction(operation)

    def persist_command(self, command: PersistenceCommand, *, replayed: bool = False) -> PersistenceReceipt:
        def operation(session: Session) -> PersistenceReceipt:
            if command.kind == "observation":
                return self._apply_observation_command(session, command, replayed=replayed)
            if command.kind == "translation":
                return self._apply_translation_command(session, command, replayed=replayed)
            if command.kind == "ocr":
                return self._apply_ocr_command(session, command, replayed=replayed)
            if command.kind == "segmented_unit":
                return self._apply_segmented_unit_command(session, command, replayed=replayed)
            raise PersistenceError(f"Unsupported persistence command: {command.kind}")

        return self._run_transaction(operation)

    def persist_translation(self, result: TranslationResult) -> PersistenceReceipt:
        return self.persist_command(translation_persistence_command(result))

    def persist_ocr(self, result: OCRFrameResult) -> PersistenceReceipt:
        return self.persist_command(ocr_persistence_command(result))

    def persist_segmented_unit(self, unit: SegmentedTextUnit) -> PersistenceReceipt:
        return self.persist_command(segmented_unit_persistence_command(unit))

    def _apply_observation_command(
        self, session: Session, command: PersistenceCommand, *, replayed: bool
    ) -> PersistenceReceipt:
        observation = command.payload["observation"]
        result_payload = command.payload["result"]
        story_payload = result_payload.get("story")
        config = DeduplicationConfig.from_env()
        repository = PostgresStoryRepository(
            session,
            embedding_model=str(result_payload.get("embedding_model") or config.model_name),
            embedding_revision=str(result_payload.get("embedding_revision") or config.model_revision),
        )
        actual_story_id: uuid.UUID | None = None
        if story_payload:
            story = _story_from_payload(story_payload)
            primary_hash = story_features(story.original_text).exact_hash
            existing_same_hash = session.scalar(
                select(Sentence).where(
                    Sentence.calendar_date == story.calendar_date,
                    Sentence.exact_hash == primary_hash,
                )
            )
            if existing_same_hash and str(existing_same_hash.id) != story.story_id:
                actual_story_id = existing_same_hash.id
                story.story_id = str(existing_same_hash.id)
                repository.update_story(story)
            elif session.get(Sentence, _uuid(story.story_id)):
                repository.update_story(story)
                actual_story_id = _uuid(story.story_id)
            else:
                repository.create_story(story)
                actual_story_id = _uuid(story.story_id)

        occurrence = _occurrence_from_payload(result_payload["occurrence"])
        if actual_story_id is not None:
            occurrence = StoryOccurrence(
                **{**asdict(occurrence), "story_id": str(actual_story_id)}
            )
        repository.add_occurrence(occurrence)
        if result_payload.get("review_case"):
            repository.add_review(_review_from_payload(result_payload["review_case"]))
        session.flush()

        occurrence_row = session.scalar(
            select(SentenceOccurrence).where(
                SentenceOccurrence.observation_id == observation["observation_id"]
            )
        )
        if occurrence_row is None:
            raise PersistenceError("Occurrence was not persisted")
        occurrence_row.unit_id = observation.get("unit_id")
        occurrence_row.snapshot_version = observation.get("snapshot_version")
        occurrence_row.urgency = observation.get("urgency") or "normal"
        occurrence_row.review_required = bool(observation.get("review_required"))
        occurrence_row.raw_ocr_id = _uuid(observation.get("raw_ocr_id"))
        if actual_story_id is not None:
            occurrence_row.sentence_id = actual_story_id

        sentence_id = occurrence_row.sentence_id
        if sentence_id is not None:
            self._apply_classification_evidence(session, sentence_id, observation)
            self._apply_keyword_evidence(session, sentence_id, observation)
            self._apply_source_occurrence(session, sentence_id, occurrence_row)

        source_language = observation["language"]
        if source_language in {"en", "ur"}:
            target_language = "en" if source_language == "ur" else "ur"
            model_name, revision = (
                (UR_EN_MODEL, UR_EN_REVISION)
                if (source_language, target_language) == ("ur", "en")
                else (EN_UR_MODEL, EN_UR_REVISION)
            )
            pending = session.scalar(
                select(OccurrenceTranslation).where(
                    OccurrenceTranslation.occurrence_id == occurrence_row.id
                )
            )
            if pending is None:
                session.add(
                    OccurrenceTranslation(
                        occurrence_id=occurrence_row.id,
                        source_language=source_language,
                        target_language=target_language,
                        source_text=observation["text"],
                        status="pending",
                        model_name=model_name,
                        model_revision=revision,
                        engine="pending-local-cpu",
                        quality_checks={},
                        cache_hit=False,
                    )
                )

        if result_payload.get("review_case"):
            review_row = session.scalar(
                select(DedupReviewCase).where(
                    DedupReviewCase.observation_id == observation["observation_id"]
                )
            )
            if review_row:
                review_row.occurrence_id = occurrence_row.id

        trace = self._upsert_trace(
            session,
            trace_key=f"observation:{observation['observation_id']}",
            trace_type="observation",
            status="replayed" if replayed else "persisted",
            stream_id=_uuid(observation["stream_id"]),
            observed_at=_datetime(observation["observed_at"]),
            unit_id=observation.get("unit_id"),
            observation_id=observation["observation_id"],
            raw_ocr_id=_uuid(observation.get("raw_ocr_id")),
            occurrence_id=occurrence_row.id,
            sentence_id=sentence_id,
            detail={
                "command_id": command.command_id,
                "dedup_action": result_payload["action"],
                "dedup_layer": result_payload["dedup_layer"],
                "summary_eligible": result_payload["summary_eligible"],
                "category_ids": occurrence_row.category_ids,
                "keyword_ids": occurrence_row.keyword_ids,
            },
        )

        for event in result_payload.get("emitted_events", []):
            event_type = str(event.get("event", "pipeline_event"))
            aggregate_id = (
                str(sentence_id)
                if event_type.startswith("canonical_story") and sentence_id
                else observation["observation_id"]
            )
            self._add_outbox(
                session,
                event_key=f"{event_type}:{observation['observation_id']}",
                event_type=event_type,
                aggregate_type="sentence" if event_type.startswith("canonical_story") else "observation",
                aggregate_id=aggregate_id,
                payload=event.get("data", event),
            )

        if result_payload.get("summary_eligible") and sentence_id is not None:
            self._enqueue_job(
                session,
                job_type="summarize_story",
                payload={"sentence_id": str(sentence_id)},
                priority=60,
                dedup_key=f"summarize_story:{sentence_id}",
            )

        urgency = observation.get("urgency") or "normal"
        if urgency in {"high", "critical"} and sentence_id is not None:
            first_keyword = next(iter(observation.get("keyword_hits", [])), None)
            existing_alert = session.scalar(
                select(AlertLog.id).where(
                    AlertLog.sentence_id == sentence_id,
                    AlertLog.severity == urgency,
                )
            )
            if existing_alert is None:
                session.add(
                    AlertLog(
                        sentence_id=sentence_id,
                        keyword_id=_uuid(first_keyword["keyword_id"]) if first_keyword else None,
                        severity=urgency,
                    )
                )
        self._notify_outbox(session)
        return PersistenceReceipt(
            command_id=command.command_id,
            kind=command.kind,
            status="replayed" if replayed else "persisted",
            observation_id=observation["observation_id"],
            occurrence_id=str(occurrence_row.id),
            sentence_id=str(sentence_id) if sentence_id else None,
        )

    def _apply_classification_evidence(
        self, session: Session, sentence_id: uuid.UUID, observation: dict[str, Any]
    ) -> None:
        for decision in observation.get("category_decisions", []):
            if not decision.get("accepted"):
                continue
            category_id = decision["category_id"]
            link = session.get(
                SentenceCategory,
                {"sentence_id": sentence_id, "category_id": category_id},
            )
            evidence = {
                "reason": decision.get("reason"),
                "supporting_context": decision.get("supporting_context", []),
                "excluded_context": decision.get("excluded_context", []),
                "keyword_ids": decision.get("keyword_ids", []),
            }
            if link is None:
                session.add(
                    SentenceCategory(
                        sentence_id=sentence_id,
                        category_id=category_id,
                        classification_score=float(decision.get("score", 1.0)),
                        classifier_version=observation.get("snapshot_version") or "unknown",
                        evidence=evidence,
                    )
                )
            else:
                link.classification_score = max(
                    link.classification_score, float(decision.get("score", 1.0))
                )
                link.classifier_version = observation.get("snapshot_version") or link.classifier_version
                link.evidence = evidence

    def _apply_keyword_evidence(
        self, session: Session, sentence_id: uuid.UUID, observation: dict[str, Any]
    ) -> None:
        for hit in observation.get("keyword_hits", []):
            keyword_id = _uuid(hit["keyword_id"])
            link = session.get(
                SentenceKeyword,
                {"sentence_id": sentence_id, "keyword_id": keyword_id},
            )
            if link is None:
                session.add(
                    SentenceKeyword(
                        sentence_id=sentence_id,
                        keyword_id=keyword_id,
                        match_type="contextual" if hit.get("requires_context") else "exact",
                        match_score=1.0,
                        matched_text=hit.get("matched_text") or hit.get("term") or "",
                        text_span={
                            "normalized": hit.get("normalized_span", []),
                            "original": hit.get("original_span", []),
                        },
                    )
                )

    def _apply_source_occurrence(
        self, session: Session, sentence_id: uuid.UUID, occurrence: SentenceOccurrence
    ) -> None:
        source = session.scalar(
            select(SentenceSource).where(
                SentenceSource.sentence_id == sentence_id,
                SentenceSource.stream_id == occurrence.stream_id,
            )
        )
        if source is None:
            session.add(
                SentenceSource(
                    sentence_id=sentence_id,
                    stream_id=occurrence.stream_id,
                    raw_ocr_id=occurrence.raw_ocr_id,
                    first_seen_at=occurrence.observed_at,
                    last_seen_at=occurrence.observed_at,
                    occurrence_count=1,
                    source_text=occurrence.source_text,
                )
            )
        else:
            source.first_seen_at = min(source.first_seen_at, occurrence.observed_at)
            source.last_seen_at = max(source.last_seen_at, occurrence.observed_at)
            count = session.scalar(
                select(func.count(SentenceOccurrence.id)).where(
                    SentenceOccurrence.sentence_id == sentence_id,
                    SentenceOccurrence.stream_id == occurrence.stream_id,
                )
            )
            source.occurrence_count = int(count or 1)
            source.source_text = occurrence.source_text
            source.raw_ocr_id = occurrence.raw_ocr_id or source.raw_ocr_id

    def _apply_translation_command(
        self, session: Session, command: PersistenceCommand, *, replayed: bool
    ) -> PersistenceReceipt:
        payload = command.payload
        occurrence = session.scalar(
            select(SentenceOccurrence).where(
                SentenceOccurrence.observation_id == payload["observation_id"]
            )
        )
        if occurrence is None:
            raise PersistenceError(
                f"Translation arrived before observation persistence: {payload['observation_id']}"
            )
        row = session.scalar(
            select(OccurrenceTranslation).where(
                OccurrenceTranslation.occurrence_id == occurrence.id
            )
        )
        if row is None:
            row = OccurrenceTranslation(
                occurrence_id=occurrence.id,
                source_language=payload["source_language"],
                target_language=payload["target_language"],
                source_text=payload["source_text"],
                model_name=payload["model_name"],
                model_revision=payload["model_revision"],
                engine=payload["engine"],
            )
            session.add(row)
        row.translated_text = payload.get("translated_text")
        row.status = payload["status"]
        row.model_name = payload["model_name"]
        row.model_revision = payload["model_revision"]
        row.engine = payload["engine"]
        row.quality_checks = payload.get("quality", {})
        row.latency_ms = payload.get("latency_ms")
        row.cache_hit = bool(payload.get("cache_hit"))
        row.failure_reason = payload.get("failure_reason")

        quality = payload.get("quality", {})
        translated = payload.get("translated_text")
        if translated and payload["status"] == "complete" and quality.get("accepted"):
            memory_hash = translation_memory_key(
                payload["source_text"], payload["source_language"], payload["target_language"]
            )
            memory = session.scalar(
                select(TranslationMemory).where(
                    TranslationMemory.source_hash == memory_hash,
                    TranslationMemory.source_language == payload["source_language"],
                    TranslationMemory.target_language == payload["target_language"],
                    TranslationMemory.model_revision == payload["model_revision"],
                )
            )
            if memory is None:
                session.add(
                    TranslationMemory(
                        source_hash=memory_hash,
                        source_language=payload["source_language"],
                        target_language=payload["target_language"],
                        source_text=payload["source_text"],
                        translated_text=translated,
                        model_name=payload["model_name"],
                        model_revision=payload["model_revision"],
                        engine=payload["engine"],
                        quality_checks=quality,
                        hit_count=1 if payload.get("cache_hit") else 0,
                        last_used_at=datetime.now(timezone.utc),
                    )
                )
            else:
                memory.translated_text = translated
                memory.quality_checks = quality
                memory.last_used_at = datetime.now(timezone.utc)
                if payload.get("cache_hit"):
                    memory.hit_count += 1

        if occurrence.sentence_id is not None:
            canonical = session.scalar(
                select(Translation).where(Translation.sentence_id == occurrence.sentence_id)
            )
            sentence = session.get(Sentence, occurrence.sentence_id)
            status = (
                "complete"
                if payload["status"] == "complete"
                else "failed"
                if payload["status"] == "failed"
                else "partial"
            )
            new_score = float(quality.get("score") or 0.0)
            is_display_sentence = bool(sentence and sentence.original_text == payload["source_text"])
            should_replace = (
                canonical is None
                or (payload["status"] == "complete" and quality.get("accepted") and (
                    is_display_sentence
                    or canonical.quality_score is None
                    or new_score >= canonical.quality_score
                ))
            )
            if should_replace:
                if canonical is None:
                    canonical = Translation(
                        sentence_id=occurrence.sentence_id,
                        translation_status=status,
                        source_language=payload["source_language"],
                    )
                    session.add(canonical)
                canonical.translation_status = status
                canonical.source_language = payload["source_language"]
                canonical.quality_score = new_score
                canonical.failure_reason = payload.get("failure_reason")
                if payload["source_language"] == "en":
                    canonical.english_text = payload["source_text"]
                    canonical.urdu_text = translated
                    canonical.model_ur = payload["model_name"]
                    canonical.model_revision_ur = payload["model_revision"]
                else:
                    canonical.urdu_text = payload["source_text"]
                    canonical.english_text = translated
                    canonical.model_en = payload["model_name"]
                    canonical.model_revision_en = payload["model_revision"]

        trace = session.scalar(
            select(PipelineTrace).where(
                PipelineTrace.trace_key == f"observation:{payload['observation_id']}"
            )
        )
        if trace:
            detail = dict(trace.detail or {})
            detail["translation"] = {
                "status": payload["status"],
                "model": payload["model_name"],
                "revision": payload["model_revision"],
                "latency_ms": payload.get("latency_ms"),
                "cache_hit": payload.get("cache_hit"),
            }
            trace.detail = detail
            if replayed:
                trace.status = "replayed"

        self._add_outbox(
            session,
            event_key=(
                f"translation_ready:{payload['observation_id']}:{payload['model_revision']}"
            ),
            event_type="translation_ready",
            aggregate_type="observation",
            aggregate_id=payload["observation_id"],
            payload=payload,
        )
        self._notify_outbox(session)
        return PersistenceReceipt(
            command_id=command.command_id,
            kind=command.kind,
            status="replayed" if replayed else "persisted",
            observation_id=payload["observation_id"],
            occurrence_id=str(occurrence.id),
            sentence_id=str(occurrence.sentence_id) if occurrence.sentence_id else None,
        )

    def _apply_ocr_command(
        self, session: Session, command: PersistenceCommand, *, replayed: bool
    ) -> PersistenceReceipt:
        payload = command.payload
        stream_id = _uuid(payload["stream_id"])
        frame_timestamp = _datetime(payload["frame_timestamp"])
        existing = session.scalar(
            select(RawOCRText).where(
                RawOCRText.stream_id == stream_id,
                RawOCRText.frame_timestamp == frame_timestamp,
                RawOCRText.frame_sha256 == payload["frame_sha256"],
            )
        )
        if existing is None:
            diagnostics = payload.get("diagnostics", {})
            existing = RawOCRText(
                stream_id=stream_id,
                frame_timestamp=frame_timestamp,
                raw_text=payload.get("raw_text") or "",
                normalized_text=payload.get("normalized_text") or "",
                confidence_score=float(payload.get("confidence", 0.0)),
                ocr_engine=" -> ".join(payload.get("engine_chain", [])) or "unknown",
                ocr_model="mixed-script-pipeline",
                frame_sha256=payload["frame_sha256"],
                perceptual_hash=None,
                frame_width=int(diagnostics.get("frame_width", 1)),
                frame_height=int(diagnostics.get("frame_height", 1)),
                regions=payload.get("lines", []),
                processing_metadata={
                    "frame_sequence": payload.get("frame_sequence"),
                    "processing_ms": payload.get("processing_ms"),
                    "fallback_used": payload.get("fallback_used"),
                    "duplicate_of_recent_frame": payload.get("duplicate_of_recent_frame"),
                    "duplicate_similarity": payload.get("duplicate_similarity"),
                    "review_required": payload.get("review_required"),
                    "skipped_downstream": payload.get("skipped_downstream"),
                    "diagnostics": diagnostics,
                },
                expires_at=frame_timestamp + timedelta(days=self.config.ocr_retention_days),
            )
            session.add(existing)
            session.flush()
        self._upsert_trace(
            session,
            trace_key=f"frame:{payload['stream_id']}:{payload['frame_sequence']}",
            trace_type="frame",
            status="replayed" if replayed else "persisted",
            stream_id=stream_id,
            observed_at=frame_timestamp,
            frame_sequence=int(payload["frame_sequence"]),
            raw_ocr_id=existing.id,
            detail={
                "command_id": command.command_id,
                "frame_sha256": payload["frame_sha256"],
                "confidence": payload.get("confidence"),
                "engine_chain": payload.get("engine_chain", []),
                "review_required": payload.get("review_required"),
                "skipped_downstream": payload.get("skipped_downstream"),
            },
        )
        return PersistenceReceipt(
            command_id=command.command_id,
            kind=command.kind,
            status="replayed" if replayed else "persisted",
        )

    def _apply_segmented_unit_command(
        self, session: Session, command: PersistenceCommand, *, replayed: bool
    ) -> PersistenceReceipt:
        payload = command.payload
        self._upsert_trace(
            session,
            trace_key=f"unit:{payload['unit_id']}",
            trace_type="unit",
            status="replayed" if replayed else "persisted",
            stream_id=_uuid(payload["stream_id"]),
            observed_at=_datetime(payload["last_seen_at"]),
            frame_sequence=int(payload["last_frame_sequence"]),
            unit_id=payload["unit_id"],
            detail={"command_id": command.command_id, **payload},
        )
        return PersistenceReceipt(
            command_id=command.command_id,
            kind=command.kind,
            status="replayed" if replayed else "persisted",
        )

    def _upsert_trace(
        self,
        session: Session,
        *,
        trace_key: str,
        trace_type: str,
        status: str,
        stream_id: uuid.UUID | None,
        observed_at: datetime,
        frame_sequence: int | None = None,
        unit_id: str | None = None,
        observation_id: str | None = None,
        raw_ocr_id: uuid.UUID | None = None,
        occurrence_id: uuid.UUID | None = None,
        sentence_id: uuid.UUID | None = None,
        detail: dict[str, Any] | None = None,
    ) -> PipelineTrace:
        trace = session.scalar(select(PipelineTrace).where(PipelineTrace.trace_key == trace_key))
        if trace is None:
            trace = PipelineTrace(
                trace_key=trace_key,
                trace_type=trace_type,
                status=status,
                stream_id=stream_id,
                observed_at=observed_at,
                frame_sequence=frame_sequence,
                unit_id=unit_id,
                observation_id=observation_id,
                raw_ocr_id=raw_ocr_id,
                occurrence_id=occurrence_id,
                sentence_id=sentence_id,
                pipeline_version=PIPELINE_VERSION,
                detail=detail or {},
            )
            session.add(trace)
        else:
            trace.status = status
            trace.raw_ocr_id = raw_ocr_id or trace.raw_ocr_id
            trace.occurrence_id = occurrence_id or trace.occurrence_id
            trace.sentence_id = sentence_id or trace.sentence_id
            trace.detail = detail or trace.detail
        return trace

    def _add_outbox(
        self,
        session: Session,
        *,
        event_key: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> OutboxEvent:
        existing = session.scalar(select(OutboxEvent).where(OutboxEvent.event_key == event_key))
        if existing:
            return existing
        row = OutboxEvent(
            event_key=event_key,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload,
            available_at=datetime.now(timezone.utc),
            max_attempts=self.config.outbox_max_attempts,
        )
        session.add(row)
        return row

    def _enqueue_job(
        self,
        session: Session,
        *,
        job_type: str,
        payload: dict[str, Any],
        priority: int,
        dedup_key: str,
    ) -> ProcessingJob:
        existing = session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.dedup_key == dedup_key,
                ProcessingJob.status.in_(["queued", "running"]),
            )
        )
        if existing:
            return existing
        row = ProcessingJob(
            job_type=job_type,
            payload=payload,
            priority=priority,
            status="queued",
            dedup_key=dedup_key,
        )
        session.add(row)
        return row

    @staticmethod
    def _notify_outbox(session: Session) -> None:
        # Notification is delivered only after the surrounding transaction commits.
        session.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": OUTBOX_CHANNEL, "payload": "new"})

    def load_day_stories(
        self,
        calendar_date: date,
        embedding_provider: EmbeddingProvider,
        *,
        limit: int = 5000,
    ) -> list[CanonicalStory]:
        with self.session_factory() as session, session.begin():
            repository = PostgresStoryRepository(
                session,
                embedding_model=embedding_provider.model_name,
                embedding_revision=embedding_provider.model_revision,
                embedding_provider=embedding_provider,
            )
            rows = list(
                session.scalars(
                    select(Sentence)
                    .where(Sentence.calendar_date == calendar_date, Sentence.review_status != "rejected")
                    .order_by(Sentence.last_seen_at.desc())
                    .limit(limit)
                )
            )
            return [repository._to_story(row) for row in rows]

    def purge_expired_ocr(self, *, now: datetime | None = None) -> int:
        cutoff = now or datetime.now(timezone.utc)
        with self.session_factory() as session, session.begin():
            result = session.execute(delete(RawOCRText).where(RawOCRText.expires_at < cutoff))
            return int(result.rowcount or 0)


class PostgresReadRepository:
    """Read models used later by REST/WebSocket APIs without collapsing repeated observations."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self.session_factory = session_factory or build_session_factory()

    @staticmethod
    def live_window_statement(*, cutoff: datetime, limit: int = 5000):
        return (
            select(SentenceOccurrence, Stream, OccurrenceTranslation)
            .join(Stream, Stream.id == SentenceOccurrence.stream_id)
            .outerjoin(
                OccurrenceTranslation,
                OccurrenceTranslation.occurrence_id == SentenceOccurrence.id,
            )
            .where(
                SentenceOccurrence.emitted_live.is_(True),
                SentenceOccurrence.observed_at >= cutoff,
            )
            .order_by(SentenceOccurrence.observed_at.desc(), SentenceOccurrence.created_at.desc())
            .limit(max(1, min(limit, 20_000)))
        )

    def load_live_window(
        self,
        *,
        now: datetime | None = None,
        window_minutes: int = 30,
        limit: int = 5000,
    ) -> list[LiveObservationRecord]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = current - timedelta(minutes=max(1, window_minutes))
        with self.session_factory() as session:
            rows = session.execute(
                self.live_window_statement(cutoff=cutoff, limit=limit)
            ).all()
        records: list[LiveObservationRecord] = []
        for occurrence, stream, translation in rows:
            records.append(
                LiveObservationRecord(
                    occurrence_id=str(occurrence.id),
                    observation_id=occurrence.observation_id,
                    canonical_story_id=str(occurrence.sentence_id) if occurrence.sentence_id else None,
                    stream_id=str(occurrence.stream_id),
                    channel_name=stream.channel_name,
                    observed_at=occurrence.observed_at,
                    source_text=occurrence.source_text,
                    source_language=occurrence.language,
                    confidence=occurrence.confidence_score,
                    category_ids=tuple(occurrence.category_ids or ()),
                    keyword_ids=tuple(occurrence.keyword_ids or ()),
                    urgency=occurrence.urgency,
                    review_required=occurrence.review_required,
                    decision=occurrence.decision,
                    dedup_layer=occurrence.dedup_layer,
                    translation_status=translation.status if translation else "pending",
                    target_language=translation.target_language if translation else None,
                    translated_text=translation.translated_text if translation else None,
                )
            )
        return records


class ResilientPersistencePipeline:
    """PostgreSQL-first pipeline with an in-memory outage mirror and durable disk spool."""

    def __init__(
        self,
        persistence: PostgresPersistenceService,
        embedding_provider: EmbeddingProvider,
        *,
        dedup_config: DeduplicationConfig | None = None,
        spool: DurableCommandSpool | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.persistence = persistence
        self.embedding_provider = embedding_provider
        self.dedup_config = dedup_config or DeduplicationConfig.from_env()
        self.spool = spool or DurableCommandSpool(persistence.config)
        self.event_sink = event_sink or (lambda event: None)
        self._fallback_repository = InMemoryStoryRepository()
        self._fallback_service = DeduplicationService(
            self._fallback_repository, self.embedding_provider, self.dedup_config
        )
        self._fallback_lock = threading.RLock()
        self._warmed_dates: set[date] = set()
        self.database_failures = 0
        self.spooled_commands = 0

    def _warm_fallback(self, calendar_date: date) -> None:
        with self._fallback_lock:
            if calendar_date in self._warmed_dates:
                return
            stories = self.persistence.load_day_stories(
                calendar_date, self.embedding_provider, limit=self.dedup_config.candidate_limit
            )
            for story in stories:
                cloned = copy.deepcopy(story)
                if cloned.story_id in self._fallback_repository.stories:
                    self._fallback_repository.update_story(cloned)
                else:
                    self._fallback_repository.create_story(cloned)
            self._warmed_dates.add(calendar_date)

    def _mirror_result(self, result: DeduplicationResult) -> None:
        with self._fallback_lock:
            if result.story is not None:
                story = copy.deepcopy(result.story)
                if story.story_id in self._fallback_repository.stories:
                    self._fallback_repository.update_story(story)
                else:
                    self._fallback_repository.create_story(story)
            if not any(item.observation_id == result.occurrence.observation_id for item in self._fallback_repository.occurrences):
                self._fallback_repository.add_occurrence(copy.deepcopy(result.occurrence))
            if result.review_case and not any(item.observation_id == result.review_case.observation_id for item in self._fallback_repository.reviews):
                self._fallback_repository.add_review(copy.deepcopy(result.review_case))

    def process(
        self, observation: DetectionObservation, *, raw_ocr_id: str | None = None
    ) -> tuple[DeduplicationResult, PersistenceReceipt]:
        calendar_date = pakistan_calendar_date(observation.observed_at)
        try:
            self._warm_fallback(calendar_date)
            result, receipt = self.persistence.process_observation(
                observation,
                self.embedding_provider,
                self.dedup_config,
                raw_ocr_id=raw_ocr_id,
            )
            self._mirror_result(result)
            for event in result.emitted_events:
                self.event_sink({**event, "persistence": "committed"})
            return result, receipt
        except Exception as exc:
            if not _transient_database_error(exc):
                raise
            self.database_failures += 1
            with self._fallback_lock:
                result = self._fallback_service.process(observation)
            command = observation_persistence_command(
                observation,
                result,
                raw_ocr_id=raw_ocr_id,
                embedding_model=self.embedding_provider.model_name,
                embedding_revision=self.embedding_provider.model_revision,
                embedding_dimensions=self.embedding_provider.dimensions,
            )
            path = self.spool.enqueue(command, last_error=f"{exc.__class__.__name__}: {exc}")
            self.spooled_commands += 1
            for event in result.emitted_events:
                self.event_sink({**event, "persistence": "spooled"})
            return result, PersistenceReceipt(
                command_id=command.command_id,
                kind=command.kind,
                status="spooled",
                observation_id=observation.observation_id,
                occurrence_id=result.occurrence.occurrence_id,
                sentence_id=result.story.story_id if result.story else None,
                spooled_path=str(path),
            )

    def persist_command_resilient(
        self,
        command: PersistenceCommand,
        *,
        observation_id: str | None = None,
    ) -> PersistenceReceipt:
        try:
            return self.persistence.persist_command(command)
        except Exception as exc:
            if not _transient_database_error(exc):
                raise
            path = self.spool.enqueue(command, last_error=f"{exc.__class__.__name__}: {exc}")
            self.database_failures += 1
            self.spooled_commands += 1
            return PersistenceReceipt(
                command_id=command.command_id,
                kind=command.kind,
                status="spooled",
                observation_id=observation_id,
                spooled_path=str(path),
            )

    def persist_translation(self, result: TranslationResult) -> PersistenceReceipt:
        return self.persist_command_resilient(
            translation_persistence_command(result), observation_id=result.observation_id
        )

    def persist_ocr(self, result: OCRFrameResult) -> PersistenceReceipt:
        return self.persist_command_resilient(ocr_persistence_command(result))

    def persist_segmented_unit(self, unit: SegmentedTextUnit) -> PersistenceReceipt:
        return self.persist_command_resilient(segmented_unit_persistence_command(unit))

    def replay(self, *, maximum: int | None = None) -> dict[str, int]:
        processed = replay_spool(self.persistence, self.spool, maximum=maximum)
        return processed


class OutboxDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        config: PersistenceConfig | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.session_factory = session_factory or build_session_factory()
        self.config = config or PersistenceConfig.from_env()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def claim(self, limit: int | None = None) -> list[OutboxDelivery]:
        limit = limit or self.config.outbox_batch_size
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=self.config.outbox_lease_seconds)
        with self.session_factory() as session, session.begin():
            rows = list(
                session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.published_at.is_(None),
                        OutboxEvent.dead_lettered_at.is_(None),
                        OutboxEvent.available_at <= now,
                        or_(OutboxEvent.leased_until.is_(None), OutboxEvent.leased_until < now),
                    )
                    .order_by(OutboxEvent.created_at.asc())
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            deliveries: list[OutboxDelivery] = []
            for row in rows:
                row.leased_until = lease_until
                row.lease_owner = self.worker_id
                deliveries.append(
                    OutboxDelivery(
                        event_id=str(row.id),
                        event_key=row.event_key,
                        event_type=row.event_type,
                        aggregate_type=row.aggregate_type,
                        aggregate_id=row.aggregate_id,
                        payload=row.payload,
                        attempts=row.attempts,
                    )
                )
            return deliveries

    def mark_published(self, event_id: str) -> None:
        with self.session_factory() as session, session.begin():
            row = session.get(OutboxEvent, _uuid(event_id), with_for_update=True)
            if row is None:
                return
            row.published_at = datetime.now(timezone.utc)
            row.leased_until = None
            row.lease_owner = None
            row.last_error = None

    def mark_failed(self, event_id: str, error: BaseException | str) -> None:
        with self.session_factory() as session, session.begin():
            row = session.get(OutboxEvent, _uuid(event_id), with_for_update=True)
            if row is None:
                return
            row.attempts += 1
            row.last_error = str(error)[:4000]
            row.leased_until = None
            row.lease_owner = None
            if row.attempts >= row.max_attempts:
                row.dead_lettered_at = datetime.now(timezone.utc)
            else:
                delay = min(300, 2 ** min(row.attempts, 8))
                row.available_at = datetime.now(timezone.utc) + timedelta(seconds=delay)

    def dispatch_once(self, publisher: PersistencePublisher, limit: int | None = None) -> dict[str, int]:
        sent = failed = 0
        for delivery in self.claim(limit):
            try:
                publisher(delivery.event_type, delivery.payload)
            except Exception as exc:
                self.mark_failed(delivery.event_id, exc)
                failed += 1
            else:
                self.mark_published(delivery.event_id)
                sent += 1
        return {"published": sent, "failed": failed}


class PostgresJobQueue:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        config: PersistenceConfig | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.session_factory = session_factory or build_session_factory()
        self.config = config or PersistenceConfig.from_env()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def claim(self, job_types: Sequence[str], *, limit: int = 1) -> list[dict[str, Any]]:
        if not job_types:
            return []
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=self.config.job_lease_seconds)
        with self.session_factory() as session, session.begin():
            rows = list(
                session.scalars(
                    select(ProcessingJob)
                    .where(
                        ProcessingJob.job_type.in_(list(job_types)),
                        or_(
                            and_(
                                ProcessingJob.status == "queued",
                                ProcessingJob.available_at <= now,
                            ),
                            and_(
                                ProcessingJob.status == "running",
                                ProcessingJob.leased_until < now,
                            ),
                        ),
                    )
                    .order_by(ProcessingJob.priority.desc(), ProcessingJob.available_at.asc())
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            output: list[dict[str, Any]] = []
            for row in rows:
                row.status = "running"
                row.lease_owner = self.worker_id
                row.leased_until = lease_until
                row.attempts += 1
                output.append(
                    {
                        "id": str(row.id),
                        "job_type": row.job_type,
                        "payload": row.payload,
                        "attempts": row.attempts,
                    }
                )
            return output

    def succeed(self, job_id: str) -> None:
        with self.session_factory() as session, session.begin():
            row = session.get(ProcessingJob, _uuid(job_id), with_for_update=True)
            if row:
                row.status = "succeeded"
                row.completed_at = datetime.now(timezone.utc)
                row.leased_until = None
                row.lease_owner = None

    def fail(self, job_id: str, error: BaseException | str) -> None:
        with self.session_factory() as session, session.begin():
            row = session.get(ProcessingJob, _uuid(job_id), with_for_update=True)
            if row is None:
                return
            row.last_error = str(error)[:4000]
            row.leased_until = None
            row.lease_owner = None
            if row.attempts >= row.max_attempts:
                row.status = "dead_letter"
                row.completed_at = datetime.now(timezone.utc)
            else:
                row.status = "queued"
                row.available_at = datetime.now(timezone.utc) + timedelta(
                    seconds=min(300, 2 ** min(row.attempts, 8))
                )


def _story_from_payload(payload: dict[str, Any]) -> CanonicalStory:
    return CanonicalStory(
        story_id=payload["story_id"],
        calendar_date=_date(payload["calendar_date"]),
        original_text=payload["original_text"],
        normalized_text=payload["normalized_text"],
        original_language=payload["original_language"],
        category_ids=set(payload.get("category_ids", [])),
        first_seen_at=_datetime(payload["first_seen_at"]),
        last_seen_at=_datetime(payload["last_seen_at"]),
        occurrence_count=int(payload["occurrence_count"]),
        source_channels=set(payload.get("source_channels", [])),
        source_stream_ids=set(payload.get("source_stream_ids", [])),
        exact_hashes=set(payload.get("exact_hashes", [])),
        embedding=[float(item) for item in payload.get("embedding", [])],
        embedding_count=int(payload.get("embedding_count", 1)),
        confidence=float(payload["confidence"]),
        latest_text=payload["latest_text"],
        summary_state=payload.get("summary_state", "eligible"),
        review_status=payload.get("review_status", "accepted"),
    )


def _occurrence_from_payload(payload: dict[str, Any]) -> StoryOccurrence:
    return StoryOccurrence(
        occurrence_id=payload["occurrence_id"],
        observation_id=payload["observation_id"],
        story_id=payload.get("story_id"),
        stream_id=payload["stream_id"],
        channel_name=payload["channel_name"],
        observed_at=_datetime(payload["observed_at"]),
        calendar_date=_date(payload["calendar_date"]),
        source_text=payload["source_text"],
        normalized_text=payload["normalized_text"],
        language=payload["language"],
        confidence=float(payload["confidence"]),
        category_ids=tuple(payload.get("category_ids", [])),
        keyword_ids=tuple(payload.get("keyword_ids", [])),
        exact_hash=payload["exact_hash"],
        decision=payload["decision"],
        dedup_layer=payload["dedup_layer"],
        semantic_similarity=payload.get("semantic_similarity"),
        lexical_overlap=payload.get("lexical_overlap"),
        decision_metadata=payload.get("decision_metadata", {}),
        emitted_live=bool(payload.get("emitted_live", True)),
    )


def _review_from_payload(payload: dict[str, Any]) -> ReviewCase:
    return ReviewCase(
        review_id=payload["review_id"],
        observation_id=payload["observation_id"],
        candidate_story_id=payload["candidate_story_id"],
        calendar_date=_date(payload["calendar_date"]),
        semantic_similarity=float(payload["semantic_similarity"]),
        lexical_overlap=float(payload["lexical_overlap"]),
        evidence=payload.get("evidence", {}),
        status=payload.get("status", "pending"),
    )


def replay_spool(
    persistence: PostgresPersistenceService,
    spool: DurableCommandSpool,
    *,
    maximum: int | None = None,
) -> dict[str, int]:
    replayed = failed = corrupt = 0
    while maximum is None or replayed + failed + corrupt < maximum:
        try:
            item = spool.claim_next()
        except CorruptSpoolItemError:
            corrupt += 1
            continue
        if item is None:
            break
        try:
            persistence.persist_command(item.command, replayed=True)
        except Exception as exc:
            if _transient_database_error(exc):
                spool.reject(item, exc)
                failed += 1
                break
            spool.reject(item, exc, permanent=True)
            failed += 1
        else:
            spool.acknowledge(item)
            replayed += 1
    return {"replayed": replayed, "failed": failed, "corrupt": corrupt}


def persistence_doctor(*, check_database: bool = False) -> dict[str, Any]:
    config = PersistenceConfig.from_env()
    report: dict[str, Any] = {
        "status": "ready",
        "phase": 8,
        "python_policy": "CPython 3.12.x x64",
        "database": "postgresql",
        "redis": False,
        "durability": {
            "transactional_story_occurrence_translation_writes": True,
            "transactional_outbox": True,
            "outbox_skip_locked_leases": True,
            "postgres_job_queue": True,
            "checksum_verified_local_outage_spool": True,
            "raw_ocr_retention_days": config.ocr_retention_days,
            "no_raw_frame_storage": True,
        },
        "spool": {},
        "checks": {},
    }
    try:
        spool = DurableCommandSpool(config)
        probe = spool.root / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report["spool"] = spool.stats()
        report["checks"]["spool_writable"] = True
    except Exception as exc:
        report["status"] = "not_ready"
        report["checks"]["spool_writable"] = False
        report["checks"]["spool_error"] = f"{exc.__class__.__name__}: {exc}"
    if check_database:
        try:
            factory = build_session_factory()
            with factory() as session:
                revision = session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
                session.execute(text("SELECT 1"))
            report["checks"]["postgresql"] = "ready"
            report["checks"]["schema_revision"] = revision
            if revision not in {"20260719_0004", "20260719_0005", "20260720_0006"}:
                report["status"] = "not_ready"
        except Exception as exc:
            report["status"] = "not_ready"
            report["checks"]["postgresql"] = f"unavailable: {exc.__class__.__name__}"
    else:
        try:
            get_database_url()
            report["checks"]["database_url"] = "configured"
        except Exception:
            report["checks"]["database_url"] = "not_configured"
    return report
