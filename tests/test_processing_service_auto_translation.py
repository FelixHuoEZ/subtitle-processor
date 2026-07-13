from pathlib import Path

import pytest

from app.services.processing_service import ProcessingService


class FakeFileService:
    def __init__(self, tmp_path):
        self.tmp_path = Path(tmp_path)
        self.tasks = {}

    def list_files(self):
        return self.tasks

    def update_file_info(self, task_id, updates):
        self.tasks.setdefault(task_id, {}).update(updates)

    def save_file(self, content, filename):
        path = self.tmp_path / filename
        path.write_text(content, encoding="utf-8")
        return str(path)


class FakeTranslationService:
    default_target_language = "zh"

    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "status": "completed",
            "content": (
                "1\n00:00:00,000 --> 00:00:01,000\nTranslated subtitle"
            ),
            "providers": ["deepl_api"],
            "total_segments": 1,
            "translated_segments": 1,
            "failed_segments": 0,
            "error": None,
        }

    def translate_subtitle_content_detailed(
        self, content, target_language, source_language
    ):
        self.calls.append((content, target_language, source_language))
        return dict(self.result)


def build_service(monkeypatch, tmp_path, translator=None, **env):
    for key in (
        "AUTO_TRANSLATE_NON_TARGET_LANGUAGE",
        "AUTO_TRANSLATE_NON_ZH",
        "AUTO_TRANSLATE_TARGET_LANGUAGE",
        "AUTO_TRANSLATE_MIN_SOURCE_CONFIDENCE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))

    file_service = FakeFileService(tmp_path)
    translator = translator or FakeTranslationService()
    service = ProcessingService(
        file_service=file_service,
        video_service=None,
        transcription_service=None,
        subtitle_service=None,
        readwise_service=None,
        translation_service=translator,
    )
    return service, file_service, translator


def test_old_non_zh_switch_does_not_enable_automatic_translation(
    monkeypatch, tmp_path
):
    service, _, translator = build_service(
        monkeypatch,
        tmp_path,
        AUTO_TRANSLATE_NON_ZH="true",
    )
    task = {
        "id": "task-old-switch",
        "language": "en",
        "language_details": {"language": "en", "confidence": 0.99},
        "subtitle_content": "English subtitle",
    }

    assert service._apply_auto_translation(task["id"], task) is True
    assert translator.calls == []
    assert "translation_status" not in task


def test_configured_target_language_is_not_limited_to_chinese(monkeypatch, tmp_path):
    service, _, translator = build_service(
        monkeypatch,
        tmp_path,
        AUTO_TRANSLATE_NON_TARGET_LANGUAGE="true",
        AUTO_TRANSLATE_TARGET_LANGUAGE="en",
    )
    original_path = str(tmp_path / "original.srt")
    task = {
        "id": "task-to-en",
        "video_info": {"title": "Demo"},
        "language": "zh",
        "language_details": {"language": "zh", "confidence": 0.97},
        "subtitle_content": "中文字幕",
        "subtitle_path": original_path,
    }

    assert service._apply_auto_translation(task["id"], task) is True

    assert translator.calls == [("中文字幕", "en", "zh")]
    assert task["translation_status"] == "completed"
    assert task["translation_target_language"] == "en"
    assert task["original_subtitle_path"] == original_path
    assert task["subtitle_path"] != original_path
    assert Path(task["subtitle_path"]).read_text(encoding="utf-8").endswith(
        "Translated subtitle"
    )


def test_downloaded_subtitle_track_language_takes_precedence_over_spoken_language(
    monkeypatch, tmp_path
):
    service, _, translator = build_service(
        monkeypatch,
        tmp_path,
        AUTO_TRANSLATE_NON_TARGET_LANGUAGE="true",
        AUTO_TRANSLATE_TARGET_LANGUAGE="zh",
    )
    task = {
        "id": "task-track-language",
        "video_info": {"title": "Demo"},
        "language": "ja",
        "language_details": {"language": "ja", "confidence": 0.91},
        "subtitle_metadata": {"matched_lang": "en-US"},
        "subtitle_content": "English subtitle",
        "subtitle_path": str(tmp_path / "original.srt"),
    }

    assert service._apply_auto_translation(task["id"], task) is True

    assert translator.calls == [("English subtitle", "zh", "en")]
    assert task["translation_source_language"] == "en"


@pytest.mark.parametrize("source_language", [None, "auto", "mixed", "unknown"])
def test_uncertain_source_languages_are_skipped(
    monkeypatch, tmp_path, source_language
):
    service, _, translator = build_service(
        monkeypatch,
        tmp_path,
        AUTO_TRANSLATE_NON_TARGET_LANGUAGE="true",
    )
    task = {
        "id": f"task-{source_language}",
        "language": source_language,
        "language_details": {"language": source_language, "confidence": 0.99},
        "subtitle_content": "Subtitle",
    }

    assert service._apply_auto_translation(task["id"], task) is True
    assert translator.calls == []
    assert task["translation_status"] == "skipped"
    assert task["translation_reason"] == "uncertain_source_language"


def test_partial_translation_keeps_original_and_fails_before_readwise(
    monkeypatch, tmp_path
):
    translator = FakeTranslationService(
        {
            "status": "partial",
            "content": None,
            "providers": ["deepl_api"],
            "total_segments": 3,
            "translated_segments": 2,
            "failed_segments": 1,
            "error": "subtitle_block_3_failed",
        }
    )
    service, _, _ = build_service(
        monkeypatch,
        tmp_path,
        translator=translator,
        AUTO_TRANSLATE_NON_TARGET_LANGUAGE="true",
    )
    original_path = str(tmp_path / "original.srt")
    original_content = "Original subtitle"
    task = {
        "id": "task-partial",
        "language": "en",
        "language_details": {"language": "en", "confidence": 0.96},
        "subtitle_content": original_content,
        "subtitle_path": original_path,
    }

    assert service._apply_auto_translation(task["id"], task) is False

    assert task["status"] == "failed"
    assert task["subtitle_content"] == original_content
    assert task["subtitle_path"] == original_path
    assert task["translation_result"]["failed_segments"] == 1


def test_invalid_target_configuration_fails_instead_of_sending_original(
    monkeypatch, tmp_path
):
    service, _, translator = build_service(
        monkeypatch,
        tmp_path,
        AUTO_TRANSLATE_NON_TARGET_LANGUAGE="true",
        AUTO_TRANSLATE_TARGET_LANGUAGE="auto",
    )
    task = {
        "id": "task-invalid-target",
        "language": "en",
        "language_details": {"language": "en", "confidence": 0.96},
        "subtitle_content": "Original subtitle",
    }

    assert service._apply_auto_translation(task["id"], task) is False
    assert translator.calls == []
    assert task["status"] == "failed"
    assert task["translation_reason"] == "invalid_target_language"


def test_enabled_switch_adds_translation_to_full_text_progress_plan(
    monkeypatch, tmp_path
):
    service, _, _ = build_service(
        monkeypatch,
        tmp_path,
        AUTO_TRANSLATE_NON_TARGET_LANGUAGE="true",
    )
    task = {"id": "task-plan", "status": "processing"}
    service.task_progress.start_run(task, path="unknown")

    service._set_progress_plan_from_video_result(
        task,
        {"subtitle_content": "Subtitle", "download_asset_cache_hit": False},
    )

    plan = task["progress_runs"][-1]["plan"]
    assert plan == [
        "source_analysis",
        "normalize_subtitles",
        "translate_subtitles",
        "send_readwise",
    ]
