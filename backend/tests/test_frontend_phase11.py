from pathlib import Path

from app import app

ROOT = Path(__file__).resolve().parents[2]


def test_phase11_exposes_prefixed_and_compatibility_websocket_routes() -> None:
    websocket_paths = {route.path for route in app.routes if getattr(route, "path", "").endswith("/ws/live")}
    assert "/api/v1/ws/live" in websocket_paths
    assert "/ws/live" in websocket_paths


def test_frontend_uses_real_rest_and_websocket_contracts() -> None:
    main = (ROOT / "frontend" / "src" / "main.jsx").read_text(encoding="utf-8")
    api = (ROOT / "frontend" / "src" / "api.js").read_text(encoding="utf-8")
    assert 'client.feed({ limit: 100' in main
    assert 'client.stories({' in main
    assert 'client.summary(' in main
    assert 'client.search({' in main
    assert 'client.historyDates(' in main
    assert '"/api/v1/ws/live"' in api
    assert "new_sentence" in api or "new_sentence" in main
    assert "translation_ready" in main


def test_frontend_contains_no_embedded_sample_news_or_simulation_timer() -> None:
    main = (ROOT / "frontend" / "src" / "main.jsx").read_text(encoding="utf-8")
    forbidden = [
        "BASE_OBSERVATIONS",
        "STORY_COUNTS",
        "sample news archive",
        "setInterval(timer",  # legacy simulated insertion pattern
        "Geo News · Politics · first reported",
    ]
    for marker in forbidden:
        assert marker not in main


def test_frontend_preserves_live_repeats_and_opposite_translation_only() -> None:
    api = (ROOT / "frontend" / "src" / "api.js").read_text(encoding="utf-8")
    main = (ROOT / "frontend" / "src" / "main.jsx").read_text(encoding="utf-8")
    assert "byObservation" in api
    assert "pruneLiveItems" in api
    assert "translationForDisplay" in api
    assert "oppositeStoryText" in api
    assert "dedupDecision" in main


def test_frontend_has_no_css_gradients_or_remote_font_dependency() -> None:
    css = (ROOT / "frontend" / "src" / "styles.css").read_text(encoding="utf-8").lower()
    assert "linear-gradient" not in css
    assert "radial-gradient" not in css
    assert "fonts.googleapis.com" not in css
