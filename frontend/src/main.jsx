import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ApiClient,
  ApiClientError,
  LiveSocket,
  applyTranslationEvent,
  mergeLiveItems,
  normalizeStory,
  oppositeStoryText,
  pruneLiveItems,
  translationForDisplay,
} from "./api.js";
import "./styles.css";

const NAV = ["live", "stories", "categories", "archive", "search", "saved", "status", "admin"];
const PKT_TIME_ZONE = "Asia/Karachi";

const TEXT = {
  en: {
    app: "Newsroom",
    appSub: "Pakistan stream intelligence",
    nav: { live: "Live", stories: "Story Desk", categories: "Categories", archive: "Archive", search: "Search", saved: "Saved", status: "Status", admin: "Admin" },
    liveTitle: "Live broadcast feed", liveSub: "Every accepted observation from the latest 30 minutes. Repeated broadcasts remain visible.",
    storiesTitle: "Canonical stories", storiesSub: "Repeated coverage is grouped into one daily story without removing its occurrence history.",
    categoriesTitle: "Category desk", categoriesSub: "Open a category to view its verified brief and unique stories.",
    archiveTitle: "Daily archive", archiveSub: "Browse permanent, duplicate-free category briefs by Pakistan calendar date.",
    searchTitle: "Historical search", searchSub: "Search original Urdu or English text and stored translations.",
    savedTitle: "Saved newsroom", savedSub: "Items are stored in this browser for quick review.",
    statusTitle: "System status", statusSub: "Read-only stream and processing health. Configuration remains inside Admin.",
    adminTitle: "Admin workspace", adminSub: "Stream, keyword, OCR, model and system controls are restricted to administrators.",
    connected: "Connected", connecting: "Connecting", reconnecting: "Reconnecting", degraded: "Degraded", offline: "Offline", paused: "Paused",
    pause: "Pause", resume: "Resume", showNew: "Show new", newItems: "new items", latest: "Latest first", loadOlder: "Load older",
    allChannels: "All channels", allCategories: "All categories", allLanguages: "All source languages", english: "English", urdu: "Urdu", mixed: "Mixed",
    breakingOnly: "Breaking only", compact: "Compact", comfortable: "Comfortable", autoPause: "Feed pauses while you hover to read.",
    pendingTranslation: "Opposite-language translation is being prepared", translationFailed: "Translation needs review; the original remains preserved.",
    repeated: "Repeated coverage", firstReport: "First report", review: "Review required", confidence: "OCR", ago: "ago", now: "now",
    copy: "Copy", save: "Save", saved: "Saved", openStory: "Open story", appearance: "appearance", appearances: "appearances", channels: "channels",
    uniqueStories: "unique stories", observations: "observations", activeStreams: "active streams", avgConfidence: "average OCR", latency: "pipeline latency",
    today: "Today", selectCategory: "Select category", selectDate: "Select date", facts: "verified facts", noFacts: "No verified facts are available for this category and date.",
    noLive: "No observations match the current filters in the rolling window.", noStories: "No unique stories were returned.", noDates: "No archived dates are available.",
    searchPlaceholder: "Search court, بارش, budget…", searchButton: "Search", clear: "Clear", noSearch: "No matching stories were found.", searchHint: "Enter at least two characters.",
    retry: "Retry", loading: "Loading", unavailable: "Backend data is currently unavailable.", stale: "Showing the last available data while reconnecting.",
    streamHealth: "Stream health", processingHealth: "Processing health", lastSeen: "Last seen", fps: "capture FPS", reconnects: "reconnects", healthy: "Healthy",
    signIn: "Sign in", password: "Admin password", signOut: "Sign out", invalidLogin: "The administrator password was rejected.",
    adminReadOnly: "Streams, categories, and exact keywords are managed here without restarting the application.",
    keywords: "Keywords", streams: "Streams", active: "Active", inactive: "Inactive", runtimeEditing: "Runtime taxonomy", exactPolicy: "Exact normalized matching",
    light: "Light", dark: "Dark", language: "اردو", pkt: "PKT", pageOf: "page", refreshed: "Refreshed", window: "30-minute window",
    details: "Details", summary: "Verified brief", underlying: "Underlying stories", viewCategory: "Open category", back: "Back",
    source: "Source", firstSeen: "First seen", lastUpdated: "Last updated", occurrencesPreserved: "Occurrences preserved in Live",
    keyboard: "Press / to search", copyDone: "Copied to clipboard", savedDone: "Saved", removedDone: "Removed from saved items",
    adminNeeded: "Administrator authentication is required.", noSaved: "Nothing is saved yet.", translation: "Translation",
    whatsapp: "WhatsApp", email: "Email", share: "Share", shareUnavailable: "Sharing is available after the opposite-language translation is ready.", shareFailed: "Unable to prepare the share text.",
    adminReady: "Runtime controls are active. Changes are stored immediately and refresh the matcher without restarting the application.", addStream: "Add stream", channelName: "Channel name", youtubeUrl: "YouTube Live URL", captureRate: "Capture FPS", disable: "Disable", confirmDisable: "Disable this item?",
    addKeyword: "Add keyword", keywordTerm: "Keyword or exact phrase", sourceLanguage: "Language", priority: "Priority", normal: "Normal", high: "High", critical: "Critical", requiresContext: "Requires supporting context", contextTerms: "Context terms (comma separated)", excludedTerms: "Excluded terms (comma separated)", edit: "Edit", delete: "Remove", apply: "Apply", cancel: "Cancel",
    addCategory: "Create category", categoryId: "Machine ID", categoryEnglish: "English label", categoryUrdu: "Urdu label", categoryColor: "Color", keywordTest: "Test exact matching", testText: "Paste a complete Urdu or English sentence", testRun: "Run test", noMatches: "No exact keyword matches.", management: "Management", taxonomy: "Taxonomy", testLab: "Test lab", actionComplete: "Change saved",
  },
  ur: {
    app: "نیوز روم",
    appSub: "پاکستان اسٹریم انٹیلیجنس",
    nav: { live: "براہ راست", stories: "منفرد خبریں", categories: "زمرہ جات", archive: "محفوظ خلاصے", search: "تلاش", saved: "محفوظ", status: "حالت", admin: "انتظامیہ" },
    liveTitle: "براہ راست نشریاتی فیڈ", liveSub: "گزشتہ ۳۰ منٹ کی ہر درست نشریاتی خبر۔ دوبارہ نشر ہونے والی خبر بھی یہاں نظر آتی ہے۔",
    storiesTitle: "منفرد خبریں", storiesSub: "بار بار نشر ہونے والی خبر ایک روزانہ خبر میں جمع ہوتی ہے، مگر ہر نشریات کا ریکارڈ محفوظ رہتا ہے۔",
    categoriesTitle: "زمرہ جاتی ڈیسک", categoriesSub: "تصدیق شدہ خلاصہ اور منفرد خبریں دیکھنے کے لیے زمرہ کھولیں۔",
    archiveTitle: "روزانہ محفوظ خلاصے", archiveSub: "پاکستانی تاریخ کے مطابق مستقل اور غیر مکرر زمرہ جاتی خلاصے دیکھیں۔",
    searchTitle: "تاریخی تلاش", searchSub: "اصل اردو یا انگریزی خبر اور محفوظ ترجمے میں تلاش کریں۔",
    savedTitle: "محفوظ نیوز روم", savedSub: "فوری جائزے کے لیے خبریں اس براؤزر میں محفوظ رہتی ہیں۔",
    statusTitle: "نظام کی حالت", statusSub: "اسٹریم اور پراسیسنگ کی صرف معلومات۔ تمام ترتیبات انتظامیہ میں ہیں۔",
    adminTitle: "انتظامی ورک اسپیس", adminSub: "اسٹریم، کلیدی الفاظ، او سی آر، ماڈلز اور نظام کی ترتیبات صرف منتظم کے لیے ہیں۔",
    connected: "منسلک", connecting: "رابطہ ہو رہا ہے", reconnecting: "دوبارہ رابطہ", degraded: "محدود", offline: "آف لائن", paused: "رکا ہوا",
    pause: "روکیں", resume: "جاری کریں", showNew: "نئی خبریں دکھائیں", newItems: "نئی خبریں", latest: "تازہ ترین پہلے", loadOlder: "پرانا مواد دکھائیں",
    allChannels: "تمام چینلز", allCategories: "تمام زمرے", allLanguages: "تمام اصل زبانیں", english: "انگریزی", urdu: "اردو", mixed: "مخلوط",
    breakingOnly: "صرف اہم خبریں", compact: "مختصر", comfortable: "کشادہ", autoPause: "پڑھنے کے دوران ماؤس رکھنے سے فیڈ رک جاتی ہے۔",
    pendingTranslation: "دوسری زبان کا ترجمہ تیار ہو رہا ہے", translationFailed: "ترجمے کو جائزے کی ضرورت ہے؛ اصل خبر محفوظ ہے۔",
    repeated: "دوبارہ نشر", firstReport: "پہلی خبر", review: "جائزہ درکار", confidence: "او سی آر", ago: "پہلے", now: "ابھی",
    copy: "کاپی", save: "محفوظ کریں", saved: "محفوظ", openStory: "خبر کھولیں", appearance: "نشریات", appearances: "نشریات", channels: "چینلز",
    uniqueStories: "منفرد خبریں", observations: "نشریاتی خبریں", activeStreams: "فعال اسٹریمز", avgConfidence: "اوسط او سی آر", latency: "پائپ لائن وقت",
    today: "آج", selectCategory: "زمرہ منتخب کریں", selectDate: "تاریخ منتخب کریں", facts: "تصدیق شدہ حقائق", noFacts: "اس زمرے اور تاریخ کے لیے کوئی تصدیق شدہ حقیقت موجود نہیں۔",
    noLive: "موجودہ فلٹرز کے مطابق گزشتہ ۳۰ منٹ میں کوئی خبر نہیں۔", noStories: "کوئی منفرد خبر نہیں ملی۔", noDates: "کوئی محفوظ تاریخ موجود نہیں۔",
    searchPlaceholder: "عدالت، rain، بجٹ…", searchButton: "تلاش", clear: "صاف کریں", noSearch: "کوئی متعلقہ خبر نہیں ملی۔", searchHint: "کم از کم دو حروف لکھیں۔",
    retry: "دوبارہ کوشش", loading: "لوڈ ہو رہا ہے", unavailable: "بیک اینڈ ڈیٹا ابھی دستیاب نہیں۔", stale: "دوبارہ رابطے کے دوران آخری دستیاب معلومات دکھائی جا رہی ہیں۔",
    streamHealth: "اسٹریم کی حالت", processingHealth: "پراسیسنگ کی حالت", lastSeen: "آخری رابطہ", fps: "کیپچر رفتار", reconnects: "دوبارہ رابطے", healthy: "درست",
    signIn: "سائن اِن", password: "انتظامی پاس ورڈ", signOut: "سائن آؤٹ", invalidLogin: "انتظامی پاس ورڈ قبول نہیں ہوا۔",
    adminReadOnly: "اسٹریم، زمرے اور درست کلیدی الفاظ یہاں ایپ دوبارہ شروع کیے بغیر منظم کیے جاتے ہیں۔",
    keywords: "کلیدی الفاظ", streams: "اسٹریمز", active: "فعال", inactive: "غیر فعال", runtimeEditing: "فوری درجہ بندی", exactPolicy: "صرف درست معمول شدہ مماثلت",
    light: "لائٹ", dark: "ڈارک", language: "English", pkt: "پاکستان وقت", pageOf: "صفحہ", refreshed: "تازہ", window: "۳۰ منٹ کی ونڈو",
    details: "تفصیل", summary: "تصدیق شدہ خلاصہ", underlying: "بنیادی خبریں", viewCategory: "زمرہ کھولیں", back: "واپس",
    source: "ذریعہ", firstSeen: "پہلی بار", lastUpdated: "آخری تازہ کاری", occurrencesPreserved: "تمام نشریات براہ راست صفحے میں محفوظ",
    keyboard: "تلاش کے لیے / دبائیں", copyDone: "کاپی ہو گیا", savedDone: "محفوظ ہو گیا", removedDone: "محفوظ فہرست سے ہٹا دیا گیا",
    adminNeeded: "انتظامی شناخت درکار ہے۔", noSaved: "ابھی کوئی خبر محفوظ نہیں۔", translation: "ترجمہ",
    whatsapp: "واٹس ایپ", email: "ای میل", share: "شیئر", shareUnavailable: "دوسری زبان کا ترجمہ تیار ہونے کے بعد شیئرنگ دستیاب ہوگی۔", shareFailed: "شیئر متن تیار نہیں ہو سکا۔",
    adminReady: "فوری انتظامی کنٹرول فعال ہیں۔ تبدیلیاں فوراً محفوظ ہوتی ہیں اور ایپ دوبارہ شروع کیے بغیر میچر تازہ ہو جاتا ہے۔", addStream: "اسٹریم شامل کریں", channelName: "چینل کا نام", youtubeUrl: "یوٹیوب لائیو لنک", captureRate: "کیپچر ایف پی ایس", disable: "غیر فعال", confirmDisable: "اس آئٹم کو غیر فعال کریں؟",
    addKeyword: "کلیدی لفظ شامل کریں", keywordTerm: "درست کلیدی لفظ یا فقرہ", sourceLanguage: "زبان", priority: "اہمیت", normal: "معمول", high: "اہم", critical: "انتہائی اہم", requiresContext: "معاون سیاق ضروری", contextTerms: "سیاقی الفاظ (کاما سے الگ)", excludedTerms: "ممنوع الفاظ (کاما سے الگ)", edit: "ترمیم", delete: "ہٹائیں", apply: "محفوظ کریں", cancel: "منسوخ",
    addCategory: "زمرہ بنائیں", categoryId: "مشینی شناخت", categoryEnglish: "انگریزی نام", categoryUrdu: "اردو نام", categoryColor: "رنگ", keywordTest: "درست مماثلت آزمائیں", testText: "مکمل اردو یا انگریزی جملہ درج کریں", testRun: "ٹیسٹ چلائیں", noMatches: "کوئی درست کلیدی لفظ نہیں ملا۔", management: "انتظام", taxonomy: "درجہ بندی", testLab: "ٹیسٹ لیب", actionComplete: "تبدیلی محفوظ ہوگئی",
  },
};

