# Historical Requirements & Current Disposition

This document reconciles all requirements from historical product specifications (`news-statement-detection-system-spec(1–8)`, PRDs, numbered phase specifications) against the current canonical **News-Intelli-Test** codebase.

---

## Requirement Reconciliation Matrix

| ID | Historical Requirement | Origin | Current Disposition | Implementation / Rationale |
|:---|:---|:---|:---:|:---|
| **REQ-01** | Native Windows FastAPI + React Runtime | `01_PRD.md` | `IMPLEMENTED_CURRENTLY` | CPython 3.12 x64 native foundation, FastAPI backend, React 19 frontend. |
| **REQ-02** | Relational Schema & Alembic Migration Chain | `05_DATABASE_SCHEMA.md` | `IMPLEMENTED_CURRENTLY` | 30 relational tables, Alembic head `20260720_0006`. |
| **REQ-03** | Bilingual Editorial Taxonomy (16 Categories, 1,037 Keywords) | `04_KEYWORD_TAXONOMY.md` | `IMPLEMENTED_CURRENTLY` | Seeded in PostgreSQL and memory-cached in `newsintel/taxonomy.py`. |
| **REQ-04** | Continuous Broadcast Stream Ingestion Harness | `05_PIPELINE.md` | `IMPLEMENTED_CURRENTLY` | yt-dlp + FFmpeg pipe with auto-reconnection in `newsintel/streaming.py`. |
| **REQ-05** | Bilingual CPU-Optimized OCR Pipeline | `03_TECH_STACK.md` | `IMPLEMENTED_CURRENTLY` | Dual PaddleOCR (primary) and EasyOCR (fallback) in `newsintel/ocr.py`. |
| **REQ-06** | Temporal Sentence Segmentation across Frames | `02_CAPABILITY_MAP.md` | `IMPLEMENTED_CURRENTLY` | 3-frame sliding reassembly in `newsintel/segmentation.py`. |
| **REQ-07** | Deterministic Keyword Detection & Filtering | `11_KEYWORDS_AND_FILTERS.md` | `IMPLEMENTED_CURRENTLY` | Zero-fuzzy Unicode exact matcher in `newsintel/taxonomy.py`. |
| **REQ-08** | Cross-Channel Deduplication & Story Grouping | `08_DEDUPLICATION_LOGIC.md` | `IMPLEMENTED_CURRENTLY` | 30-min rolling window clustering in `newsintel/deduplication.py`. |
| **REQ-09** | Asynchronous Neural Translation (Urdu <-> English) | `config.yaml` | `IMPLEMENTED_CURRENTLY` | Asynchronous translation queue in `newsintel/translation.py`. |
| **REQ-10** | Durable Outbox & Local Outage Spool Storage | `08_CONFIGURATION.md` | `IMPLEMENTED_CURRENTLY` | Transactional outbox + SHA-256 disk spool in `newsintel/persistence.py`. |
| **REQ-11** | Bilingual Daily Category Summarization Engine | `02_ARCHITECTURE.md` | `IMPLEMENTED_CURRENTLY` | 5-minute scheduler & immutable fact ledger in `newsintel/summarization.py`. |
| **REQ-12** | Standardized REST API v1 with Cursor Pagination | `04_BACKEND.md` | `IMPLEMENTED_CURRENTLY` | FastAPI router with HMAC signed cursors in `newsintel/api.py`. |
| **REQ-13** | Real-Time Live Delivery Transport | `04_BACKEND.md` | `IMPLEMENTED_DIFFERENTLY` | Outbox-backed WebSocket (`/api/v1/ws/live`) chosen over experimental SSE. |
| **REQ-14** | React 19 + Vite Operational Dashboard | `06_FRONTEND.md` | `IMPLEMENTED_CURRENTLY` | Modular components, light/dark themes, pause-on-hover buffering. |
| **REQ-15** | Social & News Sharing Payloads | `12_BUILD_PROMPT.md` | `IMPLEMENTED_CURRENTLY` | WhatsApp, Email, Copy, and Web Share integrations in `ShareModal.jsx`. |
| **REQ-16** | Celery + Redis Distributed Task Queues | `02_ARCHITECTURE.md` | `SUPERSEDED` | Replaced by in-process asyncio task workers and PostgreSQL job tables. |
| **REQ-17** | Docker Containerized Orchestration | `09_SETUP_AND_OPERATIONS.md` | `INTENTIONALLY_EXCLUDED` | Native Windows installation chosen for performance and zero licensing overhead. |
| **REQ-18** | Fuzzy Keyword Matching & Lemmatization | `spec(4).md` | `INTENTIONALLY_EXCLUDED` | Excluded to prevent false positive news detections. |
| **REQ-19** | Graph Neural Network (GNN) Statement Clustering | `spec(7).md` | `INTENTIONALLY_EXCLUDED` | Replaced by multilingual sentence transformer embeddings and lexical overlap. |
| **REQ-20** | Continuous 72-Hour Live Broadcast Soak Test | `11_BUILD_PHASES.md` | `PARTIAL` | Synthetic & short live tests pass; 72-hour soak scheduled for production deployment. |
| **REQ-21** | Advanced Longitudinal Analytics & Sentiments | `11_BUILD_PHASES.md` | `NOT_IMPLEMENTED` | Documented as Phase 13 future roadmap enhancement. |

---

## Status Summary

- **Total Historical Requirements Reconciled**: 21
- **Currently Implemented**: 14 (66.7%)
- **Implemented Differently (Enhanced)**: 1 (4.8%)
- **Intentionally Excluded (Architectural Principle)**: 3 (14.3%)
- **Superseded**: 1 (4.8%)
- **Partial (Operational Soak Gate)**: 1 (4.8%)
- **Roadmap / Not Implemented**: 1 (4.8%)
- **Unclassified / Missing**: 0 (0%)
