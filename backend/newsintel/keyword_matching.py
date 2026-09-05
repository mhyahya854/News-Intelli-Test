from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

import ahocorasick
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import Category, Keyword
from .segmentation import SegmentedTextUnit
from .taxonomy import category_seed_rows, keyword_seed_rows

_ARABIC_DIACRITIC_RANGES = (
    (0x0610, 0x061A),
    (0x064B, 0x065F),
    (0x0670, 0x0670),
    (0x06D6, 0x06ED),
)
_URDU_EQUIVALENTS = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ي": "ی",
        "ى": "ی",
        "ئ": "ی",
        "ك": "ک",
        "ة": "ہ",
        "ۀ": "ہ",
        "ؤ": "و",
    }
)


class KeywordMatchingError(RuntimeError):
    """Keyword index, cache, or matching failure."""


class KeywordBackpressureError(KeywordMatchingError):
    """The segmentation-to-keyword handoff stayed full; nothing was dropped."""


@dataclass(slots=True, frozen=True)
class KeywordRecord:
    keyword_id: str
    category_id: str
    term: str
    normalized_term: str
    language: str
    priority: str
    match_mode: str
    requires_context: bool
    context_terms: tuple[str, ...]
    excluded_terms: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class CategoryRecord:
    category_id: str
    label_en: str
    label_ur: str
    color: str
    priority: int
    positive_terms: tuple[str, ...]
    multi_label_allowed: bool


@dataclass(slots=True, frozen=True)
class KeywordSnapshot:
    version: str
    loaded_at: datetime
    categories: tuple[CategoryRecord, ...]
    keywords: tuple[KeywordRecord, ...]
    source: str


class KeywordProvider(Protocol):
    def load_snapshot(self) -> KeywordSnapshot: ...


@dataclass(slots=True, frozen=True)
class TextView:
    original: str
    normalized: str
    normalized_to_original: tuple[int, ...]

    def original_span(self, start: int, end_exclusive: int) -> tuple[int, int]:
        if not self.normalized_to_original or start >= end_exclusive:
            return (0, 0)
        start = max(0, min(start, len(self.normalized_to_original) - 1))
        end_index = max(start, min(end_exclusive - 1, len(self.normalized_to_original) - 1))
        return self.normalized_to_original[start], self.normalized_to_original[end_index] + 1


@dataclass(slots=True, frozen=True)
class KeywordHit:
    keyword_id: str
    category_id: str
    term: str
    normalized_term: str
    language: str
    priority: str
    requires_context: bool
    matched_text: str
    normalized_span: tuple[int, int]
    original_span: tuple[int, int]

    def serializable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["normalized_span"] = list(self.normalized_span)
        payload["original_span"] = list(self.original_span)
        return payload


@dataclass(slots=True, frozen=True)
class CategoryDecision:
    category_id: str
    label_en: str
    label_ur: str
    color: str
    score: float
    accepted: bool
    reason: str
    keyword_ids: tuple[str, ...]
    supporting_context: tuple[str, ...]
    excluded_context: tuple[str, ...]

    def serializable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["keyword_ids"] = list(self.keyword_ids)
        payload["supporting_context"] = list(self.supporting_context)
        payload["excluded_context"] = list(self.excluded_context)
        return payload


@dataclass(slots=True, frozen=True)
class DetectionObservation:
    observation_id: str
    unit_id: str
    stream_id: str
    channel_name: str
    observed_at: datetime
    text: str
    normalized_text: str
    language: str
    confidence: float
    review_required: bool
    keyword_hits: tuple[KeywordHit, ...]
    category_decisions: tuple[CategoryDecision, ...]
    urgency: str
    emit_immediately: bool
    canonical_story_status: str
    summary_status: str
    snapshot_version: str

    @property
    def accepted_categories(self) -> tuple[CategoryDecision, ...]:
        return tuple(item for item in self.category_decisions if item.accepted)

    @property
    def matched(self) -> bool:
        return bool(self.accepted_categories)

    def serializable(self) -> dict[str, Any]:
        return {
            "event": "detection_observed",
            "data": {
                "observation_id": self.observation_id,
                "unit_id": self.unit_id,
                "stream_id": self.stream_id,
                "channel_name": self.channel_name,
                "observed_at": self.observed_at.isoformat(),
                "text": self.text,
                "normalized_text": self.normalized_text,
                "language": self.language,
                "confidence": self.confidence,
                "review_required": self.review_required,
                "keyword_hits": [item.serializable() for item in self.keyword_hits],
                "category_decisions": [item.serializable() for item in self.category_decisions],
                "accepted_category_ids": [item.category_id for item in self.accepted_categories],
                "urgency": self.urgency,
                "delivery": {
                    "emit_immediately": self.emit_immediately,
                    "canonical_story_status": self.canonical_story_status,
                    "summary_status": self.summary_status,
                },
                "snapshot_version": self.snapshot_version,
            },
        }


