from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app import app
from newsintel.api import (
    CursorCodec,
    CreateCategoryRequest,
    CreateKeywordRequest,
    CreateStreamRequest,
    OutboxWebSocketPump,
    PostgresApiRepository,
    UpdateKeywordRequest,
    get_repository,
)
from newsintel.persistence import OutboxDelivery

NOW = datetime(2026, 7, 20, 3, 30, tzinfo=timezone.utc)
STREAM_ID = str(uuid.uuid4())
STORY_ID = str(uuid.uuid4())
OCCURRENCE_ID = str(uuid.uuid4())
KEYWORD_ID = str(uuid.uuid4())


class FakeRepository:
    def __init__(self) -> None:
        self.created_stream = None
        self.deactivated = None
        self.deactivated_keyword = None
        self.keyword = {
            "id": KEYWORD_ID,
            "category_id": "politics",
            "term": "government",
            "normalized_term": "government",
            "language": "en",
            "is_active": True,
            "priority": "normal",
            "match_mode": "phrase",
            "requires_context": False,
            "context_terms": [],
            "excluded_terms": [],
            "added_by": "admin-api",
        }

    def list_streams(self):
        return [{
            "id": STREAM_ID, "channel_name": "Geo News", "youtube_url": "https://youtube.com/watch?v=geo",
            "is_active": True, "status": "live", "frame_rate_fps": 2.0,
            "last_seen_at": NOW, "last_frame_at": NOW, "reconnect_count_today": 0,
            "consecutive_failures": 0,
        }]

    def create_stream(self, request: CreateStreamRequest):
        self.created_stream = request
        return {**self.list_streams()[0], "channel_name": request.channel_name, "youtube_url": str(request.youtube_url), "frame_rate_fps": request.frame_rate_fps}

    def deactivate_stream(self, stream_id: str):
        self.deactivated = stream_id

    def list_feed(self, **kwargs):
        items = []
        for suffix, seconds in (("first", 0), ("repeat", -20)):
            items.append({
                "occurrence_id": str(uuid.uuid4()), "observation_id": f"obs-{suffix}",
                "canonical_story_id": STORY_ID, "stream_id": STREAM_ID, "channel_name": "Geo News",
                "observed_at": NOW, "original_text": "حکومت نے اجلاس طلب کیا۔",
                "original_language": "ur", "confidence": 0.97,
                "category_ids": ["politics"], "keyword_ids": [KEYWORD_ID], "urgency": "normal",
                "review_required": False, "dedup_decision": "merged" if suffix == "repeat" else "created",
                "dedup_layer": "exact_hash" if suffix == "repeat" else "new_canonical_story",
                "translation": {"status": "complete", "source_language": "ur", "target_language": "en", "translated_text": "The government called a meeting."},
            })
        return items, "next-token", True

    def get_occurrence(self, occurrence_id: str):
        if occurrence_id != OCCURRENCE_ID:
            return None
        return {
            "calendar_date": date(2026, 7, 20),
            "channel_name": "Geo News",
            "share_text": "[Geo News] [2026-07-20 05:30:00 PM PKT] — حکومت نے اجلاس طلب کیا۔ | English: The government called a meeting. | اردو: حکومت نے اجلاس طلب کیا۔ | Source: Pakistani News Stream Intelligence",
        }

    def get_sentence(self, sentence_id: str):
        if sentence_id != STORY_ID:
            return None
        return {
            "id": STORY_ID, "original_text": "حکومت نے اجلاس طلب کیا۔", "original_language": "ur",
            "english_text": "The government called a meeting.", "urdu_text": "حکومت نے اجلاس طلب کیا۔",
            "translation_status": "complete", "confidence": 0.97,
            "categories": [{"id": "politics", "label_en": "Politics", "label_ur": "سیاست", "color": "#A78BFA"}],
            "keywords": [{"id": KEYWORD_ID, "category_id": "politics", "term": "حکومت", "language": "ur", "priority": "normal", "matched_text": "حکومت"}],
            "sources": [{"stream_id": STREAM_ID, "channel_name": "Geo News", "first_seen_at": NOW, "last_seen_at": NOW, "occurrence_count": 2}],
            "first_seen_at": NOW, "last_seen_at": NOW, "occurrence_count": 2,
            "calendar_date": date(2026, 7, 20), "review_status": "accepted",
            "share_text": "[Geo News] [2026-07-20 08:30:00 AM PKT] — حکومت نے اجلاس طلب کیا۔ | English: The government called a meeting. | اردو: حکومت نے اجلاس طلب کیا۔ | Source: Pakistani News Stream Intelligence",
        }

    def list_stories(self, **kwargs):
        return [{
            "id": STORY_ID, "original_text": "حکومت نے اجلاس طلب کیا۔", "original_language": "ur",
            "english_text": "The government called a meeting.", "urdu_text": "حکومت نے اجلاس طلب کیا۔",
            "first_seen_at": NOW, "last_seen_at": NOW, "occurrence_count": 2, "confidence": 0.97,
            "category_ids": ["politics"], "channel_names": ["Geo News", "ARY News"], "review_status": "accepted",
        }], None, False

    def list_categories(self, pakistan_date: date):
        return [{"id": "politics", "label_en": "Politics", "label_ur": "سیاست", "color": "#A78BFA", "is_active": True, "priority": 80, "today_story_count": 1, "today_observation_count": 2}]

    def get_summary(self, category_id: str, calendar_date: date):
        if category_id == "missing":
            return None
        return {
            "id": str(uuid.uuid4()), "category": {"id": "politics", "label_en": "Politics", "label_ur": "سیاست", "color": "#A78BFA"},
            "calendar_date": calendar_date, "summary_text_en": "The government called a meeting.",
            "summary_text_ur": "حکومت نے اجلاس طلب کیا۔", "source_sentence_count": 1,
            "last_updated_at": NOW, "finalized_at": None,
            "facts": [{"id": str(uuid.uuid4()), "order": 1, "text_en": "The government called a meeting.", "text_ur": "حکومت نے اجلاس طلب کیا۔", "kind": "story", "priority": 50, "source_first_seen_at": NOW, "source_last_seen_at": NOW}],
        }

    def history_dates(self, category_id):
        return [date(2026, 7, 20), date(2026, 7, 19)]

    def search(self, **kwargs):
        return self.list_stories(**kwargs)

    def list_keywords(self, **kwargs):
        return [self.keyword], None, False

    def create_keyword(self, request: CreateKeywordRequest):
        self.keyword = {**self.keyword, **request.model_dump(), "id": KEYWORD_ID, "normalized_term": request.term.lower(), "is_active": True, "match_mode": "phrase", "added_by": "admin-api"}
        return self.keyword

    def update_keyword(self, keyword_id: str, request: UpdateKeywordRequest):
        self.keyword.update(request.model_dump(exclude_unset=True))
        return self.keyword

    def deactivate_keyword(self, keyword_id: str):
        self.deactivated_keyword = keyword_id
        self.keyword["is_active"] = False

    def test_keyword_match(self, request):
        return {
            "data": {
                "keyword_hits": [{"keyword_id": KEYWORD_ID, "term": "government", "matched_text": "government", "original_span": [4, 14]}],
                "category_decisions": [{"category_id": "politics", "label_en": "Politics", "label_ur": "سیاست", "color": "#A78BFA", "score": 1.0, "accepted": True, "reason": "exact keyword evidence"}],
            },
            "diagnostic_source": "postgresql-active-taxonomy",
        }

    def create_category(self, request: CreateCategoryRequest):
        return {"id": request.id, "label_en": request.label_en, "label_ur": request.label_ur, "color": request.color}

    def overview_stats(self, pakistan_date: date):
        return {"pakistan_date": pakistan_date, "unique_stories": 1, "observations": 2, "active_streams": 1, "average_ocr_confidence": 0.97, "average_end_to_end_latency_ms": 850.0, "per_category": [{"category_id": "politics", "observations": 2}], "per_stream": [{"stream_id": STREAM_ID, "channel_name": "Geo News", "observations": 2}]}

    def keyword_frequency(self, **kwargs):
        return [{"date": date(2026, 7, 20), "keyword_id": KEYWORD_ID, "term": "government", "language": "en", "category_id": "politics", "count": 2}]