const ICON_PATHS = {
  live: "M4 12a8 8 0 0 1 8-8m-8 8a8 8 0 0 0 8 8m0-12a4 4 0 0 0 0 8m0-8a4 4 0 0 1 0 8m0-5a1 1 0 1 0 0 2 1 1 0 0 0 0-2",
  stories: "M5 5h14v12H8l-3 3V5Zm4 4h6m-6 4h8",
  categories: "M4 4h6v6H4V4Zm10 0h6v6h-6V4ZM4 14h6v6H4v-6Zm10 0h6v6h-6v-6Z",
  archive: "M5 4h14v16H5V4Zm0 5h14M9 2v4m6-4v4",
  search: "m20 20-4.4-4.4m2.4-5.1a7.5 7.5 0 1 1-15 0 7.5 7.5 0 0 1 15 0Z",
  saved: "M7 4h10v17l-5-3-5 3V4Z",
  status: "M4 18h4V9H4v9Zm6 0h4V4h-4v14Zm6 0h4v-7h-4v7Z",
  admin: "M12 3a4 4 0 1 1 0 8 4 4 0 0 1 0-8Zm-7 18a7 7 0 0 1 14 0",
  pause: "M8 5v14m8-14v14", play: "m8 5 11 7-11 7V5Z", moon: "M20 15.5A8 8 0 0 1 8.5 4 8 8 0 1 0 20 15.5Z",
  sun: "M12 4V2m0 20v-2m8-8h2M2 12h2m13.7-5.7 1.4-1.4M4.9 19.1l1.4-1.4m0-11.4L4.9 4.9m14.2 14.2-1.4-1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  copy: "M8 8h11v12H8V8Zm-3 8H4V4h11v1", check: "m5 12 4 4L19 6", refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  chevron: "m9 18 6-6-6-6", external: "M14 4h6v6m0-6-9 9M5 7v13h13v-5", filter: "M4 5h16l-6 7v6l-4 2v-8L4 5Z",
  alert: "M12 4 3 20h18L12 4Zm0 6v4m0 3h.01", close: "m6 6 12 12M18 6 6 18", plus: "M12 5v14m-7-7h14",
  whatsapp: "M5 21l1.3-4.2A8 8 0 1 1 9 19.4L5 21Zm5-12c.4 2.3 1.7 3.7 4 4.4", email: "M3 5h18v14H3V5Zm0 1 9 7 9-7", share: "M8 12l8-5v4c3.8.4 5.5 2.6 5 6-1.2-1.7-2.8-2.6-5-2.7v4L8 12Z",
};

function Icon({ name, size = 18 }) {
  return <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true"><path d={ICON_PATHS[name] || ICON_PATHS.live} /></svg>;
}

function useStoredState(key, initialValue, storage = "local") {
  const store = storage === "session" ? window.sessionStorage : window.localStorage;
  const [value, setValue] = useState(() => {
    try { const raw = store.getItem(key); return raw === null ? initialValue : JSON.parse(raw); } catch { return initialValue; }
  });
  useEffect(() => { try { store.setItem(key, JSON.stringify(value)); } catch { /* storage may be unavailable */ } }, [key, store, value]);
  return [value, setValue];
}

function pakistanDate() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: PKT_TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}

