# Phase 9 human summary acceptance set

The synthetic benchmark protects deterministic edge cases, but it does not prove that a full day of Pakistani news is complete. Before production acceptance, build a human-reviewed manifest from real canonical stories after translation and deduplication have passed.

For each category and Pakistan calendar date:

1. Export the accepted canonical stories and their complete English/Urdu translations.
2. Mark every story that must appear once in the Daily Brief.
3. Mark repeated or superseded stories that must not create another fact.
4. Record changed figures, dates, negations, rulings, scores, and status changes as separate review cases unless a human confirms they are the same developing story.
5. Run the five-minute summarizer twice with no new stories and confirm the second run changes nothing.
6. Compare the archived summary facts against this manifest. Missing or unexpected facts are release-blocking failures.

Use `manifest.example.jsonl` as the review format. One JSON object represents one category/day. `expected_facts` are complete bilingual facts that should appear exactly once and in source-time order. `forbidden_duplicates` are facts that must not appear as additional summary rows.

This fixture is intentionally human-authored. Model confidence, semantic similarity, and canonical-story counts are not substitutes for an end-of-day factual review.