@dataclass(slots=True, frozen=True)
class KeywordBenchmarkCase:
    case_id: str
    text: str
    language: str
    expected_categories: tuple[str, ...]
    forbidden_categories: tuple[str, ...]
    expected_terms: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class KeywordBenchmarkReport:
    case_count: int
    category_true_positive: int
    category_false_positive: int
    category_false_negative: int
    category_precision: float
    category_recall: float
    exact_case_pass_rate: float
    accepted: bool
    failures: tuple[dict[str, Any], ...]

    def serializable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failures"] = list(self.failures)
        return payload


def _is_removed_mark(character: str) -> bool:
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _ARABIC_DIACRITIC_RANGES)


def normalize_match_text(value: str) -> str:
    """Normalize Unicode representation without correcting, stemming, or guessing words."""
    return build_text_view(value).normalized


def build_text_view(value: str) -> TextView:
    normalized_chars: list[str] = []
    mapping: list[int] = []
    previous_was_space = True
    for original_index, original_char in enumerate(value):
        for character in unicodedata.normalize("NFKC", original_char):
            if character == "ـ" or _is_removed_mark(character):
                continue
            character = character.translate(_URDU_EQUIVALENTS).casefold()
            for folded_char in character:
                if folded_char.isspace():
                    if not previous_was_space:
                        normalized_chars.append(" ")
                        mapping.append(original_index)
                    previous_was_space = True
                    continue
                normalized_chars.append(folded_char)
                mapping.append(original_index)
                previous_was_space = False
    if normalized_chars and normalized_chars[-1] == " ":
        normalized_chars.pop()
        mapping.pop()
    return TextView(value, "".join(normalized_chars), tuple(mapping))


def _is_token_character(character: str) -> bool:
    if not character:
        return False
    category = unicodedata.category(character)
    return category[0] in {"L", "N", "M"} or character == "_"


def _has_exact_boundaries(text: str, start: int, end_exclusive: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end_exclusive] if end_exclusive < len(text) else ""
    return not _is_token_character(before) and not _is_token_character(after)


def _priority_rank(value: str) -> int:
    return {"normal": 0, "high": 1, "critical": 2}.get(value, 0)