function formatDate(value, language, options = {}) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(language === "ur" ? "ur-PK" : "en-PK", { timeZone: PKT_TIME_ZONE, day: "2-digit", month: "short", year: "numeric", ...options }).format(new Date(value));
}

function formatTime(value, language, includeSeconds = false) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(language === "ur" ? "ur-PK" : "en-PK", { timeZone: PKT_TIME_ZONE, hour: "2-digit", minute: "2-digit", second: includeSeconds ? "2-digit" : undefined }).format(new Date(value));
}

function formatNumber(value, language, maximumFractionDigits = 0) {
  return new Intl.NumberFormat(language === "ur" ? "ur-PK" : "en-PK", { maximumFractionDigits }).format(value ?? 0);
}

function relativeTime(value, language) {
  const deltaSeconds = Math.round((Date.parse(value) - Date.now()) / 1000);
  if (!Number.isFinite(deltaSeconds)) return "—";
  if (Math.abs(deltaSeconds) < 10) return TEXT[language].now;
  const formatter = new Intl.RelativeTimeFormat(language === "ur" ? "ur-PK" : "en-PK", { numeric: "auto" });
  if (Math.abs(deltaSeconds) < 60) return formatter.format(deltaSeconds, "second");
  return formatter.format(Math.round(deltaSeconds / 60), "minute");
}

function textDirection(language) { return language === "ur" ? "rtl" : language === "en" ? "ltr" : "auto"; }
function categoryName(category, language) { return category ? (language === "ur" ? category.label_ur : category.label_en) : "—"; }

function ErrorNotice({ error, labels, onRetry }) {
  if (!error) return null;
  return <div className="notice error"><Icon name="alert" /><div><strong>{labels.unavailable}</strong><span>{error.message || String(error)}</span></div>{onRetry && <button onClick={onRetry}>{labels.retry}</button>}</div>;
}

function LoadingRows({ count = 3 }) {
  return <div className="loading-rows" aria-label="Loading">{Array.from({ length: count }, (_, index) => <div className="loading-row" key={index}><i /><span /><span /></div>)}</div>;
}

function EmptyState({ children }) { return <div className="empty-state"><span className="empty-mark">N</span><p>{children}</p></div>; }

function PageHeader({ title, subtitle, children }) {
  return <header className="page-header"><div><h1>{title}</h1><p>{subtitle}</p></div>{children && <div className="page-header-actions">{children}</div>}</header>;
}

function CategoryBadge({ id, categories, language }) {
  const category = categories.get(id);
  return <span className="category-badge" style={{ "--category": category?.color || "#8b6dd8" }}>{categoryName(category, language) || id}</span>;
}

function Metric({ label, value, hint, tone = "purple" }) {
  return <article className={`metric ${tone}`}><small>{label}</small><strong>{value}</strong>{hint && <span>{hint}</span>}</article>;
}

function ShareBar({ client, labels, occurrenceId = null, sentenceId = null, categoryId = null, date = null, toast = () => {}, compact = false }) {
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(false);
  const available = Boolean(occurrenceId || sentenceId || (categoryId && date));
  const prepare = useCallback(async () => {
    if (payload) return payload;
    if (!available) throw new Error(labels.shareUnavailable);
    setLoading(true);
    try {
      const value = occurrenceId ? await client.shareOccurrence(occurrenceId) : sentenceId ? await client.shareSentence(sentenceId) : await client.shareSummary(categoryId, date);
      setPayload(value);
      return value;
    } finally { setLoading(false); }
  }, [payload, available, occurrenceId, sentenceId, categoryId, date, client, labels.shareUnavailable]);
  const action = async (mode, event) => {
    event?.stopPropagation();
    try {
      const value = await prepare();
      if (mode === "whatsapp") window.open(value.whatsapp_url, "_blank", "noopener,noreferrer");
      else if (mode === "email") window.location.href = value.email_url;
      else if (mode === "share" && navigator.share) await navigator.share(value.web_share);
      else { await navigator.clipboard.writeText(value.text); toast(labels.copyDone); }
    } catch (error) {
      toast(error?.code === "translation_not_ready" ? labels.shareUnavailable : labels.shareFailed);
    }
  };
  return <div className={`share-bar ${compact ? "compact" : ""}`} aria-label={labels.share}>
    <button disabled={!available || loading} title={labels.whatsapp} onClick={(event) => action("whatsapp", event)}><Icon name="whatsapp" />{!compact && labels.whatsapp}</button>
    <button disabled={!available || loading} title={labels.email} onClick={(event) => action("email", event)}><Icon name="email" />{!compact && labels.email}</button>
    <button disabled={!available || loading} title={labels.copy} onClick={(event) => action("copy", event)}><Icon name="copy" />{!compact && labels.copy}</button>
    <button disabled={!available || loading} title={labels.share} onClick={(event) => action("share", event)}><Icon name="share" />{!compact && labels.share}</button>
  </div>;
}

function useLiveFeed(client) {
  const [items, setItems] = useState([]);
  const [buffer, setBuffer] = useState([]);
  const [nextCursor, setNextCursor] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [status, setStatus] = useState("connecting");
  const [error, setError] = useState(null);
  const [manualPaused, setManualPaused] = useState(false);
  const [hoverPaused, setHoverPaused] = useState(false);
  const socketRef = useRef(null);
  const pausedRef = useRef(false);
  pausedRef.current = manualPaused || hoverPaused;

  const synchronize = useCallback(async ({ cursor = null, append = false } = {}) => {
    try {
      const collected = [];
      const seenCursors = new Set();
      let activeCursor = cursor;
      let response = null;
      do {
        response = await client.feed({ limit: 100, cursor: activeCursor, window_minutes: 30 });
        collected.push(...(response.items || []));
        const next = response.page?.next_cursor || null;
        if (next && seenCursors.has(next)) throw new ApiClientError("The live-feed cursor repeated unexpectedly.", { code: "cursor_loop" });
        if (next) seenCursors.add(next);
        activeCursor = next;
      } while (!cursor && response.page?.has_more && activeCursor);
      setItems((current) => append ? mergeLiveItems(current, collected) : mergeLiveItems([], collected));
      setNextCursor(response?.page?.next_cursor || null);
      setHasMore(Boolean(response?.page?.has_more));
      setError(null);
    } catch (caught) {
      setError(caught);
    }
  }, [client]);

  useEffect(() => {
    synchronize();
    const socket = new LiveSocket({
      onStatus: setStatus,
      onResync: () => synchronize(),
      onEnvelope: (envelope) => {
        const event = envelope.event;
        const data = envelope.data || {};
        if (event === "connected") return;
        if (event === "snapshot") {
          setItems((current) => mergeLiveItems(current, data.items || []));
          setNextCursor(data.next_cursor || null);
          setHasMore(Boolean(data.has_more));
          if (data.has_more) synchronize();
          return;
        }
        if (event === "new_sentence" || event === "live_observation") {
          if (pausedRef.current) setBuffer((current) => mergeLiveItems(current, [data]));
          else setItems((current) => mergeLiveItems(current, [data]));
          return;
        }
        if (event === "translation_ready") {
          setItems((current) => applyTranslationEvent(current, data));
          setBuffer((current) => applyTranslationEvent(current, data));
          return;
        }
        if (["canonical_story_created", "canonical_story_updated", "summary_updated"].includes(event)) {
          window.dispatchEvent(new CustomEvent("newsintel:data-changed", { detail: envelope }));
        }
      },
    });
    socketRef.current = socket;
    socket.connect();
    const pruneTimer = window.setInterval(() => {
      setItems((current) => pruneLiveItems(current));
      setBuffer((current) => pruneLiveItems(current));
    }, 15_000);
    const syncTimer = window.setInterval(() => synchronize(), 45_000);
    return () => { window.clearInterval(pruneTimer); window.clearInterval(syncTimer); socket.close(); };
  }, [synchronize]);

  const flush = useCallback(() => {
    setItems((current) => mergeLiveItems(current, buffer));
    setBuffer([]);
  }, [buffer]);

  useEffect(() => { if (!manualPaused && !hoverPaused && buffer.length) flush(); }, [manualPaused, hoverPaused, buffer.length, flush]);

  return { items, bufferCount: buffer.length, nextCursor, hasMore, status, error, manualPaused, setManualPaused, setHoverPaused, synchronize, loadOlder: () => nextCursor && synchronize({ cursor: nextCursor, append: true }), flush };
}

