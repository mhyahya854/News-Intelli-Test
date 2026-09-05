# Changelog

## 0.12.0-phase.12 — 2026-07-20

### Added
- Reusable frontend `ShareBar` backed by server-generated sharing payloads.
- Exact live-occurrence sharing endpoint using the reporting channel and broadcast timestamp.
- WhatsApp, Email, clipboard and native Web Share actions across Live, Story Desk, story detail, category summary and archive summary surfaces.
- Complete password-protected Admin workspaces for streams, exact keywords, categories and taxonomy testing.
- Runtime stream add/deactivate, keyword add/edit/enable/disable/soft-delete and category creation workflows.
- Exact-match test lab backed by the active PostgreSQL taxonomy.
- Phase 12 API, OpenAPI, security-placement and frontend source-contract tests.

### Corrected
- Story-detail views now expose the same sharing actions as story cards.
- Live observations no longer reuse canonical-story timestamps when shared.
- Incomplete translations cannot produce partial bilingual sharing payloads.
- Stale Phase 11 read-only administrator copy and frontend package version were removed.
- Administrative write functions remain absent from public pages.

### Deliberate constraints
- Keyword deletion is non-destructive: it deactivates the row for auditability.
- No fuzzy spelling, typo dictionaries, stemming or autocorrection were reintroduced.
- No database migration was needed because Phase 12 uses the existing normalized schema and protected API contracts.

## 0.11.0-phase.11 — 2026-07-20

### Added
- Production React frontend backed entirely by the Phase 10 REST and WebSocket APIs.
- REST bootstrap, durable WebSocket event handling, exponential reconnect and REST resynchronization.
- Rolling 30-minute Live Feed retaining later repeated broadcasts.
- Pause-on-hover and manual pause with buffered new-observation delivery.
- Real Story Desk, category dashboards, day-wise archive, historical search, Saved workspace and read-only Status page.
- Opposite-language-only translation rendering and strict single-language UI controls.
- Pastel purple/pink light theme and midnight purple/pink dark theme without gradients or remote fonts.
- Canonical `/api/v1/ws/live` route while retaining `/ws/live` compatibility.
- Administrator password/token authentication foundation with operational data restricted to Admin.
- Frontend API-client regression tests and `verify-frontend.ps1`.

### Corrected assumptions
- A successful Vite build does not prove data integration; all simulated observations were removed and contract-tested against real endpoints.
- WebSocket delivery alone is insufficient after a disconnect; the browser resynchronizes through REST after reconnect.
- Live-feed duplicate preservation and canonical-story deduplication must remain separate in the UI.
- Interface bilingualism means a selected interface language, not simultaneous duplicate control labels.
- External web fonts conflict with the offline-first requirement and are not used.

### Verification
- 172 backend and frontend-source contract tests passed.
- 5 frontend API-client tests passed.
- React production build passed.
- npm audit reported zero vulnerabilities.
- Alembic upgrade and full downgrade SQL generation passed.
- No sample news, CSS gradients or remote font imports remain.
- Populated live-data and target-Windows visual acceptance remain pending.

## 0.10.0-phase.10 — 2026-07-20

### Added
- Complete FastAPI `/api/v1` REST contract for streams, live occurrences, canonical stories, categories, summaries, history, search, administration, statistics and sharing.
- Opaque HMAC-signed cursor pagination for growing datasets.
- Password-derived `X-Admin-Token` authentication and OpenAPI API-key documentation.
- Rolling 30-minute feed that intentionally retains repeated broadcast occurrences.
- Server-generated WhatsApp, email and Web Share payloads using the PRD format.
- PostgreSQL transactional-outbox WebSocket pump with `new_sentence`, `translation_ready`, canonical-story and summary events.
- WebSocket bootstrap snapshot, heartbeat response and reconnect-safe REST recovery.
- Consistent JSON error envelopes for API, validation and HTTP failures.
- Phase 10 API query indexes and Alembic revision `20260720_0006`.
- API doctor, Windows verification script and automated endpoint/WebSocket tests.

### Corrected assumptions
- The live feed must not use canonical-story deduplication: repeated broadcasts are separate live occurrences.
- WebSocket delivery alone is not a recovery mechanism; every connection receives a REST-equivalent 30-minute snapshot.
- Binary HMAC bytes cannot safely use an arbitrary delimiter split; cursors now use a fixed-length signature boundary.
- `204 No Content` responses must be genuinely bodyless.
- Search does not use misspelling or fuzzy-substitution logic under the user's exact-source-text policy.
- `LISTEN/NOTIFY` is not treated as durable storage; committed outbox rows remain authoritative.

