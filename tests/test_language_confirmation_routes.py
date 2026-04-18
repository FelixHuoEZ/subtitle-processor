import os
import tempfile
from pathlib import Path

from flask import Flask

_TMP_DIR = Path(tempfile.mkdtemp(prefix="subtitle_processor_test_"))
os.makedirs(_TMP_DIR / "uploads", exist_ok=True)
os.makedirs(_TMP_DIR / "outputs", exist_ok=True)

from app.config import config_manager


class _TestConfigManager:
    def get_config_value(self, key_path, default=None):
        if key_path == "app.upload_folder":
            return str(_TMP_DIR / "uploads")
        if key_path == "app.output_folder":
            return str(_TMP_DIR / "outputs")
        return default


config_manager._config_manager = _TestConfigManager()

from app.routes import process_routes, upload_routes


def _build_process_test_client():
    app = Flask(__name__)
    app.register_blueprint(process_routes.process_bp)
    return app.test_client()


def test_should_request_language_confirmation_for_telegram_low_confidence():
    task_info = {
        "request_source": "telegram",
        "url": "https://www.youtube.com/watch?v=demo123",
    }
    result = {
        "language_details": {"language": "en", "confidence": 0.62},
        "content_locale": "zh",
        "content_locale_details": {"language": "zh", "confidence": 0.91},
        "video_info": {"title": "Demo Video", "uploader": "Demo Channel"},
        "skip_processing_for_url_only": False,
    }

    confirmation = upload_routes._should_request_language_confirmation(
        task_info, result
    )

    assert confirmation is not None
    assert confirmation["status"] == "pending"
    assert confirmation["reason"] == "low_spoken_confidence"
    assert confirmation["url"] == task_info["url"]
    assert confirmation["suggested_language"] == "en"


def test_should_request_language_confirmation_for_zh_locale_mismatch_under_085():
    task_info = {
        "request_source": "telegram",
        "url": "https://www.youtube.com/watch?v=7R9H-EX6cnI",
    }
    result = {
        "language_details": {"language": "en", "confidence": 0.8331},
        "content_locale": "zh",
        "content_locale_details": {"language": "zh", "confidence": 0.91},
        "video_info": {"title": "再次改良英语 这能行吗", "uploader": "Demo Channel"},
        "skip_processing_for_url_only": False,
    }

    confirmation = upload_routes._should_request_language_confirmation(
        task_info, result
    )

    assert confirmation is not None
    assert confirmation["reason"] == "content_locale_spoken_mismatch"
    assert confirmation["url"] == "https://www.youtube.com/watch?v=7R9H-EX6cnI"
    assert confirmation["suggested_language"] == "en"


def test_apply_language_confirmation_updates_language_and_readwise_mode():
    result = {
        "language": "en",
        "language_details": {"language": "en", "confidence": 0.66},
        "content_locale_details": {"language": "zh", "confidence": 0.9},
        "track_catalog": [],
    }
    task_info = {}
    confirmation = {"status": "confirmed", "selected_language": "zh"}

    upload_routes._apply_language_confirmation(result, task_info, confirmation)

    assert result["language"] == "zh"
    assert result["language_details"]["manual_override"] is True
    assert result["readwise_mode"] == "full_text"
    assert task_info["language_override"] == "zh"


def test_status_endpoint_returns_language_confirmation(monkeypatch):
    client = _build_process_test_client()
    task_info = {
        "status": "waiting_for_language_confirmation",
        "language": "en",
        "language_details": {"language": "en", "confidence": 0.64},
        "content_locale": "zh",
        "content_locale_details": {"language": "zh", "confidence": 0.92},
        "readwise_mode": "url_only",
        "readwise_reason": "low_confidence_conflict",
        "spoken_pattern": "zh_framed_foreign_body",
        "url": "https://www.youtube.com/watch?v=demo123",
        "language_confirmation": {
            "status": "pending",
            "reason": "low_spoken_confidence",
            "url": "https://www.youtube.com/watch?v=demo123",
        },
    }

    monkeypatch.setattr(
        process_routes.file_service,
        "get_file_info",
        lambda task_id: task_info if task_id == "task-1" else None,
    )

    response = client.get("/process/status/task-1")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "waiting_for_language_confirmation"
    assert payload["language_confirmation"]["reason"] == "low_spoken_confidence"
    assert payload["language"] == "en"
    assert payload["content_locale"] == "zh"


def test_confirm_language_endpoint_updates_pending_task(monkeypatch):
    client = _build_process_test_client()
    task_info = {
        "status": "waiting_for_language_confirmation",
        "language_confirmation": {"status": "pending", "url": "https://youtu.be/demo"},
    }
    captured_updates = []

    def fake_get_file_info(task_id):
        return task_info if task_id == "task-1" else None

    def fake_update_file_info(task_id, updates):
        captured_updates.append((task_id, updates))
        task_info.update(updates)

    monkeypatch.setattr(process_routes.file_service, "get_file_info", fake_get_file_info)
    monkeypatch.setattr(
        process_routes.file_service, "update_file_info", fake_update_file_info
    )

    response = client.post(
        "/process/status/task-1/language",
        json={"language": "zh", "source": "telegram"},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["selected_language"] == "zh"
    assert captured_updates
    assert (
        captured_updates[0][1]["language_confirmation"]["selected_language"] == "zh"
    )