function LiveCard({ item, categories, language, labels, density, saved, onToggleSaved, onOpenStory, client, toast }) {
  const opposite = translationForDisplay(item);
  const repeated = !["new_story", "new", "first_report"].includes(item.dedupDecision);
  return <article className={`live-card ${density} ${item.urgency === "critical" ? "critical" : item.urgency === "high" ? "high" : ""}`}>
    <div className="live-card-rail" />
    <header className="live-card-header">
      <div className="source-line"><span className="channel-avatar">{item.channelName.slice(0, 1).toUpperCase()}</span><div><strong>{item.channelName}</strong><time title={formatDate(item.observedAt, language)}>{relativeTime(item.observedAt, language)} · {formatTime(item.observedAt, language, true)}</time></div></div>
      <div className="card-badges">{item.categoryIds.slice(0, 2).map((id) => <CategoryBadge key={id} id={id} categories={categories} language={language} />)}{repeated && <span className="repeat-badge">{labels.repeated}</span>}{item.reviewRequired && <span className="review-badge">{labels.review}</span>}</div>
    </header>
    <div className="news-copy">
      <p className="original-text" dir={textDirection(item.originalLanguage)} lang={item.originalLanguage}>{item.originalText}</p>
      {opposite ? <div className="translation-block"><small>{labels.translation}</small><p dir={textDirection(item.translation.targetLanguage)} lang={item.translation.targetLanguage}>{opposite}</p></div> : item.translation.status === "failed" || item.translation.status === "review" ? <div className="translation-state failed"><Icon name="alert" />{labels.translationFailed}</div> : <div className="translation-state"><span className="typing"><i /><i /><i /></span>{labels.pendingTranslation}</div>}
    </div>
    <footer className="card-footer">
      <div className="evidence"><span>{labels.confidence} {formatNumber(item.confidence * 100, language, 1)}%</span><span>{item.dedupLayer}</span></div>
      <div className="card-actions"><ShareBar client={client} labels={labels} occurrenceId={item.occurrenceId} toast={toast} compact /><button className={saved ? "active" : ""} onClick={() => onToggleSaved({ type: "observation", id: item.observationId, data: item })}><Icon name={saved ? "check" : "saved"} />{saved ? labels.saved : labels.save}</button>{item.canonicalStoryId && onOpenStory && <button onClick={() => onOpenStory(item.canonicalStoryId)}>{labels.openStory}<Icon name="chevron" /></button>}</div>
    </footer>
  </article>;
}

function LivePage({ client, live, categories, streams, language, labels, density, setDensity, savedMap, onToggleSaved, toast, navigate }) {
  const [filters, setFilters] = useState({ channel: "", category: "", lang: "", breaking: false });
  const visible = useMemo(() => live.items.filter((item) =>
    (!filters.channel || item.channelName === filters.channel) &&
    (!filters.category || item.categoryIds.includes(filters.category)) &&
    (!filters.lang || item.originalLanguage === filters.lang) &&
    (!filters.breaking || ["high", "critical"].includes(item.urgency))
  ), [live.items, filters]);
  return <section>
    <PageHeader title={labels.liveTitle} subtitle={labels.liveSub}>
      <div className={`connection ${live.status}`}><i />{labels[live.manualPaused ? "paused" : live.status] || live.status}<span>{labels.window}</span></div>
    </PageHeader>
    <div className="toolbar live-toolbar">
      <label><span>{labels.allChannels}</span><select value={filters.channel} onChange={(event) => setFilters({ ...filters, channel: event.target.value })}><option value="">{labels.allChannels}</option>{streams.map((stream) => <option key={stream.id} value={stream.channel_name}>{stream.channel_name}</option>)}</select></label>
      <label><span>{labels.allCategories}</span><select value={filters.category} onChange={(event) => setFilters({ ...filters, category: event.target.value })}><option value="">{labels.allCategories}</option>{[...categories.values()].map((category) => <option key={category.id} value={category.id}>{categoryName(category, language)}</option>)}</select></label>
      <label><span>{labels.allLanguages}</span><select value={filters.lang} onChange={(event) => setFilters({ ...filters, lang: event.target.value })}><option value="">{labels.allLanguages}</option><option value="en">{labels.english}</option><option value="ur">{labels.urdu}</option><option value="mixed">{labels.mixed}</option></select></label>
      <label className="check-control"><input type="checkbox" checked={filters.breaking} onChange={(event) => setFilters({ ...filters, breaking: event.target.checked })} /><span />{labels.breakingOnly}</label>
      <div className="density-control"><button className={density === "compact" ? "active" : ""} onClick={() => setDensity("compact")}>{labels.compact}</button><button className={density === "comfortable" ? "active" : ""} onClick={() => setDensity("comfortable")}>{labels.comfortable}</button></div>
      <button className="primary-control" onClick={() => setTimeout(() => live.setManualPaused(!live.manualPaused), 0)}><Icon name={live.manualPaused ? "play" : "pause"} />{live.manualPaused ? labels.resume : labels.pause}</button>
    </div>
    {live.bufferCount > 0 && <button className="new-items-pill" onClick={live.flush}><span>{formatNumber(live.bufferCount, language)}</span>{labels.newItems} · {labels.showNew}</button>}
    {live.error && <ErrorNotice error={live.error} labels={labels} onRetry={() => live.synchronize()} />}
    <div className="feed-context"><span><Icon name="live" />{formatNumber(visible.length, language)} {labels.observations}</span><span>{labels.autoPause}</span></div>
    <div className="live-list" onMouseEnter={() => live.setHoverPaused(true)} onMouseLeave={() => live.setHoverPaused(false)}>
      {visible.length ? visible.map((item) => <LiveCard key={item.key} item={item} categories={categories} language={language} labels={labels} density={density} saved={savedMap.has(`observation:${item.observationId}`)} onToggleSaved={onToggleSaved} client={client} toast={toast} onOpenStory={(id) => navigate("stories", { story: id })} />) : live.error ? null : <EmptyState>{labels.noLive}</EmptyState>}
    </div>
    {live.hasMore && <button className="load-more" onClick={live.loadOlder}>{labels.loadOlder}</button>}
  </section>;
}

function StoryCard({ story, categories, language, labels, saved, onToggleSaved, selected = false, onOpen, client, toast }) {
  const opposite = oppositeStoryText(story);
  return <article className={`story-card ${selected ? "selected" : ""}`} onClick={() => onOpen?.(story)}>
    <header><div className="card-badges">{story.categoryIds.slice(0, 2).map((id) => <CategoryBadge key={id} id={id} categories={categories} language={language} />)}</div><button className={saved ? "icon-button active" : "icon-button"} onClick={(event) => { event.stopPropagation(); onToggleSaved({ type: "story", id: story.id, data: story }); }}><Icon name={saved ? "check" : "saved"} /></button></header>
    <p className="story-original" dir={textDirection(story.originalLanguage)}>{story.originalText}</p>
    {opposite && <p className="story-translation" dir={textDirection(story.originalLanguage === "ur" ? "en" : "ur")}>{opposite}</p>}
    <footer><div className="story-meta"><span><b>{formatNumber(story.occurrenceCount, language)}</b> {story.occurrenceCount === 1 ? labels.appearance : labels.appearances}</span><span><b>{formatNumber(story.channelNames.length, language)}</b> {labels.channels}</span><time>{formatTime(story.lastSeenAt, language)} · {labels.pkt}</time></div><ShareBar client={client} labels={labels} sentenceId={story.id} toast={toast} compact /></footer>
  </article>;
}

function StoryDetail({ storyId, client, categories, language, labels, onClose, toast }) {
  const [detail, setDetail] = useState(null); const [error, setError] = useState(null);
  useEffect(() => { const controller = new AbortController(); client.sentence(storyId, controller.signal).then(setDetail).catch(setError); return () => controller.abort(); }, [client, storyId]);
  return <aside className="detail-drawer"><header><button onClick={onClose}><Icon name="close" /></button><div><small>{labels.details}</small><h2>{detail ? categoryName(categories.get(detail.categories?.[0]?.id), language) : labels.loading}</h2></div></header>{error ? <ErrorNotice error={error} labels={labels} /> : !detail ? <LoadingRows count={4} /> : <div className="detail-body"><p className="detail-original" dir={textDirection(detail.original_language)}>{detail.original_text}</p>{detail.original_language === "ur" && detail.english_text && <p dir="ltr" className="detail-translation">{detail.english_text}</p>}{detail.original_language === "en" && detail.urdu_text && <p dir="rtl" className="detail-translation">{detail.urdu_text}</p>}<dl><div><dt>{labels.firstSeen}</dt><dd>{formatDate(detail.first_seen_at, language)} · {formatTime(detail.first_seen_at, language)}</dd></div><div><dt>{labels.lastUpdated}</dt><dd>{formatDate(detail.last_seen_at, language)} · {formatTime(detail.last_seen_at, language)}</dd></div><div><dt>{labels.appearances}</dt><dd>{formatNumber(detail.occurrence_count, language)}</dd></div><div><dt>{labels.source}</dt><dd>{detail.sources?.map((source) => source.channel_name).join(" · ") || "—"}</dd></div></dl><div className="keyword-list">{detail.keywords?.map((keyword) => <span key={keyword.id}>{keyword.term}</span>)}</div><ShareBar client={client} labels={labels} sentenceId={storyId} toast={toast} /></div>}</aside>;
}

