from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .streaming import FrameEnvelope

# Avoid the Windows oneDNN/PIR path linked to the observed PaddleOCR failure.
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_enable_pir_api", "0")
os.environ.setdefault("FLAGS_enable_pir_in_executor", "0")

ARABIC_RANGES = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)
LATIN_RE = re.compile(r"[A-Za-z]")
NUMBER_RE = re.compile(r"(?:\d[\d,.:/-]*\d|\d)")
SPACE_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[^\w\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]+", re.UNICODE)


class OCRError(RuntimeError):
    """Base OCR failure."""


class OCRDependencyError(OCRError):
    """A required OCR dependency is unavailable."""


class OCRModelError(OCRError):
    """An OCR model failed to initialize or infer."""


@dataclass(slots=True, frozen=True)
class OCRConfig:
    detection_model: str = "PP-OCRv6_small_det"
    urdu_recognition_model: str = "arabic_PP-OCRv5_mobile_rec"
    english_recognition_model: str = "en_PP-OCRv5_mobile_rec"
    device: str = "cpu"
    cpu_threads: int = 4
    detection_threshold: float = 0.25
    detection_box_threshold: float = 0.45
    detection_unclip_ratio: float = 1.8
    minimum_candidate_confidence: float = 0.35
    high_confidence_threshold: float = 0.95
    fallback_trigger_threshold: float = 0.95
    fallback_audit_interval_frames: int = 10
    frame_text_duplicate_threshold: float = 0.97
    max_recent_frame_texts: int = 10
    preserve_low_confidence_candidates: bool = True
    custom_detection_model_dir: str | None = None
    custom_urdu_model_dir: str | None = None
    custom_english_model_dir: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("detection_threshold", self.detection_threshold),
            ("detection_box_threshold", self.detection_box_threshold),
            ("minimum_candidate_confidence", self.minimum_candidate_confidence),
            ("high_confidence_threshold", self.high_confidence_threshold),
            ("fallback_trigger_threshold", self.fallback_trigger_threshold),
            ("frame_text_duplicate_threshold", self.frame_text_duplicate_threshold),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.cpu_threads < 1:
            raise ValueError("cpu_threads must be positive")
        if self.fallback_audit_interval_frames < 1:
            raise ValueError("fallback_audit_interval_frames must be positive")
        if self.max_recent_frame_texts < 1:
            raise ValueError("max_recent_frame_texts must be positive")


@dataclass(slots=True, frozen=True)
class OCRLine:
    text: str
    confidence: float
    polygon: tuple[tuple[float, float], ...]
    engine: str
    model: str
    script: str
    accepted: bool
    needs_review: bool
    alternatives: tuple[dict[str, Any], ...] = ()

    def serializable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["polygon"] = [list(point) for point in self.polygon]
        payload["alternatives"] = list(self.alternatives)
        return payload


@dataclass(slots=True, frozen=True)
class OCRFrameResult:
    stream_id: str
    channel_name: str
    frame_sequence: int
    frame_sha256: str
    frame_timestamp: datetime
    raw_text: str
    normalized_text: str
    confidence: float
    lines: tuple[OCRLine, ...]
    engine_chain: tuple[str, ...]
    processing_ms: float
    fallback_used: bool
    duplicate_of_recent_frame: bool
    duplicate_similarity: float
    review_required: bool
    skipped_downstream: bool
    diagnostics: dict[str, Any]

    def serializable(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "channel_name": self.channel_name,
            "frame_sequence": self.frame_sequence,
            "frame_sha256": self.frame_sha256,
            "frame_timestamp": self.frame_timestamp.isoformat(),
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "confidence": self.confidence,
            "lines": [line.serializable() for line in self.lines],
            "engine_chain": list(self.engine_chain),
            "processing_ms": self.processing_ms,
            "fallback_used": self.fallback_used,
            "duplicate_of_recent_frame": self.duplicate_of_recent_frame,
            "duplicate_similarity": self.duplicate_similarity,
            "review_required": self.review_required,
            "skipped_downstream": self.skipped_downstream,
            "diagnostics": self.diagnostics,
        }


class PrimaryOCREngine(Protocol):
    name: str

    def recognize(self, image_bytes: bytes) -> list[OCRLine]: ...


class FallbackOCREngine(Protocol):
    name: str

    def recognize(self, image_bytes: bytes) -> list[OCRLine]: ...


def contains_arabic(text: str) -> bool:
    return any(any(start <= ord(char) <= end for start, end in ARABIC_RANGES) for char in text)


