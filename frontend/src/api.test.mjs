import test from "node:test";
import assert from "node:assert/strict";
import {
  applyTranslationEvent,
  buildWebSocketUrl,
  mergeLiveItems,
  normalizeFeedItem,
  pruneLiveItems,
  translationForDisplay,
} from "./api.js";

const now = Date.parse("2026-07-20T12:00:00Z");

function occurrence(overrides = {}) {
  return {
    occurrence_id: "11111111-1111-4111-8111-111111111111",
    observation_id: "obs-1",
    canonical_story_id: "22222222-2222-4222-8222-222222222222",
    stream_id: "33333333-3333-4333-8333-333333333333",
    channel_name: "Geo News",
    observed_at: "2026-07-20T11:55:00Z",
    original_text: "وزیر اعظم نے اجلاس طلب کر لیا۔",
    original_language: "ur",
    confidence: 0.97,
    category_ids: ["politics"],
    keyword_ids: ["keyword-1"],
    urgency: "normal",
    review_required: false,
    dedup_decision: "new_story",
    dedup_layer: "exact_hash",
    translation: { status: "pending", source_language: "ur", target_language: "en", translated_text: null },
    ...overrides,
  };
}

test("normalizes REST feed items without duplicating the original language", () => {
  const item = normalizeFeedItem(occurrence());
  assert.equal(item.originalLanguage, "ur");
  assert.equal(item.translation.text, null);
  assert.deepEqual(item.categoryIds, ["politics"]);
});

test("applies the opposite-language translation to the matching observation", () => {
  const initial = [normalizeFeedItem(occurrence())];
  const updated = applyTranslationEvent(initial, {
    observation_id: "obs-1",
    status: "complete",
    source_language: "ur",
    target_language: "en",
    translated_text: "The prime minister convened a meeting.",
  }, { now });
  assert.equal(translationForDisplay(updated[0]), "The prime minister convened a meeting.");
});

test("preserves repeated observations as separate live items", () => {
  const first = occurrence();
  const repeated = occurrence({
    occurrence_id: "44444444-4444-4444-8444-444444444444",
    observation_id: "obs-2",
    observed_at: "2026-07-20T11:58:00Z",
    dedup_decision: "semantic_repeat",
  });
  const items = mergeLiveItems([], [first, repeated], { now });
  assert.equal(items.length, 2);
  assert.equal(items[0].observationId, "obs-2");
});

test("prunes observations outside the rolling 30-minute window", () => {
  const items = [
    normalizeFeedItem(occurrence()),
    normalizeFeedItem(occurrence({ occurrence_id: "55555555-5555-4555-8555-555555555555", observation_id: "old", observed_at: "2026-07-20T11:20:00Z" })),
  ];
  assert.deepEqual(pruneLiveItems(items, { now, windowMinutes: 30 }).map((item) => item.observationId), ["obs-1"]);
});

test("builds a same-origin secure websocket URL", () => {
  assert.equal(buildWebSocketUrl("/api/v1/ws/live", { protocol: "https:", host: "news.local" }), "wss://news.local/api/v1/ws/live");
});