### Verification
- 167 backend tests passed at the Phase 10 boundary.
- OpenAPI contains every required REST path and the `X-Admin-Token` security scheme.
- WebSocket connect, snapshot, ping/pong and outbox event mapping tests passed.
- Alembic upgrade and full downgrade SQL generation passed through `20260720_0006`.
- Live PostgreSQL and loaded-pipeline latency acceptance remain target-computer tasks.

## 0.9.0-phase.9 — 2026-07-19

### Added
- Five-minute PostgreSQL summary scheduler with advisory locking and idempotent run keys.
- Permanent additive English/Urdu fact ledger per category and Pakistan calendar date.
- `summary_runs` audit history and `summary_review_cases` ambiguity queue.
- Exact bilingual fact hashes plus multilingual embedding comparison.
- Fact-source links and traceability to every canonical source sentence.
- Summary update outbox events for the future WebSocket layer.
- Summarization doctor, synthetic benchmark, Windows verification script, and human full-day review fixture.
- Compact Daily Brief frontend with category filters, freshness state, verified fact count, PKT timestamps, and one selected interface language.

### Corrected assumptions
- A production intelligence summary must not rely on an English-only or unvalidated abstractive model.
- Canonical-story deduplication is necessary but not sufficient; the summary needs its own factual duplicate guard.
- Semantic similarity cannot override changed numbers, dates, entities, negation, or event state.
- Missing translation must delay a bilingual fact rather than create a partial or invented summary.
- A rerun without new stories must be a strict no-op.
- Repeated Live Feed observations and duplicate-free Daily Brief facts are separate product behaviors.

### Verification
- Backend regression, synthetic summary decisions, migration generation, Python 3.12 grammar, React build, and package integrity are recorded under `artifacts/phase9`.
- Live PostgreSQL, real E5 model calibration, and full-day human factual review remain target-host acceptance gates.

## 0.8.0-phase.8 — 2026-07-19

### Added
- Transactional PostgreSQL persistence for canonical stories, exact occurrences, category/keyword evidence, source provenance, review cases, alerts, translation jobs, and outbox events.
- Permanent occurrence-level translations and exact translation memory committed atomically.
- Seven-day raw OCR retention with frame metadata and no raw frame-image storage.
- End-to-end `pipeline_traces` for frames, segmented units, and observations.
- A rolling 30-minute PostgreSQL read model that intentionally retains repeated live observations.
- Idempotent transactional outbox with event keys, leases, retries, and dead-letter handling.
- PostgreSQL processing-job leases using `FOR UPDATE SKIP LOCKED` and active deduplication keys.
- Atomic checksum-verified local outage spool with ordered replay, restart recovery, capacity protection, and corruption quarantine.
- Persistence-aware runtime handoffs from OCR through asynchronous translation.
- Phase 8 migration `20260719_0004`, persistence CLI, API diagnostics, and Windows verification script.

### Corrected assumptions
- `LISTEN/NOTIFY` cannot replace durable event storage; it is only a wake-up signal for the transactional outbox.
- Redis is not required for restart-safe buffering in this PostgreSQL-only Windows build.
- Live-feed repetitions and canonical-story deduplication are separate storage concerns.
- Replay commands must carry the exact embedding model identity used when the decision was produced.
- A database outage must never cause the in-memory fallback to forget same-day canonical stories that were already committed before the outage.
- Each stage must persist or spool its evidence before forwarding work downstream.

### Verification
- 141 backend tests passed.
- Python 3.12 grammar validation passed for 39 Python files.
- SQLAlchemy mapper configuration passed for 28 tables.
- Alembic upgrade SQL generated 656 lines; full downgrade SQL generated 112 lines.
- React production build passed and npm audit reported zero known vulnerabilities.
- Live PostgreSQL acceptance remains pending on the target Windows computer.

## 0.8.0-phase.7 — 2026-07-19

