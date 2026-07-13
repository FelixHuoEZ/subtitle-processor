from flask import Flask

from app.routes import process_routes


class FakeFileService:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.saved = []
        self.file_info = {
            "original_filename": "subtitle.srt",
            "subtitle_content": (
                "1\n00:00:00,000 --> 00:00:01,000\nHello"
            ),
        }

    def get_file_info(self, file_id):
        return self.file_info if file_id == "task-1" else None

    def save_file(self, content, filename):
        path = self.tmp_path / filename
        path.write_text(content, encoding="utf-8")
        self.saved.append((content, filename))
        return str(path)


class FakeTranslationService:
    default_target_language = "zh"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def translate_subtitle_content_detailed(
        self, content, target_language, source_language
    ):
        self.calls.append((content, target_language, source_language))
        return dict(self.result)


def build_client(monkeypatch, tmp_path, translation_result):
    file_service = FakeFileService(tmp_path)
    translation_service = FakeTranslationService(translation_result)
    monkeypatch.setattr(process_routes, "file_service", file_service)
    monkeypatch.setattr(
        process_routes,
        "translation_service",
        translation_service,
    )
    app = Flask(__name__)
    app.register_blueprint(process_routes.process_bp)
    return app.test_client(), file_service, translation_service


def test_manual_translation_uses_configured_default_target(monkeypatch, tmp_path):
    client, file_service, translation_service = build_client(
        monkeypatch,
        tmp_path,
        {
            "status": "completed",
            "content": "1\n00:00:00,000 --> 00:00:01,000\n你好",
            "source_language": "auto",
            "target_language": "zh",
            "providers": ["deepl_api"],
            "total_segments": 1,
        },
    )

    response = client.post("/process/translate/task-1", json={})

    assert response.status_code == 200
    assert translation_service.calls[0][1:] == ("zh", "auto")
    assert response.get_json()["target_language"] == "zh"
    assert len(file_service.saved) == 1


def test_manual_partial_translation_is_not_saved_as_success(monkeypatch, tmp_path):
    client, file_service, _ = build_client(
        monkeypatch,
        tmp_path,
        {
            "status": "partial",
            "content": None,
            "source_language": "en",
            "target_language": "zh",
            "providers": ["deepl_api"],
            "total_segments": 3,
            "translated_segments": 2,
            "failed_segments": 1,
            "error": "subtitle_block_3_failed",
        },
    )

    response = client.post(
        "/process/translate/task-1",
        json={"source_lang": "en", "target_lang": "zh"},
    )

    assert response.status_code == 502
    payload = response.get_json()
    assert payload["translation_status"] == "partial"
    assert payload["failed_segments"] == 1
    assert file_service.saved == []
