from pathlib import Path

from fastapi.testclient import TestClient

from app import app
from newsintel.api import get_repository
from test_api_phase10 import FakeRepository, OCCURRENCE_ID, KEYWORD_ID


def _client(monkeypatch):
    repo = FakeRepository()
    app.dependency_overrides[get_repository] = lambda: repo
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse battery staple")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    return repo, TestClient(app)


def _token(client: TestClient) -> str:
    response = client.post("/api/v1/admin/auth/token", json={"password": "correct horse battery staple"})
    assert response.status_code == 200
    return response.json()["token"]


def test_exact_live_occurrence_share_uses_server_payload(monkeypatch) -> None:
    repo, client = _client(monkeypatch)
    try:
        response = client.get(f"/api/v1/share/occurrence/{OCCURRENCE_ID}")
        assert response.status_code == 200
        body = response.json()
        assert body["text"].startswith("[Geo News] [2026-07-20 05:30:00 PM PKT]")
        assert "English: The government called a meeting." in body["text"]
        assert "اردو: حکومت نے اجلاس طلب کیا۔" in body["text"]
        assert body["whatsapp_url"].startswith("https://wa.me/")
        assert body["email_url"].startswith("mailto:")
        assert body["web_share"]["text"] == body["text"]
    finally:
        client.close()
        app.dependency_overrides.clear()


def test_keyword_deactivation_and_exact_runtime_test_are_admin_only(monkeypatch) -> None:
    repo, client = _client(monkeypatch)
    try:
        denied = client.post("/api/v1/admin/keywords/test", json={"text": "The government met today.", "language": "en"})
        assert denied.status_code == 401
        token = _token(client)
        headers = {"X-Admin-Token": token}
        tested = client.post("/api/v1/admin/keywords/test", headers=headers, json={"text": "The government met today.", "language": "en"})
        assert tested.status_code == 200
        assert tested.json()["diagnostic_source"] == "postgresql-active-taxonomy"
        assert tested.json()["data"]["keyword_hits"][0]["term"] == "government"
        removed = client.delete(f"/api/v1/admin/keywords/{KEYWORD_ID}", headers=headers)
        assert removed.status_code == 204
        assert removed.content == b""
        assert repo.deactivated_keyword == KEYWORD_ID
    finally:
        client.close()
        app.dependency_overrides.clear()


def test_phase12_openapi_contains_new_sharing_and_admin_routes(monkeypatch) -> None:
    repo, client = _client(monkeypatch)
    try:
        schema = client.get("/openapi.json").json()
        assert "/api/v1/share/occurrence/{occurrence_id}" in schema["paths"]
        assert "delete" in schema["paths"]["/api/v1/admin/keywords/{keyword_id}"]
        assert "/api/v1/admin/keywords/test" in schema["paths"]
        assert schema["paths"]["/api/v1/admin/keywords/test"]["post"]["security"] == [{"AdminToken": []}]
    finally:
        client.close()
        app.dependency_overrides.clear()


def test_frontend_places_runtime_controls_in_admin_and_reuses_sharebar() -> None:
    project_root = Path(__file__).resolve().parents[2]
    source = (project_root / "frontend/src/main.jsx").read_text(encoding="utf-8")
    api = (project_root / "frontend/src/api.js").read_text(encoding="utf-8")
    assert "function ShareBar" in source
    assert "shareOccurrence" in api and "shareSentence" in api and "shareSummary" in api
    assert "function AdminPage" in source
    assert "client.createStream" in source
    assert "client.createKeyword" in source
    assert "client.createCategory" in source
    assert "client.testKeywordMatch" in source
    public_prefix = source.split("function AdminPage", 1)[0]
    assert "client.createStream" not in public_prefix
    assert "client.createKeyword" not in public_prefix
    assert "client.createCategory" not in public_prefix
    assert "gradient" not in (project_root / "frontend/src/styles.css").read_text(encoding="utf-8").lower()