function StoriesPage({ client, categories, language, labels, savedMap, onToggleSaved, routeState, clearRouteState, toast }) {
  const [items, setItems] = useState([]); const [category, setCategory] = useState(""); const [date, setDate] = useState(pakistanDate()); const [cursor, setCursor] = useState(null); const [hasMore, setHasMore] = useState(false); const [loading, setLoading] = useState(true); const [error, setError] = useState(null); const [selected, setSelected] = useState(routeState?.story || null);
  const load = useCallback(async ({ append = false, next = null } = {}) => { setLoading(true); try { const response = await client.stories({ category, date, limit: 30, cursor: next }); const normalized = response.items.map(normalizeStory); setItems((current) => append ? [...current, ...normalized] : normalized); setCursor(response.page.next_cursor); setHasMore(response.page.has_more); setError(null); } catch (caught) { setError(caught); } finally { setLoading(false); } }, [client, category, date]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { const handler = () => load(); window.addEventListener("newsintel:data-changed", handler); return () => window.removeEventListener("newsintel:data-changed", handler); }, [load]);
  useEffect(() => { if (routeState?.story) setSelected(routeState.story); }, [routeState]);
  useEffect(() => () => clearRouteState?.(), [clearRouteState]);
  return <section><PageHeader title={labels.storiesTitle} subtitle={labels.storiesSub}><div className="header-filters"><select value={category} onChange={(event) => setCategory(event.target.value)}><option value="">{labels.allCategories}</option>{[...categories.values()].map((item) => <option key={item.id} value={item.id}>{categoryName(item, language)}</option>)}</select><input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></div></PageHeader>{error && <ErrorNotice error={error} labels={labels} onRetry={() => load()} />}{loading && !items.length ? <LoadingRows count={5} /> : items.length ? <div className="story-grid">{items.map((story) => <StoryCard key={story.id} story={story} categories={categories} language={language} labels={labels} saved={savedMap.has(`story:${story.id}`)} onToggleSaved={onToggleSaved} client={client} toast={toast} selected={selected === story.id} onOpen={(item) => setSelected(item.id)} />)}</div> : !error && <EmptyState>{labels.noStories}</EmptyState>}{hasMore && <button className="load-more" onClick={() => load({ append: true, next: cursor })}>{labels.loadOlder}</button>}{selected && <StoryDetail storyId={selected} client={client} categories={categories} language={language} labels={labels} toast={toast} onClose={() => setSelected(null)} />}</section>;
}

function SummaryPanel({ summary, language, labels, client, categoryId, date, toast }) {
  if (!summary?.facts?.length) return <EmptyState>{labels.noFacts}</EmptyState>;
  return <article className="summary-panel"><header><div><small>{labels.summary}</small><h2>{categoryName(summary.category, language)}</h2></div><div className="summary-actions"><div className="summary-meta"><strong>{formatNumber(summary.facts.length, language)}</strong><span>{labels.facts}</span></div><ShareBar client={client} labels={labels} categoryId={categoryId} date={date} toast={toast} /></div></header><ol>{summary.facts.map((fact) => <li key={fact.id}><span>{formatNumber(fact.order, language)}</span><div><p dir={language === "ur" ? "rtl" : "ltr"}>{language === "ur" ? fact.text_ur : fact.text_en}</p><time>{formatTime(fact.source_last_seen_at, language)} · {labels.pkt}</time></div></li>)}</ol><footer>{labels.lastUpdated}: {summary.last_updated_at ? `${formatDate(summary.last_updated_at, language)} · ${formatTime(summary.last_updated_at, language)}` : "—"}</footer></article>;
}

function CategoryDesk({ client, categories, categoryId, date, language, labels, onBack, savedMap, onToggleSaved, toast, embedded = false }) {
  const [summary, setSummary] = useState(null); const [stories, setStories] = useState([]); const [error, setError] = useState(null); const [loading, setLoading] = useState(true);
  const load = useCallback(async () => { setLoading(true); setError(null); setSummary(null); setStories([]); const [summaryResult, storyResult] = await Promise.allSettled([client.summary(categoryId, date), client.categoryStories(categoryId, { date, limit: 50 })]); if (summaryResult.status === "fulfilled") setSummary(summaryResult.value); else if (summaryResult.reason?.status !== 404) setError(summaryResult.reason); if (storyResult.status === "fulfilled") setStories(storyResult.value.items.map(normalizeStory)); else setError(storyResult.reason); setLoading(false); }, [client, categoryId, date]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { const handler = () => load(); window.addEventListener("newsintel:data-changed", handler); return () => window.removeEventListener("newsintel:data-changed", handler); }, [load]);
  const category = categories.get(categoryId);
  return <section>{onBack && <button className="back-button" onClick={onBack}><Icon name="chevron" />{labels.back}</button>}{!embedded && <PageHeader title={categoryName(category, language)} subtitle={`${formatDate(`${date}T12:00:00Z`, language)} · ${labels.occurrencesPreserved}`} />}{embedded && <div className="embedded-desk-title"><div><small>{labels.summary}</small><h2>{categoryName(category, language)}</h2></div><span>{formatDate(`${date}T12:00:00Z`, language)}</span></div>}{error && <ErrorNotice error={error} labels={labels} onRetry={load} />}{loading ? <LoadingRows count={5} /> : <div className="desk-layout"><SummaryPanel summary={summary} language={language} labels={labels} client={client} categoryId={categoryId} date={date} toast={toast} /><div><div className="section-label"><span>{labels.underlying}</span><b>{formatNumber(stories.length, language)}</b></div>{stories.length ? <div className="story-stack">{stories.map((story) => <StoryCard key={story.id} story={story} categories={categories} language={language} labels={labels} saved={savedMap.has(`story:${story.id}`)} onToggleSaved={onToggleSaved} toast={toast}  client={client} />)}</div> : <EmptyState>{labels.noStories}</EmptyState>}</div></div>}</section>;
}

function CategoriesPage({ client, categoriesResponse, categories, language, labels, savedMap, onToggleSaved, toast }) {
  const [selected, setSelected] = useState(null);
  if (selected) return <CategoryDesk client={client} categories={categories} categoryId={selected} date={categoriesResponse?.pakistan_date || pakistanDate()} language={language} labels={labels} onBack={() => setSelected(null)} savedMap={savedMap} onToggleSaved={onToggleSaved} toast={toast} />;
  return <section><PageHeader title={labels.categoriesTitle} subtitle={labels.categoriesSub} /><div className="category-grid">{[...categories.values()].map((category) => <button className="category-tile" key={category.id} onClick={() => setSelected(category.id)} style={{ "--category": category.color }}><span className="category-icon">{categoryName(category, language).slice(0, 1)}</span><div><h2>{categoryName(category, language)}</h2><p>{formatNumber(category.today_story_count, language)} {labels.uniqueStories}</p></div><div className="category-observations"><b>{formatNumber(category.today_observation_count, language)}</b><small>{labels.observations}</small></div><Icon name="chevron" /></button>)}</div></section>;
}

function ArchivePage({ client, categories, language, labels, savedMap, onToggleSaved, toast }) {
  const [category, setCategory] = useState(""); const [dates, setDates] = useState([]); const [date, setDate] = useState("");
  useEffect(() => { if (!category && categories.size) setCategory(categories.keys().next().value); }, [category, categories]);
  useEffect(() => { if (!category) return; const controller = new AbortController(); client.historyDates(category, controller.signal).then((response) => { setDates(response.dates); setDate((current) => response.dates.includes(current) ? current : response.dates[0] || ""); }).catch(() => { setDates([]); setDate(""); }); return () => controller.abort(); }, [client, category]);
  return <section><PageHeader title={labels.archiveTitle} subtitle={labels.archiveSub}><div className="header-filters"><select value={category} onChange={(event) => setCategory(event.target.value)}>{[...categories.values()].map((item) => <option key={item.id} value={item.id}>{categoryName(item, language)}</option>)}</select><select value={date} onChange={(event) => setDate(event.target.value)} disabled={!dates.length}>{dates.length ? dates.map((item) => <option key={item} value={item}>{formatDate(`${item}T12:00:00Z`, language)}</option>) : <option>{labels.noDates}</option>}</select></div></PageHeader>{category && date ? <CategoryDesk client={client} categories={categories} categoryId={category} date={date} language={language} labels={labels} onBack={null} embedded savedMap={savedMap} onToggleSaved={onToggleSaved} toast={toast} /> : <EmptyState>{labels.noDates}</EmptyState>}</section>;
}

