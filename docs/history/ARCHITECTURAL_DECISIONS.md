# Canonical Architectural Decisions

This document records the foundational architectural decisions (ADRs) governing the **News-Intelli-Test** platform, explaining the technical rationale behind choices and deviations from early specifications.

---

## ADR 01: Native Windows Execution (No Docker)

- **Context**: Early documentation proposed multi-container Docker Compose deployments.
- **Decision**: The canonical application runs natively on 64-bit Windows under Python 3.12 and Node.js.
- **Rationale**: Direct execution avoids container virtualization overhead, simplifies CPU hardware thread affinity for OCR and translation engines, provides native access to local media devices, and eliminates Docker Desktop licensing and virtualization requirements on developer machines.

---

## ADR 02: Modular Backend Architecture (`newsintel`)

- **Context**: Later recovery artifacts included flattened monolithic scripts (`app.py`, `pipeline.py`).
- **Decision**: Retain the structured, modular package architecture defined in Phase 12 (`backend/newsintel/`).
- **Rationale**: Modular separation of concerns allows independent testing, maintenance, and isolation of streaming (`streaming.py`), optical recognition (`ocr.py`), text segmentation (`segmentation.py`), taxonomy (`taxonomy.py`), deduplication (`deduplication.py`), translation (`translation.py`), and persistence (`persistence.py`).

---

## ADR 03: Canonical API & Streaming Contract: `/api/v1` + WebSocket

- **Context**: A loose 1.0.0 repair line experimented with a flattened `/api` prefix and Server-Sent Events (SSE) at `/api/live`.
- **Decision**: The canonical contract is strictly `/api/v1` with bi-directional WebSocket delivery at `/api/v1/ws/live` backed by a transactional outbox.
- **Rationale**:
  1. The `/api/v1` REST schema is strongly typed with Pydantic response models and validated across 183 automated tests.
  2. The transactional outbox pattern guarantees that observations, story updates, and translation patches are committed to PostgreSQL atomically before broadcast, preventing ghost events.
  3. WebSocket bi-directionality allows client subscription filtering, pause/resume signaling, and connection health handshakes that SSE cannot natively support.

---

## ADR 04: Deterministic Zero-Fuzzy Taxonomy Matching

- **Context**: Historical NSD specifications explored fuzzy Levenshtein distance, regex approximations, and morphological lemmatization.
- **Decision**: Keyword classification strictly adheres to deterministic zero-fuzzy exact Unicode matching.
- **Rationale**: News broadcast crawlers often contain proper nouns, acronyms, and political terminology where single-character variations significantly alter meaning. Eliminating fuzzy approximations prevents false positive alerts and maintains absolute editorial accuracy across 1,037 canonical keywords.

---

## ADR 05: CPU-Only Dual-Engine OCR Pipeline

- **Context**: Live news ticker text in Urdu and English must be extracted reliably without requiring dedicated GPU hardware.
- **Decision**: Implement a CPU-optimized dual-engine pipeline using PaddleOCR as the primary model and EasyOCR as a transparent fallback.
- **Rationale**: PaddleOCR delivers superior text boundary detection for Urdu Nastaliq and English headline text on CPU, while EasyOCR provides recovery resilience if Paddle encounters an unsupported font glyph or corrupted frame buffer. Thread concurrency is strictly controlled via environment configuration (`OCR_CPU_THREADS`) to avoid CPU saturation.

---

## ADR 06: Decoupled Asynchronous Translation Queue

- **Context**: Machine translation of Urdu statements to English (and vice versa) introduces latency that must not block real-time news delivery.
- **Decision**: Real-time statements are emitted immediately in their original detected language. A database job is queued asynchronously for translation, and once generated, the translation is patched onto the live observation via WebSocket.
- **Rationale**: Preserves sub-second live ticker responsiveness while ensuring translations populate seamlessly as background compute completes.
