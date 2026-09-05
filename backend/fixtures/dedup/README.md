# Phase 6 Deduplication Benchmark

The synthetic manifest verifies control flow only. Production acceptance requires a manually labelled corpus of real Geo News and at least one second Pakistani news channel containing:

- exact repeats at different times;
- reworded same-story reports;
- the same story in Urdu and English;
- same-topic but different people, places, figures, dates, rulings, scores, and outcomes;
- cross-channel repeats inside and outside the 30-minute window;
- identical reports across two Pakistan calendar dates.

Every row must declare whether the observation creates a canonical story, merges with one, or is withheld for review. Thresholds cannot be accepted from the synthetic set alone.