def _snapshot_version(categories: Sequence[CategoryRecord], keywords: Sequence[KeywordRecord]) -> str:
    payload = {
        "categories": [asdict(item) for item in categories],
        "keywords": [asdict(item) for item in keywords],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class InMemoryKeywordProvider:
    def __init__(
        self,
        categories: Sequence[CategoryRecord],
        keywords: Sequence[KeywordRecord],
        *,
        source: str = "memory",
    ) -> None:
        self.categories = list(categories)
        self.keywords = list(keywords)
        self.source = source
        self.failure: Exception | None = None

    def load_snapshot(self) -> KeywordSnapshot:
        if self.failure is not None:
            raise self.failure
        categories = tuple(sorted(self.categories, key=lambda item: item.category_id))
        keywords = tuple(sorted(self.keywords, key=lambda item: (item.normalized_term, item.category_id, item.keyword_id)))
        return KeywordSnapshot(
            version=_snapshot_version(categories, keywords),
            loaded_at=datetime.now(timezone.utc),
            categories=categories,
            keywords=keywords,
            source=self.source,
        )


class SQLAlchemyKeywordProvider:
    """Loads active categories and keywords from PostgreSQL without modifying the database."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def load_snapshot(self) -> KeywordSnapshot:
        with self.session_factory() as session:
            categories = session.scalars(
                select(Category).where(Category.is_active.is_(True)).order_by(Category.id)
            ).all()
            keywords = session.scalars(
                select(Keyword)
                .where(Keyword.is_active.is_(True))
                .order_by(Keyword.normalized_term, Keyword.category_id, Keyword.id)
            ).all()
        category_records = tuple(
            CategoryRecord(
                category_id=item.id,
                label_en=item.label_en,
                label_ur=item.label_ur,
                color=item.color,
                priority=item.priority,
                positive_terms=tuple(
                    normalize_match_text(str(term))
                    for term in item.context_profile.get("positive_terms", [])
                    if normalize_match_text(str(term))
                ),
                multi_label_allowed=bool(item.context_profile.get("multi_label_allowed", True)),
            )
            for item in categories
        )
        active_category_ids = {item.category_id for item in category_records}
        keyword_records = tuple(
            KeywordRecord(
                keyword_id=str(item.id),
                category_id=item.category_id,
                term=item.term,
                normalized_term=normalize_match_text(item.normalized_term or item.term),
                language=item.language,
                priority=item.priority,
                match_mode=item.match_mode,
                requires_context=item.requires_context,
                context_terms=tuple(normalize_match_text(term) for term in item.context_terms if normalize_match_text(term)),
                excluded_terms=tuple(normalize_match_text(term) for term in item.excluded_terms if normalize_match_text(term)),
            )
            for item in keywords
            if item.category_id in active_category_ids
        )
        return KeywordSnapshot(
            version=_snapshot_version(category_records, keyword_records),
            loaded_at=datetime.now(timezone.utc),
            categories=category_records,
            keywords=keyword_records,
            source="postgresql",
        )


def seeded_keyword_provider() -> InMemoryKeywordProvider:
    categories = [
        CategoryRecord(
            category_id=str(row["id"]),
            label_en=str(row["label_en"]),
            label_ur=str(row["label_ur"]),
            color=str(row["color"]),
            priority=int(row["priority"]),
            positive_terms=tuple(normalize_match_text(str(term)) for term in row["context_profile"].get("positive_terms", [])),
            multi_label_allowed=bool(row["context_profile"].get("multi_label_allowed", True)),
        )
        for row in category_seed_rows()
    ]
    keywords: list[KeywordRecord] = []
    for row in keyword_seed_rows():
        stable_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"newsintel:{row['category_id']}:{row['language']}:{row['normalized_term']}",
        )
        keywords.append(
            KeywordRecord(
                keyword_id=str(stable_id),
                category_id=str(row["category_id"]),
                term=str(row["term"]),
                normalized_term=normalize_match_text(str(row["normalized_term"])),
                language=str(row["language"]),
                priority=str(row["priority"]),
                match_mode=str(row["match_mode"]),
                requires_context=bool(row["requires_context"]),
                context_terms=tuple(normalize_match_text(str(term)) for term in row["context_terms"]),
                excluded_terms=tuple(normalize_match_text(str(term)) for term in row["excluded_terms"]),
            )
        )
    return InMemoryKeywordProvider(categories, keywords, source="seed-preview")


class CompiledKeywordIndex:
    def __init__(self, snapshot: KeywordSnapshot) -> None:
        self.snapshot = snapshot
        self.categories = {item.category_id: item for item in snapshot.categories}
        self.keywords = {item.keyword_id: item for item in snapshot.keywords}
        grouped: dict[str, list[KeywordRecord]] = defaultdict(list)
        for keyword in snapshot.keywords:
            if keyword.normalized_term:
                grouped[keyword.normalized_term].append(keyword)
        self.automaton = ahocorasick.Automaton()
        for term, records in sorted(grouped.items()):
            self.automaton.add_word(term, (term, tuple(item.keyword_id for item in records)))
        self.automaton.make_automaton()

        context_grouped: dict[str, set[str]] = defaultdict(set)
        for category in snapshot.categories:
            for term in category.positive_terms:
                if term:
                    context_grouped[term].add(category.category_id)
        for keyword in snapshot.keywords:
            for term in keyword.context_terms:
                if term:
                    context_grouped[term].add(keyword.category_id)
        self.context_automaton = ahocorasick.Automaton()
        self.context_term_count = len(context_grouped)
        for term, category_ids in sorted(context_grouped.items()):
            self.context_automaton.add_word(term, (term, tuple(sorted(category_ids))))
        if self.context_term_count:
            self.context_automaton.make_automaton()

    @staticmethod
    def _iter_exact(automaton: ahocorasick.Automaton, text: str) -> Iterable[tuple[int, int, Any]]:
        for end_inclusive, value in automaton.iter(text):
            term = value[0]
            start = end_inclusive - len(term) + 1
            end_exclusive = end_inclusive + 1
            if _has_exact_boundaries(text, start, end_exclusive):
                yield start, end_exclusive, value

    def find_keyword_hits(self, text_view: TextView) -> tuple[KeywordHit, ...]:
        hits: list[KeywordHit] = []
        seen: set[tuple[str, int, int]] = set()
        for start, end_exclusive, (_, keyword_ids) in self._iter_exact(self.automaton, text_view.normalized):
            original_start, original_end = text_view.original_span(start, end_exclusive)
            for keyword_id in keyword_ids:
                identity = (keyword_id, start, end_exclusive)
                if identity in seen:
                    continue
                seen.add(identity)
                record = self.keywords[keyword_id]
                hits.append(
                    KeywordHit(
                        keyword_id=record.keyword_id,
                        category_id=record.category_id,
                        term=record.term,
                        normalized_term=record.normalized_term,
                        language=record.language,
                        priority=record.priority,
                        requires_context=record.requires_context,
                        matched_text=text_view.original[original_start:original_end],
                        normalized_span=(start, end_exclusive),
                        original_span=(original_start, original_end),
                    )
                )
        return tuple(sorted(hits, key=lambda item: (item.normalized_span[0], -len(item.normalized_term), item.category_id, item.keyword_id)))

    def find_context(self, normalized_text: str) -> Mapping[str, tuple[str, ...]]:
        if not self.context_term_count:
            return {}
        found: dict[str, set[str]] = defaultdict(set)
        for _, _, (term, category_ids) in self._iter_exact(self.context_automaton, normalized_text):
            for category_id in category_ids:
                found[category_id].add(term)
        return {category_id: tuple(sorted(terms)) for category_id, terms in found.items()}

    def classify(self, text_view: TextView, hits: Sequence[KeywordHit]) -> tuple[CategoryDecision, ...]:
        hits_by_category: dict[str, list[KeywordHit]] = defaultdict(list)
        for hit in hits:
            hits_by_category[hit.category_id].append(hit)
        context_by_category = self.find_context(text_view.normalized)
        decisions: list[CategoryDecision] = []
        for category_id, category_hits in sorted(hits_by_category.items()):
            category = self.categories.get(category_id)
            if category is None:
                continue
            keyword_records = [self.keywords[item.keyword_id] for item in category_hits]
            matched_terms = {item.normalized_term for item in category_hits}
            strong_hits = [item for item in category_hits if not item.requires_context]
            ambiguous_hits = [item for item in category_hits if item.requires_context]
            context_terms = set(context_by_category.get(category_id, ())) - matched_terms
            context_terms.update(
                term
                for record in keyword_records
                for term in record.context_terms
                if term != record.normalized_term and self._contains_exact(text_view.normalized, term)
            )
            excluded_terms = {
                term
                for record in keyword_records
                for term in record.excluded_terms
                if self._contains_exact(text_view.normalized, term)
            }
            distinct_keyword_terms = {item.normalized_term for item in category_hits}
            priority = max((_priority_rank(item.priority) for item in category_hits), default=0)

            if excluded_terms and not strong_hits:
                accepted = False
                reason = "excluded_context"
                score = 0.0
            elif strong_hits:
                accepted = True
                reason = "explicit_category_keyword"
                score = 0.72
                score += min(0.14, 0.05 * (len({item.normalized_term for item in strong_hits}) - 1))
                score += min(0.08, 0.03 * len(context_terms))
                score += (0.03 if priority == 1 else 0.05 if priority == 2 else 0.0)
                score -= min(0.25, 0.12 * len(excluded_terms))
            elif ambiguous_hits and (context_terms or len(distinct_keyword_terms) >= 2):
                accepted = True
                reason = "ambiguous_keyword_supported_by_full_sentence"
                score = 0.56
                score += min(0.18, 0.06 * len(context_terms))
                score += min(0.12, 0.06 * (len(distinct_keyword_terms) - 1))
                score += (0.03 if priority == 1 else 0.05 if priority == 2 else 0.0)
            else:
                accepted = False
                reason = "ambiguous_keyword_without_category_context"
                score = 0.0

            decisions.append(
                CategoryDecision(
                    category_id=category_id,
                    label_en=category.label_en,
                    label_ur=category.label_ur,
                    color=category.color,
                    score=round(max(0.0, min(score, 1.0)), 6),
                    accepted=accepted,
                    reason=reason,
                    keyword_ids=tuple(sorted({item.keyword_id for item in category_hits})),
                    supporting_context=tuple(sorted(context_terms)),
                    excluded_context=tuple(sorted(excluded_terms)),
                )
            )
        return tuple(sorted(decisions, key=lambda item: (not item.accepted, -item.score, item.category_id)))

    @staticmethod
    def _contains_exact(text: str, term: str) -> bool:
        if not term:
            return False
        start = text.find(term)
        while start >= 0:
            end = start + len(term)
            if _has_exact_boundaries(text, start, end):
                return True
            start = text.find(term, start + 1)
        return False


class KeywordMatcherService:
    def __init__(
        self,
        provider: KeywordProvider,
        *,
        refresh_interval_seconds: float = 60.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be positive")
        self.provider = provider
        self.refresh_interval_seconds = refresh_interval_seconds
        self.monotonic_clock = monotonic_clock
        self._lock = threading.RLock()
        self._index: CompiledKeywordIndex | None = None
        self._loaded_monotonic = float("-inf")
        self.refresh_count = 0
        self.refresh_failures = 0
        self.last_refresh_error: str | None = None

    @property
    def snapshot(self) -> KeywordSnapshot | None:
        return self._index.snapshot if self._index else None

    def refresh(self, *, force: bool = False) -> KeywordSnapshot:
        now = self.monotonic_clock()
        with self._lock:
            if not force and self._index is not None and now - self._loaded_monotonic < self.refresh_interval_seconds:
                return self._index.snapshot
            try:
                snapshot = self.provider.load_snapshot()
                if not snapshot.categories:
                    raise KeywordMatchingError("Active category snapshot is empty")
                if not snapshot.keywords:
                    raise KeywordMatchingError("Active keyword snapshot is empty")
                if self._index is None or snapshot.version != self._index.snapshot.version:
                    self._index = CompiledKeywordIndex(snapshot)
                self._loaded_monotonic = now
                self.refresh_count += 1
                self.last_refresh_error = None
                return self._index.snapshot
            except Exception as exc:
                self.refresh_failures += 1
                self.last_refresh_error = f"{exc.__class__.__name__}: {exc}"
                if self._index is None:
                    raise KeywordMatchingError("No valid keyword snapshot is available") from exc
                # Stale-while-error: preserve matching rather than causing live data loss.
                self._loaded_monotonic = now
                return self._index.snapshot

    def match_text(
        self,
        *,
        text: str,
        language: str,
        unit_id: str,
        stream_id: str,
        channel_name: str,
        observed_at: datetime,
        confidence: float,
        review_required: bool,
    ) -> DetectionObservation:
        snapshot = self.refresh()
        assert self._index is not None
        text_view = build_text_view(text)
        hits = self._index.find_keyword_hits(text_view)
        decisions = self._index.classify(text_view, hits)
        accepted_keyword_ids = {
            keyword_id
            for decision in decisions
            if decision.accepted
            for keyword_id in decision.keyword_ids
        }
        accepted_hits = tuple(item for item in hits if item.keyword_id in accepted_keyword_ids)
        urgency_rank = max((_priority_rank(item.priority) for item in accepted_hits), default=0)
        urgency = "critical" if urgency_rank == 2 else "high" if urgency_rank == 1 else "normal"
        return DetectionObservation(
            observation_id=str(uuid.uuid4()),
            unit_id=unit_id,
            stream_id=stream_id,
            channel_name=channel_name,
            observed_at=observed_at,
            text=text,
            normalized_text=text_view.normalized,
            language=language,
            confidence=confidence,
            review_required=review_required,
            keyword_hits=accepted_hits,
            category_decisions=decisions,
            urgency=urgency,
            emit_immediately=bool(accepted_hits),
            canonical_story_status="pending_phase_6_deduplication" if accepted_hits else "not_applicable",
            summary_status="blocked_until_canonical_story_deduplication" if accepted_hits else "not_applicable",
            snapshot_version=snapshot.version,
        )

    def match_unit(self, unit: SegmentedTextUnit) -> DetectionObservation:
        return self.match_text(
            text=unit.text,
            language=unit.language,
            unit_id=unit.unit_id,
            stream_id=unit.stream_id,
            channel_name=unit.channel_name,
            observed_at=unit.last_seen_at,
            confidence=unit.confidence,
            review_required=unit.review_required,
        )

    def diagnostics(self) -> dict[str, Any]:
        snapshot = self.snapshot
        return {
            "status": "ready" if snapshot else "not_loaded",
            "phase": 5,
            "policy": {
                "exact_normalized_whole_word_or_phrase_only": True,
                "full_sentence_context_classification": True,
                "fuzzy_matching": False,
                "stemming": False,
                "misspelling_variants": False,
                "autocorrection": False,
                "multi_label_categories": True,
                "repeat_detection_events_emit_immediately": True,
                "summaries_wait_for_canonical_deduplication": True,
            },
            "cache": {
                "refresh_interval_seconds": self.refresh_interval_seconds,
                "refresh_count": self.refresh_count,
                "refresh_failures": self.refresh_failures,
                "last_refresh_error": self.last_refresh_error,
                "snapshot_version": snapshot.version if snapshot else None,
                "snapshot_source": snapshot.source if snapshot else None,
                "category_count": len(snapshot.categories) if snapshot else 0,
                "keyword_count": len(snapshot.keywords) if snapshot else 0,
            },
        }


class KeywordObservationSink(Protocol):
    async def write(self, observation: DetectionObservation) -> None: ...


class InMemoryKeywordObservationSink:
    def __init__(self) -> None:
        self.observations: list[DetectionObservation] = []

    async def write(self, observation: DetectionObservation) -> None:
        self.observations.append(observation)


class BoundedSegmentedUnitBus:
    def __init__(self, capacity: int = 128) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._queue: asyncio.Queue[SegmentedTextUnit] = asyncio.Queue(maxsize=capacity)
        self.publish_count = 0
        self.high_watermark = 0

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def capacity(self) -> int:
        return self._queue.maxsize

    async def publish(self, unit: SegmentedTextUnit, timeout_seconds: float = 5.0) -> None:
        try:
            await asyncio.wait_for(self._queue.put(unit), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise KeywordBackpressureError(
                f"Segmented-unit queue remained full for {timeout_seconds:.1f}s; no unit was silently dropped"
            ) from exc
        self.publish_count += 1
        self.high_watermark = max(self.high_watermark, self._queue.qsize())

    async def consume(self) -> SegmentedTextUnit:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()


class KeywordBusWorker:
    def __init__(
        self,
        bus: BoundedSegmentedUnitBus,
        matcher: KeywordMatcherService,
        sink: KeywordObservationSink,
        *,
        retain_unmatched_for_metrics: bool = False,
    ) -> None:
        self.bus = bus
        self.matcher = matcher
        self.sink = sink
        self.retain_unmatched_for_metrics = retain_unmatched_for_metrics
        self.processed_units = 0
        self.matched_units = 0
        self.unmatched_units = 0
        self.failed_units = 0
        self.last_error: str | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() or self.bus.size:
            try:
                unit = await asyncio.wait_for(self.bus.consume(), timeout=0.25)
            except TimeoutError:
                continue
            try:
                observation = self.matcher.match_unit(unit)
                self.processed_units += 1
                if observation.matched:
                    self.matched_units += 1
                    await self.sink.write(observation)
                else:
                    self.unmatched_units += 1
                    if self.retain_unmatched_for_metrics:
                        await self.sink.write(observation)
            except Exception as exc:
                self.failed_units += 1
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                raise
            finally:
                self.bus.task_done()


def benchmark_keyword_matching(
    matcher: KeywordMatcherService,
    cases: Sequence[KeywordBenchmarkCase],
) -> KeywordBenchmarkReport:
    true_positive = 0
    false_positive = 0
    false_negative = 0
    exact_passes = 0
    failures: list[dict[str, Any]] = []
    for case in cases:
        observation = matcher.match_text(
            text=case.text,
            language=case.language,
            unit_id=f"benchmark:{case.case_id}",
            stream_id="benchmark-stream",
            channel_name="Benchmark Channel",
            observed_at=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
            confidence=1.0,
            review_required=False,
        )
        predicted = {item.category_id for item in observation.accepted_categories}
        expected = set(case.expected_categories)
        forbidden = set(case.forbidden_categories)
        predicted_terms = {item.normalized_term for item in observation.keyword_hits}
        expected_terms = {normalize_match_text(item) for item in case.expected_terms}
        tp = len(predicted & expected)
        fp = len(predicted - expected) + len(predicted & forbidden)
        fn = len(expected - predicted)
        true_positive += tp
        false_positive += fp
        false_negative += fn
        passed = predicted == expected and not (predicted & forbidden) and expected_terms <= predicted_terms
        if passed:
            exact_passes += 1
        else:
            failures.append(
                {
                    "case_id": case.case_id,
                    "text": case.text,
                    "expected_categories": sorted(expected),
                    "predicted_categories": sorted(predicted),
                    "forbidden_categories_present": sorted(predicted & forbidden),
                    "expected_terms": sorted(expected_terms),
                    "predicted_terms": sorted(predicted_terms),
                }
            )
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    pass_rate = exact_passes / len(cases) if cases else 1.0
    return KeywordBenchmarkReport(
        case_count=len(cases),
        category_true_positive=true_positive,
        category_false_positive=false_positive,
        category_false_negative=false_negative,
        category_precision=round(precision, 6),
        category_recall=round(recall, 6),
        exact_case_pass_rate=round(pass_rate, 6),
        accepted=precision == 1.0 and recall == 1.0 and pass_rate == 1.0 and not failures,
        failures=tuple(failures),
    )


def load_keyword_manifest(path: Path) -> list[KeywordBenchmarkCase]:
    cases: list[KeywordBenchmarkCase] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on manifest line {line_number}: {exc}") from exc
        cases.append(
            KeywordBenchmarkCase(
                case_id=str(payload["id"]),
                text=str(payload["text"]),
                language=str(payload.get("language", "mixed")),
                expected_categories=tuple(str(item) for item in payload.get("expected_categories", [])),
                forbidden_categories=tuple(str(item) for item in payload.get("forbidden_categories", [])),
                expected_terms=tuple(str(item) for item in payload.get("expected_terms", [])),
            )
        )
    if not cases:
        raise ValueError("Keyword manifest contains no cases")
    return cases


def keyword_runtime_config_from_env() -> dict[str, Any]:
    return {
        "refresh_interval_seconds": float(os.getenv("KEYWORD_CACHE_REFRESH_SECONDS", "60")),
        "bus_capacity": int(os.getenv("KEYWORD_BUS_CAPACITY", "128")),
        "publish_timeout_seconds": float(os.getenv("KEYWORD_BUS_PUBLISH_TIMEOUT_SECONDS", "5")),
    }


def keyword_doctor() -> dict[str, Any]:
    config = keyword_runtime_config_from_env()
    provider = seeded_keyword_provider()
    matcher = KeywordMatcherService(provider, refresh_interval_seconds=config["refresh_interval_seconds"])
    matcher.refresh(force=True)
    report = matcher.diagnostics()
    report["engine"] = {
        "name": "pyahocorasick",
        "version": getattr(ahocorasick, "__version__", "2.3.1"),
        "unicode": bool(getattr(matcher._index.automaton, "unicode", True)) if matcher._index else True,
        "algorithm": "Aho-Corasick exact multi-pattern search",
    }
    report["runtime"] = {
        "python_required": "3.12.x x64",
        "database_source_in_production": "PostgreSQL active categories and keywords",
        "seed_preview_is_for_diagnostics_only": True,
    }
    return report