def script_profile(text: str) -> str:
    arabic = sum(
        1 for char in text if any(start <= ord(char) <= end for start, end in ARABIC_RANGES)
    )
    latin = len(LATIN_RE.findall(text))
    if arabic and latin:
        return "mixed"
    if arabic:
        return "ur"
    if latin:
        return "en"
    return "unknown"


def normalize_ocr_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    translation = str.maketrans(
        {
            "ي": "ی",
            "ى": "ی",
            "ك": "ک",
            "ۀ": "ہ",
            "ة": "ہ",
            "ؤ": "و",
            "إ": "ا",
            "أ": "ا",
            "ٱ": "ا",
            "ـ": "",
            "\u200c": " ",
            "\u200d": "",
            "\ufeff": "",
        }
    )
    text = text.translate(translation)
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return SPACE_RE.sub(" ", text).strip()


def comparison_text(text: str) -> str:
    normalized = normalize_ocr_text(text).casefold()
    cleaned = "".join(
        char if char.isspace() or unicodedata.category(char)[0] in {"L", "N"} else " "
        for char in normalized
    )
    return SPACE_RE.sub(" ", cleaned).strip()


def _levenshtein_distance(left: Sequence[Any], right: Sequence[Any]) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for index_left, item_left in enumerate(left, start=1):
        current = [index_left]
        for index_right, item_right in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[index_right] + 1,
                    previous[index_right - 1] + (item_left != item_right),
                )
            )
        previous = current
    return previous[-1]


def similarity(left: str, right: str) -> float:
    left_normalized = comparison_text(left)
    right_normalized = comparison_text(right)
    if not left_normalized and not right_normalized:
        return 1.0
    maximum = max(len(left_normalized), len(right_normalized), 1)
    return max(0.0, 1.0 - (_levenshtein_distance(left_normalized, right_normalized) / maximum))


def _number_signature(text: str) -> tuple[str, ...]:
    return tuple(NUMBER_RE.findall(normalize_ocr_text(text)))


def _polygon_bounds(polygon: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in polygon]
    ys = [float(point[1]) for point in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def _intersection_over_union(
    left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]
) -> float:
    lx1, ly1, lx2, ly2 = _polygon_bounds(left)
    rx1, ry1, rx2, ry2 = _polygon_bounds(right)
    ix1, iy1 = max(lx1, rx1), max(ly1, ry1)
    ix2, iy2 = min(lx2, rx2), min(ly2, ry2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
    union += max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1) - intersection
    return intersection / union if union > 0 else 0.0


def _line_sort_key(line: OCRLine) -> tuple[float, float]:
    x1, y1, x2, y2 = _polygon_bounds(line.polygon)
    return ((y1 + y2) / 2, x1)


def _weighted_confidence(lines: Sequence[OCRLine]) -> float:
    if not lines:
        return 0.0
    weights = [max(1, len(comparison_text(line.text))) for line in lines]
    return sum(line.confidence * weight for line, weight in zip(lines, weights)) / sum(weights)


def _payload_from_result(result: Any) -> Mapping[str, Any]:
    if isinstance(result, Mapping):
        payload: Any = result
    else:
        payload = getattr(result, "json", None)
        if callable(payload):
            payload = payload()
        if payload is None:
            payload = getattr(result, "res", None)
        if payload is None and hasattr(result, "to_dict"):
            payload = result.to_dict()
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, Mapping):
        raise OCRModelError(f"Unsupported PaddleOCR result type: {type(result)!r}")
    nested = payload.get("res")
    return nested if isinstance(nested, Mapping) else payload


def _as_polygon(value: Any) -> tuple[tuple[float, float], ...]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, Sequence):
        return ()
    polygon: list[tuple[float, float]] = []
    for point in value:
        if hasattr(point, "tolist"):
            point = point.tolist()
        if isinstance(point, Sequence) and len(point) >= 2:
            polygon.append((float(point[0]), float(point[1])))
    return tuple(polygon)


def _decode_image(image_bytes: bytes) -> Any:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - covered by doctor
        raise OCRDependencyError("opencv-python-headless and numpy are required") from exc
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise OCRError("Unable to decode frame image")
    return image


