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


def test_queue_status_refresh_removes_completed_tasks(app_module, monkeypatch):
    app_module.active_tasks.clear()
    app_module._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-1",
        url="https://youtu.be/abc123",
        status="processing",
    )

    class DummyResponse:
        status_code = 200
        text = "{'status': 'completed'}"

        def json(self):
            return {
                "status": "completed",
                "video_info": {"title": "Done Title", "uploader": "Done Channel"},
            }

    monkeypatch.setattr(app_module.requests, "get", lambda *args, **kwargs: DummyResponse())

    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=2),
        message=SimpleNamespace(reply_text=reply_text, date=None),
    )

    asyncio.run(app_module.queue_status(update, SimpleNamespace()))

    reply_text.assert_awaited_once_with("当前没有正在处理的任务。")
    assert app_module._list_active_tasks(1, 2) == []


def test_queue_urls_returns_only_urls(app_module, monkeypatch):
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

    class DummyResponse:
        status_code = 200
        text = "{'status': 'queued'}"

        def json(self):
            return {"status": "queued"}

    monkeypatch.setattr(app_module.requests, "get", lambda *args, **kwargs: DummyResponse())

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


def test_monitor_process_completion_reschedules_after_timeout(app_module, monkeypatch):
    app_module.active_tasks.clear()
    app_module._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-1",
        url="https://youtu.be/abc123",
        status="processing",
        message_id=99,
    )

    class DummyResponse:
        status_code = 200
        text = "{'status': 'processing'}"

        def json(self):
            return {"status": "processing", "progress": 42, "video_info": {}}

    monkeypatch.setattr(app_module.requests, "get", lambda *args, **kwargs: DummyResponse())

    rescheduled = {"called": False}

    def fake_schedule_background_task(context, coro):
        rescheduled["called"] = True
        coro.close()
        return None

    monkeypatch.setattr(app_module, "schedule_background_task", fake_schedule_background_task)

    bot = SimpleNamespace(edit_message_text=AsyncMock())
    context = SimpleNamespace(bot=bot)

    asyncio.run(
        app_module.monitor_process_completion(
            context,
            user_id=1,
            chat_id=2,
            message_id=99,
            process_id="proc-1",
            poll_interval=0,
            max_attempts=1,
        )
    )

    assert rescheduled["called"] is True
    bot.edit_message_text.assert_awaited_once_with(
        "⏳ 处理时间超过预期，但仍在后台处理中。请稍后使用 /queue 或网页查询结果。",
        chat_id=2,
        message_id=99,
    )
    task = app_module._get_active_task(1, 2, "proc-1")
    assert task is not None
    assert task["status"] == "processing"
    assert task["timeout_notice_sent"] is True
    assert task["monitor_rounds"] == 2
