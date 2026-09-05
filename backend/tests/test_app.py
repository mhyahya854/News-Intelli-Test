from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_health_contract() -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["phase"] == 12
    assert body["deployment"] == "windows-native-no-docker"
    assert body["database"] == "postgresql"


def test_api_root_contract() -> None:
    response = client.get("/api/v1")
    assert response.status_code == 200
    body = response.json()
    assert body["docs"] == "/docs"
    assert body["schema"] == "/api/v1/schema"
    assert body["ingestion_doctor"] == "/api/v1/ingestion/doctor"
    assert body["ocr_doctor"] == "/api/v1/ocr/doctor"
    assert body["ocr_config"] == "/api/v1/ocr/config"
    assert body["segmentation_doctor"] == "/api/v1/segmentation/doctor"
    assert body["segmentation_config"] == "/api/v1/segmentation/config"
    assert body["keyword_matching_doctor"] == "/api/v1/keyword-matching/doctor"
    assert body["keyword_matching_config"] == "/api/v1/keyword-matching/config"
    assert body["keyword_matching_test"] == "/api/v1/keyword-matching/test"
    assert body["deduplication_doctor"] == "/api/v1/deduplication/doctor"
    assert body["deduplication_config"] == "/api/v1/deduplication/config"
    assert body["translation_doctor"] == "/api/v1/translation/doctor"
    assert body["translation_config"] == "/api/v1/translation/config"
    assert body["summarization_doctor"] == "/api/v1/summarization/doctor"
    assert body["summarization_config"] == "/api/v1/summarization/config"


def test_ingestion_doctor_contract() -> None:
    response = client.get("/api/v1/ingestion/doctor")
    assert response.status_code in {200, 503}
    body = response.json()
    assert body["checks"]["hardware_acceleration"]["mode"] == "disabled"
    assert body["checks"]["yt_dlp"]["ok"] is True


def test_ocr_config_contract() -> None:
    response = client.get("/api/v1/ocr/config")
    assert response.status_code == 200
    body = response.json()
    assert body["device"] == "cpu"
    assert body["models"]["detection"] == "PP-OCRv6_small_det"
    assert body["thresholds"]["high_confidence"] == 0.95
    assert body["policy"]["low_confidence_text_is_retained"] is True


def test_ocr_doctor_contract() -> None:
    response = client.get("/api/v1/ocr/doctor")
    assert response.status_code in {200, 503}
    body = response.json()
    assert body["python_required"] == "3.12.x x64"
    assert body["device"] == "cpu"
    assert body["accuracy_policy"]["target_word_accuracy"] == 0.95


def test_segmentation_config_contract() -> None:
    response = client.get("/api/v1/segmentation/config")
    assert response.status_code == 200
    body = response.json()
    assert body["rolling_frame_count"] == 3
    assert body["screen_region_isolation"] is True
    assert body["matching_policy"] == {
        "exact_normalized_overlap_only": True,
        "fuzzy_overlap": False,
        "misspelling_variants": False,
        "autocorrection": False,
    }


def test_segmentation_doctor_contract() -> None:
    response = client.get("/api/v1/segmentation/doctor")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["phase"] == 4
    assert body["policy"]["incomplete_fragments_are_not_sent_downstream"] is True


def test_keyword_matching_config_contract() -> None:
    response = client.get("/api/v1/keyword-matching/config")
    assert response.status_code == 200
    body = response.json()
    assert body["cache_refresh_seconds"] == 60.0
    assert body["matching_policy"]["fuzzy_matching"] is False
    assert body["matching_policy"]["stemming"] is False
    assert body["delivery_policy"]["matched_observations_emit_immediately"] is True
    assert body["delivery_policy"]["summary_input"] == "canonical non-duplicate stories only"


def test_keyword_matching_preview_contract() -> None:
    response = client.post(
        "/api/v1/keyword-matching/test",
        json={"text": "The patient has a severe case of dengue.", "language": "en"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["event"] == "detection_observed"
    assert body["data"]["accepted_category_ids"] == ["health"]
    assert body["data"]["delivery"]["emit_immediately"] is True
    assert body["data"]["delivery"]["summary_status"] == "blocked_until_canonical_story_deduplication"


def test_deduplication_configuration_contract() -> None:
    response = client.get("/api/v1/deduplication/config")
    assert response.status_code == 200
    body = response.json()
    assert body["delivery"]["every_observation_emits_immediately"] is True
    assert body["delivery"]["only_new_canonical_story_enters_summary"] is True
    assert body["guards"]["spelling_fuzziness"] is False


def test_deduplication_doctor_contract_without_model_load() -> None:
    response = client.get("/api/v1/deduplication/doctor")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["safeguards"]["uncertain_matches_hidden_from_summaries"] is True


def test_translation_configuration_contract() -> None:
    response = client.get("/api/v1/translation/config")
    assert response.status_code == 200
    body = response.json()
    assert body["output_policy"]["translate_only_opposite_language"] is True
    assert body["output_policy"]["word_by_word_translation"] is False
    assert body["output_policy"]["roman_urdu_output_allowed"] is False
    assert body["live_delivery"]["window_minutes"] == 30
    assert body["live_delivery"]["repeated_observations_visible"] is True


def test_translation_doctor_contract_without_model_load() -> None:
    response = client.get("/api/v1/translation/doctor")
    assert response.status_code in {200, 503}
    body = response.json()
    assert body["policy"]["complete_sentence_only"] is True
    assert body["policy"]["translate_only_opposite_language"] is True
    assert body["policy"]["live_window_minutes"] == 30


def test_summarization_configuration_contract() -> None:
    response = client.get("/api/v1/summarization/config")
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "authoritative-extractive-fact-ledger"
    assert body["interval_seconds"] == 300
    assert body["output_policy"]["additive_only"] is True
    assert body["output_policy"]["optional_abstractive_digest_enabled"] is False


def test_summarization_doctor_contract_without_model_load() -> None:
    response = client.get("/api/v1/summarization/doctor")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["abstractive_model_required"] is False