@pytest.fixture()
def fake_repo(monkeypatch):
    repo = FakeRepository()
    app.dependency_overrides[get_repository] = lambda: repo
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse battery staple")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    yield repo
    app.dependency_overrides.clear()


@pytest.fixture()
def client(fake_repo):
    with TestClient(app) as value:
        yield value


def admin_token(client: TestClient) -> str:
    response = client.post("/api/v1/admin/auth/token", json={"password": "correct horse battery staple"})
    assert response.status_code == 200
    return response.json()["token"]


def test_cursor_is_opaque_round_trip_and_rejects_tampering() -> None:
    codec = CursorCodec("test-secret")
    value = codec.encode(NOW, STORY_ID)
    assert "2026" not in value
    decoded = codec.decode(value)
    assert decoded.timestamp == NOW
    assert decoded.identifier == STORY_ID
    midpoint = len(value) // 2
    tampered = value[:midpoint] + ("A" if value[midpoint] != "A" else "B") + value[midpoint + 1:]
    with pytest.raises(Exception):
        codec.decode(tampered)


def test_feed_preserves_repeated_live_occurrences_and_cursor(client: TestClient) -> None:
    response = client.get("/api/v1/feed?limit=20")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["items"][0]["canonical_story_id"] == body["items"][1]["canonical_story_id"]
    assert body["items"][0]["observation_id"] != body["items"][1]["observation_id"]
    assert body["repeats_are_preserved"] is True
    assert body["window_minutes"] == 30
    assert body["page"]["next_cursor"] == "next-token"


