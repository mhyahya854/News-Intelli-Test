# Superseded Implementations & Abandoned Architectures

This document catalogs historical architectures, prototypes, and code implementations that were explicitly superseded or abandoned during the evolution of **News-Intelli-Test**.

---

## 1. Monolithic Script Architecture (`app(1–4).py`, `pipeline(1–4).py`)

- **Description**: Single-file monolithic Python scripts that bundled routing, database operations, OCR, and background tasks together.
- **Why Superseded**:
  - Lack of testability and component isolation.
  - High risk of regression during bug fixing.
  - Absence of formal database migration tracking (Alembic).
- **Current Replacement**: Modular `backend/newsintel/` package and FastAPI router hierarchy in `backend/newsintel/api.py`.

---

## 2. Server-Sent Events (SSE) Live Feed (`/api/live`)

- **Description**: An experimental unidirectional event-streaming prototype introduced in loose repair files (`app(4).py` / `App(3).jsx`).
- **Why Superseded**:
  - Unidirectional nature prevented client feedback, subscriptions, or backpressure negotiation.
  - Incompatible response schemas with Phase 12 client models.
  - Bypassed the transactional outbox reliability layer.
- **Current Replacement**: Bi-directional WebSocket endpoint (`/api/v1/ws/live`) backed by PostgreSQL transactional outbox.

---

## 3. Celery + Redis Distributed Task Queuing

- **Description**: Early architectural drafts specified a distributed Celery worker pool and Redis message broker.
- **Why Superseded**:
  - Introduced unnecessary operational overhead for single-host broadcast capture.
  - Redis requires third-party Windows ports or WSL2, adding complexity.
- **Current Replacement**: Asynchronous native Python `asyncio` task queues paired with durable PostgreSQL table queues (`translation_jobs`, `outbox_events`).

---

## 4. Docker Desktop & Containerized Deployment

- **Description**: Multi-container Docker Compose architecture containing backend, frontend, PostgreSQL, and Redis containers.
- **Why Superseded**:
  - Complex CPU hardware acceleration pass-through on Windows.
  - Licensing constraints for commercial Docker Desktop usage on Windows workstations.
  - Performance degradation during intensive continuous video frame decoding and OCR.
- **Current Replacement**: Native Windows environment using standardized setup scripts (`setup.ps1`, `start.ps1`) and native PostgreSQL 17.

---

## 5. Fuzzy Keyword & Lemmatization Matching

- **Description**: Early NSD specifications called for Levenshtein edit distance and morphological stemming for news classification.
- **Why Superseded**:
  - Induced significant false positive rates in political news categorization.
  - Caused semantic drift in proper nouns and political entity names.
- **Current Replacement**: Strict deterministic zero-fuzzy exact Unicode matching against a verified 1,037-keyword dictionary.

---

## 6. Graph Neural Network (GNN) Statement Clustering

- **Description**: Conceptual spec proposing GNN graph embeddings for cross-channel story attribution.
- **Why Superseded**:
  - Immature and unmaintained library dependencies for multilingual text on Windows CPU.
  - Excessive computational complexity for real-time 30-minute news clustering.
- **Current Replacement**: Deterministic lexical token n-gram overlap combined with calibrated multilingual sentence transformer embeddings (`intfloat/multilingual-e5-small`).