### Added
- Complete-sentence Urdu→English and English→Urdu translation service.
- Original-first event delivery followed by asynchronous `translation_ready` updates.
- Rolling 30-minute live occurrence feed where later repeats remain visible.
- Direction-aware 35 ms micro-batching and exact in-batch repeat collapse.
- Exact translation memory and PostgreSQL persistence schema.
- CTranslate2 INT8 CPU adapter for pinned OPUS-MT checkpoints.
- Quality validation for script, numbers, length, control tokens and Roman Urdu.
- Strict human-curated benchmark with approved complete alternatives and required/forbidden terms.
- Fully reorganized reader frontend: Live, Story Desk, Categories, Daily Brief, Search, Saved and Admin.

### Corrected assumptions
- Translation must never block the original live observation.
- Live-feed duplication and summary duplication are different concerns.
- A static `human_approved=true` flag is not a valid benchmark; output must match reviewer-approved translations.
- OPUS-MT is a lightweight baseline candidate, not a quality guarantee; distilled IndicTrans2 remains a benchmark challenger if needed.
- English and Urdu interface labels must not be displayed together.

### Verification
- 124 backend regression tests passed.
- Micro-batching, cache reuse, 30-minute expiry, event order and translation quality failures tested.
- Phase 7 migration adds exact occurrence translation and permanent translation memory.
- Frontend production build validation is recorded under `artifacts/phase7`.
- Real model quality and target-host resource acceptance remain pending.

## 0.4.0-phase.3 — 2026-07-19

### Added
- CPU-only bilingual OCR subsystem for captured broadcast frames.
- One shared `PP-OCRv6_small_det` detector with script-specific Urdu/Arabic and English PP-OCRv5 recognizers.
- Perspective-correct line extraction, bounding boxes, confidence, script classification, and engine provenance.
- EasyOCR fallback for low-confidence recovery and periodic independent auditing.
- Mixed-script line reconciliation that preserves the original detected text.
- OCR worker attached to the Phase 2 bounded frame bus.
- OCR configuration and dependency/model diagnostic API endpoints.
- Strict labelled benchmark CLI with character accuracy, word accuracy, line recall, numeric-token recall, median latency, and p95 latency.
- Ground-truth fixture format and recommended 60-frame Pakistani-news test-set composition.
- CPU-only PyTorch installation from the official PyTorch CPU wheel index.
- Doctor rejection of CUDA-linked PyTorch builds.

### Corrected assumptions
- A single Arabic OCR pass is not sufficient for mixed Urdu/English Pakistani news frames.
- Model confidence is not measured transcription accuracy.
- Text below 95% confidence must not be discarded, because that would violate the no-missed-detection objective.
- EasyOCR is a recovery engine, not an accuracy guarantee.
- Frame deduplication cannot ignore changed numbers, dates, scores, prices, or casualty counts.
- Published model-table scores do not prove 95% accuracy on Nastaliq television tickers.
- Default PyPI PyTorch resolution is not accepted because it may introduce CUDA packages; the setup uses the official CPU-only index explicitly.

### Verification
- 41 backend regression tests passed.
- Shared-detector/two-recognizer PaddleOCR API adapter tested against the PaddleOCR 3.7 interface shape.
- Low-confidence fallback, periodic audit, mixed-script merge, numeric-change dedup guard, strict benchmark rejection, and bus-worker behavior tested.
- Python 3.12 syntax-policy validation passed.
- React production build passed.
- Real labelled Pakistani-news OCR acceptance remains pending on the target Windows host.

## 0.3.0-phase.2 — 2026-07-19

### Added
- CPython 3.12.x x64 as the single enforced runtime.
- yt-dlp live media resolver and CPU-only FFmpeg frame reader.
- Incremental JPEG pipe parser with frame-size safeguards.
- Source metadata, UTC timestamp, monotonic sequence, dimensions, and SHA-256 per frame.
- Bounded frame bus that fails visibly on backpressure instead of dropping frames.
- Stream supervision, escalating reconnects, alert state, degraded retries, and media-URL rotation.
- Geo News probe command and PowerShell launcher.
- Continuity, sequence, startup, queue, CPU, and RAM acceptance metrics.
- Structured JSON report on live resolver or capture failure.

### Verification
- 22 backend tests passed at the Phase 2 boundary.
- Real local FFmpeg synthetic frame extraction passed.
- Python 3.12 grammar compatibility passed.
- React production build passed.

## 0.2.0-phase.1 — 2026-07-19

- Complete PostgreSQL schema, migration, taxonomy, durable jobs, provenance, and model registry.

