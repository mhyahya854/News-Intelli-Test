# Recovered Architecture

```text
recovered-codebase/
  backend/
    app.py
    newsintel/
      database.py models.py persistence.py runtime_pipeline.py
      streaming.py ocr.py segmentation.py taxonomy.py
      keyword_matching.py deduplication.py translation.py summarization.py
    alembic/versions/
    tests/
  frontend/
    src/main.jsx
    src/App.jsx
    src/styles.css
    package.json package-lock.json vite.config.js
  config/
    config.yaml.example
    model-manifest.example.json
  migrations/
  scripts/
  fixtures/
  docs/
  README.md
```

The recovered architecture should keep modular domain services and migrations from Phase 12, expose one documented `/api` contract, use SSE only if the repaired UI/backend contract is selected, and isolate the explicit stream worker lifecycle. Configuration and model paths must be environment-driven. Tests should be retained beside the modules rather than removed from a distribution package.