function SearchPage({ client, categories, language, labels, savedMap, onToggleSaved, toast }) {
  const [query, setQuery] = useState(""); const [submitted, setSubmitted] = useState(""); const [category, setCategory] = useState(""); const [lang, setLang] = useState(""); const [dateFrom, setDateFrom] = useState(""); const [dateTo, setDateTo] = useState(""); const [items, setItems] = useState([]); const [loading, setLoading] = useState(false); const [error, setError] = useState(null); const [cursor, setCursor] = useState(null); const [hasMore, setHasMore] = useState(false);
  const execute = useCallback(async ({ next = null, append = false } = {}) => { if (submitted.trim().length < 2) return; setLoading(true); try { const response = await client.search({ q: submitted.trim(), lang, category, date_from: dateFrom, date_to: dateTo, limit: 30, cursor: next }); const normalized = response.items.map(normalizeStory); setItems((current) => append ? [...current, ...normalized] : normalized); setCursor(response.page.next_cursor); setHasMore(response.page.has_more); setError(null); } catch (caught) { setError(caught); } finally { setLoading(false); } }, [client, submitted, lang, category, dateFrom, dateTo]);
  useEffect(() => { if (submitted) execute(); }, [execute, submitted]);
  const submit = (event) => { event.preventDefault(); setSubmitted(query.trim()); };
  return <section><PageHeader title={labels.searchTitle} subtitle={labels.searchSub} /><form className="search-form" onSubmit={submit}><div className="search-input"><Icon name="search" /><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder={labels.searchPlaceholder} dir="auto" /><button type="button" onClick={() => { setQuery(""); setSubmitted(""); setItems([]); }}><Icon name="close" /></button></div><button className="primary-control" disabled={query.trim().length < 2}>{labels.searchButton}</button></form><div className="search-filters"><select value={category} onChange={(event) => setCategory(event.target.value)}><option value="">{labels.allCategories}</option>{[...categories.values()].map((item) => <option key={item.id} value={item.id}>{categoryName(item, language)}</option>)}</select><select value={lang} onChange={(event) => setLang(event.target.value)}><option value="">{labels.allLanguages}</option><option value="en">{labels.english}</option><option value="ur">{labels.urdu}</option><option value="mixed">{labels.mixed}</option></select><input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} /><input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} /></div>{error && <ErrorNotice error={error} labels={labels} onRetry={() => execute()} />}{loading && !items.length ? <LoadingRows count={5} /> : !submitted ? <EmptyState>{labels.searchHint}</EmptyState> : items.length ? <div className="story-grid">{items.map((story) => <StoryCard key={story.id} story={story} categories={categories} language={language} labels={labels} saved={savedMap.has(`story:${story.id}`)} onToggleSaved={onToggleSaved} toast={toast}  client={client} />)}</div> : !loading && <EmptyState>{labels.noSearch}</EmptyState>}{hasMore && <button className="load-more" onClick={() => execute({ next: cursor, append: true })}>{labels.loadOlder}</button>}</section>;
}

function SavedPage({ client, savedMap, categories, language, labels, onToggleSaved, toast }) {
  const entries = [...savedMap.values()];
  return <section><PageHeader title={labels.savedTitle} subtitle={labels.savedSub} />{entries.length ? <div className="saved-list">{entries.map((entry) => entry.type === "story" ? <StoryCard key={`story:${entry.id}`} story={entry.data} categories={categories} language={language} labels={labels} saved onToggleSaved={onToggleSaved} client={client} toast={toast} /> : <LiveCard key={`observation:${entry.id}`} item={entry.data} categories={categories} language={language} labels={labels} density="compact" saved onToggleSaved={onToggleSaved} toast={toast}  client={client} />)}</div> : <EmptyState>{labels.noSaved}</EmptyState>}</section>;
}

function StatusPage({ client, streams, stats, categories, language, labels, refreshBase }) {
  return <section><PageHeader title={labels.statusTitle} subtitle={labels.statusSub}><button className="secondary-control" onClick={refreshBase}><Icon name="refresh" />{labels.refreshed}</button></PageHeader><div className="metric-grid"><Metric label={labels.activeStreams} value={formatNumber(stats?.active_streams, language)} hint={`${formatNumber(streams.length, language)} ${labels.streams.toLowerCase()}`} /><Metric label={labels.uniqueStories} value={formatNumber(stats?.unique_stories, language)} tone="pink" /><Metric label={labels.observations} value={formatNumber(stats?.observations, language)} tone="blue" /><Metric label={labels.avgConfidence} value={stats?.average_ocr_confidence == null ? "—" : `${formatNumber(stats.average_ocr_confidence * 100, language, 1)}%`} tone="mint" /><Metric label={labels.latency} value={stats?.average_end_to_end_latency_ms == null ? "—" : `${formatNumber(stats.average_end_to_end_latency_ms, language)} ms`} tone="amber" /></div><div className="status-grid"><section className="status-panel"><header><h2>{labels.streamHealth}</h2><span>{formatNumber(streams.length, language)}</span></header>{streams.length ? streams.map((stream) => <article className="stream-row" key={stream.id}><span className={`stream-dot ${stream.status}`} /><div><strong>{stream.channel_name}</strong><small>{stream.status} · {formatNumber(stream.frame_rate_fps, language, 1)} {labels.fps}</small></div><div><b>{stream.last_seen_at ? relativeTime(stream.last_seen_at, language) : "—"}</b><small>{formatNumber(stream.reconnect_count_today, language)} {labels.reconnects}</small></div></article>) : <EmptyState>{labels.unavailable}</EmptyState>}</section><section className="status-panel"><header><h2>{labels.processingHealth}</h2><span>{labels.healthy}</span></header><div className="category-bars">{stats?.per_category?.slice(0, 10).map((row) => { const category = row.category_id; const max = Math.max(...stats.per_category.map((item) => item.observations), 1); return <div key={category}><label><span>{categoryName(categories.get(category), language) || category}</span><b>{formatNumber(row.observations, language)}</b></label><i><span style={{ width: `${Math.max(4, row.observations / max * 100)}%` }} /></i></div>; })}</div></section></div></section>;
}

