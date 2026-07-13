import logging

from app.services import transcription_service as transcription_module
from app.services.transcription_service import TranscriptionService


def build_transcription_service(monkeypatch):
    monkeypatch.delenv("AUDIO_PROBE_PROVIDERS", raising=False)
    monkeypatch.delenv("AUDIO_PROBE_MIN_CONFIDENCE", raising=False)
    monkeypatch.delenv("AUDIO_PROBE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AUDIO_PROBE_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("AUDIO_PROBE_OPENAI_MODEL", raising=False)
    return TranscriptionService()


def test_audio_probe_disables_openai_once_when_separate_key_is_unavailable(
    monkeypatch, caplog
):
    original_get_config_value = transcription_module.get_config_value

    def fake_get_config_value(key, default=None):
        if key == "audio_probe.providers":
            return ["configured_funasr", "openai"]
        if key == "audio_probe.openai.api_key":
            return ""
        return original_get_config_value(key, default)

    monkeypatch.setattr(transcription_module, "get_config_value", fake_get_config_value)
    monkeypatch.setattr(TranscriptionService, "_openai_warning_emitted", False)

    with caplog.at_level(logging.WARNING):
        first = build_transcription_service(monkeypatch)
        second = build_transcription_service(monkeypatch)

    assert first.audio_probe_providers == ["configured_funasr"]
    assert second.audio_probe_providers == ["configured_funasr"]
    warnings = [
        record
        for record in caplog.records
        if "OpenAI Whisper provider 已自动禁用" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_openai_audio_probe_uses_compatible_http_endpoint(monkeypatch, tmp_path):
    original_get_config_value = transcription_module.get_config_value

    def fake_get_config_value(key, default=None):
        values = {
            "audio_probe.providers": ["openai"],
            "audio_probe.openai.api_key": "audio-key",
            "audio_probe.openai.base_url": "https://audio.example.test/v1",
            "audio_probe.openai.model": "whisper-test",
        }
        return values.get(key, original_get_config_value(key, default))

    monkeypatch.setattr(transcription_module, "get_config_value", fake_get_config_value)
    service = build_transcription_service(monkeypatch)
    audio_file = tmp_path / "probe.wav"
    audio_file.write_bytes(b"probe")
    calls = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"text": "Hello", "duration": 1.5, "words": []}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(transcription_module.requests, "post", fake_post)

    result = service._transcribe_with_openai(str(audio_file))

    assert result["text"] == "Hello"
    assert calls[0][0] == "https://audio.example.test/v1/audio/transcriptions"
    assert calls[0][1]["data"]["model"] == "whisper-test"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer audio-key"


def test_audio_probe_prefers_configured_funasr_when_result_is_usable(
    monkeypatch, tmp_path
):
    service = build_transcription_service(monkeypatch)
    service.audio_probe_providers = ["configured_funasr", "openai"]
    audio_file = tmp_path / "probe.wav"
    audio_file.write_bytes(b"probe")
    calls = []

    monkeypatch.setattr(
        service,
        "_probe_audio_language_with_configured_funasr",
        lambda _: calls.append("configured_funasr")
        or {
            "language": "en",
            "confidence": 0.91,
            "samples": [{"offset_seconds": 6.0}],
            "provider": "configured_funasr",
            "provider_metadata": {"model_language_bias": "zh"},
        },
    )
    monkeypatch.setattr(
        service,
        "_probe_audio_language_with_openai",
        lambda _: calls.append("openai")
        or {
            "language": "zh",
            "confidence": 0.88,
            "samples": [{"offset_seconds": 6.0}],
            "provider": "openai",
            "provider_metadata": {"provider": "openai"},
        },
    )

    result = service.detect_audio_language(str(audio_file))

    assert result["provider"] == "configured_funasr"
    assert calls == ["configured_funasr"]


def test_audio_probe_falls_back_when_local_result_is_low_confidence(
    monkeypatch, tmp_path
):
    service = build_transcription_service(monkeypatch)
    service.audio_probe_providers = ["configured_funasr", "openai"]
    audio_file = tmp_path / "probe.wav"
    audio_file.write_bytes(b"probe")
    calls = []

    monkeypatch.setattr(
        service,
        "_probe_audio_language_with_configured_funasr",
        lambda _: calls.append("configured_funasr")
        or {
            "language": "zh",
            "confidence": 0.42,
            "samples": [{"offset_seconds": 6.0}],
            "provider": "configured_funasr",
            "provider_metadata": {"model_language_bias": "zh"},
        },
    )
    monkeypatch.setattr(
        service,
        "_probe_audio_language_with_openai",
        lambda _: calls.append("openai")
        or {
            "language": "en",
            "confidence": 0.88,
            "samples": [{"offset_seconds": 6.0}],
            "provider": "openai",
            "provider_metadata": {"provider": "openai"},
        },
    )

    result = service.detect_audio_language(str(audio_file))

    assert result["provider"] == "openai"
    assert calls == ["configured_funasr", "openai"]


def test_audio_probe_continues_when_single_language_model_matches_its_bias(
    monkeypatch, tmp_path
):
    service = build_transcription_service(monkeypatch)
    service.audio_probe_providers = ["configured_funasr", "openai"]
    audio_file = tmp_path / "probe.wav"
    audio_file.write_bytes(b"probe")
    calls = []

    monkeypatch.setattr(
        service,
        "_probe_audio_language_with_configured_funasr",
        lambda _: calls.append("configured_funasr")
        or {
            "language": "zh",
            "confidence": 0.95,
            "samples": [{"offset_seconds": 6.0}],
            "provider": "configured_funasr",
            "provider_metadata": {"model_language_bias": "zh"},
        },
    )
    monkeypatch.setattr(
        service,
        "_probe_audio_language_with_openai",
        lambda _: calls.append("openai")
        or {
            "language": "en",
            "confidence": 0.87,
            "samples": [{"offset_seconds": 6.0}],
            "provider": "openai",
            "provider_metadata": {"provider": "openai"},
        },
    )

    result = service.detect_audio_language(str(audio_file))

    assert result["provider"] == "openai"
    assert calls == ["configured_funasr", "openai"]


def test_audio_probe_returns_best_candidate_when_no_provider_is_decisive(
    monkeypatch, tmp_path
):
    service = build_transcription_service(monkeypatch)
    service.audio_probe_providers = ["configured_funasr", "openai"]
    audio_file = tmp_path / "probe.wav"
    audio_file.write_bytes(b"probe")
    calls = []

    monkeypatch.setattr(
        service,
        "_probe_audio_language_with_configured_funasr",
        lambda _: calls.append("configured_funasr")
        or {
            "language": "zh",
            "confidence": 0.83,
            "samples": [{"offset_seconds": 6.0}],
            "provider": "configured_funasr",
            "provider_metadata": {"model_language_bias": "zh"},
        },
    )
    monkeypatch.setattr(
        service,
        "_probe_audio_language_with_openai",
        lambda _: calls.append("openai") or None,
    )

    result = service.detect_audio_language(str(audio_file))

    assert result["provider"] == "configured_funasr"
    assert result["language"] == "zh"
    assert calls == ["configured_funasr", "openai"]


def test_audio_probe_sample_scoring_discounts_english():
    assert TranscriptionService._score_audio_probe_sample("zh", 1.0) == 1.0
    assert TranscriptionService._score_audio_probe_sample("en", 1.0) == 0.78


def test_audio_probe_repeat_bonus_rewards_sustained_samples():
    adjusted = TranscriptionService._apply_audio_probe_sample_adjustments(
        {"zh": 0.0, "en": 1.56},
        {"zh": 0, "en": 2},
    )

    assert adjusted["en"] > 1.56


def test_audio_probe_uncertainty_lowers_confidence_without_flipping_language():
    decision = TranscriptionService._decide_audio_probe_primary_language(
        {"zh": 0.0, "en": 1.7472},
        uncertainty_mass=0.1799,
        min_total=0.2,
        min_margin=0.12,
        min_confidence=0.58,
    )

    assert decision["language"] == "en"
    assert 0.85 < decision["confidence"] < 1.0


def test_audio_probe_two_en_and_one_mixed_stays_en_but_becomes_less_certain():
    decision = TranscriptionService._decide_audio_probe_primary_language(
        {"zh": 0.0, "en": 1.7472},
        uncertainty_mass=0.35,
        min_total=0.2,
        min_margin=0.12,
        min_confidence=0.58,
    )

    assert decision["language"] == "en"
    assert 0.82 < decision["confidence"] < 0.84
