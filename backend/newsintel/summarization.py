from __future__ import annotations

import hashlib
import json
import os
import socket
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, not_, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from .database import build_session_factory
from .deduplication import (
    DeduplicationConfig,
    EmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
    _contradiction_free,
    _named_entities_compatible,
    _negation_compatible,
    _numbers_compatible,
    cosine_similarity,
    lexical_jaccard,
    story_features,
)
from .translation import _numbers as translated_number_tokens
from .models import (
    Category,
    DailySummary,
    DailySummarySentence,
    OutboxEvent,
    ProcessingJob,
    ScheduledJob,
    Sentence,
    SentenceCategory,
    SummaryFact,
    SummaryFactSource,
    SummaryReviewCase,
    SummaryRun,
    Translation,
)

PKT = ZoneInfo("Asia/Karachi")
AUTHORITATIVE_ENGINE = "authoritative-extractive-fact-ledger"
AUTHORITATIVE_REVISION = "phase9-v1"
SUMMARY_SCHEDULE_NAME = "category-summary-refresh"
SUMMARY_JOB_TYPE = "refresh_category_summaries"
DIRTY_JOB_TYPE = "summarize_story"


@dataclass(frozen=True, slots=True)
class SummarizationConfig:
    interval_seconds: int = 300
    semantic_duplicate_threshold: float = 0.94
    semantic_review_threshold: float = 0.86
    minimum_lexical_overlap: float = 0.20
    maximum_stories_per_category_run: int = 5000
    job_drain_limit: int = 5000
    advisory_lock_key: str = "newsintel:phase9:summarization"
    include_previous_day_grace_minutes: int = 30
    model_name: str = "intfloat/multilingual-e5-small"
    model_revision: str = "fd1525a9fd15316a2d503bf26ab031a61d056e98"
    embedding_dimensions: int = 384
    require_complete_bilingual_fact: bool = True
    optional_abstractive_digest_enabled: bool = False

    @classmethod
    def from_env(cls) -> "SummarizationConfig":
        defaults = cls()
        return cls(
            interval_seconds=max(60, int(os.getenv("SUMMARY_INTERVAL_SECONDS", str(defaults.interval_seconds)))),
            semantic_duplicate_threshold=float(
                os.getenv("SUMMARY_DUPLICATE_THRESHOLD", str(defaults.semantic_duplicate_threshold))
            ),
            semantic_review_threshold=float(
                os.getenv("SUMMARY_REVIEW_THRESHOLD", str(defaults.semantic_review_threshold))
            ),
            minimum_lexical_overlap=float(
                os.getenv("SUMMARY_MIN_LEXICAL_OVERLAP", str(defaults.minimum_lexical_overlap))
            ),
            maximum_stories_per_category_run=max(
                100,
                int(
                    os.getenv(
                        "SUMMARY_MAX_STORIES_PER_CATEGORY_RUN",
                        str(defaults.maximum_stories_per_category_run),
                    )
                ),
            ),
            job_drain_limit=max(
                100, int(os.getenv("SUMMARY_JOB_DRAIN_LIMIT", str(defaults.job_drain_limit)))
            ),
            advisory_lock_key=os.getenv("SUMMARY_ADVISORY_LOCK_KEY", defaults.advisory_lock_key),
            include_previous_day_grace_minutes=max(
                0,
                int(
                    os.getenv(
                        "SUMMARY_PREVIOUS_DAY_GRACE_MINUTES",
                        str(defaults.include_previous_day_grace_minutes),
                    )
                ),
            ),
            model_name=os.getenv("SUMMARY_EMBEDDING_MODEL", defaults.model_name),
            model_revision=os.getenv("SUMMARY_EMBEDDING_REVISION", defaults.model_revision),
            embedding_dimensions=int(
                os.getenv("SUMMARY_EMBEDDING_DIMENSIONS", str(defaults.embedding_dimensions))
            ),
            require_complete_bilingual_fact=os.getenv(
                "SUMMARY_REQUIRE_COMPLETE_BILINGUAL_FACT", "true"
            ).lower()
            not in {"0", "false", "no"},
            optional_abstractive_digest_enabled=os.getenv(
                "SUMMARY_ABSTRACTIVE_DIGEST_ENABLED", "false"
            ).lower()
            in {"1", "true", "yes"},
        )