def test_stream_admin_requires_header_and_uses_password_derived_token(client: TestClient, fake_repo: FakeRepository) -> None:
    denied = client.post("/api/v1/admin/streams", json={"channel_name": "ARY News", "youtube_url": "https://youtube.com/watch?v=ary", "frame_rate_fps": 1})
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_admin_token"
    token = admin_token(client)
    created = client.post("/api/v1/admin/streams", headers={"X-Admin-Token": token}, json={"channel_name": "ARY News", "youtube_url": "https://youtube.com/watch?v=ary", "frame_rate_fps": 1})
    assert created.status_code == 201
    assert created.json()["channel_name"] == "ARY News"
    deleted = client.delete(f"/api/v1/admin/streams/{STREAM_ID}", headers={"X-Admin-Token": token})
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert fake_repo.deactivated == STREAM_ID


def test_sentence_category_summary_history_and_share_contracts(client: TestClient) -> None:
    sentence = client.get(f"/api/v1/sentence/{STORY_ID}")
    assert sentence.status_code == 200
    assert sentence.json()["english_text"] == "The government called a meeting."
    category = client.get("/api/v1/category/politics/sentences?date=2026-07-20")
    assert category.status_code == 200
    assert category.json()["items"][0]["occurrence_count"] == 2
    summary = client.get("/api/v1/category/politics/summary?date=2026-07-20")
    assert summary.status_code == 200
    assert len(summary.json()["facts"]) == 1
    dates = client.get("/api/v1/history/dates?category=politics")
    assert dates.json()["dates"] == ["2026-07-20", "2026-07-19"]
    share = client.get(f"/api/v1/share/{STORY_ID}")
    assert share.status_code == 200
    assert "English:" in share.json()["text"]
    assert "اردو:" in share.json()["text"]
    summary_share = client.get("/api/v1/share/summary/politics?date=2026-07-20")
    assert summary_share.status_code == 200
    assert summary_share.json()["whatsapp_url"].startswith("https://wa.me/")


def test_search_statistics_and_categories(client: TestClient) -> None:
    categories = client.get("/api/v1/categories")
    assert categories.status_code == 200
    assert categories.json()["items"][0]["today_observation_count"] == 2
    search = client.get("/api/v1/search?q=government&lang=en")
    assert search.status_code == 200
    assert "no typo correction" in search.json()["matching_policy"]
    overview = client.get("/api/v1/stats/overview")
    assert overview.status_code == 200
    assert overview.json()["observations"] == 2
    frequency = client.get("/api/v1/stats/keyword-frequency?category=politics")
    assert frequency.status_code == 200
    assert frequency.json()["items"][0]["count"] == 2


