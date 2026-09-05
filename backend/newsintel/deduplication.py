from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Protocol, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from .keyword_matching import DetectionObservation, normalize_match_text

PKT = ZoneInfo("Asia/Karachi")

_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_TOKEN_RE = re.compile(r"[\w\u0600-\u06FF]+", re.UNICODE)
_NUMBER_RE = re.compile(r"(?<!\w)[+-]?(?:\d{1,3}(?:[,،]\d{3})+|\d+)(?:[.:]\d+)?%?(?!\w)")

_NEGATION_TERMS = {
    "en": {"no", "not", "never", "denied", "denies", "reject", "rejected", "without"},
    "ur": {"نہیں", "نہ", "انکار", "مسترد", "بغیر"},
}

# Terms within one set can materially reverse the same event. Exact matching only.
_CONTRADICTION_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"approved", "rejected", "منظور", "مسترد"}),
    frozenset({"granted", "denied", "منظور", "انکار"}),
    frozenset({"won", "lost", "جیت", "ہار", "فتح", "شکست"}),
    frozenset({"increased", "decreased", "اضافہ", "کمی"}),
    frozenset({"live", "dead", "alive", "killed", "زندہ", "ہلاک", "جاں", "بحق"}),
    frozenset({"open", "closed", "کھلا", "بند"}),
)


@dataclass(slots=True, frozen=True)
class DeduplicationConfig:
    model_name: str = "intfloat/multilingual-e5-small"
    model_revision: str = "fd1525a9fd15316a2d503bf26ab031a61d056e98"
    embedding_dimensions: int = 384
    same_day_auto_merge_similarity: float = 0.93
    cross_channel_window_similarity: float = 0.90
    cross_language_auto_merge_similarity: float = 0.94
    review_similarity: float = 0.84
    minimum_lexical_overlap: float = 0.24
    high_semantic_lexical_bypass: float = 0.965
    cross_channel_window_minutes: int = 30
    candidate_limit: int = 5000
    preserve_every_observation: bool = True
    summary_accepts_only_new_canonical_stories: bool = True

    @classmethod
    def from_env(cls) -> "DeduplicationConfig":
        defaults = cls()
        return cls(
            model_name=os.getenv("DEDUP_EMBEDDING_MODEL", defaults.model_name),
            model_revision=os.getenv("DEDUP_EMBEDDING_REVISION", defaults.model_revision),
            embedding_dimensions=int(os.getenv("DEDUP_EMBEDDING_DIMENSIONS", str(defaults.embedding_dimensions))),
            same_day_auto_merge_similarity=float(os.getenv("DEDUP_SAME_DAY_AUTO_MERGE", str(defaults.same_day_auto_merge_similarity))),
            cross_channel_window_similarity=float(os.getenv("DEDUP_CROSS_CHANNEL_WINDOW_MERGE", str(defaults.cross_channel_window_similarity))),
            cross_language_auto_merge_similarity=float(os.getenv("DEDUP_CROSS_LANGUAGE_AUTO_MERGE", str(defaults.cross_language_auto_merge_similarity))),
            review_similarity=float(os.getenv("DEDUP_REVIEW_THRESHOLD", str(defaults.review_similarity))),
            minimum_lexical_overlap=float(os.getenv("DEDUP_MIN_LEXICAL_OVERLAP", str(defaults.minimum_lexical_overlap))),
            high_semantic_lexical_bypass=float(os.getenv("DEDUP_HIGH_SEMANTIC_BYPASS", str(defaults.high_semantic_lexical_bypass))),
            cross_channel_window_minutes=int(os.getenv("DEDUP_CROSS_CHANNEL_WINDOW_MINUTES", str(defaults.cross_channel_window_minutes))),
            candidate_limit=int(os.getenv("DEDUP_CANDIDATE_LIMIT", str(defaults.candidate_limit))),
        )


@dataclass(slots=True, frozen=True)
class StoryFeatures:
    exact_key: str
    exact_hash: str
    tokens: frozenset[str]
    numbers: tuple[str, ...]
    proper_names: frozenset[str]
    negations: frozenset[str]
    contradiction_terms: frozenset[str]