function AdminPage({ client, language, labels, adminToken, setAdminToken, streams, categories, refreshBase, toast }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState("streams");
  const [keywords, setKeywords] = useState([]);
  const [keywordCategory, setKeywordCategory] = useState("");
  const [streamForm, setStreamForm] = useState({ channel_name: "", youtube_url: "", frame_rate_fps: 0.5 });
  const [keywordForm, setKeywordForm] = useState({ category_id: "", term: "", language: "en", priority: "normal", requires_context: false, context_terms: "", excluded_terms: "" });
  const [categoryForm, setCategoryForm] = useState({ id: "", label_en: "", label_ur: "", color: "#9b7bd8", priority: 50 });
  const [testText, setTestText] = useState("");
  const [testLanguage, setTestLanguage] = useState("mixed");
  const [testResult, setTestResult] = useState(null);

  const login = async (event) => {
    event.preventDefault(); setLoading(true);
    try { const response = await client.adminToken(password); setAdminToken(response.token); setPassword(""); setError(null); }
    catch (caught) { setError(caught); }
    finally { setLoading(false); }
  };
  const loadKeywords = useCallback(async () => {
    if (!adminToken) return;
    setLoading(true);
    try {
      const collected = []; let cursor = null; let response;
      do { response = await client.adminKeywords({ category: keywordCategory || undefined, limit: 100, cursor }); collected.push(...response.items); cursor = response.page?.next_cursor || null; } while (response.page?.has_more && cursor);
      setKeywords(collected); setError(null);
    } catch (caught) { setError(caught); if (caught.status === 401) setAdminToken(null); }
    finally { setLoading(false); }
  }, [adminToken, client, keywordCategory, setAdminToken]);
  useEffect(() => { if (adminToken && tab === "taxonomy") loadKeywords(); }, [adminToken, tab, loadKeywords]);
  useEffect(() => { if (!keywordForm.category_id && categories.size) setKeywordForm((current) => ({ ...current, category_id: categories.keys().next().value })); }, [categories, keywordForm.category_id]);

  const runAction = async (action, after) => {
    setLoading(true); setError(null);
    try { await action(); await after?.(); toast(labels.actionComplete); }
    catch (caught) { setError(caught); if (caught.status === 401) setAdminToken(null); }
    finally { setLoading(false); }
  };
  const commaList = (value) => value.split(",").map((item) => item.trim()).filter(Boolean);
  const addStream = (event) => { event.preventDefault(); runAction(() => client.createStream({ ...streamForm, frame_rate_fps: Number(streamForm.frame_rate_fps) }), async () => { setStreamForm({ channel_name: "", youtube_url: "", frame_rate_fps: 0.5 }); await refreshBase(); }); };
  const disableStream = (id) => { if (window.confirm(labels.confirmDisable)) runAction(() => client.deactivateStream(id), refreshBase); };
  const addKeyword = (event) => { event.preventDefault(); runAction(() => client.createKeyword({ ...keywordForm, context_terms: commaList(keywordForm.context_terms), excluded_terms: commaList(keywordForm.excluded_terms) }), async () => { setKeywordForm((current) => ({ ...current, term: "", context_terms: "", excluded_terms: "" })); await loadKeywords(); }); };
  const saveKeyword = (keyword) => runAction(() => client.updateKeyword(keyword.id, { term: keyword.term, is_active: keyword.is_active, priority: keyword.priority, requires_context: keyword.requires_context, context_terms: keyword.context_terms || [], excluded_terms: keyword.excluded_terms || [] }), loadKeywords);
  const disableKeyword = (id) => { if (window.confirm(labels.confirmDisable)) runAction(() => client.deactivateKeyword(id), loadKeywords); };
  const addCategory = (event) => { event.preventDefault(); runAction(() => client.createCategory({ ...categoryForm, priority: Number(categoryForm.priority) }), async () => { setCategoryForm({ id: "", label_en: "", label_ur: "", color: "#9b7bd8", priority: 50 }); await refreshBase(); }); };
  const runTest = (event) => { event.preventDefault(); runAction(async () => setTestResult(await client.testKeywordMatch({ text: testText, language: testLanguage }))); };

  if (!adminToken) return <section><PageHeader title={labels.adminTitle} subtitle={labels.adminSub} /><form className="admin-login" onSubmit={login}><div className="admin-symbol"><Icon name="admin" size={24} /></div><label>{labels.password}<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>{error && <span className="form-error">{error.status === 401 ? labels.invalidLogin : error.message}</span>}<button className="primary-control" disabled={loading || !password}>{loading ? labels.loading : labels.signIn}</button></form></section>;

  const acceptedDecisions = testResult?.data?.category_decisions?.filter((item) => item.accepted) || [];
  const hits = testResult?.data?.keyword_hits || [];
  return <section>
    <PageHeader title={labels.adminTitle} subtitle={labels.adminSub}><button className="secondary-control" onClick={() => setAdminToken(null)}>{labels.signOut}</button></PageHeader>
    <div className="notice info"><Icon name="check" /><div><strong>{labels.adminReady}</strong><span>{labels.exactPolicy}</span></div></div>
    <div className="admin-tabs"><button className={tab === "streams" ? "active" : ""} onClick={() => setTab("streams")}>{labels.streams}</button><button className={tab === "taxonomy" ? "active" : ""} onClick={() => setTab("taxonomy")}>{labels.taxonomy}</button><button className={tab === "test" ? "active" : ""} onClick={() => setTab("test")}>{labels.testLab}</button></div>
    {error && <ErrorNotice error={error} labels={labels} />}
    {tab === "streams" && <div className="admin-layout">
      <form className="admin-form" onSubmit={addStream}><header><h2>{labels.addStream}</h2><span>{formatNumber(streams.length, language)}</span></header><label>{labels.channelName}<input required value={streamForm.channel_name} onChange={(event) => setStreamForm({ ...streamForm, channel_name: event.target.value })} /></label><label>{labels.youtubeUrl}<input required type="url" value={streamForm.youtube_url} onChange={(event) => setStreamForm({ ...streamForm, youtube_url: event.target.value })} /></label><label>{labels.captureRate}<input required type="number" min="0.1" max="10" step="0.1" value={streamForm.frame_rate_fps} onChange={(event) => setStreamForm({ ...streamForm, frame_rate_fps: event.target.value })} /></label><button className="primary-control" disabled={loading}><Icon name="plus" />{labels.addStream}</button></form>
      <section className="admin-panel"><header><h2>{labels.streams}</h2><span>{formatNumber(streams.length, language)}</span></header><div className="admin-rows">{streams.map((stream) => <article key={stream.id}><span className={`stream-dot ${stream.status}`} /><div><strong>{stream.channel_name}</strong><small>{stream.youtube_url}</small></div><div className="row-meta"><span>{formatNumber(stream.frame_rate_fps, language, 1)} FPS</span><span>{stream.is_active ? labels.active : labels.inactive}</span></div><button className="danger-control" disabled={!stream.is_active || loading} onClick={() => disableStream(stream.id)}>{labels.disable}</button></article>)}</div></section>
    </div>}
    {tab === "taxonomy" && <div className="taxonomy-workspace">
      <div className="admin-form-grid">
        <form className="admin-form" onSubmit={addKeyword}><header><h2>{labels.addKeyword}</h2></header><label>{labels.selectCategory}<select required value={keywordForm.category_id} onChange={(event) => setKeywordForm({ ...keywordForm, category_id: event.target.value })}>{[...categories.values()].map((category) => <option key={category.id} value={category.id}>{categoryName(category, language)}</option>)}</select></label><label>{labels.keywordTerm}<input required dir="auto" value={keywordForm.term} onChange={(event) => setKeywordForm({ ...keywordForm, term: event.target.value })} /></label><div className="form-pair"><label>{labels.sourceLanguage}<select value={keywordForm.language} onChange={(event) => setKeywordForm({ ...keywordForm, language: event.target.value })}><option value="en">{labels.english}</option><option value="ur">{labels.urdu}</option></select></label><label>{labels.priority}<select value={keywordForm.priority} onChange={(event) => setKeywordForm({ ...keywordForm, priority: event.target.value })}><option value="normal">{labels.normal}</option><option value="high">{labels.high}</option><option value="critical">{labels.critical}</option></select></label></div><label className="inline-check"><input type="checkbox" checked={keywordForm.requires_context} onChange={(event) => setKeywordForm({ ...keywordForm, requires_context: event.target.checked })} />{labels.requiresContext}</label><label>{labels.contextTerms}<input dir="auto" value={keywordForm.context_terms} onChange={(event) => setKeywordForm({ ...keywordForm, context_terms: event.target.value })} /></label><label>{labels.excludedTerms}<input dir="auto" value={keywordForm.excluded_terms} onChange={(event) => setKeywordForm({ ...keywordForm, excluded_terms: event.target.value })} /></label><button className="primary-control" disabled={loading}><Icon name="plus" />{labels.addKeyword}</button></form>
        <form className="admin-form" onSubmit={addCategory}><header><h2>{labels.addCategory}</h2></header><label>{labels.categoryId}<input required pattern="[a-z0-9][a-z0-9_-]*" value={categoryForm.id} onChange={(event) => setCategoryForm({ ...categoryForm, id: event.target.value.toLowerCase() })} /></label><label>{labels.categoryEnglish}<input required value={categoryForm.label_en} onChange={(event) => setCategoryForm({ ...categoryForm, label_en: event.target.value })} /></label><label>{labels.categoryUrdu}<input required dir="rtl" value={categoryForm.label_ur} onChange={(event) => setCategoryForm({ ...categoryForm, label_ur: event.target.value })} /></label><div className="form-pair"><label>{labels.categoryColor}<input type="color" value={categoryForm.color} onChange={(event) => setCategoryForm({ ...categoryForm, color: event.target.value })} /></label><label>{labels.priority}<input type="number" min="0" max="100" value={categoryForm.priority} onChange={(event) => setCategoryForm({ ...categoryForm, priority: event.target.value })} /></label></div><button className="primary-control" disabled={loading}><Icon name="plus" />{labels.addCategory}</button></form>
      </div>
      <section className="admin-panel keyword-manager"><header><h2>{labels.keywords}</h2><div><select value={keywordCategory} onChange={(event) => setKeywordCategory(event.target.value)}><option value="">{labels.allCategories}</option>{[...categories.values()].map((category) => <option key={category.id} value={category.id}>{categoryName(category, language)}</option>)}</select><button className="secondary-control" onClick={loadKeywords}><Icon name="refresh" />{labels.refreshed}</button></div></header>{loading && !keywords.length ? <LoadingRows count={5} /> : <div className="keyword-table">{keywords.map((keyword, index) => <article key={keyword.id}><CategoryBadge id={keyword.category_id} categories={categories} language={language} /><input dir={keyword.language === "ur" ? "rtl" : "ltr"} value={keyword.term} onChange={(event) => setKeywords((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, term: event.target.value } : item))} /><select value={keyword.priority} onChange={(event) => setKeywords((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, priority: event.target.value } : item))}><option value="normal">{labels.normal}</option><option value="high">{labels.high}</option><option value="critical">{labels.critical}</option></select><label className="switch"><input type="checkbox" checked={keyword.is_active} onChange={(event) => setKeywords((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, is_active: event.target.checked } : item))} /><span /></label><button onClick={() => saveKeyword(keyword)}>{labels.apply}</button><button className="danger-control" onClick={() => disableKeyword(keyword.id)}>{labels.delete}</button></article>)}</div>}</section>
    </div>}
    {tab === "test" && <div className="test-lab"><form className="admin-form" onSubmit={runTest}><header><h2>{labels.keywordTest}</h2></header><label>{labels.sourceLanguage}<select value={testLanguage} onChange={(event) => setTestLanguage(event.target.value)}><option value="mixed">{labels.mixed}</option><option value="en">{labels.english}</option><option value="ur">{labels.urdu}</option></select></label><label>{labels.testText}<textarea required dir="auto" rows="7" value={testText} onChange={(event) => setTestText(event.target.value)} /></label><button className="primary-control" disabled={loading || !testText.trim()}>{labels.testRun}</button></form><section className="admin-panel test-results"><header><h2>{labels.details}</h2><span>{hits.length}</span></header>{testResult ? <><div className="keyword-list">{hits.map((hit) => <span key={`${hit.keyword_id}-${hit.original_span?.join("-")}`}>{hit.term || hit.matched_text}</span>)}</div><div className="decision-list">{acceptedDecisions.map((decision) => <article key={decision.category_id}><i style={{ background: decision.color }} /><div><strong>{language === "ur" ? decision.label_ur : decision.label_en}</strong><small>{decision.reason}</small></div><b>{formatNumber(decision.score * 100, language, 0)}%</b></article>)}</div>{!hits.length && <EmptyState>{labels.noMatches}</EmptyState>}</> : <EmptyState>{labels.testText}</EmptyState>}</section></div>}
  </section>;
}

