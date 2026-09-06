# Pakistani News Stream Intelligence Platform

A high-performance broadcast news intelligence system designed to ingest live Pakistani television streams, detect and extract Urdu and English news tickers (*bayaanat*), reconstruct rolling text sentences across video frames, classify statements against a curated bilingual taxonomy, deduplicate reporting into canonical stories, and stream real-time updates to an analyst dashboard.

---

## 1. System Capabilities & Features

- **Live Stream Ingestion**: Captures video frames from television broadcasts and YouTube live channels (e.g., Geo News, ARY News, Dawn News) at a controlled cadence (default 2.0 fps) via CPU-only FFmpeg pipelines.
- **Dual-Engine CPU OCR**: High-accuracy text detection and recognition across lower-third tickers and breaking news banners using **PaddleOCR** with automatic failover to **EasyOCR**.
- **Exact Rolling Sentence Reconstruction**: Reconstructs continuous statements from fragmented scrolling ticker frames using prefix/suffix character overlap algorithms while preserving numbers, abbreviations, and distinct factual updates.
- **Bilingual Keyword Taxonomy**: Classifies news statements into 16 topical categories (politics, judiciary, economy, security, etc.) against 1,037 canonical Urdu and English keywords.
- **Canonical Story Deduplication**: Clusters cross-channel reporting into unified canonical stories using exact lexical matching, n-gram token overlap, and multilingual sentence embeddings (`intfloat/multilingual-e5-small`).
- **Machine Translation**: Automated Urdu $\leftrightarrow$ English neural translation via CTranslate2 with micro-batching and transparent fallback to source text.
- **PostgreSQL Persistence & Outbox**: ACID-compliant relational storage across 30 tables with transactional Outbox pattern and local file-spool fallback during database maintenance.
- **Real-Time Live Updates**: WebSocket push streaming (`/api/v1/ws/live`) delivering initial state snapshots and instant event notifications (`new_sentence`, `canonical_story_created`).
- **Analyst Dashboard**: Modern React 19 + Vite dashboard with virtualized live feeds, category filtering, search, and system health telemetry.

---

## 2. Architecture Overview

```mermaid
graph TD
    A[Broadcast Streams / YouTube Live] -->|FFmpeg CPU capture| B[Frame Queue / Ingestion]
    B -->|Lower-third crop| C[PaddleOCR / EasyOCR]
    C -->|Extracted lines| D[Rolling Sentence Reconstructor]
    D -->|Complete statement| E[Keyword Taxonomy Classifier]
    E -->|Categorized observation| F[Deduplication Engine]
    F -->|Canonical story / Occurrence| G[PostgreSQL 17 Database]
    G -->|Transactional Outbox| H[Outbox Publisher]
    H -->|WebSocket /api/v1/ws/live| I[React Dashboard Frontend]
    G -->|REST API /api/v1| I
```

---

## 3. Prerequisites

- **Operating System**: Windows 10/11 x64 or Linux x64.
- **Python**: CPython **3.12.x** (x64 required).
- **Node.js**: Node.js **20.19+** or **22.12+** and npm.
- **PostgreSQL**: PostgreSQL **16** or **17** running locally or accessible via network.
- **FFmpeg**: `ffmpeg` (version 7.x or 8.x) installed and available on system PATH.

---

## 4. Configuration

Copy the sample configuration file to `.env`:

```bash
cp .env.example .env
```

Key environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `APP_PORT` | `8001` | Backend HTTP and WebSocket port |
| `FRONTEND_ORIGIN` | `http://127.0.0.1:5173` | Allowed CORS origin for frontend |
| `DATABASE_URL` | `postgresql://user:pass@127.0.0.1:5432/pak_news_recovery` | PostgreSQL connection string |
| `ADMIN_PASSWORD` | `CHANGE_ME` | Password for administrative endpoints |
| `STREAM_CAPTURE_FPS` | `2.0` | Frame capture rate from live streams |
| `OCR_CPU_THREADS` | `4` | Number of CPU threads dedicated to OCR inference |
| `API_CURSOR_SECRET` | *(random)* | Secret key for signing pagination cursors |
| `ADMIN_TOKEN_SECRET`| *(random)* | Secret key for issuing admin session tokens |

---

## 5. Backend Setup & Installation

1. **Create and activate a virtual environment**:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. **Install official CPU-only PyTorch**:

   *Note: PyTorch CPU wheels must be installed from the official index to avoid pulling multi-gigabyte CUDA dependencies.*

   ```powershell
   pip install -r backend/requirements-torch-cpu.txt
   ```
   *(Installs `torch==2.14.0` and `torchvision==0.29.0` via `https://download.pytorch.org/whl/cpu`)*

3. **Install application dependencies**:

   ```powershell
   pip install -r backend/requirements.txt
   ```

4. **Run Alembic database migrations**:

   Ensure PostgreSQL is running and the database specified in `DATABASE_URL` exists, then run:

   ```powershell
   cd backend
   alembic upgrade head
   cd ..
   ```

5. **Seed the keyword taxonomy**:

   ```powershell
   $env:PYTHONPATH="backend"
   python -m newsintel.seed
   ```

---

## 6. Frontend Setup

