import importlib.util
from pathlib import Path

import pytest


def _load_app_module(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("tokens:\n  telegram: dummy\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_PATH", str(config_path))

    module_path = Path(__file__).resolve().parents[1] / "telegram-bot" / "app.py"
    spec = importlib.util.spec_from_file_location(
        "telegram_bot_app_language_confirmation", module_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    return _load_app_module(monkeypatch, tmp_path)


def test_language_confirmation_prompt_includes_corresponding_video_url(app_module):
    task = {"url": "https://www.youtube.com/watch?v=task-url"}
    confirmation = {
        "url": "https://www.youtube.com/watch?v=7R9H-EX6cnI",
        "suggested_language": "en",
        "suggested_confidence": 0.68,
        "content_locale": "zh",
        "reason": "low_spoken_confidence",
        "video_title": "再次改良英语 这能行吗",
    }

    prompt = app_module._build_language_confirmation_prompt(task, confirmation)

    assert "对应 URL：" in prompt
    assert "https://www.youtube.com/watch?v=7R9H-EX6cnI" in prompt
    assert "https://www.youtube.com/watch?v=task-url" not in prompt


def test_language_confirmation_keyboard_contains_zh_en_auto_buttons(app_module):
    keyboard = app_module._build_language_confirmation_keyboard("proc-123")
    buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert [button.text for button in buttons] == ["按中文处理", "按英文处理", "保持自动"]
    assert [button.callback_data for button in buttons] == [
        "langsel:proc-123:zh",
        "langsel:proc-123:en",
        "langsel:proc-123:auto",
    ]


@pytest.mark.parametrize(
    ("callback_data", "expected"),
    [
        ("langsel:proc-123:zh", ("proc-123", "zh")),
        ("langsel:proc-123:en", ("proc-123", "en")),
        ("langsel:proc-123:auto", ("proc-123", "auto")),
    ],
)
def test_parse_language_confirmation_callback_data_accepts_valid_values(
    app_module, callback_data, expected
):
    assert app_module._parse_language_confirmation_callback_data(callback_data) == expected


@pytest.mark.parametrize(
    "callback_data",
    [
        None,
        "",
        "langsel::zh",
        "langsel:proc-123",
        "langsel:proc-123:ja",
        "langsel:proc-123:zh:extra",
        "other:proc-123:zh",
    ],
)
def test_parse_language_confirmation_callback_data_rejects_invalid_values(
    app_module, callback_data
):
    assert app_module._parse_language_confirmation_callback_data(callback_data) is None
