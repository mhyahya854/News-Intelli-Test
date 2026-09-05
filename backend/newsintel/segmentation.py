from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from .ocr import OCRFrameResult, OCRLine, normalize_ocr_text, script_profile

SPACE_RE = re.compile(r"\s+")
WORD_OR_PUNCT_RE = re.compile(
    r"[A-Za-z0-9]+(?:[./:-][A-Za-z0-9]+)*|[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]+|[^\w\s]",
    re.UNICODE,
)
LETTER_OR_NUMBER_RE = re.compile(r"[A-Za-z0-9\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", re.UNICODE)
ENGLISH_ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.", "no.", "nos.",
    "rs.", "pkr.", "vs.", "etc.", "e.g.", "i.e.", "a.m.", "p.m.", "u.s.", "u.k.",
}
URDU_TERMINALS = {"۔", "؟"}
COMMON_TERMINALS = {"!", "?"}
CLOSING_MARKS = {'"', "'", "’", "”", ")", "]", "}"}


class SegmentationError(RuntimeError):
    """Sentence reconstruction or segmentation failure."""


class SegmentationBackpressureError(SegmentationError):
    """The OCR-to-segmentation handoff remained full."""


@dataclass(slots=True, frozen=True)
class SegmentationConfig:
    rolling_frame_count: int = 3
    row_tolerance_ratio: float = 0.025
    track_tolerance_ratio: float = 0.045
    stable_headline_frames: int = 2
    stale_track_frames: int = 6
    minimum_unit_characters: int = 8
    minimum_unit_words: int = 3
    maximum_pending_characters: int = 1200
    emitted_hash_capacity: int = 1000
    exact_overlap_only: bool = True
    allow_fuzzy_overlap: bool = False
    allow_autocorrection: bool = False

    def __post_init__(self) -> None:
        if self.rolling_frame_count != 3:
            raise ValueError("rolling_frame_count is fixed at 3 by the Phase 4 contract")
        if not 0 < self.row_tolerance_ratio <= 0.2:
            raise ValueError("row_tolerance_ratio must be between 0 and 0.2")
        if not 0 < self.track_tolerance_ratio <= 0.2:
            raise ValueError("track_tolerance_ratio must be between 0 and 0.2")
        if self.stable_headline_frames < 2:
            raise ValueError("stable_headline_frames must be at least 2")
        if self.stale_track_frames < self.rolling_frame_count:
            raise ValueError("stale_track_frames cannot be shorter than the rolling window")
        if self.minimum_unit_characters < 1 or self.minimum_unit_words < 1:
            raise ValueError("minimum unit limits must be positive")
        if self.maximum_pending_characters < 100:
            raise ValueError("maximum_pending_characters is too small")
        if self.emitted_hash_capacity < 1:
            raise ValueError("emitted_hash_capacity must be positive")
        if not self.exact_overlap_only or self.allow_fuzzy_overlap or self.allow_autocorrection:
            raise ValueError("Phase 4 permits exact normalized overlap only")


@dataclass(slots=True, frozen=True)
class RegionObservation:
    text: str
    normalized_text: str
    language: str
    polygon: tuple[tuple[float, float], ...]
    center_y_ratio: float
    region_class: str
    confidence: float
    review_required: bool
    source_line_indexes: tuple[int, ...]


@dataclass(slots=True, frozen=True)
class SegmentedTextUnit:
    unit_id: str
    stream_id: str
    channel_name: str
    track_id: str
    text: str
    normalized_text: str
    language: str
    unit_type: str
    completion_reason: str
    confidence: float
    review_required: bool
    first_frame_sequence: int
    last_frame_sequence: int
    first_seen_at: datetime
    last_seen_at: datetime
    source_frame_sequences: tuple[int, ...]
    source_line_indexes: tuple[int, ...]

    def serializable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["first_seen_at"] = self.first_seen_at.isoformat()
        payload["last_seen_at"] = self.last_seen_at.isoformat()
        payload["source_frame_sequences"] = list(self.source_frame_sequences)
        payload["source_line_indexes"] = list(self.source_line_indexes)
        return payload