## 0.1.0-phase.0 — 2026-07-19

- Native Windows FastAPI/React foundation.
- PostgreSQL readiness check.
- Docker, Redis, Celery, and Nginx omitted by user instruction.

## 0.6.0-phase.5 — 2026-07-19

### Added
- Unicode-aware `pyahocorasick` 2.3.1 exact multi-pattern keyword index.
- Exact whole-word and whole-phrase boundary validation for Urdu and English.
- Immutable active-keyword snapshots loaded from PostgreSQL and refreshed every 60 seconds without restart.
- Stale-while-error behavior: the last valid index remains active if PostgreSQL is temporarily unavailable.
- Deterministic full-sentence category classifier with explicit evidence, context, exclusions, and multi-label output.
- Immediate `detection_observed` event contract for every accepted broadcast appearance.
- Explicit delivery states separating live observations, Phase 6 canonical stories, and Phase 9 summary eligibility.
- Bounded segmentation-to-keyword bus and worker with visible backpressure instead of silent loss.
- Keyword matching doctor, configuration endpoint, and test-match endpoint.
- Strict synthetic bilingual/context benchmark and CLI.
- Phase 5 dashboard status and observation → canonical story → summary flow visualization.

### Corrected assumptions
- A matched substring is not sufficient; token boundaries are mandatory.
- A broad word such as `case`, `power`, `board`, `budget`, `result`, or a city name cannot classify a category alone.
- Terms present in more than one category are automatically context-dependent.
- Repeated broadcasts should not disappear operationally: every occurrence is visible immediately, while summaries receive only the eventual canonical story.
- Fuzzy matching, stemming, typo dictionaries, and autocorrection remain disabled by user instruction.
- Database refresh failure must not stop live matching or cause data loss; the last good immutable index remains active.

### Verification
- 85 backend tests passed.
- 24-case benchmark: category precision 1.0, recall 1.0, exact-case pass rate 1.0.
- 5,000-sentence build-environment microbenchmark over 1,037 keywords: median 0.1571 ms, p95 0.2335 ms per sentence.
- Python 3.12 grammar-policy validation passed.
- Alembic PostgreSQL SQL generation passed.
- React production build passed.
- npm dependency-tree integrity passed; vulnerability auditing must be rerun when the registry audit endpoint is reachable.

## 0.7.0-phase.6 — 2026-07-19

### Added
- Canonical-story deduplication service with Pakistan calendar-day scoping.
- Exact normalized hash, semantic same-day, and 30-minute cross-channel merge layers.
- CPU-only `intfloat/multilingual-e5-small` SentenceTransformers provider pinned to a fixed revision.
- Permanent `sentence_occurrences` ledger preserving every repeat timestamp and source.
- `dedup_review_cases` for ambiguous semantic candidates withheld from summaries.
- Number, named-anchor, negation, event-state, category-overlap, and exact-token merge safeguards.
- Bounded keyword-observation bus and Phase 6 worker with visible backpressure.
- Immediate `detection_observed`, `canonical_story_created`, `canonical_story_updated`, and `dedup_review_requested` event contracts.
- Deduplication doctor, configuration API, CLI benchmark, fixtures, and model acceptance policy.
- Compact reader-facing React shell with pastel purple/pink light mode, midnight purple/pink dark mode, single selected interface language, and opposite-language translation only.

### Corrected assumptions
- Semantic similarity alone is insufficient for safe news-story merging.
- Repeated reports must remain visible and auditable instead of being discarded.
- Summary deduplication and live observation delivery are separate concerns.
- A 250-story candidate limit could miss older same-day repeats; the practical daily horizon is now 5,000 candidates pending indexed persistence in Phase 8.
- Cross-language candidates without compatible anchors are withheld for review until translation-based entity comparison is available.
- An uncertain candidate must not create a second summary fact or be silently merged.

### Verification
- 105 backend regression tests passed.
- Eight synthetic deduplication control steps passed exactly.
- Alembic upgrade and downgrade SQL generation passed through revision `20260719_0002`.
- SentenceTransformers 5.6.0 package metadata confirms Python >=3.10 and remains compatible with the fixed Python 3.12 policy.
- Frontend production build and dependency-tree integrity passed. The npm audit endpoint was unreachable in this environment.
- Real bilingual model and threshold acceptance remain pending on the target Windows host.