def _crop_polygon(image: Any, polygon: Sequence[Sequence[float]], padding: int = 4) -> Any:
    import cv2
    import numpy as np

    points = np.asarray(polygon, dtype=np.float32)
    if points.shape != (4, 2):
        x1, y1, x2, y2 = _polygon_bounds(polygon)
        height, width = image.shape[:2]
        return image[
            max(0, int(y1) - padding) : min(height, int(y2) + padding),
            max(0, int(x1) - padding) : min(width, int(x2) + padding),
        ]
    ordered = np.zeros((4, 2), dtype=np.float32)
    point_sum = points.sum(axis=1)
    point_diff = np.diff(points, axis=1).reshape(-1)
    ordered[0] = points[point_sum.argmin()]
    ordered[2] = points[point_sum.argmax()]
    ordered[1] = points[point_diff.argmin()]
    ordered[3] = points[point_diff.argmax()]
    top_left, top_right, bottom_right, bottom_left = ordered
    target_width = int(max(np.linalg.norm(bottom_right - bottom_left), np.linalg.norm(top_right - top_left)))
    target_height = int(max(np.linalg.norm(top_right - bottom_right), np.linalg.norm(top_left - bottom_left)))
    if target_width < 2 or target_height < 2:
        return image[0:0, 0:0]
    destination = np.array(
        [[0, 0], [target_width - 1, 0], [target_width - 1, target_height - 1], [0, target_height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(ordered, destination)
    return cv2.warpPerspective(image, matrix, (target_width, target_height), flags=cv2.INTER_CUBIC)


def _encode_jpeg(image: Any) -> bytes:
    import cv2

    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        raise OCRError("Unable to encode OCR crop")
    return encoded.tobytes()


class PaddleDualScriptEngine:
    """Shared text detection with independent Urdu/Arabic and English recognition.

    Running one Arabic-only end-to-end OCR pipeline is not sufficient for Pakistani
    television frames because English banners and Urdu tickers frequently coexist.
    This engine detects once, then recognizes every detected line with both small
    script-specific models and keeps script-plausible candidates.
    """

    name = "paddle-dual-script"

    def __init__(self, config: OCRConfig | None = None) -> None:
        self.config = config or OCRConfig()
        self._detector: Any | None = None
        self._urdu_recognizer: Any | None = None
        self._english_recognizer: Any | None = None

    def _initialize(self) -> None:
        if self._detector is not None:
            return
        try:
            from paddleocr import TextDetection, TextRecognition
        except ImportError as exc:
            raise OCRDependencyError(
                "PaddleOCR is not installed. Run setup.ps1 under CPython 3.12 x64."
            ) from exc
        common = {
            "device": self.config.device,
            "engine": "paddle_static",
            "enable_hpi": False,
            "cpu_threads": self.config.cpu_threads,
        }
        try:
            self._detector = TextDetection(
                model_name=self.config.detection_model,
                model_dir=self.config.custom_detection_model_dir,
                thresh=self.config.detection_threshold,
                box_thresh=self.config.detection_box_threshold,
                unclip_ratio=self.config.detection_unclip_ratio,
                **common,
            )
            self._urdu_recognizer = TextRecognition(
                model_name=self.config.urdu_recognition_model,
                model_dir=self.config.custom_urdu_model_dir,
                **common,
            )
            self._english_recognizer = TextRecognition(
                model_name=self.config.english_recognition_model,
                model_dir=self.config.custom_english_model_dir,
                **common,
            )
        except Exception as exc:  # pragma: no cover - model-runtime specific
            raise OCRModelError(f"PaddleOCR model initialization failed: {exc}") from exc

    @staticmethod
    def _recognize_crop(model: Any, crop: Any) -> tuple[str, float]:
        output = model.predict(input=crop, batch_size=1)
        if not output:
            return "", 0.0
        payload = _payload_from_result(output[0])
        return str(payload.get("rec_text", "")).strip(), float(payload.get("rec_score", 0.0))

    def recognize(self, image_bytes: bytes) -> list[OCRLine]:
        self._initialize()
        image = _decode_image(image_bytes)
        try:
            detected = self._detector.predict(input=image, batch_size=1)
        except Exception as exc:  # pragma: no cover - model-runtime specific
            raise OCRModelError(f"PaddleOCR detection failed: {exc}") from exc
        if not detected:
            return []
        payload = _payload_from_result(detected[0])
        polygons = payload.get("dt_polys", [])
        scores = payload.get("dt_scores", [])
        if hasattr(polygons, "tolist"):
            polygons = polygons.tolist()
        if hasattr(scores, "tolist"):
            scores = scores.tolist()
        lines: list[OCRLine] = []
        for index, raw_polygon in enumerate(polygons):
            polygon = _as_polygon(raw_polygon)
            if len(polygon) < 4:
                continue
            detection_score = float(scores[index]) if index < len(scores) else 1.0
            crop = _crop_polygon(image, polygon)
            if getattr(crop, "size", 0) == 0:
                continue
            urdu_text, urdu_score = self._recognize_crop(self._urdu_recognizer, crop)
            english_text, english_score = self._recognize_crop(self._english_recognizer, crop)
            candidates: list[tuple[str, float, str, str]] = []
            if urdu_text and contains_arabic(urdu_text):
                candidates.append((urdu_text, urdu_score * detection_score, "ur", self.config.urdu_recognition_model))
            if english_text and LATIN_RE.search(english_text):
                candidates.append((english_text, english_score * detection_score, "en", self.config.english_recognition_model))
            if not candidates:
                fallback_candidates = [
                    (urdu_text, urdu_score * detection_score, script_profile(urdu_text), self.config.urdu_recognition_model),
                    (english_text, english_score * detection_score, script_profile(english_text), self.config.english_recognition_model),
                ]
                candidates = [candidate for candidate in fallback_candidates if candidate[0]]
            candidates.sort(key=lambda item: item[1], reverse=True)
            if not candidates:
                continue
            chosen_text, chosen_score, chosen_script, chosen_model = candidates[0]
            alternatives = tuple(
                {
                    "text": text,
                    "confidence": round(score, 6),
                    "script": script,
                    "model": model,
                }
                for text, score, script, model in candidates[1:]
                if comparison_text(text) != comparison_text(chosen_text)
            )
            if len(candidates) > 1:
                secondary_text, secondary_score, secondary_script, _ = candidates[1]
                if (
                    chosen_script in {"ur", "en"}
                    and secondary_script in {"ur", "en"}
                    and chosen_script != secondary_script
                    and secondary_score >= self.config.minimum_candidate_confidence
                ):
                    chosen_text = f"{chosen_text} | {secondary_text}"
                    chosen_script = "mixed"
                    chosen_score = min(1.0, max(chosen_score, secondary_score))
            if chosen_score < self.config.minimum_candidate_confidence and not self.config.preserve_low_confidence_candidates:
                continue
            lines.append(
                OCRLine(
                    text=normalize_ocr_text(chosen_text),
                    confidence=max(0.0, min(1.0, chosen_score)),
                    polygon=polygon,
                    engine=self.name,
                    model=chosen_model,
                    script=chosen_script,
                    accepted=chosen_score >= self.config.high_confidence_threshold,
                    needs_review=chosen_score < self.config.high_confidence_threshold,
                    alternatives=alternatives,
                )
            )
        return sorted(lines, key=_line_sort_key)


class EasyOCRFallbackEngine:
    name = "easyocr-fallback"

    def __init__(self, config: OCRConfig | None = None) -> None:
        self.config = config or OCRConfig()
        self._reader: Any | None = None

    def _initialize(self) -> None:
        if self._reader is not None:
            return
        try:
            import easyocr
        except ImportError as exc:
            raise OCRDependencyError(
                "EasyOCR is not installed. Run setup.ps1 under CPython 3.12 x64."
            ) from exc
        try:
            self._reader = easyocr.Reader(["ur", "en"], gpu=False, verbose=False)
        except Exception as exc:  # pragma: no cover - model-runtime specific
            raise OCRModelError(f"EasyOCR initialization failed: {exc}") from exc

    def recognize(self, image_bytes: bytes) -> list[OCRLine]:
        self._initialize()
        image = _decode_image(image_bytes)
        try:
            results = self._reader.readtext(
                image,
                detail=1,
                paragraph=False,
                decoder="beamsearch",
                beamWidth=5,
                contrast_ths=0.1,
                adjust_contrast=0.7,
                text_threshold=0.45,
                low_text=0.25,
                link_threshold=0.3,
                canvas_size=2560,
                mag_ratio=1.25,
            )
        except Exception as exc:  # pragma: no cover - model-runtime specific
            raise OCRModelError(f"EasyOCR inference failed: {exc}") from exc
        lines: list[OCRLine] = []
        for polygon_value, text, confidence in results:
            normalized = normalize_ocr_text(str(text))
            if not normalized:
                continue
            polygon = _as_polygon(polygon_value)
            score = float(confidence)
            lines.append(
                OCRLine(
                    text=normalized,
                    confidence=max(0.0, min(1.0, score)),
                    polygon=polygon,
                    engine=self.name,
                    model="easyocr-urdu-english-generation2",
                    script=script_profile(normalized),
                    accepted=score >= self.config.high_confidence_threshold,
                    needs_review=score < self.config.high_confidence_threshold,
                )
            )
        return sorted(lines, key=_line_sort_key)


def merge_ocr_lines(
    primary: Sequence[OCRLine], fallback: Sequence[OCRLine], high_confidence_threshold: float
) -> list[OCRLine]:
    merged = list(primary)
    for candidate in fallback:
        best_index: int | None = None
        best_overlap = 0.0
        for index, existing in enumerate(merged):
            overlap = _intersection_over_union(existing.polygon, candidate.polygon)
            if overlap > best_overlap:
                best_overlap = overlap
                best_index = index
        if best_index is None or best_overlap < 0.35:
            merged.append(candidate)
            continue
        existing = merged[best_index]
        text_similarity = similarity(existing.text, candidate.text)
        if text_similarity >= 0.85:
            winner = candidate if candidate.confidence > existing.confidence else existing
            loser = existing if winner is candidate else candidate
            alternatives = tuple(winner.alternatives) + (
                {
                    "text": loser.text,
                    "confidence": loser.confidence,
                    "script": loser.script,
                    "model": loser.model,
                    "engine": loser.engine,
                },
            )
            merged[best_index] = OCRLine(
                text=winner.text,
                confidence=winner.confidence,
                polygon=winner.polygon,
                engine=winner.engine,
                model=winner.model,
                script=winner.script,
                accepted=winner.confidence >= high_confidence_threshold,
                needs_review=winner.confidence < high_confidence_threshold,
                alternatives=alternatives,
            )
        elif existing.script != candidate.script and {existing.script, candidate.script} <= {"ur", "en"}:
            combined = f"{existing.text} | {candidate.text}"
            merged[best_index] = OCRLine(
                text=combined,
                confidence=max(existing.confidence, candidate.confidence),
                polygon=existing.polygon,
                engine=f"{existing.engine}+{candidate.engine}",
                model=f"{existing.model}+{candidate.model}",
                script="mixed",
                accepted=max(existing.confidence, candidate.confidence) >= high_confidence_threshold,
                needs_review=max(existing.confidence, candidate.confidence) < high_confidence_threshold,
                alternatives=tuple(existing.alternatives) + tuple(candidate.alternatives),
            )
        elif candidate.confidence > existing.confidence:
            merged[best_index] = candidate
    unique: list[OCRLine] = []
    for line in sorted(merged, key=_line_sort_key):
        if any(
            _intersection_over_union(line.polygon, existing.polygon) > 0.75
            and similarity(line.text, existing.text) > 0.92
            for existing in unique
        ):
            continue
        unique.append(line)
    return unique


@dataclass(slots=True)
class FrameTextDeduplicator:
    threshold: float = 0.97
    capacity: int = 10
    _recent: dict[str, deque[tuple[str, tuple[str, ...], int]]] = field(
        default_factory=lambda: defaultdict(deque)
    )

    def compare_and_remember(self, stream_id: str, text: str, sequence: int) -> tuple[bool, float, int | None]:
        normalized = comparison_text(text)
        numbers = _number_signature(text)
        best_similarity = 0.0
        best_sequence: int | None = None
        duplicate = False
        for previous_text, previous_numbers, previous_sequence in self._recent[stream_id]:
            score = similarity(normalized, previous_text)
            if score > best_similarity:
                best_similarity = score
                best_sequence = previous_sequence
            if score >= self.threshold and numbers == previous_numbers:
                duplicate = True
                break
        recent = self._recent[stream_id]
        recent.append((normalized, numbers, sequence))
        while len(recent) > self.capacity:
            recent.popleft()
        return duplicate, best_similarity, best_sequence


class OCRPipeline:
    def __init__(
        self,
        primary: PrimaryOCREngine | None = None,
        fallback: FallbackOCREngine | None = None,
        config: OCRConfig | None = None,
    ) -> None:
        self.config = config or OCRConfig()
        self.primary = primary or PaddleDualScriptEngine(self.config)
        self.fallback = fallback or EasyOCRFallbackEngine(self.config)
        self._deduplicator = FrameTextDeduplicator(
            threshold=self.config.frame_text_duplicate_threshold,
            capacity=self.config.max_recent_frame_texts,
        )
        self._stream_frame_counts: dict[str, int] = defaultdict(int)

    def process_frame(self, frame: FrameEnvelope) -> OCRFrameResult:
        started = time.perf_counter()
        primary_lines = self.primary.recognize(frame.jpeg_bytes)
        self._stream_frame_counts[frame.stream_id] += 1
        frame_count = self._stream_frame_counts[frame.stream_id]
        low_confidence = any(
            line.confidence < self.config.fallback_trigger_threshold for line in primary_lines
        )
        periodic_audit = frame_count % self.config.fallback_audit_interval_frames == 0
        fallback_needed = not primary_lines or low_confidence or periodic_audit
        fallback_lines: list[OCRLine] = []
        fallback_error: str | None = None
        if fallback_needed:
            try:
                fallback_lines = self.fallback.recognize(frame.jpeg_bytes)
            except OCRError as exc:
                fallback_error = str(exc)
        lines = merge_ocr_lines(
            primary_lines,
            fallback_lines,
            high_confidence_threshold=self.config.high_confidence_threshold,
        )
        raw_text = "\n".join(line.text for line in lines if line.text).strip()
        normalized_text = normalize_ocr_text(raw_text)
        duplicate, duplicate_similarity, duplicate_sequence = self._deduplicator.compare_and_remember(
            frame.stream_id, normalized_text, frame.sequence
        )
        confidence = _weighted_confidence(lines)
        review_required = not lines or any(line.needs_review for line in lines)
        diagnostics = {
            "frame_width": frame.width,
            "frame_height": frame.height,
            "primary_line_count": len(primary_lines),
            "fallback_line_count": len(fallback_lines),
            "merged_line_count": len(lines),
            "periodic_fallback_audit": periodic_audit,
            "fallback_error": fallback_error,
            "duplicate_reference_sequence": duplicate_sequence,
            "number_signature": list(_number_signature(raw_text)),
            "high_confidence_line_ratio": (
                sum(line.accepted for line in lines) / len(lines) if lines else 0.0
            ),
        }
        elapsed_ms = (time.perf_counter() - started) * 1000
        return OCRFrameResult(
            stream_id=frame.stream_id,
            channel_name=frame.channel_name,
            frame_sequence=frame.sequence,
            frame_sha256=frame.sha256,
            frame_timestamp=frame.captured_at,
            raw_text=raw_text,
            normalized_text=normalized_text,
            confidence=round(confidence, 6),
            lines=tuple(lines),
            engine_chain=tuple(
                engine for engine in (self.primary.name, self.fallback.name if fallback_needed else "") if engine
            ),
            processing_ms=round(elapsed_ms, 3),
            fallback_used=bool(fallback_lines),
            duplicate_of_recent_frame=duplicate,
            duplicate_similarity=round(duplicate_similarity, 6),
            review_required=review_required,
            skipped_downstream=duplicate,
            diagnostics=diagnostics,
        )

    async def process_frame_async(self, frame: FrameEnvelope) -> OCRFrameResult:
        return await asyncio.to_thread(self.process_frame, frame)


@dataclass(slots=True, frozen=True)
class OCRBenchmarkSample:
    sample_id: str
    image_path: Path
    ground_truth_lines: tuple[str, ...]
    language: str
    source: str = "manual"


@dataclass(slots=True, frozen=True)
class OCRBenchmarkSampleResult:
    sample_id: str
    predicted_text: str
    ground_truth_text: str
    character_accuracy: float
    word_accuracy: float
    line_recall: float
    numeric_token_recall: float
    confidence: float
    processing_ms: float
    accepted: bool


@dataclass(slots=True, frozen=True)
class OCRBenchmarkReport:
    samples: tuple[OCRBenchmarkSampleResult, ...]
    character_accuracy: float
    word_accuracy: float
    line_recall: float
    numeric_token_recall: float
    median_processing_ms: float
    p95_processing_ms: float
    accepted: bool
    acceptance_rules: dict[str, Any]

    def serializable(self) -> dict[str, Any]:
        return {
            "samples": [asdict(sample) for sample in self.samples],
            "character_accuracy": self.character_accuracy,
            "word_accuracy": self.word_accuracy,
            "line_recall": self.line_recall,
            "numeric_token_recall": self.numeric_token_recall,
            "median_processing_ms": self.median_processing_ms,
            "p95_processing_ms": self.p95_processing_ms,
            "accepted": self.accepted,
            "acceptance_rules": self.acceptance_rules,
        }


def _accuracy(reference: Sequence[Any], prediction: Sequence[Any]) -> float:
    denominator = max(len(reference), 1)
    return max(0.0, 1.0 - (_levenshtein_distance(reference, prediction) / denominator))


def _line_recall(reference_lines: Sequence[str], prediction_lines: Sequence[str]) -> float:
    if not reference_lines:
        return 1.0
    recovered = 0
    for reference in reference_lines:
        if max((similarity(reference, predicted) for predicted in prediction_lines), default=0.0) >= 0.85:
            recovered += 1
    return recovered / len(reference_lines)


def _token_recall(reference: Sequence[str], prediction: Sequence[str]) -> float:
    if not reference:
        return 1.0
    remaining = list(prediction)
    recovered = 0
    for token in reference:
        if token in remaining:
            recovered += 1
            remaining.remove(token)
    return recovered / len(reference)


def load_benchmark_manifest(path: Path) -> list[OCRBenchmarkSample]:
    base = path.parent
    samples: list[OCRBenchmarkSample] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            payload = json.loads(raw_line)
            image_path = Path(payload["image"])
            if not image_path.is_absolute():
                image_path = (base / image_path).resolve()
            ground_truth = payload.get("ground_truth_lines")
            if not isinstance(ground_truth, list) or not ground_truth:
                raise ValueError(f"Manifest line {line_number} needs non-empty ground_truth_lines")
            samples.append(
                OCRBenchmarkSample(
                    sample_id=str(payload.get("id", f"sample-{line_number}")),
                    image_path=image_path,
                    ground_truth_lines=tuple(str(item) for item in ground_truth),
                    language=str(payload.get("language", "mixed")),
                    source=str(payload.get("source", "manual")),
                )
            )
    if not samples:
        raise ValueError("Benchmark manifest contains no samples")
    return samples


def benchmark_engine(
    engine: PrimaryOCREngine,
    samples: Sequence[OCRBenchmarkSample],
    minimum_word_accuracy: float = 0.95,
    minimum_line_recall: float = 1.0,
    minimum_numeric_token_recall: float = 1.0,
) -> OCRBenchmarkReport:
    results: list[OCRBenchmarkSampleResult] = []
    for sample in samples:
        image_bytes = sample.image_path.read_bytes()
        started = time.perf_counter()
        lines = engine.recognize(image_bytes)
        elapsed_ms = (time.perf_counter() - started) * 1000
        predicted_lines = [normalize_ocr_text(line.text) for line in lines]
        reference_text = "\n".join(normalize_ocr_text(line) for line in sample.ground_truth_lines)
        predicted_text = "\n".join(predicted_lines)
        reference_chars = list(comparison_text(reference_text))
        predicted_chars = list(comparison_text(predicted_text))
        reference_words = comparison_text(reference_text).split()
        predicted_words = comparison_text(predicted_text).split()
        character_accuracy = _accuracy(reference_chars, predicted_chars)
        word_accuracy = _accuracy(reference_words, predicted_words)
        line_recall = _line_recall(sample.ground_truth_lines, predicted_lines)
        numeric_recall = _token_recall(_number_signature(reference_text), _number_signature(predicted_text))
        confidence = _weighted_confidence(lines)
        accepted = (
            word_accuracy >= minimum_word_accuracy
            and line_recall >= minimum_line_recall
            and numeric_recall >= minimum_numeric_token_recall
        )
        results.append(
            OCRBenchmarkSampleResult(
                sample_id=sample.sample_id,
                predicted_text=predicted_text,
                ground_truth_text=reference_text,
                character_accuracy=round(character_accuracy, 6),
                word_accuracy=round(word_accuracy, 6),
                line_recall=round(line_recall, 6),
                numeric_token_recall=round(numeric_recall, 6),
                confidence=round(confidence, 6),
                processing_ms=round(elapsed_ms, 3),
                accepted=accepted,
            )
        )
    latencies = sorted(result.processing_ms for result in results)
    p95_index = min(len(latencies) - 1, max(0, math.ceil(len(latencies) * 0.95) - 1))
    report_word_accuracy = statistics.fmean(result.word_accuracy for result in results)
    report_line_recall = statistics.fmean(result.line_recall for result in results)
    report_numeric_recall = statistics.fmean(result.numeric_token_recall for result in results)
    report_character_accuracy = statistics.fmean(result.character_accuracy for result in results)
    accepted = (
        all(result.accepted for result in results)
        and report_word_accuracy >= minimum_word_accuracy
        and report_line_recall >= minimum_line_recall
        and report_numeric_recall >= minimum_numeric_token_recall
    )
    return OCRBenchmarkReport(
        samples=tuple(results),
        character_accuracy=round(report_character_accuracy, 6),
        word_accuracy=round(report_word_accuracy, 6),
        line_recall=round(report_line_recall, 6),
        numeric_token_recall=round(report_numeric_recall, 6),
        median_processing_ms=round(statistics.median(latencies), 3),
        p95_processing_ms=round(latencies[p95_index], 3),
        accepted=accepted,
        acceptance_rules={
            "minimum_word_accuracy": minimum_word_accuracy,
            "minimum_line_recall": minimum_line_recall,
            "minimum_numeric_token_recall": minimum_numeric_token_recall,
            "confidence_is_not_treated_as_ground_truth_accuracy": True,
        },
    )


def ocr_runtime_config_from_env() -> OCRConfig:
    def optional_path(name: str) -> str | None:
        value = os.getenv(name, "").strip()
        return value or None

    return OCRConfig(
        cpu_threads=int(os.getenv("OCR_CPU_THREADS", "4")),
        detection_threshold=float(os.getenv("OCR_DETECTION_THRESHOLD", "0.25")),
        detection_box_threshold=float(os.getenv("OCR_DETECTION_BOX_THRESHOLD", "0.45")),
        detection_unclip_ratio=float(os.getenv("OCR_DETECTION_UNCLIP_RATIO", "1.8")),
        minimum_candidate_confidence=float(os.getenv("OCR_MINIMUM_CANDIDATE_CONFIDENCE", "0.35")),
        high_confidence_threshold=float(os.getenv("OCR_HIGH_CONFIDENCE_THRESHOLD", "0.95")),
        fallback_trigger_threshold=float(os.getenv("OCR_FALLBACK_TRIGGER_THRESHOLD", "0.95")),
        fallback_audit_interval_frames=int(os.getenv("OCR_FALLBACK_AUDIT_INTERVAL_FRAMES", "10")),
        frame_text_duplicate_threshold=float(os.getenv("OCR_FRAME_DUPLICATE_THRESHOLD", "0.97")),
        custom_detection_model_dir=optional_path("OCR_CUSTOM_DETECTION_MODEL_DIR"),
        custom_urdu_model_dir=optional_path("OCR_CUSTOM_URDU_MODEL_DIR"),
        custom_english_model_dir=optional_path("OCR_CUSTOM_ENGLISH_MODEL_DIR"),
    )


def ocr_doctor(load_models: bool = False) -> dict[str, Any]:
    dependencies: dict[str, Any] = {}
    for module_name in ("numpy", "cv2", "paddle", "paddleocr", "torch", "easyocr"):
        try:
            module = __import__(module_name)
            details: dict[str, Any] = {
                "available": True,
                "version": getattr(module, "__version__", "unknown"),
            }
            if module_name == "torch":
                build_cuda = getattr(getattr(module, "version", None), "cuda", None)
                details.update(
                    {
                        "cuda_runtime_available": bool(module.cuda.is_available()),
                        "build_cuda": build_cuda,
                        "cpu_only_build": build_cuda is None,
                    }
                )
            dependencies[module_name] = details
        except Exception as exc:
            dependencies[module_name] = {
                "available": False,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
    models: dict[str, Any] = {"loaded": False, "status": "not_requested"}
    if load_models and all(dependencies[name]["available"] for name in ("paddle", "paddleocr")):
        try:
            engine = PaddleDualScriptEngine(ocr_runtime_config_from_env())
            engine._initialize()
            models = {
                "loaded": True,
                "status": "ready",
                "detection": engine.config.detection_model,
                "urdu": engine.config.urdu_recognition_model,
                "english": engine.config.english_recognition_model,
            }
        except Exception as exc:
            models = {"loaded": False, "status": "error", "error": str(exc)}
    required = ("numpy", "cv2", "paddle", "paddleocr", "torch", "easyocr")
    status = "ready" if all(dependencies[name]["available"] for name in required) else "not_ready"
    torch_details = dependencies.get("torch", {})
    if torch_details.get("available") and not torch_details.get("cpu_only_build", False):
        status = "not_ready"
    if load_models and not models["loaded"]:
        status = "not_ready"
    return {
        "status": status,
        "python_required": "3.12.x x64",
        "device": "cpu",
        "dependencies": dependencies,
        "models": models,
        "configuration": asdict(ocr_runtime_config_from_env()),
        "accuracy_policy": {
            "target_word_accuracy": 0.95,
            "required_line_recall": 1.0,
            "confidence_threshold": 0.95,
            "low_confidence_text_is_retained_for_review": True,
            "model_confidence_is_not_accuracy": True,
        },
    }

class OCRResultSink(Protocol):
    async def write(self, result: OCRFrameResult) -> None: ...


class InMemoryOCRResultSink:
    def __init__(self) -> None:
        self.results: list[OCRFrameResult] = []

    async def write(self, result: OCRFrameResult) -> None:
        self.results.append(result)


class OCRBusWorker:
    """Consumes the Phase 2 bounded frame bus without bypassing backpressure."""

    def __init__(self, bus: Any, pipeline: OCRPipeline, sink: OCRResultSink) -> None:
        self.bus = bus
        self.pipeline = pipeline
        self.sink = sink
        self.processed_frames = 0
        self.failed_frames = 0
        self.last_error: str | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() or self.bus.size:
            try:
                frame = await asyncio.wait_for(self.bus.consume(), timeout=0.25)
            except TimeoutError:
                continue
            try:
                result = await self.pipeline.process_frame_async(frame)
                await self.sink.write(result)
                self.processed_frames += 1
            except Exception as exc:
                self.failed_frames += 1
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                raise
            finally:
                self.bus.task_done()