@dataclass(slots=True)
class CanonicalStory:
    story_id: str
    calendar_date: date
    original_text: str
    normalized_text: str
    original_language: str
    category_ids: set[str]
    first_seen_at: datetime
    last_seen_at: datetime
    occurrence_count: int
    source_channels: set[str]
    source_stream_ids: set[str]
    exact_hashes: set[str]
    embedding: list[float]
    embedding_count: int
    confidence: float
    latest_text: str
    summary_state: str = "eligible"
    review_status: str = "accepted"

    def serializable(self) -> dict[str, Any]:
        return {
            "story_id": self.story_id,
            "calendar_date": self.calendar_date.isoformat(),
            "original_text": self.original_text,
            "latest_text": self.latest_text,
            "original_language": self.original_language,
            "category_ids": sorted(self.category_ids),
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "occurrence_count": self.occurrence_count,
            "source_channels": sorted(self.source_channels),
            "source_stream_ids": sorted(self.source_stream_ids),
            "confidence": self.confidence,
            "summary_state": self.summary_state,
            "review_status": self.review_status,
        }


@dataclass(slots=True, frozen=True)
class StoryOccurrence:
    occurrence_id: str
    observation_id: str
    story_id: str | None
    stream_id: str
    channel_name: str
    observed_at: datetime
    calendar_date: date
    source_text: str
    normalized_text: str
    language: str
    confidence: float
    category_ids: tuple[str, ...]
    keyword_ids: tuple[str, ...]
    exact_hash: str
    decision: str
    dedup_layer: str
    semantic_similarity: float | None
    lexical_overlap: float | None
    decision_metadata: dict[str, Any]
    emitted_live: bool = True

    def serializable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["observed_at"] = self.observed_at.isoformat()
        payload["calendar_date"] = self.calendar_date.isoformat()
        return payload


@dataclass(slots=True, frozen=True)
class ReviewCase:
    review_id: str
    observation_id: str
    candidate_story_id: str
    calendar_date: date
    semantic_similarity: float
    lexical_overlap: float
    evidence: dict[str, Any]
    status: str = "pending"


@dataclass(slots=True, frozen=True)
class CandidateScore:
    story_id: str
    semantic_similarity: float
    lexical_overlap: float
    within_cross_channel_window: bool
    same_language: bool
    numbers_compatible: bool
    named_entities_compatible: bool
    negation_compatible: bool
    contradiction_free: bool
    category_overlap: tuple[str, ...]
    merge_threshold: float
    auto_merge_allowed: bool
    review_allowed: bool
    reasons: tuple[str, ...]

    def serializable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["category_overlap"] = list(self.category_overlap)
        payload["reasons"] = list(self.reasons)
        return payload


@dataclass(slots=True, frozen=True)
class DeduplicationResult:
    observation_id: str
    action: str
    dedup_layer: str
    story: CanonicalStory | None
    occurrence: StoryOccurrence
    best_candidate: CandidateScore | None
    summary_eligible: bool
    emitted_events: tuple[dict[str, Any], ...]
    review_case: ReviewCase | None = None

    def serializable(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "action": self.action,
            "dedup_layer": self.dedup_layer,
            "story": self.story.serializable() if self.story else None,
            "occurrence": self.occurrence.serializable(),
            "best_candidate": self.best_candidate.serializable() if self.best_candidate else None,
            "summary_eligible": self.summary_eligible,
            "review_case": asdict(self.review_case) if self.review_case else None,
            "emitted_events": list(self.emitted_events),
        }


