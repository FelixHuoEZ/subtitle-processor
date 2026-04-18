import importlib.util
from pathlib import Path


def _load_telegram_app():
    app_path = Path(__file__).resolve().parents[1] / "telegram-bot" / "app.py"
    spec = importlib.util.spec_from_file_location("telegram_app", app_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


telegram_app = _load_telegram_app()


def test_extract_video_urls_supports_multiple_separators():
    text = (
        "https://youtu.be/abc123, https://www.bilibili.com/video/BV1xx\n"
        "https://b23.tv/xyz https://youtu.be/abc123"
    )
    urls = telegram_app.extract_video_urls(text)
    assert urls == [
        "https://youtu.be/abc123",
        "https://www.bilibili.com/video/BV1xx",
        "https://b23.tv/xyz",
    ]


def test_extract_video_urls_ignores_non_video_text():
    text = "hello, world, example.com/not-video"
    assert telegram_app.extract_video_urls(text) == []


def test_shorten_url_keeps_tail_and_limits_length():
    url = "https://www.youtube.com/watch?v=" + "x" * 80
    shortened = telegram_app._shorten_url(url, max_length=50)
    assert shortened.startswith("https://www.you")
    assert shortened.endswith("x" * 14)
    assert len(shortened) <= 50


def test_active_task_tracking_lifecycle():
    telegram_app.active_tasks.clear()
    telegram_app._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-1",
        url="https://youtu.be/abc123",
        status="queued",
    )
    tasks = telegram_app._list_active_tasks(1, 2)
    assert len(tasks) == 1
    assert tasks[0]["status"] == "queued"

    telegram_app._update_active_task_status(1, 2, "proc-1", "processing")
    tasks = telegram_app._list_active_tasks(1, 2)
    assert tasks[0]["status"] == "processing"

    telegram_app._remove_active_task(1, 2, "proc-1")
    tasks = telegram_app._list_active_tasks(1, 2)
    assert tasks == []


def test_clear_failed_tasks_keeps_active_entries():
    telegram_app.active_tasks.clear()
    telegram_app._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-1",
        url="https://youtu.be/abc123",
        status="processing",
    )
    telegram_app._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-2",
        url="https://youtu.be/def456",
        status="failed",
    )
    cleared = telegram_app._clear_failed_tasks(1, 2)
    assert cleared == 1
    tasks = telegram_app._list_active_tasks(1, 2)
    assert len(tasks) == 1
    assert tasks[0]["status"] == "processing"


def test_clear_all_tasks_removes_every_entry():
    telegram_app.active_tasks.clear()
    telegram_app._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-1",
        url="https://youtu.be/abc123",
        status="processing",
    )
    telegram_app._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-2",
        url="https://youtu.be/def456",
        status="failed",
    )
    cleared = telegram_app._clear_all_tasks(1, 2)
    assert cleared == 2
    assert telegram_app._list_active_tasks(1, 2) == []


def test_register_active_task_records_metadata():
    telegram_app.active_tasks.clear()
    telegram_app._register_active_task(
        user_id=1,
        chat_id=2,
        process_id="proc-1",
        url="https://youtu.be/abc123",
        status="queued",
        location="archive",
        tags=["t1"],
        hotwords=["h1", "h2"],
    )
    tasks = telegram_app._list_active_tasks(1, 2)
    assert len(tasks) == 1
    assert tasks[0]["location"] == "archive"
    assert tasks[0]["tags"] == ["t1"]
    assert tasks[0]["hotwords"] == ["h1", "h2"]