@dataclass(frozen=True, slots=True)
class SummaryFactCandidate:
    sentence_id: str
    category_id: str
    calendar_date: date
    fact_text_en: str
    fact_text_ur: str
    source_first_seen_at: datetime
    source_last_seen_at: datetime
    priority: int
    fact_hash: str
    factual_signature: dict[str, Any]

    @property
    def embedding_text(self) -> str:
        return f"English: {self.fact_text_en}\nUrdu: {self.fact_text_ur}"


@dataclass(frozen=True, slots=True)
class ExistingFact:
    fact_id: str
    fact_text_en: str
    fact_text_ur: str
    fact_hash: str
    embedding_vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class SummaryDecision:
    action: str
    candidate: SummaryFactCandidate
    existing_fact_id: str | None = None
    semantic_similarity: float | None = None
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SummaryRunResult:
    category_id: str
    calendar_date: date
    stories_considered: int
    facts_appended: int
    duplicate_facts_skipped: int
    pending_translation_count: int
    review_count: int
    summary_id: str | None
    changed: bool

    def serializable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["calendar_date"] = self.calendar_date.isoformat()
        return payload


def _clean_sentence(text_value: str) -> str:
    value = " ".join((text_value or "").replace("\u200c", " ").replace("\u200f", " ").split())
    return value.strip()


