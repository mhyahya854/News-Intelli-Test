# Pak News Intelligence

## RECOVERED SOURCE BASELINE - RUNTIME VALIDATION PENDING

This directory is the first clean reconstruction from the Phase 12 modular source tree. It is intended to detect and organize Urdu and English statements from Pakistani news streams, retain evidence, translate and deduplicate observations, group stories, produce summaries, and expose newsroom workflows through a React frontend.

## Recovery status

The modular backend, migrations, tests, fixtures, frontend source, and package metadata were recovered from the Phase 12 archive. The confirmed PaddleOCR Windows environment workaround from `Source_and_Config/pipeline(4).py` was ported into `backend/newsintel/ocr.py`. The later monolithic `/api` plus SSE runtime remains a deferred integration candidate because its response shapes and schema differ from this Phase 12 `/api/v1` plus WebSocket baseline.

No dependencies have been installed, no service has been started, no migration has been executed, and no live OCR or stream has been run. PostgreSQL credentials, model bodies, FFmpeg/source availability, continuous ingestion, Paddle inference, frontend build/runtime, and end-to-end contract compatibility remain unvalidated.

The intended future setup is documented in the parent `_RECOVERY_EXECUTION/PHASE2_RUNTIME_PLAN.md`. Provenance and merge decisions are in the parent `_RECOVERY_EXECUTION` directory.

# Pakistani News Stream Intelligence Platform

Implementation through **Phase 12: sharing and protected runtime administration**.

The project remains a native Windows, Docker-free stack fixed to:

- CPython 3.12 x64
- FastAPI REST and WebSocket backend
- React and Vite frontend
- PostgreSQL as the authoritative database, queue, scheduler and outbox
- CPU-only OCR, translation and embedding models

## Historical Phase 12 contents

The following sections describe what the source archive claims to contain. They are provenance context, not results of this recovery run.

Every public news surface now uses the backend's authoritative share payload rather than rebuilding share text in React.

```text
Live occurrence
  Exact channel + exact broadcast timestamp + original + English + Urdu

Canonical story
  First-source share record for the accepted daily story

Daily brief
  Category + Pakistan date + verified English and Urdu summary
```

Available sharing actions:

- WhatsApp deep link
- Email `mailto:` payload
- Copy to clipboard
- Native Web Share, with copy fallback

A live observation is shareable only after its opposite-language translation is complete. Until then, the original observation stays visible but the share action reports that the bilingual payload is not ready.

## Protected Admin workspace

All write controls remain inside the password-protected Admin page:

- Add and deactivate YouTube Live streams
- Configure per-stream capture rate
- Add exact English or Urdu keywords
- Edit keyword text, priority, context requirements and exclusions
- Enable, disable or soft-delete keywords
- Add runtime categories with English/Urdu labels and category color
- Test a complete sentence against the active PostgreSQL taxonomy

Changes take effect through the existing runtime taxonomy refresh without restarting the application. The test lab uses exact normalized matching only; it does not add fuzzy spelling, typo recovery, stemming or autocorrection.

Public pages continue to expose only reader-facing functions: Live, Story Desk, Categories, Archive, Search, Saved and reader-safe Status.

## Core view behavior

```text
Live
  Every accepted occurrence from the latest 30 minutes
  Later repetitions remain visible with their new timestamp and channel

Story Desk
  One canonical daily story with occurrence and channel counts

Category / Archive briefs
  Additive, verified and non-duplicated bilingual facts
```

English mode shows English controls only. Urdu mode shows Urdu controls only. An Urdu original displays only its English translation beneath it; an English original displays only its Urdu translation.

## Future setup

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

The script does not install or create PostgreSQL. After your existing PostgreSQL installation is configured:

1. Set `DATABASE_URL` in `.env`.
2. Apply migrations through `20260720_0006`.
3. Run the idempotent taxonomy seed.

```powershell
Push-Location backend
$env:PYTHONPATH = "$PWD"
..\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
..\.venv\Scripts\python.exe -m newsintel.seed
Pop-Location
```

Start the development services:

```powershell
.\start.ps1
```

Open:

```text
Frontend:             http://127.0.0.1:5173
API documentation:    http://127.0.0.1:8000/docs
Canonical WebSocket:  ws://127.0.0.1:8000/api/v1/ws/live
Compatibility route:  ws://127.0.0.1:8000/ws/live
```

## Historical verification claims (not rerun)

```powershell
.\verify-frontend.ps1
```

The original release documentation says its gate runs:

- the complete backend regression suite;
- Phase 12 sharing and Admin source/API contracts;
- frontend API-client tests;
- production frontend build;
- npm vulnerability audit;
- checks for embedded sample data, gradients and remote fonts;
- checks that runtime write operations remain confined to Admin.

Automatically verified for this release:

- 176 backend and source-contract tests
- 5 frontend API/event tests
- 49 Python files parsed using Python 3.12 grammar
- 30 SQLAlchemy tables configured
- Alembic upgrade and full downgrade SQL generation
- React production build
- zero npm vulnerabilities
- no CSS gradients or remote font imports

## Target-computer acceptance still required

No live PostgreSQL server or populated browser session was available in the build environment. On the Windows target, verify:

1. Admin login with the configured password.
2. Add a temporary stream and confirm it appears without an application restart.
3. Add, edit, disable and soft-delete a temporary exact keyword.
4. Add a temporary category and verify its bilingual labels.
5. Run the test lab against active PostgreSQL taxonomy data.
6. Share one Urdu and one English live occurrence through all four actions.
7. Confirm the occurrence share uses the exact channel and broadcast timestamp.
8. Confirm a not-yet-translated occurrence cannot emit an incomplete share payload.
9. Verify all controls in both English and Urdu interface modes.

## Next phase

**Phase 13 — Statistics and useful extras** will add compact analytical views, high-priority alerts, operational trend charts and carefully selected export/PWA features without reintroducing dashboard clutter.
