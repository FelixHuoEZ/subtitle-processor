import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _load_app_module(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("tokens:\n  telegram: dummy\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_PATH", str(config_path))

    module_path = Path(__file__).resolve().parents[1] / "telegram-bot" / "app.py"
    spec = importlib.util.spec_from_file_location("telegram_bot_app", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    return _load_app_module(monkeypatch, tmp_path)


def test_format_task_display_prefers_title(app_module):
    task = {
        "url": "https://example.com/watch?v=123",
        "title": "My Sample Video",
        "uploader": "Sample Channel",
    }
    result = app_module._format_task_display(task)
    assert "My Sample Video" in result
    assert "Sample Channel" in result
    assert "example.com" in result


def test_format_task_display_falls_back_to_url(app_module):
    task = {
        "url": "https://example.com/watch?v=123",
        "title": "   ",
    }
    result = app_module._format_task_display(task)
    assert result == app_module._shorten_url(task["url"])


def test_format_task_display_uses_uploader_without_title(app_module):
    task = {
        "url": "https://example.com/watch?v=123",
        "uploader": "Sample Channel",
    }
    result = app_module._format_task_display(task)
    assert "Sample Channel" in result
    assert "example.com" in result


def test_update_active_task_metadata_sets_title(app_module):
    app_module.active_tasks.clear()
    app_module._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-1",
        url="https://example.com/watch?v=123",
        status="queued",
    )
    app_module._update_active_task_metadata(
        1, 2, "proc-1", title="Demo Title", uploader="Demo Channel"
    )
    tasks = app_module._list_active_tasks(1, 2)
    assert tasks[0]["title"] == "Demo Title"
    assert tasks[0]["uploader"] == "Demo Channel"


def test_queue_urls_returns_only_urls(app_module):
    app_module.active_tasks.clear()
    app_module._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-1",
        url="https://youtu.be/abc123",
        status="queued",
    )
    app_module._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-2",
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        status="failed",
    )

    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=2),
        message=SimpleNamespace(reply_text=reply_text, date=None),
    )

    asyncio.run(app_module.queue_urls(update, SimpleNamespace()))

    reply_text.assert_awaited_once_with(
        "https://youtu.be/abc123\nhttps://www.bilibili.com/video/BV1xx411c7mD",
        parse_mode=None,
    )


def test_queue_urls_returns_empty_message_when_no_tasks(app_module):
    app_module.active_tasks.clear()

    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=2),
        message=SimpleNamespace(reply_text=reply_text, date=None),
    )

    asyncio.run(app_module.queue_urls(update, SimpleNamespace()))

    reply_text.assert_awaited_once_with("当前没有正在处理的任务。")


def test_queue_clear_all_clears_all_visible_tasks(app_module):
    app_module.active_tasks.clear()
    app_module._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-1",
        url="https://youtu.be/abc123",
        status="queued",
    )
    app_module._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-2",
        url="https://www.bilibili.com/video/BV1xx411c7mD",
        status="failed",
    )

    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=2),
        message=SimpleNamespace(reply_text=reply_text, date=None),
    )

    asyncio.run(app_module.queue_clear_all(update, SimpleNamespace()))

    reply_text.assert_awaited_once_with(
        "已清空 2 条任务记录。不会取消后台正在处理的任务。"
    )
    assert app_module._list_active_tasks(1, 2) == []
