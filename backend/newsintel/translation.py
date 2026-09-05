from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import queue
import re
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence

URDU_RANGE_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
LATIN_RE = re.compile(r"[A-Za-z]")
LATIN_WORD_RE = re.compile(r"\b[A-Za-z]{2,}\b")
NUMBER_RE = re.compile(r"(?:\d[\d,.:/-]*\d|\d)")
CONTROL_TOKEN_RE = re.compile(r"(?:<pad>|</s>|<unk>|▁)", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")

URDU_TO_ASCII_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

ENGLISH_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000, "million": 1_000_000,
    "billion": 1_000_000_000,
}
URDU_NUMBER_WORDS = {
    "صفر": 0, "ایک": 1, "دو": 2, "تین": 3, "چار": 4, "پانچ": 5,
    "چھ": 6, "سات": 7, "آٹھ": 8, "نو": 9, "دس": 10,
    "گیارہ": 11, "بارہ": 12, "تیرہ": 13, "چودہ": 14, "پندرہ": 15,
    "سولہ": 16, "سترہ": 17, "اٹھارہ": 18, "انیس": 19, "بیس": 20,
    "اکیس": 21, "بائیس": 22, "تئیس": 23, "چوبیس": 24, "پچیس": 25,
    "چھبیس": 26, "ستائیس": 27, "اٹھائیس": 28, "انتیس": 29,
    "تیس": 30, "چالیس": 40, "پچاس": 50, "ساٹھ": 60, "ستر": 70,
    "اسی": 80, "نوے": 90, "سو": 100, "ہزار": 1000,
    "لاکھ": 100_000, "ملین": 1_000_000, "کروڑ": 10_000_000,
    "ارب": 1_000_000_000,
}
ENGLISH_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|neither|nor|without|denied|rejected|refused|failed\s+to|didn['’]?t|doesn['’]?t|won['’]?t|cannot|can['’]?t)\b",
    re.IGNORECASE,
)
URDU_NEGATION_RE = re.compile(r"(?:^|[\s،۔!؟?])(?:نہیں|نہ|بغیر|ہرگز|مسترد|انکار|ناکام)(?=$|[\s،۔!؟?])")

UR_EN_MODEL = "Helsinki-NLP/opus-mt-ur-en"
UR_EN_REVISION = "7be1b539f1396ec91378efec4ca6ae1b9e5da6bd"
EN_UR_MODEL = "Helsinki-NLP/opus-mt-en-ur"
EN_UR_REVISION = "4642e030400759ebc20834837cd3ed4c9ca526b5"


class TranslationError(RuntimeError):
    pass


class TranslationBackpressureError(TranslationError):
    pass


class TranslationEngine(Protocol):
    name: str

    def translate_batch(
        self,
        texts: Sequence[str],
        *,
        source_language: str,
        target_language: str,
        beam_size: int,
    ) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class TranslationConfig:
    model_root: Path
    compute_type: str = "int8"
    inter_threads: int = 2
    intra_threads: int = 2
    beam_size: int = 2
    retry_beam_size: int = 4
    max_batch_size: int = 8
    micro_batch_wait_ms: int = 35
    queue_capacity: int = 256
    publish_timeout_seconds: float = 5.0
    cache_capacity: int = 10_000
    live_window_minutes: int = 30
    maximum_source_characters: int = 900

    @classmethod
    def from_env(cls) -> "TranslationConfig":
        root = Path(os.getenv("TRANSLATION_MODEL_ROOT", "models/translation"))
        return cls(
            model_root=root,
            compute_type=os.getenv("TRANSLATION_COMPUTE_TYPE", "int8"),
            inter_threads=max(1, int(os.getenv("TRANSLATION_INTER_THREADS", "2"))),
            intra_threads=max(1, int(os.getenv("TRANSLATION_INTRA_THREADS", "2"))),
            beam_size=max(1, int(os.getenv("TRANSLATION_BEAM_SIZE", "2"))),
            retry_beam_size=max(1, int(os.getenv("TRANSLATION_RETRY_BEAM_SIZE", "4"))),
            max_batch_size=max(1, int(os.getenv("TRANSLATION_MAX_BATCH_SIZE", "8"))),
            micro_batch_wait_ms=max(0, int(os.getenv("TRANSLATION_MICRO_BATCH_WAIT_MS", "35"))),
            queue_capacity=max(1, int(os.getenv("TRANSLATION_QUEUE_CAPACITY", "256"))),
            publish_timeout_seconds=max(
                0.1, float(os.getenv("TRANSLATION_PUBLISH_TIMEOUT_SECONDS", "5"))
            ),
            cache_capacity=max(100, int(os.getenv("TRANSLATION_CACHE_CAPACITY", "10000"))),
            live_window_minutes=max(1, int(os.getenv("LIVE_FEED_WINDOW_MINUTES", "30"))),
            maximum_source_characters=max(
                80, int(os.getenv("TRANSLATION_MAX_SOURCE_CHARACTERS", "900"))
            ),
        )


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    observation_id: str
    source_text: str
    source_language: str
    observed_at: datetime
    channel_name: str
    category_ids: tuple[str, ...] = ()
    canonical_story_id: str | None = None
    is_complete_sentence: bool = True

    @property
    def target_language(self) -> str:
        return "en" if self.source_language == "ur" else "ur"