class EmbeddingProvider(Protocol):
    model_name: str
    model_revision: str
    dimensions: int

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class SentenceTransformerEmbeddingProvider:
    """Lazy CPU-only sentence embedding provider.

    The model is loaded once per process. Model acceptance is benchmark-gated; merely
    loading the model does not make the configured thresholds trustworthy.
    """

    def __init__(self, config: DeduplicationConfig | None = None) -> None:
        self.config = config or DeduplicationConfig.from_env()
        self.model_name = self.config.model_name
        self.model_revision = self.config.model_revision
        self.dimensions = self.config.embedding_dimensions
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self.model_name,
                revision=self.model_revision,
                device="cpu",
                trust_remote_code=False,
            )
            dimension = int(self._model.get_sentence_embedding_dimension())
            if dimension != self.dimensions:
                raise RuntimeError(
                    f"Embedding dimension mismatch: expected {self.dimensions}, received {dimension}"
                )
        return self._model

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        # E5 is trained with prefixes. For symmetric clustering/deduplication, the model
        # card recommends consistently prefixed inputs; query: is used for both sides.
        inputs = [f"query: {text}" for text in texts]
        vectors = model.encode(
            inputs,
            batch_size=min(16, len(inputs)),
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return [[float(value) for value in vector] for vector in vectors]


class FixedEmbeddingProvider:
    """Deterministic test/benchmark provider; never used as a production model."""

    model_name = "test/fixed-embedding"
    model_revision = "1"

    def __init__(self, vectors: dict[str, Sequence[float]], dimensions: int | None = None) -> None:
        self.vectors = {normalize_match_text(key): _normalize_vector(value) for key, value in vectors.items()}
        self.dimensions = dimensions or (len(next(iter(self.vectors.values()))) if self.vectors else 4)

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        output: list[list[float]] = []
        for text in texts:
            key = normalize_match_text(text)
            vector = self.vectors.get(key)
            if vector is None:
                digest = hashlib.sha256(key.encode("utf-8")).digest()
                values = [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(self.dimensions)]
                vector = _normalize_vector(values)
            output.append(list(vector))
        return output


class StoryRepository(Protocol):
    def exact_story(self, calendar_date: date, exact_hash: str) -> CanonicalStory | None: ...
    def candidates(self, calendar_date: date, category_ids: set[str], limit: int) -> list[CanonicalStory]: ...
    def create_story(self, story: CanonicalStory) -> None: ...
    def update_story(self, story: CanonicalStory) -> None: ...
    def add_occurrence(self, occurrence: StoryOccurrence) -> None: ...
    def add_review(self, review: ReviewCase) -> None: ...


class InMemoryStoryRepository:
    def __init__(self) -> None:
        self.stories: dict[str, CanonicalStory] = {}
        self.exact_index: dict[tuple[date, str], str] = {}
        self.occurrences: list[StoryOccurrence] = []
        self.reviews: list[ReviewCase] = []

    def exact_story(self, calendar_date: date, exact_hash: str) -> CanonicalStory | None:
        story_id = self.exact_index.get((calendar_date, exact_hash))
        return self.stories.get(story_id) if story_id else None

    def candidates(self, calendar_date: date, category_ids: set[str], limit: int) -> list[CanonicalStory]:
        values = [
            story
            for story in self.stories.values()
            if story.calendar_date == calendar_date and story.category_ids.intersection(category_ids)
        ]
        values.sort(key=lambda story: story.last_seen_at, reverse=True)
        return values[:limit]

    def create_story(self, story: CanonicalStory) -> None:
        self.stories[story.story_id] = story
        for exact_hash in story.exact_hashes:
            self.exact_index[(story.calendar_date, exact_hash)] = story.story_id

    def update_story(self, story: CanonicalStory) -> None:
        self.stories[story.story_id] = story
        for exact_hash in story.exact_hashes:
            self.exact_index[(story.calendar_date, exact_hash)] = story.story_id

    def add_occurrence(self, occurrence: StoryOccurrence) -> None:
        self.occurrences.append(occurrence)

    def add_review(self, review: ReviewCase) -> None:
        self.reviews.append(review)


def pakistan_calendar_date(value: datetime) -> date:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(PKT).date()


def exact_dedup_key(text: str) -> str:
    normalized = normalize_match_text(text)
    characters: list[str] = []
    previous_space = True
    for character in normalized:
        category = unicodedata.category(character)
        if category.startswith("P") or category.startswith("S") or character.isspace():
            if not previous_space:
                characters.append(" ")
            previous_space = True
            continue
        characters.append(character)
        previous_space = False
    return "".join(characters).strip()


def _extract_proper_names(text: str) -> frozenset[str]:
    names: set[str] = set()
    english_stop = {
        "The", "A", "An", "After", "Before", "During", "Today", "Tonight",
        "Pakistan",  # country context alone is too broad to identify one story
    }
    for match in re.finditer(r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:\s+(?:[A-Z][a-z]+|[A-Z]{2,})){0,3}\b", text):
        phrase = match.group(0).strip()
        if phrase in english_stop:
            continue
        names.add(normalize_match_text(phrase))

    urdu_title_pattern = re.compile(
        r"(?:وزیر اعظم|وزیر اعلیٰ|چیف جسٹس|جسٹس|سینیٹر|ڈاکٹر|جنرل|چیئرمین)\s+"
        r"([\u0600-\u06FF]+(?:\s+[\u0600-\u06FF]+){0,2})"
    )
    for match in urdu_title_pattern.finditer(text):
        names.add(normalize_match_text(match.group(1)))
    return frozenset(names)


def story_features(text: str) -> StoryFeatures:
    exact_key = exact_dedup_key(text)
    translated_digits = exact_key.translate(_DIGIT_TRANSLATION)
    tokens = frozenset(_TOKEN_RE.findall(translated_digits))
    numbers = tuple(sorted(set(match.group(0).replace("،", ",") for match in _NUMBER_RE.finditer(translated_digits))))
    proper_names = _extract_proper_names(text)
    negations = frozenset(term for values in _NEGATION_TERMS.values() for term in values if term in tokens)
    contradiction_terms = frozenset(term for group in _CONTRADICTION_GROUPS for term in group if term in tokens)
    return StoryFeatures(
        exact_key=exact_key,
        exact_hash=hashlib.sha256(exact_key.encode("utf-8")).hexdigest(),
        tokens=tokens,
        numbers=numbers,
        proper_names=proper_names,
        negations=negations,
        contradiction_terms=contradiction_terms,
    )


def lexical_jaccard(left: StoryFeatures, right: StoryFeatures) -> float:
    if not left.tokens and not right.tokens:
        return 1.0
    union = left.tokens.union(right.tokens)
    return len(left.tokens.intersection(right.tokens)) / len(union) if union else 0.0


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    a = np.asarray(left, dtype=np.float32)
    b = np.asarray(right, dtype=np.float32)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator == 0.0:
        return 0.0
    return float(np.dot(a, b) / denominator)


def _normalize_vector(values: Sequence[float]) -> list[float]:
    vector = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return [0.0 for _ in values]
    return [float(value) for value in vector / norm]


def _numbers_compatible(left: StoryFeatures, right: StoryFeatures) -> bool:
    # Numeric evidence is treated as event identity. A missing number on one side is
    # uncertain rather than automatically compatible, so it goes to review instead
    # of risking a false merge.
    if not left.numbers and not right.numbers:
        return True
    return set(left.numbers) == set(right.numbers)


def _named_entities_compatible(left: StoryFeatures, right: StoryFeatures) -> bool:
    if not left.proper_names and not right.proper_names:
        return True
    if not left.proper_names or not right.proper_names:
        return False
    return left.proper_names == right.proper_names


def _negation_compatible(left: StoryFeatures, right: StoryFeatures) -> bool:
    return bool(left.negations) == bool(right.negations)


def _contradiction_free(left: StoryFeatures, right: StoryFeatures) -> bool:
    for group in _CONTRADICTION_GROUPS:
        left_terms = left.contradiction_terms.intersection(group)
        right_terms = right.contradiction_terms.intersection(group)
        if left_terms and right_terms and left_terms != right_terms:
            return False
    return True


def _is_cross_channel_window(observation: DetectionObservation, story: CanonicalStory, minutes: int) -> bool:
    if observation.stream_id in story.source_stream_ids:
        return False
    difference = abs(observation.observed_at - story.last_seen_at)
    return difference <= timedelta(minutes=minutes)


def _average_embedding(existing: Sequence[float], count: int, new: Sequence[float]) -> list[float]:
    if not existing:
        return list(new)
    vector = ((np.asarray(existing, dtype=np.float32) * count) + np.asarray(new, dtype=np.float32)) / (count + 1)
    return _normalize_vector(vector.tolist())


class DeduplicationService:
    def __init__(
        self,
        repository: StoryRepository,
        embedding_provider: EmbeddingProvider,
        config: DeduplicationConfig | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.config = config or DeduplicationConfig.from_env()

    def process(self, observation: DetectionObservation) -> DeduplicationResult:
        if not observation.matched:
            raise ValueError("Only accepted keyword observations may enter story deduplication")

        calendar_date = pakistan_calendar_date(observation.observed_at)
        category_ids = {item.category_id for item in observation.accepted_categories}
        keyword_ids = tuple(sorted({item.keyword_id for item in observation.keyword_hits}))
        features = story_features(observation.text)
        exact_story = self.repository.exact_story(calendar_date, features.exact_hash)

        if exact_story is not None:
            return self._merge(
                observation,
                exact_story,
                features,
                category_ids,
                keyword_ids,
                semantic_similarity=1.0,
                lexical_overlap=1.0,
                layer="exact_hash",
                best_candidate=None,
            )

        vector = self.embedding_provider.encode([observation.text])[0]
        candidates = self.repository.candidates(calendar_date, category_ids, self.config.candidate_limit)
        scored = [self._score_candidate(observation, features, vector, category_ids, story) for story in candidates]
        scored.sort(key=lambda item: item.semantic_similarity, reverse=True)
        best = scored[0] if scored else None

        if best and best.auto_merge_allowed:
            story = next(story for story in candidates if story.story_id == best.story_id)
            return self._merge(
                observation,
                story,
                features,
                category_ids,
                keyword_ids,
                semantic_similarity=best.semantic_similarity,
                lexical_overlap=best.lexical_overlap,
                layer="cross_channel_window" if best.within_cross_channel_window else "semantic_same_day",
                best_candidate=best,
                vector=vector,
            )

        if best and best.review_allowed:
            return self._hold_for_review(observation, features, category_ids, keyword_ids, best)

        return self._create(observation, features, category_ids, keyword_ids, vector, best)

    def _score_candidate(
        self,
        observation: DetectionObservation,
        features: StoryFeatures,
        vector: Sequence[float],
        category_ids: set[str],
        story: CanonicalStory,
    ) -> CandidateScore:
        candidate_features = story_features(story.latest_text)
        semantic = cosine_similarity(vector, story.embedding)
        lexical = lexical_jaccard(features, candidate_features)
        category_overlap = tuple(sorted(category_ids.intersection(story.category_ids)))
        within_window = _is_cross_channel_window(
            observation, story, self.config.cross_channel_window_minutes
        )
        same_language = observation.language == story.original_language or "mixed" in {
            observation.language,
            story.original_language,
        }
        numbers_compatible = _numbers_compatible(features, candidate_features)
        named_entities_compatible = _named_entities_compatible(features, candidate_features)
        negation_compatible = _negation_compatible(features, candidate_features)
        contradiction_free = _contradiction_free(features, candidate_features)

        if not same_language:
            threshold = self.config.cross_language_auto_merge_similarity
        elif within_window:
            threshold = self.config.cross_channel_window_similarity
        else:
            threshold = self.config.same_day_auto_merge_similarity

        semantic_and_lexical = semantic >= threshold and (
            lexical >= self.config.minimum_lexical_overlap
            or semantic >= self.config.high_semantic_lexical_bypass
        )
        safeguards = (
            bool(category_overlap)
            and numbers_compatible
            and named_entities_compatible
            and negation_compatible
            and contradiction_free
        )
        auto_merge = semantic_and_lexical and safeguards
        review_allowed = (
            semantic >= self.config.review_similarity
            and bool(category_overlap)
            and not auto_merge
        )

        reasons: list[str] = []
        if not category_overlap:
            reasons.append("no_category_overlap")
        if not numbers_compatible:
            reasons.append("number_conflict")
        if not named_entities_compatible:
            reasons.append("named_entity_conflict_or_missing_anchor")
        if not negation_compatible:
            reasons.append("negation_conflict")
        if not contradiction_free:
            reasons.append("event_state_conflict")
        if semantic < threshold:
            reasons.append("semantic_below_auto_merge")
        if lexical < self.config.minimum_lexical_overlap and semantic < self.config.high_semantic_lexical_bypass:
            reasons.append("insufficient_exact_token_overlap")
        if auto_merge:
            reasons.append("all_auto_merge_guards_passed")

        return CandidateScore(
            story_id=story.story_id,
            semantic_similarity=round(semantic, 6),
            lexical_overlap=round(lexical, 6),
            within_cross_channel_window=within_window,
            same_language=same_language,
            numbers_compatible=numbers_compatible,
            named_entities_compatible=named_entities_compatible,
            negation_compatible=negation_compatible,
            contradiction_free=contradiction_free,
            category_overlap=category_overlap,
            merge_threshold=threshold,
            auto_merge_allowed=auto_merge,
            review_allowed=review_allowed,
            reasons=tuple(reasons),
        )

    def _create(
        self,
        observation: DetectionObservation,
        features: StoryFeatures,
        category_ids: set[str],
        keyword_ids: tuple[str, ...],
        vector: Sequence[float],
        best: CandidateScore | None,
    ) -> DeduplicationResult:
        story_id = str(uuid.uuid4())
        story = CanonicalStory(
            story_id=story_id,
            calendar_date=pakistan_calendar_date(observation.observed_at),
            original_text=observation.text,
            normalized_text=observation.normalized_text,
            original_language=observation.language,
            category_ids=set(category_ids),
            first_seen_at=observation.observed_at,
            last_seen_at=observation.observed_at,
            occurrence_count=1,
            source_channels={observation.channel_name},
            source_stream_ids={observation.stream_id},
            exact_hashes={features.exact_hash},
            embedding=list(vector),
            embedding_count=1,
            confidence=observation.confidence,
            latest_text=observation.text,
        )
        self.repository.create_story(story)
        occurrence = self._occurrence(
            observation,
            story_id,
            features,
            category_ids,
            keyword_ids,
            decision="created",
            layer="new_canonical_story",
            best=best,
        )
        self.repository.add_occurrence(occurrence)
        events = (
            observation.serializable(),
            {"event": "canonical_story_created", "data": story.serializable()},
        )
        return DeduplicationResult(
            observation_id=observation.observation_id,
            action="created",
            dedup_layer="new_canonical_story",
            story=story,
            occurrence=occurrence,
            best_candidate=best,
            summary_eligible=True,
            emitted_events=events,
        )

    def _merge(
        self,
        observation: DetectionObservation,
        story: CanonicalStory,
        features: StoryFeatures,
        category_ids: set[str],
        keyword_ids: tuple[str, ...],
        *,
        semantic_similarity: float,
        lexical_overlap: float,
        layer: str,
        best_candidate: CandidateScore | None,
        vector: Sequence[float] | None = None,
    ) -> DeduplicationResult:
        if vector is None:
            vector = self.embedding_provider.encode([observation.text])[0]
        story.last_seen_at = max(story.last_seen_at, observation.observed_at)
        story.occurrence_count += 1
        story.source_channels.add(observation.channel_name)
        story.source_stream_ids.add(observation.stream_id)
        story.category_ids.update(category_ids)
        story.exact_hashes.add(features.exact_hash)
        story.embedding = _average_embedding(story.embedding, story.embedding_count, vector)
        story.embedding_count += 1
        story.latest_text = observation.text
        # Prefer the best observed display sentence, never a lower-confidence OCR rendition.
        if observation.confidence > story.confidence or (
            math.isclose(observation.confidence, story.confidence) and len(observation.text) > len(story.original_text)
        ):
            story.original_text = observation.text
            story.normalized_text = observation.normalized_text
            story.original_language = observation.language
            story.confidence = observation.confidence
        self.repository.update_story(story)

        occurrence = self._occurrence(
            observation,
            story.story_id,
            features,
            category_ids,
            keyword_ids,
            decision="merged",
            layer=layer,
            best=best_candidate,
            semantic_similarity=semantic_similarity,
            lexical_overlap=lexical_overlap,
        )
        self.repository.add_occurrence(occurrence)
        events = (
            observation.serializable(),
            {
                "event": "canonical_story_updated",
                "data": {
                    **story.serializable(),
                    "update_reason": "repeat_observation",
                    "dedup_layer": layer,
                    "latest_occurrence": occurrence.serializable(),
                },
            },
        )
        return DeduplicationResult(
            observation_id=observation.observation_id,
            action="merged",
            dedup_layer=layer,
            story=story,
            occurrence=occurrence,
            best_candidate=best_candidate,
            summary_eligible=False,
            emitted_events=events,
        )

    def _hold_for_review(
        self,
        observation: DetectionObservation,
        features: StoryFeatures,
        category_ids: set[str],
        keyword_ids: tuple[str, ...],
        best: CandidateScore,
    ) -> DeduplicationResult:
        review = ReviewCase(
            review_id=str(uuid.uuid4()),
            observation_id=observation.observation_id,
            candidate_story_id=best.story_id,
            calendar_date=pakistan_calendar_date(observation.observed_at),
            semantic_similarity=best.semantic_similarity,
            lexical_overlap=best.lexical_overlap,
            evidence=best.serializable(),
        )
        self.repository.add_review(review)
        occurrence = self._occurrence(
            observation,
            None,
            features,
            category_ids,
            keyword_ids,
            decision="pending_review",
            layer="semantic_review_gate",
            best=best,
            semantic_similarity=best.semantic_similarity,
            lexical_overlap=best.lexical_overlap,
        )
        self.repository.add_occurrence(occurrence)
        events = (
            observation.serializable(),
            {
                "event": "dedup_review_requested",
                "data": {
                    "review_id": review.review_id,
                    "observation_id": observation.observation_id,
                    "candidate_story_id": best.story_id,
                    "summary_eligible": False,
                    "evidence": best.serializable(),
                },
            },
        )
        return DeduplicationResult(
            observation_id=observation.observation_id,
            action="pending_review",
            dedup_layer="semantic_review_gate",
            story=None,
            occurrence=occurrence,
            best_candidate=best,
            summary_eligible=False,
            emitted_events=events,
            review_case=review,
        )

    def _occurrence(
        self,
        observation: DetectionObservation,
        story_id: str | None,
        features: StoryFeatures,
        category_ids: set[str],
        keyword_ids: tuple[str, ...],
        *,
        decision: str,
        layer: str,
        best: CandidateScore | None,
        semantic_similarity: float | None = None,
        lexical_overlap: float | None = None,
    ) -> StoryOccurrence:
        return StoryOccurrence(
            occurrence_id=str(uuid.uuid4()),
            observation_id=observation.observation_id,
            story_id=story_id,
            stream_id=observation.stream_id,
            channel_name=observation.channel_name,
            observed_at=observation.observed_at,
            calendar_date=pakistan_calendar_date(observation.observed_at),
            source_text=observation.text,
            normalized_text=observation.normalized_text,
            language=observation.language,
            confidence=observation.confidence,
            category_ids=tuple(sorted(category_ids)),
            keyword_ids=keyword_ids,
            exact_hash=features.exact_hash,
            decision=decision,
            dedup_layer=layer,
            semantic_similarity=semantic_similarity,
            lexical_overlap=lexical_overlap,
            decision_metadata=best.serializable() if best else {},
        )


class DedupBackpressureError(RuntimeError):
    pass


class BoundedObservationBus:
    def __init__(self, capacity: int = 128) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._queue: asyncio.Queue[DetectionObservation] = asyncio.Queue(maxsize=capacity)
        self.publish_count = 0
        self.high_watermark = 0

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def capacity(self) -> int:
        return self._queue.maxsize

    async def publish(self, observation: DetectionObservation, timeout_seconds: float = 5.0) -> None:
        try:
            await asyncio.wait_for(self._queue.put(observation), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise DedupBackpressureError(
                f"Deduplication queue remained full for {timeout_seconds:.1f}s; no observation was silently dropped"
            ) from exc
        self.publish_count += 1
        self.high_watermark = max(self.high_watermark, self._queue.qsize())

    async def consume(self) -> DetectionObservation:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()


class DedupResultSink(Protocol):
    async def write(self, result: DeduplicationResult) -> None: ...


class InMemoryDedupResultSink:
    def __init__(self) -> None:
        self.results: list[DeduplicationResult] = []

    async def write(self, result: DeduplicationResult) -> None:
        self.results.append(result)


class DedupBusWorker:
    def __init__(
        self,
        bus: BoundedObservationBus,
        service: DeduplicationService,
        sink: DedupResultSink,
    ) -> None:
        self.bus = bus
        self.service = service
        self.sink = sink
        self.processed_observations = 0
        self.created_stories = 0
        self.merged_observations = 0
        self.pending_reviews = 0
        self.failed_observations = 0
        self.last_error: str | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() or self.bus.size:
            try:
                observation = await asyncio.wait_for(self.bus.consume(), timeout=0.25)
            except TimeoutError:
                continue
            try:
                result = self.service.process(observation)
                self.processed_observations += 1
                if result.action == "created":
                    self.created_stories += 1
                elif result.action == "merged":
                    self.merged_observations += 1
                elif result.action == "pending_review":
                    self.pending_reviews += 1
                await self.sink.write(result)
            except Exception as exc:
                self.failed_observations += 1
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                raise
            finally:
                self.bus.task_done()


def deduplication_doctor(*, load_model: bool = False) -> dict[str, Any]:
    config = DeduplicationConfig.from_env()
    report: dict[str, Any] = {
        "status": "ready",
        "phase": 6,
        "python_policy": "CPython 3.12.x x64",
        "model": {
            "name": config.model_name,
            "revision": config.model_revision,
            "dimensions": config.embedding_dimensions,
            "device": "cpu",
            "accepted_for_production": False,
            "acceptance_requires_labelled_urdu_english_story_pairs": True,
        },
        "layers": [
            "frame_text_guard_phase_3",
            "exact_normalized_hash",
            "semantic_same_day",
            "cross_channel_30_minute_window",
            "human_review_gate",
        ],
        "safeguards": {
            "category_overlap_required": True,
            "number_conflicts_block_merge": True,
            "named_entity_conflicts_block_merge": True,
            "negation_conflicts_block_merge": True,
            "event_state_conflicts_block_merge": True,
            "uncertain_matches_hidden_from_summaries": True,
            "every_observation_emitted_live": True,
            "fuzzy_spelling_logic": False,
        },
        "thresholds": asdict(config),
    }
    if load_model:
        started = time.perf_counter()
        try:
            provider = SentenceTransformerEmbeddingProvider(config)
            vectors = provider.encode(["Pakistan news story", "پاکستان کی خبر"])
            report["model"]["load_seconds"] = round(time.perf_counter() - started, 3)
            report["model"]["vector_count"] = len(vectors)
            report["model"]["loaded_dimensions"] = len(vectors[0]) if vectors else 0
        except Exception as exc:  # pragma: no cover - host/model download dependent
            report["status"] = "not_ready"
            report["model"]["error"] = f"{exc.__class__.__name__}: {exc}"
    return report


@dataclass(slots=True, frozen=True)
class DedupBenchmarkStep:
    step_id: str
    text: str
    language: str
    stream_id: str
    channel_name: str
    observed_at: datetime
    category_ids: tuple[str, ...]
    expected_action: str
    embedding: tuple[float, ...]


@dataclass(slots=True, frozen=True)
class DedupBenchmarkReport:
    step_count: int
    passed: int
    failed: int
    exact_pass_rate: float
    accepted: bool
    failures: tuple[dict[str, Any], ...]
    median_processing_ms: float
    p95_processing_ms: float

    def serializable(self) -> dict[str, Any]:
        return asdict(self)


def _benchmark_observation(step: DedupBenchmarkStep) -> DetectionObservation:
    # Minimal accepted observation without invoking Phase 5's matcher.
    from .keyword_matching import CategoryDecision, KeywordHit

    category_decisions = tuple(
        CategoryDecision(
            category_id=category_id,
            label_en=category_id.title(),
            label_ur=category_id,
            color="#7C3AED",
            score=1.0,
            accepted=True,
            reason="benchmark",
            keyword_ids=(f"kw-{category_id}",),
            supporting_context=(),
            excluded_context=(),
        )
        for category_id in step.category_ids
    )
    keyword_hits = tuple(
        KeywordHit(
            keyword_id=f"kw-{category_id}",
            category_id=category_id,
            term=category_id,
            normalized_term=category_id,
            language=step.language if step.language in {"en", "ur"} else "en",
            priority="normal",
            requires_context=False,
            matched_text=category_id,
            normalized_span=(0, len(category_id)),
            original_span=(0, len(category_id)),
        )
        for category_id in step.category_ids
    )
    return DetectionObservation(
        observation_id=f"obs-{step.step_id}",
        unit_id=f"unit-{step.step_id}",
        stream_id=step.stream_id,
        channel_name=step.channel_name,
        observed_at=step.observed_at,
        text=step.text,
        normalized_text=normalize_match_text(step.text),
        language=step.language,
        confidence=0.99,
        review_required=False,
        keyword_hits=keyword_hits,
        category_decisions=category_decisions,
        urgency="normal",
        emit_immediately=True,
        canonical_story_status="pending_phase_6_deduplication",
        summary_status="blocked_until_canonical_story_deduplication",
        snapshot_version="benchmark",
    )


def load_dedup_manifest(path: Path) -> list[DedupBenchmarkStep]:
    steps: list[DedupBenchmarkStep] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        payload = json.loads(raw)
        try:
            observed_at = datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
            steps.append(
                DedupBenchmarkStep(
                    step_id=str(payload["step_id"]),
                    text=str(payload["text"]),
                    language=str(payload["language"]),
                    stream_id=str(payload["stream_id"]),
                    channel_name=str(payload["channel_name"]),
                    observed_at=observed_at,
                    category_ids=tuple(payload["category_ids"]),
                    expected_action=str(payload["expected_action"]),
                    embedding=tuple(float(value) for value in payload["embedding"]),
                )
            )
        except Exception as exc:
            raise ValueError(f"Invalid dedup manifest line {line_number}: {exc}") from exc
    return steps


def benchmark_deduplication(steps: Sequence[DedupBenchmarkStep]) -> DedupBenchmarkReport:
    vectors = {step.text: step.embedding for step in steps}
    provider = FixedEmbeddingProvider(vectors)
    repository = InMemoryStoryRepository()
    service = DeduplicationService(repository, provider)
    failures: list[dict[str, Any]] = []
    durations: list[float] = []
    for step in steps:
        started = time.perf_counter()
        result = service.process(_benchmark_observation(step))
        durations.append((time.perf_counter() - started) * 1000.0)
        if result.action != step.expected_action:
            failures.append(
                {
                    "step_id": step.step_id,
                    "expected_action": step.expected_action,
                    "actual_action": result.action,
                    "result": result.serializable(),
                }
            )
    ordered = sorted(durations)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1) if ordered else 0
    passed = len(steps) - len(failures)
    return DedupBenchmarkReport(
        step_count=len(steps),
        passed=passed,
        failed=len(failures),
        exact_pass_rate=passed / len(steps) if steps else 0.0,
        accepted=not failures and bool(steps),
        failures=tuple(failures),
        median_processing_ms=round(median(durations), 4) if durations else 0.0,
        p95_processing_ms=round(ordered[p95_index], 4) if ordered else 0.0,
    )
