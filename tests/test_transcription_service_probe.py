from app.services.transcription_service import TranscriptionService


def build_transcription_service(monkeypatch):
    monkeypatch.delenv("AUDIO_PROBE_PROVIDERS", raising=False)
    monkeypatch.delenv("AUDIO_PROBE_MIN_CONFIDENCE", raising=False)
    return TranscriptionService()


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