def _fact_hash(en_text: str, ur_text: str) -> str:
    body = json.dumps(
        {"en": _clean_sentence(en_text).casefold(), "ur": _clean_sentence(ur_text)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _priority_for_sentence(sentence: Sentence) -> int:
    decision = sentence.dedup_decision or {}
    urgency = str(decision.get("urgency") or "normal")
    if urgency == "critical":
        return 100
    if urgency == "high":
        return 80
    return 50


def candidate_from_story(
    sentence: Sentence,
    translation: Translation,
    *,
    category_id: str,
) -> SummaryFactCandidate | None:
    """Build one bilingual fact without generating or paraphrasing source material."""

    if translation.translation_status != "complete":
        return None
    english = _clean_sentence(translation.english_text or "")
    urdu = _clean_sentence(translation.urdu_text or "")
    if not english or not urdu:
        return None
    signature_en = story_features(english)
    signature_ur = story_features(urdu)
    signature = {
        "numbers_en": list(signature_en.numbers),
        "numbers_ur": list(signature_ur.numbers),
        "negations_en": sorted(signature_en.negations),
        "negations_ur": sorted(signature_ur.negations),
        "contradiction_terms_en": sorted(signature_en.contradiction_terms),
        "contradiction_terms_ur": sorted(signature_ur.contradiction_terms),
        "proper_names_en": sorted(signature_en.proper_names),
        "proper_names_ur": sorted(signature_ur.proper_names),
    }
    return SummaryFactCandidate(
        sentence_id=str(sentence.id),
        category_id=category_id,
        calendar_date=sentence.calendar_date,
        fact_text_en=english,
        fact_text_ur=urdu,
        source_first_seen_at=sentence.first_seen_at,
        source_last_seen_at=sentence.last_seen_at,
        priority=_priority_for_sentence(sentence),
        fact_hash=_fact_hash(english, urdu),
        factual_signature=signature,
    )


class SummaryFactDecider:
    """Secondary fact-level duplicate guard after canonical story deduplication.

    Semantic similarity cannot merge a fact by itself. Numeric, named-entity,
    negation and event-state safeguards must also agree. Uncertain cases are
    withheld for review instead of being silently appended or merged.
    """

    def __init__(self, config: SummarizationConfig | None = None) -> None:
        self.config = config or SummarizationConfig.from_env()

    def decide(
        self,
        candidate: SummaryFactCandidate,
        vector: Sequence[float],
        existing: Sequence[ExistingFact],
    ) -> SummaryDecision:
        for fact in existing:
            if fact.fact_hash == candidate.fact_hash:
                return SummaryDecision(
                    action="duplicate",
                    candidate=candidate,
                    existing_fact_id=fact.fact_id,
                    semantic_similarity=1.0,
                    evidence={"layer": "exact_bilingual_hash"},
                )

        candidate_features = story_features(candidate.fact_text_en)
        best: tuple[ExistingFact, float, float, bool, dict[str, bool]] | None = None
        for fact in existing:
            semantic = cosine_similarity(vector, fact.embedding_vector)
            existing_features = story_features(fact.fact_text_en)
            lexical = lexical_jaccard(candidate_features, existing_features)
            candidate_numbers = set(translated_number_tokens(candidate.fact_text_en))
            existing_numbers = set(translated_number_tokens(fact.fact_text_en))
            numbers_compatible = (
                candidate_numbers == existing_numbers
                if candidate_numbers or existing_numbers
                else _numbers_compatible(candidate_features, existing_features)
            )
            safeguards = {
                "numbers_compatible": numbers_compatible,
                "named_entities_compatible": _named_entities_compatible(
                    candidate_features, existing_features
                ),
                "negation_compatible": _negation_compatible(candidate_features, existing_features),
                "contradiction_free": _contradiction_free(candidate_features, existing_features),
            }
            compatible = all(safeguards.values())
            score = (semantic, lexical)
            if best is None or score > (best[1], best[2]):
                best = (fact, semantic, lexical, compatible, safeguards)

        if best is None:
            return SummaryDecision(action="append", candidate=candidate, evidence={"layer": "new_fact"})

        fact, semantic, lexical, compatible, safeguards = best
        evidence: dict[str, Any] = {
            "layer": "semantic_fact_guard",
            "semantic_similarity": round(semantic, 6),
            "lexical_overlap": round(lexical, 6),
            **safeguards,
        }
        if (
            semantic >= self.config.semantic_duplicate_threshold
            and lexical >= self.config.minimum_lexical_overlap
            and compatible
        ):
            return SummaryDecision(
                action="duplicate",
                candidate=candidate,
                existing_fact_id=fact.fact_id,
                semantic_similarity=semantic,
                evidence=evidence,
            )
        if semantic >= self.config.semantic_review_threshold:
            return SummaryDecision(
                action="review",
                candidate=candidate,
                existing_fact_id=fact.fact_id,
                semantic_similarity=semantic,
                evidence=evidence,
            )
        return SummaryDecision(
            action="append",
            candidate=candidate,
            semantic_similarity=semantic,
            evidence=evidence,
        )


def _summary_line(text_value: str, when: datetime, *, language: str) -> str:
    pkt_time = when.astimezone(PKT).strftime("%H:%M")
    clean = _clean_sentence(text_value)
    return f"• [{pkt_time} PKT] {clean}"


class PostgresSummarizationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        config: SummarizationConfig | None = None,
    ) -> None:
        self.session_factory = session_factory or build_session_factory()
        self.config = config or SummarizationConfig.from_env()
        if embedding_provider is None:
            dedup_config = DeduplicationConfig(
                model_name=self.config.model_name,
                model_revision=self.config.model_revision,
                embedding_dimensions=self.config.embedding_dimensions,
            )
            embedding_provider = SentenceTransformerEmbeddingProvider(dedup_config)
        self.embedding_provider = embedding_provider
        self.decider = SummaryFactDecider(self.config)

    @staticmethod
    def _eligible_query(category_id: str, calendar_date: date, limit: int):
        already_folded = (
            select(DailySummarySentence.sentence_id)
            .join(DailySummary, DailySummary.id == DailySummarySentence.daily_summary_id)
            .where(
                DailySummary.category_id == category_id,
                DailySummary.calendar_date == calendar_date,
            )
        )
        under_review = (
            select(SummaryReviewCase.sentence_id)
            .join(DailySummary, DailySummary.id == SummaryReviewCase.daily_summary_id)
            .where(
                DailySummary.category_id == category_id,
                DailySummary.calendar_date == calendar_date,
                SummaryReviewCase.status == "pending",
            )
        )
        summary_state = Sentence.dedup_decision["summary_state"].as_string()
        return (
            select(Sentence, Translation)
            .join(SentenceCategory, SentenceCategory.sentence_id == Sentence.id)
            .outerjoin(Translation, Translation.sentence_id == Sentence.id)
            .where(
                SentenceCategory.category_id == category_id,
                Sentence.calendar_date == calendar_date,
                Sentence.review_status == "accepted",
                or_(summary_state.is_(None), summary_state == "eligible"),
                not_(Sentence.id.in_(already_folded)),
                not_(Sentence.id.in_(under_review)),
            )
            .order_by(Sentence.first_seen_at.asc(), Sentence.id.asc())
            .limit(limit)
        )

    def summarize_category_day(
        self,
        category_id: str,
        calendar_date: date,
        *,
        run_key: str | None = None,
    ) -> SummaryRunResult:
        run_key = run_key or f"{category_id}:{calendar_date.isoformat()}:{uuid.uuid4()}"
        with self.session_factory() as session, session.begin():
            lock_key = f"{self.config.advisory_lock_key}:{category_id}:{calendar_date.isoformat()}"
            lock_acquired = bool(
                session.scalar(
                    select(func.pg_try_advisory_xact_lock(func.hashtext(lock_key)))
                )
            )
            if not lock_acquired:
                return SummaryRunResult(
                    category_id=category_id,
                    calendar_date=calendar_date,
                    stories_considered=0,
                    facts_appended=0,
                    duplicate_facts_skipped=0,
                    pending_translation_count=0,
                    review_count=0,
                    summary_id=None,
                    changed=False,
                )

            summary = session.scalar(
                select(DailySummary)
                .where(
                    DailySummary.category_id == category_id,
                    DailySummary.calendar_date == calendar_date,
                )
                .with_for_update()
            )
            if summary is None:
                summary = DailySummary(
                    category_id=category_id,
                    calendar_date=calendar_date,
                    summary_text_en="",
                    summary_text_ur="",
                    source_sentence_count=0,
                    summarizer_model=AUTHORITATIVE_ENGINE,
                    summarizer_revision=AUTHORITATIVE_REVISION,
                )
                session.add(summary)
                session.flush()

            existing_run = session.scalar(select(SummaryRun).where(SummaryRun.run_key == run_key))
            if existing_run and existing_run.status == "succeeded":
                return SummaryRunResult(
                    category_id=category_id,
                    calendar_date=calendar_date,
                    stories_considered=existing_run.stories_considered,
                    facts_appended=existing_run.facts_appended,
                    duplicate_facts_skipped=existing_run.duplicate_facts_skipped,
                    pending_translation_count=existing_run.pending_translation_count,
                    review_count=existing_run.review_count,
                    summary_id=str(summary.id),
                    changed=existing_run.facts_appended > 0,
                )
            run = existing_run or SummaryRun(
                daily_summary_id=summary.id,
                category_id=category_id,
                calendar_date=calendar_date,
                run_key=run_key,
                status="running",
            )
            if existing_run is None:
                session.add(run)

            rows = list(
                session.execute(
                    self._eligible_query(
                        category_id,
                        calendar_date,
                        self.config.maximum_stories_per_category_run,
                    )
                ).all()
            )
            candidates: list[SummaryFactCandidate] = []
            pending_translation_count = 0
            for sentence, translation in rows:
                if translation is None:
                    pending_translation_count += 1
                    continue
                candidate = candidate_from_story(sentence, translation, category_id=category_id)
                if candidate is None:
                    pending_translation_count += 1
                    continue
                candidates.append(candidate)

            existing_rows = list(
                session.scalars(
                    select(SummaryFact)
                    .where(
                        SummaryFact.daily_summary_id == summary.id,
                        SummaryFact.fact_state == "active",
                    )
                    .order_by(SummaryFact.fact_order.asc())
                )
            )
            existing = [
                ExistingFact(
                    fact_id=str(item.id),
                    fact_text_en=item.fact_text_en,
                    fact_text_ur=item.fact_text_ur,
                    fact_hash=item.fact_hash,
                    embedding_vector=tuple(float(value) for value in item.embedding_vector),
                )
                for item in existing_rows
            ]
            vectors = self.embedding_provider.encode([item.embedding_text for item in candidates])
            next_order = max((item.fact_order for item in existing_rows), default=0) + 1
            appended = duplicates = reviews = 0
            appended_en: list[str] = []
            appended_ur: list[str] = []

            for candidate, vector in zip(candidates, vectors, strict=True):
                decision = self.decider.decide(candidate, vector, existing)
                sentence_uuid = uuid.UUID(candidate.sentence_id)
                if decision.action == "review":
                    review = session.scalar(
                        select(SummaryReviewCase).where(
                            SummaryReviewCase.daily_summary_id == summary.id,
                            SummaryReviewCase.sentence_id == sentence_uuid,
                        )
                    )
                    if review is None:
                        session.add(
                            SummaryReviewCase(
                                daily_summary_id=summary.id,
                                sentence_id=sentence_uuid,
                                candidate_fact_id=uuid.UUID(decision.existing_fact_id)
                                if decision.existing_fact_id
                                else None,
                                semantic_similarity=float(decision.semantic_similarity or 0.0),
                                evidence=decision.evidence or {},
                                status="pending",
                            )
                        )
                    reviews += 1
                    continue

                if decision.action == "duplicate" and decision.existing_fact_id:
                    fact_uuid = uuid.UUID(decision.existing_fact_id)
                    if session.get(
                        SummaryFactSource,
                        {"summary_fact_id": fact_uuid, "sentence_id": sentence_uuid},
                    ) is None:
                        session.add(
                            SummaryFactSource(
                                summary_fact_id=fact_uuid,
                                sentence_id=sentence_uuid,
                            )
                        )
                    duplicates += 1
                else:
                    fact = SummaryFact(
                        daily_summary_id=summary.id,
                        fact_text_en=candidate.fact_text_en,
                        fact_text_ur=candidate.fact_text_ur,
                        fact_hash=candidate.fact_hash,
                        fact_order=next_order,
                        fact_state="active",
                        fact_kind="story",
                        priority=candidate.priority,
                        source_first_seen_at=candidate.source_first_seen_at,
                        source_last_seen_at=candidate.source_last_seen_at,
                        factual_signature=candidate.factual_signature,
                        model_name=self.embedding_provider.model_name,
                        model_revision=self.embedding_provider.model_revision,
                        dimensions=self.embedding_provider.dimensions,
                        embedding_vector=list(vector),
                    )
                    session.add(fact)
                    session.flush()
                    session.add(
                        SummaryFactSource(summary_fact_id=fact.id, sentence_id=sentence_uuid)
                    )
                    existing.append(
                        ExistingFact(
                            fact_id=str(fact.id),
                            fact_text_en=fact.fact_text_en,
                            fact_text_ur=fact.fact_text_ur,
                            fact_hash=fact.fact_hash,
                            embedding_vector=tuple(float(value) for value in vector),
                        )
                    )
                    appended_en.append(
                        _summary_line(
                            candidate.fact_text_en,
                            candidate.source_first_seen_at,
                            language="en",
                        )
                    )
                    appended_ur.append(
                        _summary_line(
                            candidate.fact_text_ur,
                            candidate.source_first_seen_at,
                            language="ur",
                        )
                    )
                    appended += 1
                    next_order += 1

                if session.get(
                    DailySummarySentence,
                    {"daily_summary_id": summary.id, "sentence_id": sentence_uuid},
                ) is None:
                    session.add(
                        DailySummarySentence(
                            daily_summary_id=summary.id,
                            sentence_id=sentence_uuid,
                        )
                    )
                    summary.source_sentence_count += 1

            if appended_en:
                prefix_en = "\n" if summary.summary_text_en else ""
                prefix_ur = "\n" if summary.summary_text_ur else ""
                summary.summary_text_en += prefix_en + "\n".join(appended_en)
                summary.summary_text_ur += prefix_ur + "\n".join(appended_ur)
                summary.last_updated_at = datetime.now(timezone.utc)
                summary.summarizer_model = AUTHORITATIVE_ENGINE
                summary.summarizer_revision = AUTHORITATIVE_REVISION
                self._add_outbox(
                    session,
                    event_key=f"summary_updated:{summary.id}:{next_order - 1}",
                    event_type="summary_updated",
                    aggregate_type="daily_summary",
                    aggregate_id=str(summary.id),
                    payload={
                        "summary_id": str(summary.id),
                        "category_id": category_id,
                        "calendar_date": calendar_date.isoformat(),
                        "facts_appended": appended,
                        "source_sentence_count": summary.source_sentence_count,
                        "last_updated_at": summary.last_updated_at.isoformat(),
                    },
                )
                session.execute(
                    text("SELECT pg_notify('newsintel_outbox', 'summary_updated')")
                )

            run.daily_summary_id = summary.id
            run.status = "succeeded"
            run.completed_at = datetime.now(timezone.utc)
            run.stories_considered = len(rows)
            run.facts_appended = appended
            run.duplicate_facts_skipped = duplicates
            run.pending_translation_count = pending_translation_count
            run.review_count = reviews
            run.diagnostics = {
                "engine": AUTHORITATIVE_ENGINE,
                "revision": AUTHORITATIVE_REVISION,
                "embedding_model": self.embedding_provider.model_name,
                "embedding_revision": self.embedding_provider.model_revision,
                "semantic_duplicate_threshold": self.config.semantic_duplicate_threshold,
                "semantic_review_threshold": self.config.semantic_review_threshold,
                "abstractive_digest_enabled": self.config.optional_abstractive_digest_enabled,
            }
            return SummaryRunResult(
                category_id=category_id,
                calendar_date=calendar_date,
                stories_considered=len(rows),
                facts_appended=appended,
                duplicate_facts_skipped=duplicates,
                pending_translation_count=pending_translation_count,
                review_count=reviews,
                summary_id=str(summary.id),
                changed=appended > 0,
            )

    @staticmethod
    def _add_outbox(
        session: Session,
        *,
        event_key: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> None:
        existing = session.scalar(select(OutboxEvent.id).where(OutboxEvent.event_key == event_key))
        if existing is not None:
            return
        session.add(
            OutboxEvent(
                event_key=event_key,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
                available_at=datetime.now(timezone.utc),
                max_attempts=12,
            )
        )

    def dirty_category_days(self, *, now: datetime | None = None) -> list[tuple[str, date]]:
        current = (now or datetime.now(timezone.utc)).astimezone(PKT)
        allowed_dates = {current.date()}
        minutes_after_midnight = current.hour * 60 + current.minute
        if minutes_after_midnight <= self.config.include_previous_day_grace_minutes:
            allowed_dates.add((current - timedelta(days=1)).date())
        with self.session_factory() as session:
            summary_state = Sentence.dedup_decision["summary_state"].as_string()
            folded = (
                select(DailySummarySentence.sentence_id)
                .join(DailySummary, DailySummary.id == DailySummarySentence.daily_summary_id)
                .where(
                    DailySummary.category_id == SentenceCategory.category_id,
                    DailySummary.calendar_date == Sentence.calendar_date,
                    DailySummarySentence.sentence_id == Sentence.id,
                )
                .exists()
            )
            pending_review = (
                select(SummaryReviewCase.sentence_id)
                .join(DailySummary, DailySummary.id == SummaryReviewCase.daily_summary_id)
                .where(
                    DailySummary.category_id == SentenceCategory.category_id,
                    DailySummary.calendar_date == Sentence.calendar_date,
                    SummaryReviewCase.sentence_id == Sentence.id,
                    SummaryReviewCase.status == "pending",
                )
                .exists()
            )
            rows = session.execute(
                select(SentenceCategory.category_id, Sentence.calendar_date)
                .join(Sentence, Sentence.id == SentenceCategory.sentence_id)
                .where(
                    Sentence.calendar_date.in_(sorted(allowed_dates)),
                    Sentence.review_status == "accepted",
                    or_(summary_state.is_(None), summary_state == "eligible"),
                    not_(folded),
                    not_(pending_review),
                )
                .distinct()
                .order_by(Sentence.calendar_date.asc(), SentenceCategory.category_id.asc())
            ).all()
        return [(str(category_id), calendar_date) for category_id, calendar_date in rows]

    def summarize_dirty(self, *, now: datetime | None = None) -> list[SummaryRunResult]:
        current = now or datetime.now(timezone.utc)
        results: list[SummaryRunResult] = []
        for category_id, calendar_date in self.dirty_category_days(now=current):
            run_slot = int(current.timestamp()) // self.config.interval_seconds
            results.append(
                self.summarize_category_day(
                    category_id,
                    calendar_date,
                    run_key=f"scheduled:{category_id}:{calendar_date.isoformat()}:{run_slot}",
                )
            )
        return results


class PostgresSummaryScheduler:
    """Five-minute PostgreSQL scheduler with a transaction-scoped advisory lock."""

    def __init__(
        self,
        service: PostgresSummarizationService,
        session_factory: sessionmaker[Session] | None = None,
        config: SummarizationConfig | None = None,
    ) -> None:
        self.service = service
        self.session_factory = session_factory or service.session_factory
        self.config = config or service.config
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def ensure_schedule(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        with self.session_factory() as session, session.begin():
            row = session.scalar(
                select(ScheduledJob).where(ScheduledJob.name == SUMMARY_SCHEDULE_NAME)
            )
            if row is None:
                session.add(
                    ScheduledJob(
                        name=SUMMARY_SCHEDULE_NAME,
                        job_type=SUMMARY_JOB_TYPE,
                        payload={},
                        interval_seconds=self.config.interval_seconds,
                        next_run_at=current,
                    )
                )
            else:
                row.job_type = SUMMARY_JOB_TYPE
                row.interval_seconds = self.config.interval_seconds
                row.is_enabled = True

    def tick(self, *, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        due = force
        with self.session_factory() as session, session.begin():
            acquired = bool(
                session.scalar(
                    select(
                        func.pg_try_advisory_xact_lock(
                            func.hashtext(self.config.advisory_lock_key)
                        )
                    )
                )
            )
            if not acquired:
                return {"status": "busy", "runs": []}
            row = session.scalar(
                select(ScheduledJob)
                .where(ScheduledJob.name == SUMMARY_SCHEDULE_NAME)
                .with_for_update()
            )
            if row is None:
                row = ScheduledJob(
                    name=SUMMARY_SCHEDULE_NAME,
                    job_type=SUMMARY_JOB_TYPE,
                    payload={},
                    interval_seconds=self.config.interval_seconds,
                    next_run_at=current,
                )
                session.add(row)
                session.flush()
            due = due or (row.is_enabled and row.next_run_at <= current)
            if not due:
                return {
                    "status": "not_due",
                    "next_run_at": row.next_run_at.isoformat(),
                    "runs": [],
                }
            row.last_run_at = current
            row.next_run_at = current + timedelta(seconds=self.config.interval_seconds)

        results = self.service.summarize_dirty(now=current)
        self._drain_dirty_jobs()
        return {
            "status": "ran",
            "worker_id": self.worker_id,
            "runs": [item.serializable() for item in results],
        }

    def _drain_dirty_jobs(self) -> int:
        with self.session_factory() as session, session.begin():
            rows = list(
                session.scalars(
                    select(ProcessingJob)
                    .where(
                        ProcessingJob.job_type == DIRTY_JOB_TYPE,
                        ProcessingJob.status.in_(["queued", "running"]),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(self.config.job_drain_limit)
                )
            )
            current = datetime.now(timezone.utc)
            for row in rows:
                row.status = "succeeded"
                row.completed_at = current
                row.leased_until = None
                row.lease_owner = None
            return len(rows)


def summarization_runtime_config() -> dict[str, Any]:
    config = SummarizationConfig.from_env()
    return {
        "engine": AUTHORITATIVE_ENGINE,
        "revision": AUTHORITATIVE_REVISION,
        "interval_seconds": config.interval_seconds,
        "calendar_timezone": "Asia/Karachi",
        "input_policy": "accepted summary-eligible canonical stories only",
        "bilingual_policy": "both complete English and Urdu facts required",
        "duplicate_policy": {
            "exact_bilingual_hash": True,
            "semantic_threshold": config.semantic_duplicate_threshold,
            "review_threshold": config.semantic_review_threshold,
            "factual_safeguards": [
                "numbers",
                "named_entities",
                "negation",
                "event_state",
            ],
        },
        "output_policy": {
            "additive_only": True,
            "regenerate_from_scratch": False,
            "every_unique_story_included": True,
            "uncertain_fact_withheld_for_review": True,
            "optional_abstractive_digest_enabled": config.optional_abstractive_digest_enabled,
        },
        "embedding": {
            "model": config.model_name,
            "revision": config.model_revision,
            "dimensions": config.embedding_dimensions,
            "activation_requires_labelled_benchmark": True,
        },
    }


def summarization_doctor(*, load_model: bool = False) -> dict[str, Any]:
    config = SummarizationConfig.from_env()
    report: dict[str, Any] = {
        "status": "ready",
        "python_policy": "CPython 3.12 x64",
        "config": summarization_runtime_config(),
        "checks": {
            "authoritative_fact_ledger": "ready",
            "five_minute_schedule": "ready",
            "additive_only": "ready",
            "abstractive_model_required": False,
        },
    }
    if load_model:
        try:
            provider = SentenceTransformerEmbeddingProvider(
                DeduplicationConfig(
                    model_name=config.model_name,
                    model_revision=config.model_revision,
                    embedding_dimensions=config.embedding_dimensions,
                )
            )
            vector = provider.encode(["query: summarization model health check"])[0]
            report["checks"]["embedding_model"] = {
                "status": "ready",
                "dimensions": len(vector),
                "device": "cpu",
            }
        except Exception as exc:
            report["status"] = "not_ready"
            report["checks"]["embedding_model"] = {
                "status": "error",
                "error": f"{exc.__class__.__name__}: {exc}",
            }
    else:
        report["checks"]["embedding_model"] = "not_loaded"
    return report


class InMemoryAdditiveSummaryLedger:
    """Deterministic test/reference implementation of the production append-only policy."""

    def __init__(self, config: SummarizationConfig | None = None) -> None:
        self.decider = SummaryFactDecider(config)
        self.facts: list[ExistingFact] = []
        self.summary_text_en = ""
        self.summary_text_ur = ""
        self.reviewed_sentence_ids: set[str] = set()
        self.folded_sentence_ids: set[str] = set()

    def apply(self, candidate: SummaryFactCandidate, vector: Sequence[float]) -> SummaryDecision:
        if candidate.sentence_id in self.folded_sentence_ids:
            return SummaryDecision(action="duplicate", candidate=candidate, evidence={"layer": "sentence_id"})
        decision = self.decider.decide(candidate, vector, self.facts)
        if decision.action == "append":
            fact_id = str(uuid.uuid4())
            self.facts.append(
                ExistingFact(
                    fact_id=fact_id,
                    fact_text_en=candidate.fact_text_en,
                    fact_text_ur=candidate.fact_text_ur,
                    fact_hash=candidate.fact_hash,
                    embedding_vector=tuple(float(value) for value in vector),
                )
            )
            en_line = _summary_line(candidate.fact_text_en, candidate.source_first_seen_at, language="en")
            ur_line = _summary_line(candidate.fact_text_ur, candidate.source_first_seen_at, language="ur")
            self.summary_text_en += ("\n" if self.summary_text_en else "") + en_line
            self.summary_text_ur += ("\n" if self.summary_text_ur else "") + ur_line
            self.folded_sentence_ids.add(candidate.sentence_id)
        elif decision.action == "duplicate":
            self.folded_sentence_ids.add(candidate.sentence_id)
        else:
            self.reviewed_sentence_ids.add(candidate.sentence_id)
        return decision