1. **Install dependencies**:

   ```powershell
   cd frontend
   npm ci
   ```

2. **Build for production** (optional):

   ```powershell
   npm run build
   ```

---

## 7. Running the Platform Locally

### Starting the Backend
```powershell
$env:PYTHONPATH="backend"
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8001 --app-dir backend
```
- API Documentation (OpenAPI / Swagger): `http://127.0.0.1:8001/docs`
- Health Check: `http://127.0.0.1:8001/api/v1/health`
- Live WebSocket Feed: `ws://127.0.0.1:8001/api/v1/ws/live`

### Starting the Frontend Development Server
```powershell
cd frontend
npm run dev
```
- Frontend UI: `http://127.0.0.1:5173/` (reverse proxy automatically directs `/api` and `/ws` to port `8001`).

---

## 8. Running Automated Tests

### Backend Tests (Pytest)
```powershell
$env:PYTHONPATH="backend"
.\.venv\Scripts\pytest.exe -v
```
*Current test suite: **183 passed**, 0 failed.*

### Frontend Tests (Node.js Native Runner)
```powershell
cd frontend
npm test
```
*Current test suite: **5 passed**, 0 failed.*

---

## 9. Capability Verification Matrix

The capabilities of this recovered development baseline have been rigorously validated against functional fixtures, unit suites, and end-to-end integration tests. Below is the authoritative verification matrix:

| Subsystem / Capability | Verification Status | Verification Detail & Operational State |
|---|---|---|
| **CPU Dual-Engine OCR** | **`VERIFIED`** | Torch-first import ordering and `PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=False` enforced. PaddleOCR with EasyOCR failover operates deterministically on CPU fixtures. |
| **Rolling Sentence Reconstruction** | **`VERIFIED`** | Prefix/suffix overlap algorithm reconstructs segmented tickers; preserves decimals, numbers, and abbreviation boundaries across frames. |
| **Bilingual Taxonomy Classification** | **`VERIFIED`** | 16 categories, 1,037 canonical Urdu & English keywords seeded and queried in PostgreSQL. Scalar unnest queries repaired for PostgreSQL compatibility. |
| **Story Deduplication (Lexical)** | **`VERIFIED`** | Exact match, token overlap, and sliding-window deduplication verified against multi-observation streams. |
| **PostgreSQL Persistence & Outbox** | **`VERIFIED`** | Alembic migration head `20260720_0006` (30 tables). Identity map dedup, transactional Outbox, and local spool failover all verified against real PostgreSQL 17. |
| **REST API & WebSocket Streaming** | **`VERIFIED`** | FastAPI endpoints (`/api/v1/feed`, `/api/v1/categories`, `/api/v1/stats`) and WebSocket (`/api/v1/ws/live` snapshot + events) validated. |
| **Analyst Dashboard Frontend** | **`VERIFIED`** | React 19 + Vite dashboard with reverse proxy to backend port 8001; builds cleanly with `npm run build` and passes `npm test`. |
| **Real Product Pipeline E2E** | **`VERIFIED`** | Production end-to-end test (`test_real_pipeline_e2e.py`) exercises FrameBus → OCR → Reconstruction → Taxonomy → Persistence → Outbox → API without mock business logic. |
| **Machine Translation (Urdu ↔ English)** | **`PARTIAL`** | Architecture, micro-batching, and transparent source-preservation fallback are fully implemented and verified. Full neural inference requires mounting CTranslate2 model weights under `models/translation/`. |
| **Video Stream Ingestion** | **`PARTIAL`** | FFmpeg CPU frame extraction, JPEG stream parsing, and cadence controls verified with synthetic broadcast video fixtures. Remote YouTube live HLS capture (`yt-dlp`) depends on external network connectivity and CDN availability. |
| **Semantic Deduplication Embeddings** | **`OPTIONAL`** | Sentence-Transformers (`intfloat/multilingual-e5-small`) supported via CPU PyTorch; deterministic lexical deduplication fallback operates when weights are unmounted. |
| **Additive Summarization** | **`OPTIONAL`** | Rule-based, non-abstractive additive summary pipeline is verified; generative external LLM summarization is optional and not required for baseline operations. |
| **Live 24/7 Broadcast Endurance** | **`NOT YET VALIDATED`** | Validated under bounded runtime and synthetic fixture tests; 24/7 continuous broadcast soak testing requires dedicated network monitoring infrastructure. |
| **Speaker / Face Attribution** | **`EXCLUDED`** | Face recognition / speaker identification from early exploratory prototypes was intentionally excluded from the Phase 12 production architecture in favor of deterministic entity classification. |
| **Production Cluster Deployment** | **`NOT YET VALIDATED`** | This repository is a verified, recovered **development baseline**. It is verified for local single-node development and evaluation, not production multi-node Kubernetes clustering. |

---

## 10. Project & Recovery Status

- **Maturity**: Recovered Development Baseline (Phase 3 Hardened).
- **Alembic Head**: `20260720_0006` (30 relational tables).
- **Backend Tests**: 183 passed, 0 failed.
- **Frontend Tests**: 5 passed, 0 failed.
- **Licensing**: MIT License (`LICENSE`) with third-party notices (`NOTICE`).

