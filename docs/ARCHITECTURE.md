# System Architecture & Technical Specifications

## 1. Modular Processing Pipeline

The platform follows an asynchronous, stage-gated streaming architecture:

```text
[Broadcast Stream]
       │
       ▼ (FFmpeg CPU subprocess @ 2.0 fps)
[Bounded Frame Bus]
       │
       ▼ (PersistentOCRForwardSink)
[Dual-Engine OCR: PaddleOCR / EasyOCR]
       │
       ▼ (Line tracking & prefix/suffix overlap)
[Rolling Sentence Reconstructor]
       │
       ▼ (Bilingual dictionary automaton)
[Keyword Taxonomy Matcher]
       │
       ▼ (Lexical overlap + multilingual-e5 embeddings)
[Canonical Story Deduplicator]
       │
       ▼ (PostgreSQL ACID Transaction)
[Entities: Sentences, Occurrences, Stories] + [OutboxEvent]
       │
       ▼ (Outbox Poller / Publisher)
[WebSocket ws://.../api/v1/ws/live] ──► [React Operational Dashboard]
```

---

## 2. Database Schema (PostgreSQL 17)

The schema is governed by Alembic migrations (`20260719_0001` through `20260720_0006`) and comprises 30 relational tables:

1. **Stream & Ingestion Layer**:
   - `channels`: Registered broadcast stations and YouTube Live sources.
   - `ocr_frames`: Audited frame timestamps and perceptual hashes.
   - `scrape_schedules`: Configurable polling intervals and cron triggers.
2. **Statement & Occurrence Layer**:
   - `sentences`: Unique textual statements in Urdu and English.
   - `sentence_occurrences`: Time-stamped detections on specific channels.
   - `sentence_categories`: Associations between sentences and taxonomy categories.
   - `sentence_translations`: Aligned bilingual translations.
3. **Story & Deduplication Layer**:
   - `canonical_stories`: Clustered events aggregating cross-channel coverage.
   - `story_categories`: Category tagging on canonical story clusters.
   - `review_queue`: Statements flagged for human verification (e.g. numeric conflicts).
4. **Taxonomy Layer**:
   - `categories`: 16 core topical verticals.
   - `keywords`: 1,037 canonical keyword phrases.
5. **Event & Outbox Layer**:
   - `outbox_events`: Transactional outbox table with skip-locked leasing.
   - `summary_runs`: Recorded 5-minute summarization runs.
   - `model_candidates`: Registered versions of embedding/OCR models.

---

## 3. Resilience & Failure Isolation

- **File-Based Persistence Spooling**: When PostgreSQL encounters connection drops, writes are spooled to `data/spool/persistence/`. The spooler guarantees zero data loss and replays writes once connectivity is restored.
- **Micro-Batching**: Machine translation and deduplication queries operate in sliding time windows (default 30 minutes) to constrain memory footprint.
- **Strict Bounded Buffers**: All inter-thread buses (`BoundedFrameBus`, `BoundedOCRResultBus`) use fixed capacities to prevent runaway memory leaks under sustained load.
