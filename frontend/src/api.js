const DEFAULT_API_BASE = "/api/v1";
const DEFAULT_LIVE_WINDOW_MINUTES = 30;

export class ApiClientError extends Error {
  constructor(message, { status = 0, code = "request_failed", detail = null } = {}) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

function trimTrailingSlash(value) {
  return String(value || "").replace(/\/+$/, "");
}

export function apiBaseUrl() {
  return trimTrailingSlash(import.meta.env?.VITE_API_BASE_URL || DEFAULT_API_BASE);
}

export function buildUrl(path, params = {}, base = apiBaseUrl()) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const url = new URL(`${base}${normalizedPath}`, window.location.origin);
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    url.searchParams.set(key, String(value));
  });
  return url.toString();
}

export function buildWebSocketUrl(path = "/api/v1/ws/live", locationLike = window.location) {
  const explicit = import.meta.env?.VITE_WS_URL;
  if (explicit) return explicit;
  const protocol = locationLike.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${locationLike.host}${path}`;
}

async function parseResponse(response) {
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const body = data?.error || {};
    throw new ApiClientError(body.message || `Request failed with status ${response.status}.`, {
      status: response.status,
      code: body.code || "request_failed",
      detail: body.detail ?? null,
    });
  }
  return data;
}

export class ApiClient {
  constructor({ baseUrl = apiBaseUrl(), getAdminToken = () => null } = {}) {
    this.baseUrl = trimTrailingSlash(baseUrl);
    this.getAdminToken = getAdminToken;
  }

  async request(path, { method = "GET", params, body, admin = false, signal } = {}) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (admin) {
      const token = this.getAdminToken();
      if (!token) throw new ApiClientError("Administrator authentication is required.", { status: 401, code: "admin_required" });
      headers["X-Admin-Token"] = token;
    }
    const response = await fetch(buildUrl(path, params, this.baseUrl), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
    return parseResponse(response);
  }

  health(signal) { return this.request("/health", { signal }); }
  ready(signal) { return this.request("/ready", { signal }); }
  feed(params, signal) { return this.request("/feed", { params, signal }); }
  stories(params, signal) { return this.request("/stories", { params, signal }); }
  sentence(id, signal) { return this.request(`/sentence/${id}`, { signal }); }
  categories(signal) { return this.request("/categories", { signal }); }
  categoryStories(categoryId, params, signal) { return this.request(`/category/${categoryId}/sentences`, { params, signal }); }
  summary(categoryId, date, signal) { return this.request(`/category/${categoryId}/summary`, { params: { date }, signal }); }
  historyDates(category, signal) { return this.request("/history/dates", { params: { category }, signal }); }
  search(params, signal) { return this.request("/search", { params, signal }); }
  streams(signal) { return this.request("/streams", { signal }); }
  stats(signal) { return this.request("/stats/overview", { signal }); }
  keywordFrequency(params, signal) { return this.request("/stats/keyword-frequency", { params, signal }); }
  adminToken(password, signal) { return this.request("/admin/auth/token", { method: "POST", body: { password }, signal }); }
  adminKeywords(params, signal) { return this.request("/admin/keywords", { params, admin: true, signal }); }
  createStream(body, signal) { return this.request("/admin/streams", { method: "POST", body, admin: true, signal }); }
  deactivateStream(id, signal) { return this.request(`/admin/streams/${id}`, { method: "DELETE", admin: true, signal }); }
  createKeyword(body, signal) { return this.request("/admin/keywords", { method: "POST", body, admin: true, signal }); }
  updateKeyword(id, body, signal) { return this.request(`/admin/keywords/${id}`, { method: "PATCH", body, admin: true, signal }); }
  deactivateKeyword(id, signal) { return this.request(`/admin/keywords/${id}`, { method: "DELETE", admin: true, signal }); }
  createCategory(body, signal) { return this.request("/admin/categories", { method: "POST", body, admin: true, signal }); }
  testKeywordMatch(body, signal) { return this.request("/admin/keywords/test", { method: "POST", body, admin: true, signal }); }
  shareOccurrence(id, signal) { return this.request(`/share/occurrence/${id}`, { signal }); }
  shareSentence(id, signal) { return this.request(`/share/${id}`, { signal }); }
  shareSummary(categoryId, date, signal) { return this.request(`/share/summary/${categoryId}`, { params: { date }, signal }); }
}

export function normalizeFeedItem(item) {
  const translation = item.translation || {};
  const originalText = item.original_text ?? item.text ?? item.source_text ?? "";
  const originalLanguage = item.original_language ?? item.language ?? item.source_language ?? "unknown";
  const observationId = String(item.observation_id ?? item.id ?? item.occurrence_id ?? crypto.randomUUID());
  const occurrenceId = item.occurrence_id ? String(item.occurrence_id) : null;
  const categories = item.category_ids ?? item.accepted_category_ids ?? item.category_decisions
    ?.filter((entry) => entry.accepted)
    .map((entry) => entry.category_id) ?? [];
  const keywords = item.keyword_ids ?? item.keyword_hits?.map((entry) => entry.keyword_id) ?? [];
  const translationText = translation.translated_text ?? item.translation_text ?? item.translated_text ?? null;
  const translationStatus = translation.status ?? item.translation_status ?? item.status ?? (translationText ? "complete" : "pending");
  return {
    key: occurrenceId || observationId,
    occurrenceId,
    observationId,
    canonicalStoryId: item.canonical_story_id ?? item.story_id ?? null,
    streamId: item.stream_id ? String(item.stream_id) : null,
    channelName: item.channel_name ?? "Unknown channel",
    observedAt: item.observed_at ?? new Date().toISOString(),
    originalText,
    originalLanguage,
    confidence: Number(item.confidence ?? 0),
    categoryIds: [...new Set(categories)],
    keywordIds: [...new Set(keywords)],
    urgency: item.urgency ?? "normal",
    reviewRequired: Boolean(item.review_required),
    dedupDecision: item.dedup_decision ?? item.decision ?? (item.repeat ? "repeat" : "new_story"),
    dedupLayer: item.dedup_layer ?? "pending",
    translation: {
      status: translationStatus,
      sourceLanguage: translation.source_language ?? originalLanguage,
      targetLanguage: translation.target_language ?? item.target_language ?? (originalLanguage === "ur" ? "en" : originalLanguage === "en" ? "ur" : null),
      text: translationText,
    },
  };
}

export function normalizeStory(item) {
  return {
    id: String(item.id),
    originalText: item.original_text || "",
    originalLanguage: item.original_language || "unknown",
    englishText: item.english_text || null,
    urduText: item.urdu_text || null,
    firstSeenAt: item.first_seen_at,
    lastSeenAt: item.last_seen_at,
    occurrenceCount: Number(item.occurrence_count || 0),
    confidence: Number(item.confidence || 0),
    categoryIds: item.category_ids || [],
    channelNames: item.channel_names || [],
    reviewStatus: item.review_status || "accepted",
  };
}

export function translationForDisplay(item) {
  if (!item?.translation?.text) return null;
  if (item.originalLanguage === "ur" && item.translation.targetLanguage === "en") return item.translation.text;
  if (item.originalLanguage === "en" && item.translation.targetLanguage === "ur") return item.translation.text;
  return item.translation.text;
}

export function oppositeStoryText(story) {
  if (story.originalLanguage === "ur") return story.englishText;
  if (story.originalLanguage === "en") return story.urduText;
  return null;
}

export function pruneLiveItems(items, { now = Date.now(), windowMinutes = DEFAULT_LIVE_WINDOW_MINUTES, maximum = Number.POSITIVE_INFINITY } = {}) {
  const cutoff = now - windowMinutes * 60_000;
  return items
    .filter((item) => {
      const timestamp = Date.parse(item.observedAt);
      return Number.isFinite(timestamp) && timestamp >= cutoff;
    })
    .sort((left, right) => Date.parse(right.observedAt) - Date.parse(left.observedAt))
    .slice(0, maximum);
}

export function mergeLiveItems(current, incoming, options) {
  const byObservation = new Map();
  current.forEach((item) => byObservation.set(item.observationId, item));
  incoming.map(normalizeFeedItem).forEach((item) => {
    const previous = byObservation.get(item.observationId);
    byObservation.set(item.observationId, previous ? {
      ...previous,
      ...item,
      occurrenceId: item.occurrenceId || previous.occurrenceId,
      canonicalStoryId: item.canonicalStoryId || previous.canonicalStoryId,
      translation: item.translation.text ? item.translation : previous.translation,
    } : item);
  });
  return pruneLiveItems([...byObservation.values()], options);
}

export function applyTranslationEvent(current, payload, options) {
  const observationId = String(payload.observation_id || "");
  if (!observationId) return current;
  return pruneLiveItems(current.map((item) => item.observationId === observationId ? {
    ...item,
    translation: {
      status: payload.status || payload.translation_status || (payload.translated_text || payload.translation_text ? "complete" : "failed"),
      sourceLanguage: payload.source_language || item.originalLanguage,
      targetLanguage: payload.target_language || item.translation.targetLanguage,
      text: payload.translated_text ?? payload.translation_text ?? null,
    },
  } : item), options);
}

export function eventChangesLiveFeed(event) {
  return ["new_sentence", "live_observation", "translation_ready", "snapshot"].includes(event);
}

export class LiveSocket {
  constructor({ url = buildWebSocketUrl(), onEnvelope, onStatus, onResync, WebSocketImpl = WebSocket } = {}) {
    this.url = url;
    this.onEnvelope = onEnvelope || (() => {});
    this.onStatus = onStatus || (() => {});
    this.onResync = onResync || (() => {});
    this.WebSocketImpl = WebSocketImpl;
    this.socket = null;
    this.closedByClient = false;
    this.reconnectAttempt = 0;
    this.timer = null;
    this.lastEventId = null;
  }

  connect() {
    if (this.closedByClient || this.socket?.readyState === this.WebSocketImpl.OPEN || this.socket?.readyState === this.WebSocketImpl.CONNECTING) return;
    this.onStatus("connecting");
    const socket = new this.WebSocketImpl(this.url);
    this.socket = socket;
    socket.onopen = () => {
      const wasReconnect = this.reconnectAttempt > 0;
      this.reconnectAttempt = 0;
      this.onStatus("connected");
      if (wasReconnect) this.onResync();
    };
    socket.onmessage = (message) => {
      try {
        const envelope = JSON.parse(message.data);
        if (envelope.event_id && envelope.event_id === this.lastEventId) return;
        if (envelope.event_id) this.lastEventId = envelope.event_id;
        this.onEnvelope(envelope);
      } catch {
        this.onStatus("degraded");
      }
    };
    socket.onerror = () => this.onStatus("degraded");
    socket.onclose = () => {
      this.socket = null;
      if (this.closedByClient) return;
      this.onStatus("reconnecting");
      const delay = Math.min(30_000, 750 * (2 ** Math.min(this.reconnectAttempt, 6))) + Math.floor(Math.random() * 300);
      this.reconnectAttempt += 1;
      this.timer = window.setTimeout(() => this.connect(), delay);
    };
  }

  send(payload) {
    if (this.socket?.readyState === this.WebSocketImpl.OPEN) this.socket.send(JSON.stringify(payload));
  }

  close() {
    this.closedByClient = true;
    if (this.timer) window.clearTimeout(this.timer);
    this.socket?.close();
    this.socket = null;
    this.onStatus("closed");
  }
}
