# Recovery & Reconstruction Process

This document details the methodology used during the multi-phase forensic recovery and reconstruction that produced the verified **News-Intelli-Test** canonical codebase.

---

## The Recovery Problem

The local workspace contained historical archives, loose scripts from multiple development generations, varying configurations, and undocumented runtime modifications without an active Git revision history:
- 115 original historical files across `Documentation`, `Logs_and_Transcripts`, `Phase_Packages`, and `Source_and_Config`.
- Competing API contracts (`/api/v1` + WebSocket vs `/api` + SSE).
- Divergent database schemas and uncommitted repair scripts.
- Unverified CPU OCR dependencies on Windows platforms.

---

## Multi-Phase Recovery Strategy

The reconstruction was executed across four rigorous phases:

### Phase 1 — Forensic Baseline Audit & Codebase Consolidation
1. **Hash Manifest & Integrity**: Generated cryptographic SHA-256 baseline of all 115 original files.
2. **Lineage Reconstruction**: Mapped all historical phase packages and identified Phase 12 as the structural golden baseline.
3. **Extraction & Porting**: Extracted Phase 12 into a fresh, tracked codebase (`recovered-codebase`). Ported verified loose fixes into modular packages.
4. **Clean Codebase Hygiene**: Removed tracked secrets, machine-local hardcoded paths, and conflicting launcher artifacts.

### Phase 2 — Runtime Repair & Engine Validation
1. **Windows Native Runtime**: Configured clean Python 3.12 x64 runtime environment with native FFmpeg and PostgreSQL 17.
2. **Alembic Verification**: Validated 30 normalized relational tables and ensured Alembic migration chain reached revision `20260720_0006`.
3. **Bilingual Taxonomy Seeding**: Loaded and verified all 16 editorial categories and 1,037 exact Unicode keywords into PostgreSQL.
4. **CPU-Only OCR Harness**: Validated PaddleOCR and EasyOCR dual pipeline operating strictly in CPU mode with thread throttling to avoid Windows resource exhaustion.
5. **Contract Hardening**: Confirmed `/api/v1` REST routes and transactional outbox-backed WebSocket (`/api/v1/ws/live`) as the unified communication layer.

### Phase 3 — End-to-End Hardening & Test Verification
1. **Comprehensive Test Suite**: Expanded backend tests to 183 automated tests covering models, streaming, OCR, segmentation, taxonomy, deduplication, translation, persistence, API, and WebSocket contracts.
2. **Frontend Modernization**: Hardened React 19 + Vite dashboard, eliminating external font CDN vulnerabilities and validating production build and contract tests (5/5 passed).
3. **End-to-End Simulation**: Successfully executed a full pipeline integration test processing synthetic broadcast video frames through OCR, segmentation, deduplication, and persistence.

### Phase 4 — Public Hardening & Continuous Integration
1. **Repository Hardening**: Audited and confirmed zero active tracked secrets.
2. **Dependency Architecture**: Separated authoritative CPU PyTorch index (`requirements-torch-cpu.txt`) from standard backend requirements (`requirements.txt`).
3. **Automated CI**: Established automated GitHub Actions workflow executing backend and frontend test suites and production builds on push/PR.

---

## Disaster Recovery Guarantees

As a result of this reconstruction:
- The public repository (`News-Intelli-Test`) is 100% self-contained and reproducible from clean clone.
- No local-only artifacts or historical evidence files are required to build, test, run, or deploy the current product.