def test_keyword_and_category_admin_contract(client: TestClient) -> None:
    token = admin_token(client)
    headers = {"X-Admin-Token": token}
    listed = client.get("/api/v1/admin/keywords?category=politics", headers=headers)
    assert listed.status_code == 200
    created = client.post("/api/v1/admin/keywords", headers=headers, json={"category_id": "politics", "term": "cabinet meeting", "language": "en", "priority": "high"})
    assert created.status_code == 201
    assert created.json()["priority"] == "high"
    patched = client.patch(f"/api/v1/admin/keywords/{KEYWORD_ID}", headers=headers, json={"is_active": False})
    assert patched.status_code == 200
    assert patched.json()["is_active"] is False
    category = client.post("/api/v1/admin/categories", headers=headers, json={"id": "science", "label_en": "Science", "label_ur": "سائنس", "color": "#C4B5FD"})
    assert category.status_code == 201
    assert category.json()["id"] == "science"


def test_errors_follow_consistent_shape(client: TestClient) -> None:
    missing = client.get(f"/api/v1/sentence/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "sentence_not_found", "message": "Sentence was not found.", "detail": None}}
    invalid = client.get("/api/v1/feed?limit=1000")
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    date_range = client.get("/api/v1/search?q=x&date_from=2026-07-21&date_to=2026-07-20")
    assert date_range.status_code == 400
    assert date_range.json()["error"]["code"] == "invalid_date_range"


def test_websocket_connection_and_ping(client: TestClient) -> None:
    with client.websocket_connect("/ws/live") as websocket:
        connected = websocket.receive_json()
        assert connected["event"] == "connected"
        assert connected["data"]["window_minutes"] == 30
        snapshot = websocket.receive_json()
        assert snapshot["event"] == "snapshot"
        assert len(snapshot["data"]["items"]) == 2
        websocket.send_text("ping")
        assert websocket.receive_json()["event"] == "pong"


def test_outbox_event_mapping_preserves_spec_event_names() -> None:
    detection = OutboxDelivery(
        event_id=str(uuid.uuid4()), event_key="detection:1", event_type="detection_observed",
        aggregate_type="observation", aggregate_id="obs-1", payload={"observation_id": "obs-1"}, attempts=0,
    )
    translated = OutboxDelivery(
        event_id=str(uuid.uuid4()), event_key="translation:1", event_type="translation_ready",
        aggregate_type="observation", aggregate_id="obs-1", payload={"observation_id": "obs-1"}, attempts=0,
    )
    assert OutboxWebSocketPump.envelope(detection)["event"] == "new_sentence"
    assert OutboxWebSocketPump.envelope(translated)["event"] == "translation_ready"


def test_feed_sql_is_cursor_based_and_never_distinct() -> None:
    codec = CursorCodec("secret")
    cursor = codec.decode(codec.encode(NOW, OCCURRENCE_ID))
    statement = PostgresApiRepository.feed_statement(
        cutoff=NOW, category="politics", language="ur", stream_id=uuid.UUID(STREAM_ID), cursor=cursor, limit=20,
    )
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "sentence_occurrences.observed_at >=" in sql
    assert "'politics' = ANY" in sql
    assert "sentence_occurrences.observed_at, sentence_occurrences.id" in sql
    assert "ORDER BY sentence_occurrences.observed_at DESC" in sql
    assert "DISTINCT" not in sql.upper()
    assert "LIMIT 21" in sql


def test_openapi_exposes_admin_api_key_and_all_required_rest_paths(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    required = {
        "/api/v1/streams", "/api/v1/feed", "/api/v1/sentence/{sentence_id}",
        "/api/v1/category/{category_id}/sentences", "/api/v1/categories",
        "/api/v1/category/{category_id}/summary", "/api/v1/history/dates",
        "/api/v1/search", "/api/v1/admin/keywords", "/api/v1/admin/keywords/{keyword_id}",
        "/api/v1/admin/categories", "/api/v1/stats/overview",
        "/api/v1/stats/keyword-frequency", "/api/v1/share/{sentence_id}",
        "/api/v1/share/summary/{category_id}",
    }
    assert required.issubset(schema["paths"])
    assert schema["components"]["securitySchemes"]["AdminToken"]["name"] == "X-Admin-Token"
