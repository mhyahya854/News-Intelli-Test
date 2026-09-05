# Recovered Configuration

The active configuration contract for the modular baseline is environment-driven through the root `.env.example` and backend configuration helpers. `legacy-fixed-runtime.yaml` is retained as a traced reference from the later monolithic runtime; it is not active configuration and must not be loaded by the Phase 12 backend without an explicit adapter.

PostgreSQL credentials, model directories, frontend origin, and runtime paths remain placeholders. No secret is committed here.
