import importlib.util
from pathlib import Path

import pytest


def _load_app_module(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                "tokens:",
                "  telegram: dummy",
                "telegram:",
                "  prompt_flow:",
                "    require_location: false",
                "    require_tags: true",
                "    require_hotwords: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv(
        "PROMPT_FLOW_SETTINGS_PATH", str(tmp_path / "prompt_flow_settings.json")
    )

    module_path = Path(__file__).resolve().parents[1] / "telegram-bot" / "app.py"
    spec = importlib.util.spec_from_file_location("telegram_bot_app_prompt", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    return _load_app_module(monkeypatch, tmp_path)


def test_resolve_prompt_toggle_on(app_module):
    tags, hotwords, changed = app_module._resolve_prompt_toggle_arg("on", False, False)
    assert (tags, hotwords, changed) == (True, True, True)


def test_resolve_prompt_toggle_off(app_module):
    tags, hotwords, changed = app_module._resolve_prompt_toggle_arg("off", True, True)
    assert (tags, hotwords, changed) == (False, False, True)


def test_resolve_prompt_toggle_status(app_module):
    tags, hotwords, changed = app_module._resolve_prompt_toggle_arg(
        "status", True, False
    )
    assert (tags, hotwords, changed) == (True, False, False)


def test_resolve_prompt_toggle_default_flips_any_enabled(app_module):
    tags, hotwords, changed = app_module._resolve_prompt_toggle_arg(None, True, False)
    assert (tags, hotwords, changed) == (False, False, True)


def test_prompt_toggle_status_text(app_module):
    text = app_module._prompt_toggle_status_text(True, False)
    assert "标签输入：开启" in text
    assert "热词输入：关闭" in text


def test_prompt_flow_settings_default_written_on_load(monkeypatch, tmp_path):
    app_module = _load_app_module(monkeypatch, tmp_path)

    settings_path = tmp_path / "prompt_flow_settings.json"
    assert settings_path.exists()
    assert app_module.REQUIRE_LOCATION_SELECTION is False
    assert settings_path.read_text(encoding="utf-8").strip() == (
        "{\n"
        '  "require_location": false,\n'
        '  "require_tags": true,\n'
        '  "require_hotwords": true\n'
        "}"
    )


def test_prompt_flow_settings_file_overrides_config(monkeypatch, tmp_path):
    settings_path = tmp_path / "prompt_flow_settings.json"
    settings_path.write_text(
        "\n".join(
            [
                "{",
                '  "require_location": true,',
                '  "require_tags": false,',
                '  "require_hotwords": false',
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    app_module = _load_app_module(monkeypatch, tmp_path)

    assert app_module.REQUIRE_LOCATION_SELECTION is True
    assert app_module.REQUIRE_TAG_INPUT is False
    assert app_module.REQUIRE_HOTWORD_INPUT is False


def test_prompt_flow_settings_update_persists(monkeypatch, tmp_path):
    app_module = _load_app_module(monkeypatch, tmp_path)

    state = app_module.PROMPT_FLOW_SETTINGS_MANAGER.update_state(
        require_tags=False,
        require_hotwords=False,
    )
    app_module._apply_prompt_flow_state(state)

    reloaded = _load_app_module(monkeypatch, tmp_path)
    assert reloaded.REQUIRE_TAG_INPUT is False
    assert reloaded.REQUIRE_HOTWORD_INPUT is False