function App() {
  const [language, setLanguage] = useStoredState("newsintel-language", "en");
  const [theme, setTheme] = useStoredState("newsintel-theme", "light");
  const [density, setDensity] = useStoredState("newsintel-density", "compact");
  const [saved, setSaved] = useStoredState("newsintel-saved", []);
  const [adminToken, setAdminToken] = useStoredState("newsintel-admin-token", null, "session");
  const [route, setRoute] = useState(() => { const value = window.location.hash.replace(/^#\/?/, "").split("/")[0]; return NAV.includes(value) ? value : "live"; });
  const [routeState, setRouteState] = useState(null);
  const [categoriesResponse, setCategoriesResponse] = useState(null); const [streams, setStreams] = useState([]); const [stats, setStats] = useState(null); const [baseError, setBaseError] = useState(null); const [toastMessage, setToastMessage] = useState(""); const [clock, setClock] = useState(new Date());
  const client = useMemo(() => new ApiClient({ getAdminToken: () => adminToken }), [adminToken]);
  const live = useLiveFeed(client);
  const labels = TEXT[language]; const rtl = language === "ur";
  const categories = useMemo(() => new Map((categoriesResponse?.items || []).map((item) => [item.id, item])), [categoriesResponse]);
  const savedMap = useMemo(() => new Map(saved.map((entry) => [`${entry.type}:${entry.id}`, entry])), [saved]);

  const refreshBase = useCallback(async () => {
    const results = await Promise.allSettled([client.categories(), client.streams(), client.stats()]);
    if (results[0].status === "fulfilled") setCategoriesResponse(results[0].value);
    if (results[1].status === "fulfilled") setStreams(results[1].value.items);
    if (results[2].status === "fulfilled") setStats(results[2].value);
    const rejected = results.find((item) => item.status === "rejected");
    setBaseError(rejected?.reason || null);
  }, [client]);

  useEffect(() => { refreshBase(); const timer = window.setInterval(refreshBase, 60_000); return () => window.clearInterval(timer); }, [refreshBase]);
  useEffect(() => { const timer = window.setInterval(() => setClock(new Date()), 1_000); return () => window.clearInterval(timer); }, []);
  useEffect(() => { document.documentElement.dataset.theme = theme; document.documentElement.lang = language === "ur" ? "ur" : "en"; document.documentElement.dir = rtl ? "rtl" : "ltr"; }, [theme, language, rtl]);
  useEffect(() => { const handler = () => { const value = window.location.hash.replace(/^#\/?/, "").split("/")[0]; setRoute(NAV.includes(value) ? value : "live"); }; window.addEventListener("hashchange", handler); return () => window.removeEventListener("hashchange", handler); }, []);
  useEffect(() => { const keyHandler = (event) => { if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) { event.preventDefault(); navigate("search"); } }; window.addEventListener("keydown", keyHandler); return () => window.removeEventListener("keydown", keyHandler); }, []);
  useEffect(() => { if (!toastMessage) return; const timer = window.setTimeout(() => setToastMessage(""), 2200); return () => window.clearTimeout(timer); }, [toastMessage]);

  const navigate = (next, state = null) => { setRouteState(state); window.location.hash = `/${next}`; setRoute(next); window.scrollTo({ top: 0, behavior: "smooth" }); };
  const toggleSaved = (entry) => { const key = `${entry.type}:${entry.id}`; setSaved((current) => { const exists = current.some((item) => `${item.type}:${item.id}` === key); setToastMessage(exists ? labels.removedDone : labels.savedDone); return exists ? current.filter((item) => `${item.type}:${item.id}` !== key) : [{ ...entry, savedAt: new Date().toISOString() }, ...current].slice(0, 300); }); };

  const icons = { live: "live", stories: "stories", categories: "categories", archive: "archive", search: "search", saved: "saved", status: "status", admin: "admin" };
  return <div className={`app-shell ${rtl ? "rtl-ui" : ""}`}>
    <aside className="sidebar">
      <button className="brand" onClick={() => navigate("live")}><span className="brand-mark">N</span><span><strong>{labels.app}</strong><small>{labels.appSub}</small></span></button>
      <nav>{NAV.map((name) => <button key={name} className={route === name ? "active" : ""} onClick={() => navigate(name)}><Icon name={icons[name]} /><span>{labels.nav[name]}</span>{name === "live" && <i className={`nav-pulse ${live.status}`} />}{name === "saved" && saved.length > 0 && <b>{formatNumber(saved.length, language)}</b>}</button>)}</nav>
      <div className="sidebar-footer"><div className={`system-chip ${live.status}`}><i />{labels[live.status] || live.status}</div><small>Phase 12 · PostgreSQL</small></div>
    </aside>
    <main>
      <header className="topbar"><div className="mobile-brand"><span className="brand-mark">N</span><b>{labels.app}</b></div><div className="topbar-health"><span className={`health-dot ${baseError ? "degraded" : "healthy"}`} />{baseError ? labels.stale : `${formatNumber(stats?.active_streams, language)} ${labels.activeStreams}`}</div><div className="topbar-actions"><button className="language-toggle" onClick={() => setLanguage(language === "en" ? "ur" : "en")}>{labels.language}</button><button className="icon-button" title={theme === "light" ? labels.dark : labels.light} onClick={() => setTheme(theme === "light" ? "dark" : "light")}><Icon name={theme === "light" ? "moon" : "sun"} /></button><div className="top-clock"><strong>{formatTime(clock, language, true)}</strong><span>{labels.pkt}</span></div></div></header>
      {baseError && <div className="global-warning"><Icon name="alert" />{labels.stale}<button onClick={refreshBase}>{labels.retry}</button></div>}
      <div className="page-content" key={`${route}-${language}`}>
        {route === "live" && <LivePage client={client} live={live} categories={categories} streams={streams} language={language} labels={labels} density={density} setDensity={setDensity} savedMap={savedMap} onToggleSaved={toggleSaved} toast={setToastMessage} navigate={navigate} />}
        {route === "stories" && <StoriesPage client={client} categories={categories} language={language} labels={labels} savedMap={savedMap} onToggleSaved={toggleSaved} routeState={routeState} clearRouteState={() => setRouteState(null)} toast={setToastMessage} />}
        {route === "categories" && <CategoriesPage client={client} categoriesResponse={categoriesResponse} categories={categories} language={language} labels={labels} savedMap={savedMap} onToggleSaved={toggleSaved} toast={setToastMessage} />}
        {route === "archive" && <ArchivePage client={client} categories={categories} language={language} labels={labels} savedMap={savedMap} onToggleSaved={toggleSaved} toast={setToastMessage} />}
        {route === "search" && <SearchPage client={client} categories={categories} language={language} labels={labels} savedMap={savedMap} onToggleSaved={toggleSaved} toast={setToastMessage} />}
        {route === "saved" && <SavedPage client={client} savedMap={savedMap} categories={categories} language={language} labels={labels} onToggleSaved={toggleSaved} toast={setToastMessage} />}
        {route === "status" && <StatusPage client={client} streams={streams} stats={stats} categories={categories} language={language} labels={labels} refreshBase={refreshBase} />}
        {route === "admin" && <AdminPage client={client} language={language} labels={labels} adminToken={adminToken} setAdminToken={setAdminToken} streams={streams} categories={categories} refreshBase={refreshBase} toast={setToastMessage} />}
      </div>
    </main>
    {toastMessage && <div className="toast"><Icon name="check" />{toastMessage}</div>}
  </div>;
}

createRoot(document.getElementById("root")).render(<React.StrictMode><App /></React.StrictMode>);