@dataclass(frozen=True, slots=True)
class TranslationQuality:
    accepted: bool
    score: float
    checks: dict[str, bool]
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TranslationResult:
    observation_id: str
    source_text: str
    translated_text: str | None
    source_language: str
    target_language: str
    status: str
    model_name: str
    model_revision: str
    engine: str
    latency_ms: float
    cache_hit: bool
    quality: TranslationQuality
    failure_reason: str | None = None

    def serializable(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "status": self.status,
            "model_name": self.model_name,
            "model_revision": self.model_revision,
            "engine": self.engine,
            "latency_ms": round(self.latency_ms, 2),
            "cache_hit": self.cache_hit,
            "quality": {
                "accepted": self.quality.accepted,
                "score": round(self.quality.score, 4),
                "checks": self.quality.checks,
                "issues": list(self.quality.issues),
            },
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class LiveObservation:
    observation_id: str
    observed_at: datetime
    channel_name: str
    original_text: str
    source_language: str
    category_ids: tuple[str, ...]
    canonical_story_id: str | None
    translation_status: str = "pending"
    translation_text: str | None = None
    target_language: str | None = None
    repeat: bool = False

    def serializable(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
            "channel_name": self.channel_name,
            "original_text": self.original_text,
            "source_language": self.source_language,
            "category_ids": list(self.category_ids),
            "canonical_story_id": self.canonical_story_id,
            "translation_status": self.translation_status,
            "translation_text": self.translation_text,
            "target_language": self.target_language,
            "repeat": self.repeat,
        }


def normalize_translation_source(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = SPACE_RE.sub(" ", text).strip()
    return text


def translation_memory_key(text: str, source_language: str, target_language: str) -> str:
    normalized = normalize_translation_source(text)
    payload = f"{source_language}>{target_language}\0{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _numbers(text: str) -> tuple[str, ...]:
    normalized = text.translate(URDU_TO_ASCII_DIGITS)
    values = list(NUMBER_RE.findall(normalized))
    tokens = re.findall(r"[A-Za-z]+|[\u0600-\u06FF]+", normalized.casefold())
    values.extend(str(ENGLISH_NUMBER_WORDS[token]) for token in tokens if token in ENGLISH_NUMBER_WORDS)
    values.extend(str(URDU_NUMBER_WORDS[token]) for token in tokens if token in URDU_NUMBER_WORDS)
    return tuple(values)


def _has_negation(text: str, language: str) -> bool:
    if language == "ur":
        return bool(URDU_NEGATION_RE.search(text))
    return bool(ENGLISH_NEGATION_RE.search(text))


def _script_counts(text: str) -> tuple[int, int]:
    return len(URDU_RANGE_RE.findall(text)), len(LATIN_RE.findall(text))


def validate_complete_sentence_translation(
    source_text: str,
    translated_text: str,
    *,
    source_language: str,
    target_language: str,
) -> TranslationQuality:
    source = normalize_translation_source(source_text)
    target = normalize_translation_source(translated_text)
    source_urdu, source_latin = _script_counts(source)
    target_urdu, target_latin = _script_counts(target)
    source_numbers = set(_numbers(source))
    target_numbers = set(_numbers(target))
    length_ratio = len(target) / max(1, len(source))

    checks: dict[str, bool] = {
        "non_empty": bool(target),
        "not_control_tokens": not bool(CONTROL_TOKEN_RE.search(target)),
        "not_identical_to_source": target.casefold() != source.casefold(),
        "numbers_preserved": source_numbers.issubset(target_numbers),
        "reasonable_length": 0.22 <= length_ratio <= 4.5,
        "target_script_present": (target_urdu > 0 if target_language == "ur" else target_latin > 0),
        "target_script_dominant": (
            target_urdu >= max(1, target_latin)
            if target_language == "ur"
            else target_latin >= max(1, target_urdu)
        ),
        "source_script_consistent": (
            source_urdu >= source_latin if source_language == "ur" else source_latin >= source_urdu
        ),
        "negation_preserved": _has_negation(source, source_language) == _has_negation(target, target_language),
    }

    if target_language == "ur":
        latin_words = LATIN_WORD_RE.findall(target)
        checks["no_roman_urdu_output"] = len(latin_words) <= 3 or target_urdu >= target_latin * 2
    else:
        checks["no_roman_urdu_output"] = True

    critical = {
        "non_empty",
        "not_control_tokens",
        "numbers_preserved",
        "reasonable_length",
        "target_script_present",
        "target_script_dominant",
        "no_roman_urdu_output",
        "negation_preserved",
    }
    issues = tuple(name for name, passed in checks.items() if not passed)
    critical_passed = all(checks[name] for name in critical)
    score = sum(1 for value in checks.values() if value) / len(checks)
    return TranslationQuality(
        accepted=critical_passed and score >= 0.875,
        score=score,
        checks=checks,
        issues=issues,
    )


class MemoryTranslationCache:
    def __init__(self, capacity: int = 10_000) -> None:
        self.capacity = capacity
        self._items: OrderedDict[str, TranslationResult] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> TranslationResult | None:
        with self._lock:
            result = self._items.get(key)
            if result is not None:
                self._items.move_to_end(key)
            return result

    def put(self, key: str, result: TranslationResult) -> None:
        with self._lock:
            self._items[key] = result
            self._items.move_to_end(key)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)


class CTranslate2MarianEngine:
    name = "CTranslate2 MarianMT INT8 CPU"

    def __init__(self, config: TranslationConfig) -> None:
        self.config = config
        self._pairs: dict[tuple[str, str], tuple[Any, Any]] = {}
        self._lock = threading.Lock()

    def _pair_config(self, source: str, target: str) -> tuple[Path, str, str]:
        if (source, target) == ("ur", "en"):
            return self.config.model_root / "ur-en", UR_EN_MODEL, UR_EN_REVISION
        if (source, target) == ("en", "ur"):
            return self.config.model_root / "en-ur", EN_UR_MODEL, EN_UR_REVISION
        raise TranslationError(f"Unsupported translation direction: {source}>{target}")

    def _load_pair(self, source: str, target: str) -> tuple[Any, Any]:
        key = (source, target)
        with self._lock:
            existing = self._pairs.get(key)
            if existing is not None:
                return existing
            model_dir, _, _ = self._pair_config(source, target)
            if not model_dir.exists():
                raise TranslationError(
                    f"Converted translation model is missing: {model_dir}. "
                    "Run prepare-translation-models.ps1."
                )
            try:
                import ctranslate2
                from transformers import AutoTokenizer
            except Exception as exc:  # pragma: no cover - dependency doctor covers this
                raise TranslationError(f"Translation runtime dependency unavailable: {exc}") from exc

            translator = ctranslate2.Translator(
                str(model_dir),
                device="cpu",
                compute_type=self.config.compute_type,
                inter_threads=self.config.inter_threads,
                intra_threads=self.config.intra_threads,
                max_queued_batches=self.config.queue_capacity,
            )
            tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
            self._pairs[key] = (translator, tokenizer)
            return translator, tokenizer

    def translate_batch(
        self,
        texts: Sequence[str],
        *,
        source_language: str,
        target_language: str,
        beam_size: int,
    ) -> list[str]:
        translator, tokenizer = self._load_pair(source_language, target_language)
        token_batches = [
            tokenizer.convert_ids_to_tokens(tokenizer.encode(text, truncation=True, max_length=512))
            for text in texts
        ]
        results = translator.translate_batch(
            token_batches,
            beam_size=beam_size,
            max_batch_size=self.config.max_batch_size,
            batch_type="tokens",
            return_scores=False,
        )
        output: list[str] = []
        for result in results:
            token_ids = tokenizer.convert_tokens_to_ids(result.hypotheses[0])
            output.append(tokenizer.decode(token_ids, skip_special_tokens=True).strip())
        return output


class TranslationService:
    def __init__(
        self,
        engine: TranslationEngine,
        *,
        config: TranslationConfig | None = None,
        cache: MemoryTranslationCache | None = None,
    ) -> None:
        self.engine = engine
        self.config = config or TranslationConfig.from_env()
        self.cache = cache or MemoryTranslationCache(self.config.cache_capacity)

    def _model_identity(self, source: str, target: str) -> tuple[str, str]:
        if (source, target) == ("ur", "en"):
            return UR_EN_MODEL, UR_EN_REVISION
        return EN_UR_MODEL, EN_UR_REVISION

    def _validate_request(self, request: TranslationRequest) -> tuple[str, str, str, str, str]:
        if request.source_language not in {"en", "ur"}:
            raise TranslationError("Only English and Urdu source sentences are accepted.")
        if not request.is_complete_sentence:
            raise TranslationError("Translation accepts complete sentence units only.")
        source = normalize_translation_source(request.source_text)
        if not source:
            raise TranslationError("Source sentence is empty.")
        if len(source) > self.config.maximum_source_characters:
            raise TranslationError("Source sentence exceeds the configured complete-unit limit.")
        target = request.target_language
        key = translation_memory_key(source, request.source_language, target)
        return source, target, *self._model_identity(request.source_language, target), key

    @staticmethod
    def _cached_for_observation(cached: TranslationResult, observation_id: str) -> TranslationResult:
        return TranslationResult(
            observation_id=observation_id,
            source_text=cached.source_text,
            translated_text=cached.translated_text,
            source_language=cached.source_language,
            target_language=cached.target_language,
            status=cached.status,
            model_name=cached.model_name,
            model_revision=cached.model_revision,
            engine=cached.engine,
            latency_ms=0.0,
            cache_hit=True,
            quality=cached.quality,
            failure_reason=cached.failure_reason,
        )

    def translate(self, request: TranslationRequest) -> TranslationResult:
        return self.translate_many([request])[0]

    def translate_many(self, requests: Sequence[TranslationRequest]) -> list[TranslationResult]:
        """Translate a micro-batch while collapsing exact repeats within the batch.

        Results preserve request order. Requests are grouped by translation direction so each
        direction uses one CTranslate2 batch, and low-quality outputs are retried together.
        """
        if not requests:
            return []

        results: list[TranslationResult | None] = [None] * len(requests)
        pending_by_direction: dict[
            tuple[str, str], dict[str, dict[str, Any]]
        ] = {}

        for index, request in enumerate(requests):
            source, target, model_name, revision, key = self._validate_request(request)
            cached = self.cache.get(key)
            if cached is not None:
                results[index] = self._cached_for_observation(cached, request.observation_id)
                continue

            direction = (request.source_language, target)
            unique = pending_by_direction.setdefault(direction, {})
            entry = unique.get(key)
            if entry is None:
                unique[key] = {
                    "source": source,
                    "model_name": model_name,
                    "revision": revision,
                    "requests": [(index, request)],
                }
            else:
                entry["requests"].append((index, request))

        for (source_language, target_language), unique in pending_by_direction.items():
            entries = list(unique.values())
            texts = [entry["source"] for entry in entries]
            started = time.perf_counter()
            try:
                translated = self.engine.translate_batch(
                    texts,
                    source_language=source_language,
                    target_language=target_language,
                    beam_size=self.config.beam_size,
                )
                if len(translated) != len(entries):
                    raise TranslationError(
                        "Translation engine returned a different number of outputs than inputs."
                    )

                qualities = [
                    validate_complete_sentence_translation(
                        source,
                        output,
                        source_language=source_language,
                        target_language=target_language,
                    )
                    for source, output in zip(texts, translated, strict=True)
                ]

                retry_indexes = [
                    index
                    for index, quality in enumerate(qualities)
                    if not quality.accepted
                    and self.config.retry_beam_size > self.config.beam_size
                ]
                if retry_indexes:
                    retry_outputs = self.engine.translate_batch(
                        [texts[index] for index in retry_indexes],
                        source_language=source_language,
                        target_language=target_language,
                        beam_size=self.config.retry_beam_size,
                    )
                    if len(retry_outputs) != len(retry_indexes):
                        raise TranslationError(
                            "Translation retry returned a different number of outputs than inputs."
                        )
                    for entry_index, retry_output in zip(
                        retry_indexes, retry_outputs, strict=True
                    ):
                        translated[entry_index] = retry_output
                        qualities[entry_index] = validate_complete_sentence_translation(
                            texts[entry_index],
                            retry_output,
                            source_language=source_language,
                            target_language=target_language,
                        )

                elapsed_ms = (time.perf_counter() - started) * 1000
                per_item_latency = elapsed_ms / max(1, len(entries))
                for entry, output, quality in zip(
                    entries, translated, qualities, strict=True
                ):
                    normalized_output = normalize_translation_source(output)
                    status = "complete" if quality.accepted else "needs_review"
                    first_index, first_request = entry["requests"][0]
                    base_result = TranslationResult(
                        observation_id=first_request.observation_id,
                        source_text=entry["source"],
                        translated_text=normalized_output,
                        source_language=source_language,
                        target_language=target_language,
                        status=status,
                        model_name=entry["model_name"],
                        model_revision=entry["revision"],
                        engine=self.engine.name,
                        latency_ms=per_item_latency,
                        cache_hit=False,
                        quality=quality,
                    )
                    key = translation_memory_key(
                        entry["source"], source_language, target_language
                    )
                    self.cache.put(key, base_result)
                    results[first_index] = base_result
                    for duplicate_index, duplicate_request in entry["requests"][1:]:
                        results[duplicate_index] = self._cached_for_observation(
                            base_result, duplicate_request.observation_id
                        )
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                per_item_latency = elapsed_ms / max(1, len(entries))
                failure_reason = f"{exc.__class__.__name__}: {exc}"
                for entry in entries:
                    for index, request in entry["requests"]:
                        results[index] = TranslationResult(
                            observation_id=request.observation_id,
                            source_text=entry["source"],
                            translated_text=None,
                            source_language=source_language,
                            target_language=target_language,
                            status="failed",
                            model_name=entry["model_name"],
                            model_revision=entry["revision"],
                            engine=self.engine.name,
                            latency_ms=per_item_latency,
                            cache_hit=False,
                            quality=TranslationQuality(
                                False, 0.0, {}, ("runtime_failure",)
                            ),
                            failure_reason=failure_reason,
                        )

        if any(result is None for result in results):
            raise TranslationError("Translation batch did not resolve every request.")
        return [result for result in results if result is not None]


class RollingLiveFeed:
    """Thread-safe 30-minute occurrence window. Repeats are intentionally retained."""

    def __init__(self, window_minutes: int = 30, capacity: int = 20_000) -> None:
        self.window = timedelta(minutes=window_minutes)
        self.capacity = capacity
        self._order: deque[str] = deque()
        self._items: dict[str, LiveObservation] = {}
        self._lock = threading.Lock()

    def add_pending(self, request: TranslationRequest, *, repeat: bool = False) -> LiveObservation:
        item = LiveObservation(
            observation_id=request.observation_id,
            observed_at=request.observed_at,
            channel_name=request.channel_name,
            original_text=request.source_text,
            source_language=request.source_language,
            category_ids=request.category_ids,
            canonical_story_id=request.canonical_story_id,
            repeat=repeat,
        )
        with self._lock:
            if request.observation_id not in self._items:
                self._order.appendleft(request.observation_id)
            self._items[request.observation_id] = item
            self._trim_locked(request.observed_at)
        return item

    def apply_translation(self, result: TranslationResult) -> LiveObservation | None:
        with self._lock:
            current = self._items.get(result.observation_id)
            if current is None:
                return None
            updated = LiveObservation(
                observation_id=current.observation_id,
                observed_at=current.observed_at,
                channel_name=current.channel_name,
                original_text=current.original_text,
                source_language=current.source_language,
                category_ids=current.category_ids,
                canonical_story_id=current.canonical_story_id,
                translation_status=result.status,
                translation_text=result.translated_text,
                target_language=result.target_language,
                repeat=current.repeat,
            )
            self._items[result.observation_id] = updated
            return updated

    def snapshot(self, *, now: datetime | None = None) -> list[LiveObservation]:
        with self._lock:
            self._trim_locked(now or datetime.now(timezone.utc))
            return [self._items[item_id] for item_id in self._order if item_id in self._items]

    def _trim_locked(self, now: datetime) -> None:
        cutoff = now - self.window
        while self._order:
            oldest_id = self._order[-1]
            oldest = self._items.get(oldest_id)
            if oldest is None:
                self._order.pop()
                continue
            if len(self._order) <= self.capacity and oldest.observed_at >= cutoff:
                break
            self._order.pop()
            self._items.pop(oldest_id, None)


class TranslationCoordinator:
    """Publishes the original immediately and translation updates asynchronously."""

    def __init__(
        self,
        service: TranslationService,
        *,
        feed: RollingLiveFeed | None = None,
        config: TranslationConfig | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        result_sink: Callable[[TranslationResult], None] | None = None,
    ) -> None:
        self.service = service
        self.config = config or service.config
        self.feed = feed or RollingLiveFeed(self.config.live_window_minutes)
        self.event_sink = event_sink or (lambda event: None)
        self.result_sink = result_sink or (lambda result: None)
        self._queue: queue.Queue[TranslationRequest | None] = queue.Queue(
            maxsize=self.config.queue_capacity
        )
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="translation-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._running.clear()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout)

    def submit(self, request: TranslationRequest, *, repeat: bool = False) -> LiveObservation:
        pending = self.feed.add_pending(request, repeat=repeat)
        self.event_sink({"event": "live_observation", "data": pending.serializable()})
        try:
            self._queue.put(request, timeout=self.config.publish_timeout_seconds)
        except queue.Full as exc:
            self.event_sink(
                {
                    "event": "translation_backpressure",
                    "data": {"observation_id": request.observation_id},
                }
            )
            raise TranslationBackpressureError("Translation queue is saturated; request was not dropped silently.") from exc
        return pending

    def process_one(self, request: TranslationRequest) -> TranslationResult:
        result = self.service.translate(request)
        self.result_sink(result)
        updated = self.feed.apply_translation(result)
        self.event_sink(
            {
                "event": "translation_ready",
                "data": updated.serializable() if updated else result.serializable(),
            }
        )
        return result

    def _publish_result(self, result: TranslationResult) -> None:
        self.result_sink(result)
        updated = self.feed.apply_translation(result)
        self.event_sink(
            {
                "event": "translation_ready",
                "data": updated.serializable() if updated else result.serializable(),
            }
        )

    def _run(self) -> None:
        wait_seconds = self.config.micro_batch_wait_ms / 1000
        while self._running.is_set():
            try:
                first = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if first is None:
                self._queue.task_done()
                break

            batch = [first]
            deadline = time.monotonic() + wait_seconds
            stop_after_batch = False
            while len(batch) < self.config.max_batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if item is None:
                    self._queue.task_done()
                    stop_after_batch = True
                    break
                batch.append(item)

            try:
                for result in self.service.translate_many(batch):
                    self._publish_result(result)
            finally:
                for _ in batch:
                    self._queue.task_done()
            if stop_after_batch:
                break


class StaticTranslationEngine:
    """Deterministic test/preview engine; never used as the production default."""

    name = "Static fixture engine"

    def __init__(self, mappings: dict[tuple[str, str, str], str]) -> None:
        self.mappings = mappings
        self.calls: list[tuple[str, str, int, tuple[str, ...]]] = []

    def translate_batch(
        self,
        texts: Sequence[str],
        *,
        source_language: str,
        target_language: str,
        beam_size: int,
    ) -> list[str]:
        self.calls.append((source_language, target_language, beam_size, tuple(texts)))
        return [self.mappings[(source_language, target_language, text)] for text in texts]


def translation_doctor(*, load_models: bool = False) -> dict[str, Any]:
    config = TranslationConfig.from_env()
    dependencies = {
        "ctranslate2": importlib.util.find_spec("ctranslate2") is not None,
        "transformers": importlib.util.find_spec("transformers") is not None,
        "sentencepiece": importlib.util.find_spec("sentencepiece") is not None,
    }
    pairs = {
        "ur-en": {
            "model": UR_EN_MODEL,
            "revision": UR_EN_REVISION,
            "path": str(config.model_root / "ur-en"),
            "ready": (config.model_root / "ur-en" / "model.bin").exists(),
        },
        "en-ur": {
            "model": EN_UR_MODEL,
            "revision": EN_UR_REVISION,
            "path": str(config.model_root / "en-ur"),
            "ready": (config.model_root / "en-ur" / "model.bin").exists(),
        },
    }
    errors: list[str] = []
    if not all(dependencies.values()):
        errors.append("One or more translation dependencies are missing.")
    if load_models:
        engine = CTranslate2MarianEngine(config)
        for source, target in (("ur", "en"), ("en", "ur")):
            try:
                engine._load_pair(source, target)
            except Exception as exc:
                errors.append(f"{source}>{target}: {exc}")
    elif not all(pair["ready"] for pair in pairs.values()):
        errors.append("Converted INT8 models are not prepared yet.")

    return {
        "status": "ready" if not errors else "not_ready",
        "runtime": "CPython 3.12 x64 / CTranslate2 INT8 CPU",
        "dependencies": dependencies,
        "models": pairs,
        "policy": {
            "complete_sentence_only": True,
            "original_preserved": True,
            "translate_only_opposite_language": True,
            "roman_urdu_output_allowed": False,
            "live_observation_emitted_before_translation": True,
            "live_window_minutes": config.live_window_minutes,
            "exact_repeat_translation_memory": True,
        },
        "errors": errors,
    }


def benchmark_records(
    records: Iterable[dict[str, Any]],
    service: TranslationService,
) -> dict[str, Any]:
    """Run a strict, reproducible, human-curated news-domain benchmark.

    Each manifest row supplies one or more human-approved complete translations in
    ``acceptable_translations`` (or the legacy single ``reference_text``). A case only passes
    when the runtime output exactly matches one approved normalized sentence and all required
    and forbidden term checks pass. This avoids a self-asserted ``human_approved`` flag.
    """
    cases: list[dict[str, Any]] = []
    passed_count = 0
    for index, record in enumerate(records, start=1):
        request = TranslationRequest(
            observation_id=str(record.get("id", index)),
            source_text=record["source_text"],
            source_language=record["source_language"],
            observed_at=datetime.now(timezone.utc),
            channel_name="benchmark",
        )
        result = service.translate(request)
        approved = [
            normalize_translation_source(value)
            for value in record.get("acceptable_translations", [])
            if normalize_translation_source(value)
        ]
        legacy_reference = normalize_translation_source(record.get("reference_text", ""))
        if legacy_reference and legacy_reference not in approved:
            approved.append(legacy_reference)

        output = normalize_translation_source(result.translated_text or "")
        required_terms = [
            normalize_translation_source(value)
            for value in record.get("required_terms", [])
            if normalize_translation_source(value)
        ]
        forbidden_terms = [
            normalize_translation_source(value)
            for value in record.get("forbidden_terms", [])
            if normalize_translation_source(value)
        ]
        approved_match = output in approved if approved else False
        required_terms_present = all(term.casefold() in output.casefold() for term in required_terms)
        forbidden_terms_absent = all(term.casefold() not in output.casefold() for term in forbidden_terms)
        case_passed = (
            result.status == "complete"
            and approved_match
            and required_terms_present
            and forbidden_terms_absent
        )
        passed_count += int(case_passed)
        cases.append(
            {
                "id": request.observation_id,
                "status": result.status,
                "translated_text": result.translated_text,
                "acceptable_translations": approved,
                "approved_match": approved_match,
                "required_terms": required_terms,
                "required_terms_present": required_terms_present,
                "forbidden_terms": forbidden_terms,
                "forbidden_terms_absent": forbidden_terms_absent,
                "quality": result.quality.checks,
                "passed": case_passed,
            }
        )
    total = len(cases)
    return {
        "total": total,
        "passed": passed_count,
        "pass_rate": passed_count / total if total else 0.0,
        "acceptance": (
            "all cases complete, exact-match one human-approved complete translation, "
            "preserve required terms, and avoid forbidden terms"
        ),
        "cases": cases,
    }

