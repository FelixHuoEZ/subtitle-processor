from typing import List, Optional, Dict

import pytest

from app.services.transcription_service import TranscriptionService
from app.services.hotword_service import HotwordService
from app.services.hotword_settings import HotwordSettingsManager


@pytest.fixture(autouse=True)
def isolate_hotword_settings(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Isolate HotwordSettingsManager singleton and persisted state per test."""
    settings_path = tmp_path / "hotword_settings.json"
    monkeypatch.setenv("HOTWORD_SETTINGS_PATH", str(settings_path))
    HotwordSettingsManager._instance = None
    yield
    HotwordSettingsManager._instance = None


def _create_dummy_audio(tmp_path) -> str:
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"\x00\x00")
    return str(audio_path)


def test_transcribe_audio_prefers_user_hotwords(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HOTWORD_MODE", "curated")
    monkeypatch.setenv("ENABLE_AUTO_HOTWORDS", "true")

    service = TranscriptionService()
    audio_path = _create_dummy_audio(tmp_path)

    captured: Dict[str, Optional[List[str]]] = {}

    def fake_transcribe(audio_file: str, hotwords: List[str]) -> Dict[str, str]:
        captured["audio_file"] = audio_file
        captured["hotwords"] = hotwords
        return {"text": "ok"}

    monkeypatch.setattr(service, "_transcribe_with_funasr", fake_transcribe)

    result = service.transcribe_audio(audio_path, hotwords=["自定义热词"])

    assert result == {"text": "ok"}
    assert captured["audio_file"] == audio_path
    assert captured["hotwords"] == ["自定义热词"]


def test_transcribe_audio_user_only_mode_skips_auto(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HOTWORD_MODE", "user_only")
    monkeypatch.setenv("ENABLE_AUTO_HOTWORDS", "true")

    service = TranscriptionService()
    audio_path = _create_dummy_audio(tmp_path)

    monkeypatch.setattr(
        service.hotword_service,
        "generate_hotwords",
        lambda **_: [{"word": "自动热词", "score": 0.5, "sources": ["title"], "strict": True}],
    )
    service.default_hotwords = ["默认热词"]

    captured: Dict[str, Optional[List[str]]] = {}

    def fake_transcribe(audio_file: str, hotwords: List[str]) -> Dict[str, str]:
        captured["audio_file"] = audio_file
        captured["hotwords"] = hotwords
        return {"text": "ok"}

    monkeypatch.setattr(service, "_transcribe_with_funasr", fake_transcribe)

    result = service.transcribe_audio(audio_path)

    assert result == {"text": "ok"}
    assert captured["hotwords"] == []


def test_transcribe_audio_curated_mode_uses_candidates(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HOTWORD_MODE", "curated")
    monkeypatch.setenv("ENABLE_AUTO_HOTWORDS", "true")

    service = TranscriptionService()
    audio_path = _create_dummy_audio(tmp_path)

    monkeypatch.setattr(
        service.hotword_service,
        "generate_hotwords",
        lambda **_: [{"word": "精确热词", "score": 0.6, "sources": ["title"], "strict": True}],
    )
    service.default_hotwords = ["默认热词"]

    captured: Dict[str, Optional[List[str]]] = {}

    def fake_transcribe(audio_file: str, hotwords: List[str]) -> Dict[str, str]:
        captured["audio_file"] = audio_file
        captured["hotwords"] = hotwords
        return {"text": "ok"}

    monkeypatch.setattr(service, "_transcribe_with_funasr", fake_transcribe)

    result = service.transcribe_audio(audio_path)

    assert result == {"text": "ok"}
    assert captured["hotwords"] == ["精确热词"]


def test_transcribe_audio_experiment_mode_appends_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HOTWORD_MODE", "experiment")
    monkeypatch.setenv("ENABLE_AUTO_HOTWORDS", "true")

    service = TranscriptionService()
    audio_path = _create_dummy_audio(tmp_path)

    monkeypatch.setattr(
        service.hotword_service,
        "generate_hotwords",
        lambda **_: [{"word": "实验热词", "score": 0.6, "sources": ["title"], "strict": False}],
    )
    service.default_hotwords = ["默认热词"]

    captured: Dict[str, Optional[List[str]]] = {}

    def fake_transcribe(audio_file: str, hotwords: List[str]) -> Dict[str, str]:
        captured["audio_file"] = audio_file
        captured["hotwords"] = hotwords
        return {"text": "ok"}

    monkeypatch.setattr(service, "_transcribe_with_funasr", fake_transcribe)

    result = service.transcribe_audio(audio_path)

    assert result == {"text": "ok"}
    assert captured["hotwords"] == ["实验热词", "默认热词"]


def test_generate_hotwords_filters_stopwords(monkeypatch: pytest.MonkeyPatch):
    service = HotwordService()
    service.category_hotwords = {}
    service.config = service._get_default_config()
    service.config['strategy']['enabled_methods'] = ['title_extraction']
    service.config['weights']['title_extraction'] = 0.9
    service.config['strategy']['thresholds']['curated']['min_score'] = 0.05
    service.config['strategy']['thresholds']['curated']['strict_score'] = 0.2

    monkeypatch.setattr(service, '_get_category_based_hotwords', lambda *args, **kwargs: [])
    monkeypatch.setattr(service, '_get_tag_based_hotwords', lambda tags: [])
    monkeypatch.setattr(service, '_get_learned_hotwords', lambda *args, **kwargs: [])
    monkeypatch.setattr(service, '_extract_keywords_from_title', lambda title: ['芯片', '视频', '制程'])

    candidates = service.generate_hotwords(title="芯片制程视频", mode="curated", max_hotwords=5)

    words = [c['word'] for c in candidates]
    assert '芯片' in words
    assert '制程' in words
    assert '视频' not in words


def test_transcribe_audio_auto_hotwords_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HOTWORD_MODE", "curated")
    monkeypatch.setenv("ENABLE_AUTO_HOTWORDS", "false")

    service = TranscriptionService()
    audio_path = _create_dummy_audio(tmp_path)

    monkeypatch.setattr(
        service.hotword_service,
        "generate_hotwords",
        lambda **_: [{"word": "不该出现", "score": 0.9, "sources": ["title"], "strict": True}],
    )

    captured: Dict[str, Optional[List[str]]] = {}

    def fake_transcribe(audio_file: str, hotwords: List[str]) -> Dict[str, str]:
        captured["hotwords"] = hotwords
        return {"text": "ok"}

    monkeypatch.setattr(service, "_transcribe_with_funasr", fake_transcribe)

    result = service.transcribe_audio(audio_path)

    assert result == {"text": "ok"}
    assert captured["hotwords"] == []


def test_transcribe_audio_applies_post_processor(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("HOTWORD_MODE", "user_only")
    monkeypatch.setenv("ENABLE_AUTO_HOTWORDS", "true")
    monkeypatch.setenv("ENABLE_HOTWORD_POST_PROCESS", "true")

    service = TranscriptionService()
    audio_path = _create_dummy_audio(tmp_path)

    monkeypatch.setattr(
        service,
        "_transcribe_with_funasr",
        lambda *args, **kwargs: {"text": "派森 是一门语言"},
    )

    result = service.transcribe_audio(audio_path, hotwords=["Python"])

    assert result["text"].startswith("Python")


def test_detect_audio_language_uses_segment_votes(monkeypatch: pytest.MonkeyPatch, tmp_path):
    service = TranscriptionService()
    service.openai_api_key = "test-key"
    audio_path = _create_dummy_audio(tmp_path)

    monkeypatch.setattr(service, "_get_audio_info", lambda _: {"duration_seconds": 90})
    monkeypatch.setattr(
        service,
        "_extract_audio_probe_segment",
        lambda _audio, start, _duration: f"sample-{start}.wav",
    )

    def fake_transcribe(sample_path: str):
        if "0.0" in sample_path:
            return {"text": "Hello everyone and welcome back"}
        if "35.0" in sample_path:
            return {"text": "This is a full English sentence"}
        return {"text": "Thanks for watching this episode"}

    monkeypatch.setattr(service, "_transcribe_with_openai", fake_transcribe)

    result = service.detect_audio_language(audio_path)

    assert result["language"] == "en"
    assert result["confidence"] >= 0.58
    assert len(result["samples"]) == 3
