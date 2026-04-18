import importlib.util
from pathlib import Path

import pytest


def _load_app_module(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("tokens:\n  telegram: dummy\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_PATH", str(config_path))

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