@dataclass(slots=True, frozen=True)
class IncompleteFragment:
    stream_id: str
    track_id: str
    text: str
    language: str
    reason: str
    first_frame_sequence: int
    last_frame_sequence: int

    def serializable(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class SegmentationFrameResult:
    stream_id: str
    channel_name: str
    frame_sequence: int
    frame_timestamp: datetime
    units: tuple[SegmentedTextUnit, ...]
    incomplete_fragments: tuple[IncompleteFragment, ...]
    active_track_count: int
    diagnostics: dict[str, Any]

    def serializable(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "channel_name": self.channel_name,
            "frame_sequence": self.frame_sequence,
            "frame_timestamp": self.frame_timestamp.isoformat(),
            "units": [unit.serializable() for unit in self.units],
            "incomplete_fragments": [fragment.serializable() for fragment in self.incomplete_fragments],
            "active_track_count": self.active_track_count,
            "diagnostics": self.diagnostics,
        }


@dataclass(slots=True)
class _TrackObservation:
    text: str
    normalized_text: str
    frame_sequence: int
    timestamp: datetime
    confidence: float
    review_required: bool
    source_line_indexes: tuple[int, ...]


@dataclass(slots=True)
class _TrackState:
    track_id: str
    language: str
    region_class: str
    center_y_ratio: float
    observations: deque[_TrackObservation]
    pending_text: str = ""
    pending_first_sequence: int | None = None
    pending_first_seen_at: datetime | None = None
    source_frame_sequences: list[int] = field(default_factory=list)
    source_line_indexes: set[int] = field(default_factory=set)
    confidence_values: list[float] = field(default_factory=list)
    review_required: bool = False
    stable_normalized_text: str = ""
    stable_frame_count: int = 0
    last_seen_sequence: int = 0
    last_seen_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class ExactMergeResult:
    text: str
    overlap_tokens: int
    relationship: str


def normalize_segmentation_text(text: str) -> str:
    """Unicode/whitespace normalization only; it never guesses or corrects spelling."""
    return SPACE_RE.sub(" ", normalize_ocr_text(unicodedata.normalize("NFKC", text))).strip()


def _tokenize_for_overlap(text: str) -> list[str]:
    return WORD_OR_PUNCT_RE.findall(normalize_segmentation_text(text))


def _token_key(token: str) -> str:
    stripped = token.strip(".,!?;:،؛۔؟()[]{}\"'’”").casefold()
    return stripped or token


def _content_tokens(tokens: Sequence[str]) -> list[tuple[int, str]]:
    return [(index, _token_key(token)) for index, token in enumerate(tokens) if LETTER_OR_NUMBER_RE.search(token)]


def _find_subsequence(container: Sequence[str], candidate: Sequence[str]) -> int | None:
    if not candidate or len(candidate) > len(container):
        return None
    for start in range(len(container) - len(candidate) + 1):
        if list(container[start : start + len(candidate)]) == list(candidate):
            return start
    return None


def merge_exact_overlap(existing: str, incoming: str) -> ExactMergeResult:
    """Merge frame fragments using exact normalized token overlap only.

    No edit distance, fuzzy matching, typo variants, stemming, or autocorrection is used.
    The incoming copy of an overlapped token is preferred so newly visible punctuation
    is retained.
    """
    existing = normalize_segmentation_text(existing)
    incoming = normalize_segmentation_text(incoming)
    if not existing:
        return ExactMergeResult(incoming, 0, "incoming_only")
    if not incoming:
        return ExactMergeResult(existing, 0, "existing_only")

    existing_tokens = _tokenize_for_overlap(existing)
    incoming_tokens = _tokenize_for_overlap(incoming)
    existing_content = _content_tokens(existing_tokens)
    incoming_content = _content_tokens(incoming_tokens)
    existing_keys = [key for _, key in existing_content]
    incoming_keys = [key for _, key in incoming_content]

    if existing_keys == incoming_keys:
        preferred = incoming if len(incoming) >= len(existing) else existing
        return ExactMergeResult(preferred, len(existing_keys), "identical")

    incoming_in_existing = _find_subsequence(existing_keys, incoming_keys)
    if incoming_in_existing is not None:
        return ExactMergeResult(existing, len(incoming_keys), "incoming_contained")
    existing_in_incoming = _find_subsequence(incoming_keys, existing_keys)
    if existing_in_incoming is not None:
        return ExactMergeResult(incoming, len(existing_keys), "existing_contained")

    best: ExactMergeResult | None = None
    max_overlap = min(len(existing_keys), len(incoming_keys))
    for overlap in range(max_overlap, 0, -1):
        if existing_keys[-overlap:] == incoming_keys[:overlap]:
            existing_cut_token_index = existing_content[-overlap][0]
            merged_tokens = existing_tokens[:existing_cut_token_index] + incoming_tokens
            best = ExactMergeResult(_join_tokens(merged_tokens), overlap, "append")
            break
    for overlap in range(max_overlap, 0, -1):
        if incoming_keys[-overlap:] == existing_keys[:overlap]:
            incoming_cut_token_index = incoming_content[-overlap][0]
            merged_tokens = incoming_tokens[:incoming_cut_token_index] + existing_tokens
            candidate = ExactMergeResult(_join_tokens(merged_tokens), overlap, "prepend")
            if best is None or candidate.overlap_tokens > best.overlap_tokens:
                best = candidate
            break
    return best or ExactMergeResult(existing, 0, "no_overlap")


def _join_tokens(tokens: Sequence[str]) -> str:
    if not tokens:
        return ""
    no_space_before = {".", ",", "!", "?", ";", ":", "،", "؛", "۔", "؟", ")", "]", "}"}
    no_space_after = {"(", "[", "{"}
    output = tokens[0]
    for token in tokens[1:]:
        if token in no_space_before or output[-1:] in no_space_after:
            output += token
        else:
            output += " " + token
    return normalize_segmentation_text(output)


def _period_is_boundary(text: str, index: int) -> bool:
    previous = text[index - 1] if index > 0 else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    if previous.isdigit() and following.isdigit():
        return False
    start = index
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    token = text[start : index + 1].casefold()
    next_nonspace = ""
    probe = index + 1
    while probe < len(text) and text[probe].isspace():
        probe += 1
    if probe < len(text):
        next_nonspace = text[probe]
    if token in ENGLISH_ABBREVIATIONS:
        # Titles/currency abbreviations continue into the following token. Other
        # abbreviations can still terminate a sentence when no text follows.
        if token in {"mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.", "no.", "nos.", "rs.", "pkr."}:
            return False
        return not next_nonspace
    if re.fullmatch(r"(?:[a-z]\.){2,}", token):
        return not next_nonspace
    if len(token) == 2 and token[0].isalpha():
        return not next_nonspace
    return True


def split_complete_sentences(text: str) -> tuple[list[str], str]:
    """Return punctuation-complete sentences and a retained incomplete remainder."""
    text = normalize_segmentation_text(text)
    if not text:
        return [], ""
    sentences: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        boundary = char in URDU_TERMINALS or char in COMMON_TERMINALS
        if char == ".":
            boundary = _period_is_boundary(text, index)
        if boundary:
            end = index + 1
            while end < len(text) and text[end] == char and char in {".", "!", "?"}:
                end += 1
            while end < len(text) and text[end] in CLOSING_MARKS:
                end += 1
            sentence = text[start:end].strip()
            if sentence:
                sentences.append(sentence)
            start = end
            while start < len(text) and text[start].isspace():
                start += 1
            index = start
            continue
        index += 1
    return sentences, text[start:].strip()


def _polygon_bounds(polygon: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in polygon]
    ys = [float(point[1]) for point in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def _union_polygon(lines: Sequence[OCRLine]) -> tuple[tuple[float, float], ...]:
    bounds = [_polygon_bounds(line.polygon) for line in lines]
    x1 = min(item[0] for item in bounds)
    y1 = min(item[1] for item in bounds)
    x2 = max(item[2] for item in bounds)
    y2 = max(item[3] for item in bounds)
    return ((x1, y1), (x2, y1), (x2, y2), (x1, y2))


def _region_class(polygon: Sequence[Sequence[float]], frame_width: float) -> str:
    x1, _, x2, _ = _polygon_bounds(polygon)
    width_ratio = (x2 - x1) / max(frame_width, 1.0)
    center_ratio = ((x1 + x2) / 2) / max(frame_width, 1.0)
    if width_ratio >= 0.5:
        return "wide"
    if center_ratio < 0.38:
        return "left"
    if center_ratio > 0.62:
        return "right"
    return "center"


def _split_explicit_engine_merge(line: OCRLine) -> list[OCRLine]:
    # The Phase 3 engine inserts " | " only when distinct Urdu and English readings
    # occupy the same detected region. Splitting this marker prevents unrelated topics
    # from being reconstructed together. Natural mixed-language text is otherwise kept.
    parts = [part.strip() for part in line.text.split(" | ") if part.strip()]
    if len(parts) <= 1:
        return [line]
    return [
        OCRLine(
            text=part,
            confidence=line.confidence,
            polygon=line.polygon,
            engine=line.engine,
            model=line.model,
            script=script_profile(part),
            accepted=line.accepted,
            needs_review=line.needs_review,
            alternatives=line.alternatives,
        )
        for part in parts
    ]


def extract_region_observations(
    result: OCRFrameResult, config: SegmentationConfig
) -> list[RegionObservation]:
    frame_width = float(result.diagnostics.get("frame_width") or 0)
    frame_height = float(result.diagnostics.get("frame_height") or 0)
    all_lines: list[tuple[int, OCRLine]] = []
    for index, original_line in enumerate(result.lines):
        for line in _split_explicit_engine_merge(original_line):
            if normalize_segmentation_text(line.text):
                all_lines.append((index, line))
    if not all_lines:
        return []
    if frame_width <= 0:
        frame_width = max(_polygon_bounds(line.polygon)[2] for _, line in all_lines) or 1
    if frame_height <= 0:
        frame_height = max(_polygon_bounds(line.polygon)[3] for _, line in all_lines) or 1

    sorted_lines = sorted(
        all_lines,
        key=lambda item: (
            (_polygon_bounds(item[1].polygon)[1] + _polygon_bounds(item[1].polygon)[3]) / 2,
            _polygon_bounds(item[1].polygon)[0],
        ),
    )
    rows: list[list[tuple[int, OCRLine]]] = []
    for item in sorted_lines:
        _, line = item
        _, y1, _, y2 = _polygon_bounds(line.polygon)
        center = ((y1 + y2) / 2) / frame_height
        target: list[tuple[int, OCRLine]] | None = None
        for row in rows:
            row_centers = []
            for _, row_line in row:
                _, ry1, _, ry2 = _polygon_bounds(row_line.polygon)
                row_centers.append(((ry1 + ry2) / 2) / frame_height)
            if abs(center - (sum(row_centers) / len(row_centers))) <= config.row_tolerance_ratio:
                target = row
                break
        (target if target is not None else rows.append([]) or rows[-1]).append(item)

    observations: list[RegionObservation] = []
    for row in rows:
        by_script: dict[str, list[tuple[int, OCRLine]]] = defaultdict(list)
        for item in row:
            language = item[1].script if item[1].script in {"ur", "en", "mixed"} else "unknown"
            by_script[language].append(item)
        for language, script_lines in by_script.items():
            reverse = language == "ur"
            script_lines.sort(key=lambda item: _polygon_bounds(item[1].polygon)[0], reverse=reverse)
            lines = [line for _, line in script_lines]
            text = normalize_segmentation_text(" ".join(line.text for line in lines))
            if not text:
                continue
            polygon = _union_polygon(lines)
            _, y1, _, y2 = _polygon_bounds(polygon)
            weights = [max(1, len(normalize_segmentation_text(line.text))) for line in lines]
            confidence = sum(line.confidence * weight for line, weight in zip(lines, weights)) / sum(weights)
            observations.append(
                RegionObservation(
                    text=text,
                    normalized_text=normalize_segmentation_text(text),
                    language=language,
                    polygon=polygon,
                    center_y_ratio=((y1 + y2) / 2) / frame_height,
                    region_class=_region_class(polygon, frame_width),
                    confidence=confidence,
                    review_required=any(line.needs_review for line in lines),
                    source_line_indexes=tuple(index for index, _ in script_lines),
                )
            )
    return sorted(observations, key=lambda item: (item.center_y_ratio, item.region_class, item.language))


def _valid_unit(text: str, config: SegmentationConfig) -> bool:
    normalized = normalize_segmentation_text(text)
    visible = [char for char in normalized if LETTER_OR_NUMBER_RE.match(char)]
    words = [token for token in normalized.split() if LETTER_OR_NUMBER_RE.search(token)]
    return len(visible) >= config.minimum_unit_characters and len(words) >= config.minimum_unit_words


class SentenceReconstructor:
    def __init__(self, config: SegmentationConfig | None = None) -> None:
        self.config = config or SegmentationConfig()
        self._tracks: dict[str, dict[str, _TrackState]] = defaultdict(dict)
        self._track_counters: dict[str, int] = defaultdict(int)
        self._emitted_hashes: dict[str, deque[str]] = defaultdict(deque)
        self._emitted_hash_sets: dict[str, set[str]] = defaultdict(set)

    def _new_track(self, result: OCRFrameResult, observation: RegionObservation) -> _TrackState:
        self._track_counters[result.stream_id] += 1
        track_id = f"{result.stream_id}:{self._track_counters[result.stream_id]}"
        track = _TrackState(
            track_id=track_id,
            language=observation.language,
            region_class=observation.region_class,
            center_y_ratio=observation.center_y_ratio,
            observations=deque(maxlen=self.config.rolling_frame_count),
        )
        self._tracks[result.stream_id][track_id] = track
        return track

    def _match_track(
        self,
        result: OCRFrameResult,
        observation: RegionObservation,
        used_track_ids: set[str],
    ) -> _TrackState:
        candidates: list[tuple[float, _TrackState]] = []
        for track in self._tracks[result.stream_id].values():
            if track.track_id in used_track_ids:
                continue
            if track.language != observation.language or track.region_class != observation.region_class:
                continue
            if result.frame_sequence - track.last_seen_sequence > self.config.stale_track_frames:
                continue
            distance = abs(track.center_y_ratio - observation.center_y_ratio)
            if distance <= self.config.track_tolerance_ratio:
                candidates.append((distance, track))
        if not candidates:
            return self._new_track(result, observation)
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def _remember_emission(
        self, stream_id: str, frame_timestamp: datetime, normalized_text: str
    ) -> bool:
        pkt_date = frame_timestamp.astimezone(ZoneInfo("Asia/Karachi")).date().isoformat()
        scope = f"{stream_id}:{pkt_date}"
        digest = hashlib.sha256(normalized_text.casefold().encode("utf-8")).hexdigest()
        if digest in self._emitted_hash_sets[scope]:
            return False
        queue = self._emitted_hashes[scope]
        values = self._emitted_hash_sets[scope]
        queue.append(digest)
        values.add(digest)
        while len(queue) > self.config.emitted_hash_capacity:
            values.discard(queue.popleft())
        return True

    def _make_unit(
        self,
        result: OCRFrameResult,
        track: _TrackState,
        text: str,
        unit_type: str,
        completion_reason: str,
    ) -> SegmentedTextUnit | None:
        normalized = normalize_segmentation_text(text)
        if not _valid_unit(normalized, self.config):
            return None
        if not self._remember_emission(result.stream_id, result.frame_timestamp, normalized):
            return None
        first_sequence = track.pending_first_sequence or result.frame_sequence
        first_seen = track.pending_first_seen_at or result.frame_timestamp
        sequences = tuple(dict.fromkeys(track.source_frame_sequences or [result.frame_sequence]))
        indexes = tuple(sorted(track.source_line_indexes))
        confidence = sum(track.confidence_values) / len(track.confidence_values) if track.confidence_values else 0.0
        unit_hash = hashlib.sha256(
            f"{result.stream_id}|{track.track_id}|{normalized.casefold()}".encode("utf-8")
        ).hexdigest()
        return SegmentedTextUnit(
            unit_id=unit_hash,
            stream_id=result.stream_id,
            channel_name=result.channel_name,
            track_id=track.track_id,
            text=normalized,
            normalized_text=normalized,
            language=track.language,
            unit_type=unit_type,
            completion_reason=completion_reason,
            confidence=round(confidence, 6),
            review_required=track.review_required,
            first_frame_sequence=first_sequence,
            last_frame_sequence=result.frame_sequence,
            first_seen_at=first_seen,
            last_seen_at=result.frame_timestamp,
            source_frame_sequences=sequences,
            source_line_indexes=indexes,
        )

    @staticmethod
    def _reset_pending(track: _TrackState, observation: _TrackObservation | None = None) -> None:
        track.pending_text = observation.text if observation else ""
        track.pending_first_sequence = observation.frame_sequence if observation else None
        track.pending_first_seen_at = observation.timestamp if observation else None
        track.source_frame_sequences = [observation.frame_sequence] if observation else []
        track.source_line_indexes = set(observation.source_line_indexes) if observation else set()
        track.confidence_values = [observation.confidence] if observation else []
        track.review_required = observation.review_required if observation else False

    def _consume_observation(
        self,
        result: OCRFrameResult,
        track: _TrackState,
        observation: RegionObservation,
    ) -> tuple[list[SegmentedTextUnit], list[IncompleteFragment], dict[str, Any]]:
        units: list[SegmentedTextUnit] = []
        fragments: list[IncompleteFragment] = []
        obs = _TrackObservation(
            text=observation.text,
            normalized_text=observation.normalized_text,
            frame_sequence=result.frame_sequence,
            timestamp=result.frame_timestamp,
            confidence=observation.confidence,
            review_required=observation.review_required,
            source_line_indexes=observation.source_line_indexes,
        )
        previous_normalized = track.observations[-1].normalized_text if track.observations else ""
        previous_stable_frame_count = track.stable_frame_count
        if previous_normalized == obs.normalized_text:
            track.stable_frame_count += 1
        else:
            track.stable_normalized_text = obs.normalized_text
            track.stable_frame_count = 1
        track.observations.append(obs)
        track.center_y_ratio = (track.center_y_ratio * 0.7) + (observation.center_y_ratio * 0.3)
        track.last_seen_sequence = result.frame_sequence
        track.last_seen_at = result.frame_timestamp

        relationship = "new"
        overlap_tokens = 0
        if not track.pending_text:
            self._reset_pending(track, obs)
        elif previous_normalized == obs.normalized_text:
            # Repeated static text is evidence for a complete headline, but it does not
            # get appended to itself.
            track.source_frame_sequences.append(obs.frame_sequence)
            track.source_line_indexes.update(obs.source_line_indexes)
            track.confidence_values.append(obs.confidence)
            track.review_required = track.review_required or obs.review_required
            relationship = "identical"
        else:
            merged = merge_exact_overlap(track.pending_text, obs.text)
            relationship = merged.relationship
            overlap_tokens = merged.overlap_tokens
            if merged.overlap_tokens > 0 or merged.relationship in {
                "identical", "incoming_contained", "existing_contained"
            }:
                track.pending_text = merged.text
                track.source_frame_sequences.append(obs.frame_sequence)
                track.source_line_indexes.update(obs.source_line_indexes)
                track.confidence_values.append(obs.confidence)
                track.review_required = track.review_required or obs.review_required
            else:
                # No exact overlap means the new line is a replacement, not evidence
                # that the two fragments belong to one sentence. Never invent a join.
                if previous_stable_frame_count >= self.config.stable_headline_frames:
                    headline = self._make_unit(
                        result, track, track.pending_text, "headline", "stable_replacement"
                    )
                    if headline:
                        units.append(headline)
                elif track.pending_text:
                    fragments.append(
                        IncompleteFragment(
                            stream_id=result.stream_id,
                            track_id=track.track_id,
                            text=track.pending_text,
                            language=track.language,
                            reason="replaced_without_exact_overlap",
                            first_frame_sequence=track.pending_first_sequence or result.frame_sequence,
                            last_frame_sequence=track.last_seen_sequence,
                        )
                    )
                self._reset_pending(track, obs)

        complete_sentences, remainder = split_complete_sentences(track.pending_text)
        if complete_sentences:
            for sentence in complete_sentences:
                unit = self._make_unit(result, track, sentence, "sentence", "terminal_punctuation")
                if unit:
                    units.append(unit)
            if remainder:
                track.pending_text = remainder
                track.pending_first_sequence = result.frame_sequence
                track.pending_first_seen_at = result.frame_timestamp
                track.source_frame_sequences = [result.frame_sequence]
                track.source_line_indexes = set(obs.source_line_indexes)
                track.confidence_values = [obs.confidence]
                track.review_required = obs.review_required
            else:
                self._reset_pending(track)
        elif (
            track.stable_frame_count >= self.config.stable_headline_frames
            and track.pending_text
        ):
            headline = self._make_unit(result, track, track.pending_text, "headline", "stable_text")
            if headline:
                units.append(headline)

        if len(track.pending_text) > self.config.maximum_pending_characters:
            fragments.append(
                IncompleteFragment(
                    stream_id=result.stream_id,
                    track_id=track.track_id,
                    text=track.pending_text,
                    language=track.language,
                    reason="pending_overflow_without_boundary",
                    first_frame_sequence=track.pending_first_sequence or result.frame_sequence,
                    last_frame_sequence=result.frame_sequence,
                )
            )
            self._reset_pending(track)

        return units, fragments, {
            "track_id": track.track_id,
            "relationship": relationship,
            "exact_overlap_tokens": overlap_tokens,
            "stable_frame_count": track.stable_frame_count,
            "rolling_observation_count": len(track.observations),
        }

    def process_frame(self, result: OCRFrameResult) -> SegmentationFrameResult:
        observations = extract_region_observations(result, self.config)
        units: list[SegmentedTextUnit] = []
        fragments: list[IncompleteFragment] = []
        track_diagnostics: list[dict[str, Any]] = []
        used_tracks: set[str] = set()
        for observation in observations:
            track = self._match_track(result, observation, used_tracks)
            used_tracks.add(track.track_id)
            new_units, new_fragments, diagnostics = self._consume_observation(result, track, observation)
            units.extend(new_units)
            fragments.extend(new_fragments)
            track_diagnostics.append(diagnostics)

        stale_ids = [
            track_id
            for track_id, track in self._tracks[result.stream_id].items()
            if result.frame_sequence - track.last_seen_sequence > self.config.stale_track_frames
        ]
        for track_id in stale_ids:
            track = self._tracks[result.stream_id].pop(track_id)
            if track.pending_text:
                fragments.append(
                    IncompleteFragment(
                        stream_id=result.stream_id,
                        track_id=track.track_id,
                        text=track.pending_text,
                        language=track.language,
                        reason="stale_incomplete_track",
                        first_frame_sequence=track.pending_first_sequence or track.last_seen_sequence,
                        last_frame_sequence=track.last_seen_sequence,
                    )
                )

        return SegmentationFrameResult(
            stream_id=result.stream_id,
            channel_name=result.channel_name,
            frame_sequence=result.frame_sequence,
            frame_timestamp=result.frame_timestamp,
            units=tuple(units),
            incomplete_fragments=tuple(fragments),
            active_track_count=len(self._tracks[result.stream_id]),
            diagnostics={
                "region_observation_count": len(observations),
                "emitted_unit_count": len(units),
                "incomplete_fragment_count": len(fragments),
                "exact_overlap_only": True,
                "fuzzy_overlap_enabled": False,
                "autocorrection_enabled": False,
                "tracks": track_diagnostics,
            },
        )


class SegmentationResultSink(Protocol):
    async def write(self, result: SegmentationFrameResult) -> None: ...


class InMemorySegmentationResultSink:
    def __init__(self) -> None:
        self.results: list[SegmentationFrameResult] = []

    async def write(self, result: SegmentationFrameResult) -> None:
        self.results.append(result)


class BoundedOCRResultBus:
    """No-silent-drop handoff between OCR and sentence reconstruction."""

    def __init__(self, capacity: int = 32) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._queue: asyncio.Queue[OCRFrameResult] = asyncio.Queue(maxsize=capacity)
        self.high_watermark = 0
        self.publish_count = 0

    @property
    def capacity(self) -> int:
        return self._queue.maxsize

    @property
    def size(self) -> int:
        return self._queue.qsize()

    async def publish(self, result: OCRFrameResult, timeout_seconds: float = 5.0) -> None:
        try:
            await asyncio.wait_for(self._queue.put(result), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise SegmentationBackpressureError(
                f"OCR result queue remained full for {timeout_seconds:.1f}s; no result was silently dropped"
            ) from exc
        self.publish_count += 1
        self.high_watermark = max(self.high_watermark, self._queue.qsize())

    async def write(self, result: OCRFrameResult) -> None:
        await self.publish(result)

    async def consume(self) -> OCRFrameResult:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()


class SegmentationBusWorker:
    def __init__(
        self,
        bus: BoundedOCRResultBus,
        reconstructor: SentenceReconstructor,
        sink: SegmentationResultSink,
    ) -> None:
        self.bus = bus
        self.reconstructor = reconstructor
        self.sink = sink
        self.processed_frames = 0
        self.failed_frames = 0
        self.last_error: str | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() or self.bus.size:
            try:
                ocr_result = await asyncio.wait_for(self.bus.consume(), timeout=0.25)
            except TimeoutError:
                continue
            try:
                result = self.reconstructor.process_frame(ocr_result)
                await self.sink.write(result)
                self.processed_frames += 1
            except Exception as exc:
                self.failed_frames += 1
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                raise
            finally:
                self.bus.task_done()


@dataclass(slots=True, frozen=True)
class SegmentationBenchmarkCase:
    case_id: str
    frames: tuple[OCRFrameResult, ...]
    expected_units: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class SegmentationBenchmarkReport:
    case_count: int
    expected_count: int
    predicted_count: int
    exact_matches: int
    precision: float
    recall: float
    accepted: bool
    failures: tuple[dict[str, Any], ...]

    def serializable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failures"] = list(self.failures)
        return payload


def benchmark_segmentation(
    cases: Sequence[SegmentationBenchmarkCase],
    config: SegmentationConfig | None = None,
) -> SegmentationBenchmarkReport:
    expected_total = 0
    predicted_total = 0
    exact_matches = 0
    failures: list[dict[str, Any]] = []
    for case in cases:
        reconstructor = SentenceReconstructor(config)
        predicted: list[str] = []
        for frame in case.frames:
            predicted.extend(unit.normalized_text for unit in reconstructor.process_frame(frame).units)
        expected = [normalize_segmentation_text(text) for text in case.expected_units]
        predicted = [normalize_segmentation_text(text) for text in predicted]
        expected_total += len(expected)
        predicted_total += len(predicted)
        remaining = list(predicted)
        matched = 0
        for item in expected:
            if item in remaining:
                remaining.remove(item)
                matched += 1
        exact_matches += matched
        if matched != len(expected) or remaining:
            failures.append(
                {
                    "case_id": case.case_id,
                    "expected": expected,
                    "predicted": predicted,
                    "missing": [item for item in expected if item not in predicted],
                    "unexpected": remaining,
                }
            )
    precision = exact_matches / predicted_total if predicted_total else (1.0 if expected_total == 0 else 0.0)
    recall = exact_matches / expected_total if expected_total else 1.0
    return SegmentationBenchmarkReport(
        case_count=len(cases),
        expected_count=expected_total,
        predicted_count=predicted_total,
        exact_matches=exact_matches,
        precision=round(precision, 6),
        recall=round(recall, 6),
        accepted=precision == 1.0 and recall == 1.0 and not failures,
        failures=tuple(failures),
    )


def segmentation_runtime_config_from_env() -> SegmentationConfig:
    return SegmentationConfig(
        row_tolerance_ratio=float(os.getenv("SEGMENTATION_ROW_TOLERANCE_RATIO", "0.025")),
        track_tolerance_ratio=float(os.getenv("SEGMENTATION_TRACK_TOLERANCE_RATIO", "0.045")),
        stable_headline_frames=int(os.getenv("SEGMENTATION_STABLE_HEADLINE_FRAMES", "2")),
        stale_track_frames=int(os.getenv("SEGMENTATION_STALE_TRACK_FRAMES", "6")),
        minimum_unit_characters=int(os.getenv("SEGMENTATION_MINIMUM_UNIT_CHARACTERS", "8")),
        minimum_unit_words=int(os.getenv("SEGMENTATION_MINIMUM_UNIT_WORDS", "3")),
        maximum_pending_characters=int(os.getenv("SEGMENTATION_MAXIMUM_PENDING_CHARACTERS", "1200")),
    )


def segmentation_doctor() -> dict[str, Any]:
    config = segmentation_runtime_config_from_env()
    return {
        "status": "ready",
        "phase": 4,
        "configuration": asdict(config),
        "policy": {
            "rolling_frame_count": 3,
            "screen_regions_are_independent": True,
            "urdu_and_english_regions_are_independent": True,
            "exact_normalized_overlap_only": True,
            "fuzzy_overlap": False,
            "misspelling_variants": False,
            "autocorrection": False,
            "incomplete_fragments_are_not_sent_downstream": True,
        },
    }


def load_segmentation_manifest(path: Path) -> list[SegmentationBenchmarkCase]:
    cases: list[SegmentationBenchmarkCase] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on manifest line {line_number}: {exc}") from exc
        stream_id = str(payload.get("stream_id", "fixture-stream"))
        channel_name = str(payload.get("channel_name", "Fixture Channel"))
        frames: list[OCRFrameResult] = []
        for raw_frame in payload.get("frames", []):
            sequence = int(raw_frame["sequence"])
            timestamp = datetime.fromisoformat(str(raw_frame["timestamp"]).replace("Z", "+00:00"))
            lines: list[OCRLine] = []
            for raw_ocr_line in raw_frame.get("lines", []):
                polygon = tuple(
                    (float(point[0]), float(point[1])) for point in raw_ocr_line["polygon"]
                )
                text = str(raw_ocr_line["text"])
                confidence = float(raw_ocr_line.get("confidence", 1.0))
                lines.append(
                    OCRLine(
                        text=text,
                        confidence=confidence,
                        polygon=polygon,
                        engine="segmentation-fixture",
                        model="segmentation-fixture",
                        script=str(raw_ocr_line.get("script") or script_profile(text)),
                        accepted=confidence >= 0.95,
                        needs_review=confidence < 0.95,
                    )
                )
            raw_text = "\n".join(item.text for item in lines)
            frames.append(
                OCRFrameResult(
                    stream_id=stream_id,
                    channel_name=channel_name,
                    frame_sequence=sequence,
                    frame_sha256=str(raw_frame.get("sha256", f"fixture-{sequence}")),
                    frame_timestamp=timestamp,
                    raw_text=raw_text,
                    normalized_text=normalize_segmentation_text(raw_text),
                    confidence=(sum(item.confidence for item in lines) / len(lines) if lines else 0.0),
                    lines=tuple(lines),
                    engine_chain=("segmentation-fixture",),
                    processing_ms=0.0,
                    fallback_used=False,
                    duplicate_of_recent_frame=False,
                    duplicate_similarity=0.0,
                    review_required=any(item.needs_review for item in lines),
                    skipped_downstream=False,
                    diagnostics={
                        "frame_width": int(raw_frame.get("width", 1920)),
                        "frame_height": int(raw_frame.get("height", 1080)),
                    },
                )
            )
        cases.append(
            SegmentationBenchmarkCase(
                case_id=str(payload["id"]),
                frames=tuple(frames),
                expected_units=tuple(str(item) for item in payload.get("expected_units", [])),
            )
        )
    if not cases:
        raise ValueError("Segmentation manifest contains no cases")
    return cases
