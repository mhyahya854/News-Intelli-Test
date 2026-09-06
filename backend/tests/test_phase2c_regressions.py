import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock

from newsintel.persistence import persistence_doctor
from newsintel.api import PostgresApiRepository
from newsintel.database import build_session_factory
from newsintel.ocr import ocr_doctor, ocr_runtime_config_from_env


def test_persistence_doctor_accepts_head_revision():
    """P2C-001 Regression: persistence_doctor must accept revisions through head 20260720_0006."""
    report = persistence_doctor(check_database=True)
    assert report["status"] == "ready"
    assert report["checks"]["postgresql"] == "ready"
    assert report["checks"]["schema_revision"] in {"20260719_0004", "20260719_0005", "20260720_0006"}


def test_persistence_doctor_rejects_incompatible_revision():
    """Regression: persistence_doctor must reject unexpected or outdated schema revisions."""
    from sqlalchemy.orm import Session
    with patch.object(Session, "scalar", return_value="20260719_0001"):
        report = persistence_doctor(check_database=True)
        assert report["status"] == "not_ready"
        assert report["checks"]["schema_revision"] == "20260719_0001"


def test_postgres_api_repository_categories_and_stats_query():
    """P2C-002 Regression: Verify scalar subquery unnest for category counts executes on PostgreSQL."""
    factory = build_session_factory()
    repo = PostgresApiRepository(session_factory=factory)
    
    # 1. Test list_categories
    categories = repo.list_categories(pakistan_date=date.today())
    assert isinstance(categories, list)
    assert len(categories) >= 16
    assert any(c["id"] == "judiciary" for c in categories)

    # 2. Test overview_stats
    stats = repo.overview_stats(pakistan_date=date.today())
    assert "per_category" in stats
    assert "active_streams" in stats


def test_frontend_vite_proxy_configuration():
    """Regression: Vite proxy configuration points to intended backend development port."""
    vite_config_path = Path(__file__).resolve().parents[2] / "frontend" / "vite.config.js"
    assert vite_config_path.exists(), "frontend/vite.config.js must exist"
    content = vite_config_path.read_text(encoding="utf-8")
    
    api_match = re.search(r'"/api":\s*\{\s*target:\s*"http://127\.0\.0\.1:(\d+)"', content)
    ws_match = re.search(r'"/ws":\s*\{\s*target:\s*"ws://127\.0\.0\.1:(\d+)"', content)
    
    assert api_match is not None, "Vite config must define /api proxy target"
    assert ws_match is not None, "Vite config must define /ws proxy target"
    assert api_match.group(1) == ws_match.group(1), "REST and WS proxy targets must match ports"
    assert api_match.group(1) in {"8000", "8001"}, f"Unexpected proxy port: {api_match.group(1)}"


def test_ocr_repairs_torch_first_ordering_and_mkldnn_env():
    """Regression: Torch-first import ordering and oneDNN disabling are enforced in OCR subsystem."""
    # 1. Check environment variable in newsintel.ocr
    assert os.environ.get("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT") == "False"
    assert os.environ.get("FLAGS_use_mkldnn") == "0"
    
    # 2. Check doctor import ordering: 'torch' must precede 'paddle' and 'paddleocr'
    doc = ocr_doctor(load_models=False)
    deps = list(doc.get("dependencies", {}).keys())
    assert "torch" in deps
    assert "paddle" in deps
    assert "paddleocr" in deps
    assert deps.index("torch") < deps.index("paddle")
    assert deps.index("torch") < deps.index("paddleocr")


def test_deterministic_ocr_fixture_inference():
    """Regression: OCR pipeline operates deterministically with structured mock/fixture engine."""
    from newsintel.ocr import OCRPipeline, OCRConfig, OCRLine, script_profile
    
    box = ((10.0, 10.0), (500.0, 10.0), (500.0, 80.0), (10.0, 80.0))
    expected_line = OCRLine(
        text="Pakistan News 2026",
        confidence=0.99,
        polygon=box,
        engine="deterministic-fixture",
        model="synthetic",
        script=script_profile("Pakistan News 2026"),
        accepted=True,
        needs_review=False,
    )
    
    class DeterministicFixtureEngine:
        def __init__(self):
            self.name = "deterministic-fixture"
            
        def recognize(self, image_bytes: bytes) -> list[OCRLine]:
            return [expected_line]
            
    config = OCRConfig()
    pipeline = OCRPipeline(config=config, primary=DeterministicFixtureEngine(), fallback=None)
    
    import hashlib, time
    from newsintel.streaming import FrameEnvelope
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32  # Minimal JPEG header
    frame = FrameEnvelope(
        stream_id="geo-test",
        channel_name="Geo News",
        sequence=1,
        captured_at=datetime.now(timezone.utc),
        received_monotonic_ns=time.monotonic_ns(),
        jpeg_bytes=jpeg,
        sha256=hashlib.sha256(jpeg).hexdigest(),
        width=640,
        height=360,
        source_video_id="vid",
        source_format_id="fmt",
    )
    
    result = pipeline.process_frame(frame)
    assert result.raw_text == "Pakistan News 2026"
    assert result.confidence == 0.99
    assert not result.fallback_used



