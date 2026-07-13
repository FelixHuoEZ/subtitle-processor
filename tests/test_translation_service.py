from app.services import translation_service as translation_module
from app.services.translation_service import TranslationService


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def build_service(monkeypatch, config):
    monkeypatch.setattr(
        translation_module,
        "get_config_value",
        lambda key, default=None: config.get(key, default),
    )
    return TranslationService()


def test_deepl_is_selected_from_configured_provider_order(monkeypatch):
    service = build_service(
        monkeypatch,
        {
            "tokens.deepl.api_key": "deepl-key",
            "tokens.openai": [],
            "translation.max_retries": 1,
            "translation.request_interval": 0,
            "translation.services": [
                {"name": "deepl", "enabled": True, "priority": 1},
                {"name": "deeplx", "enabled": False, "priority": 2},
            ],
        },
    )
    requests = []

    def fake_post(url, **kwargs):
        requests.append((url, kwargs))
        return FakeResponse({"translations": [{"text": "你好"}]})

    monkeypatch.setattr(translation_module.requests, "post", fake_post)

    result = service.translate_text_detailed("Hello", "zh", "en")

    assert result["status"] == "completed"
    assert result["content"] == "你好"
    assert result["providers"] == ["deepl"]
    assert len(requests) == 1
    assert requests[0][0].endswith("/translate")
    assert requests[0][1]["headers"]["Authorization"].startswith(
        "DeepL-Auth-Key "
    )


def test_legacy_openai_provider_uses_http_without_sdk(monkeypatch):
    service = build_service(
        monkeypatch,
        {
            "tokens.deepl.api_key": "",
            "tokens.openai": [
                {
                    "name": "gauss",
                    "api_key": "openai-key",
                    "api_endpoint": "https://example.test/v1/chat/completions",
                    "model": "test-model",
                }
            ],
            "translation.max_retries": 1,
            "translation.request_interval": 0,
            "translation.services": [
                {
                    "name": "openai_gauss",
                    "config_name": "gauss",
                    "enabled": True,
                    "priority": 1,
                }
            ],
        },
    )
    requests = []

    def fake_post(url, **kwargs):
        requests.append((url, kwargs))
        return FakeResponse(
            {"choices": [{"message": {"content": "你好"}}]}
        )

    monkeypatch.setattr(translation_module.requests, "post", fake_post)

    result = service.translate_text_detailed("Hello", "zh", "en")

    assert result["status"] == "completed"
    assert result["content"] == "你好"
    assert result["providers"] == ["openai_gauss"]
    assert requests[0][0] == "https://example.test/v1/chat/completions"
    assert requests[0][1]["json"]["model"] == "test-model"


def test_srt_partial_translation_never_returns_mixed_content(monkeypatch):
    service = build_service(
        monkeypatch,
        {
            "tokens.deepl.api_key": "",
            "tokens.openai": [],
            "translation.max_retries": 1,
            "translation.request_interval": 0,
        },
    )
    results = iter(
        [
            service._translation_result(
                "completed",
                "zh",
                "en",
                content="第一句",
                providers=["deepl"],
                total_segments=1,
                translated_segments=1,
            ),
            service._translation_result(
                "failed",
                "zh",
                "en",
                total_segments=1,
                failed_segments=1,
                error="provider_failed",
            ),
        ]
    )
    monkeypatch.setattr(
        service,
        "translate_text_detailed",
        lambda *_args, **_kwargs: next(results),
    )
    content = (
        "1\n00:00:00,000 --> 00:00:01,000\nFirst\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nSecond"
    )

    result = service.translate_subtitle_content_detailed(content, "zh", "en")

    assert result["status"] == "partial"
    assert result["content"] is None
    assert result["translated_segments"] == 1
    assert result["failed_segments"] == 1
    monkeypatch.setattr(
        service,
        "translate_subtitle_content_detailed",
        lambda *_args, **_kwargs: result,
    )
    assert service.translate_subtitle_content(content, "zh", "en") is None


def test_deeplx_unavailable_check_is_cached(monkeypatch):
    service = build_service(
        monkeypatch,
        {
            "tokens.deepl.api_key": "",
            "tokens.openai": [],
            "translation.max_retries": 1,
            "translation.request_interval": 0,
            "translation.deeplx_cooldown_seconds": 300,
            "translation.services": [
                {"name": "deeplx", "enabled": True, "priority": 1}
            ],
        },
    )
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({}, status_code=503)

    monkeypatch.setattr(translation_module.requests, "get", fake_get)

    assert service.translate_text("one", "zh", "en") is None
    assert service.translate_text("two", "zh", "en") is None
    assert len(calls) == 1


def test_explicitly_disabled_provider_list_does_not_reenable_fallbacks(monkeypatch):
    service = build_service(
        monkeypatch,
        {
            "tokens.deepl.api_key": "deepl-key",
            "tokens.openai": [],
            "translation.services": [
                {"name": "deepl_api", "enabled": False, "priority": 1},
                {"name": "deeplx", "enabled": False, "priority": 2},
            ],
            "translation.max_retries": 1,
            "translation.request_interval": 0,
        },
    )

    assert service.provider_specs == []
    result = service.translate_text_detailed("Hello", "zh", "en")
    assert result["status"] == "failed"
    assert result["error"] == "chunk_1_failed"
