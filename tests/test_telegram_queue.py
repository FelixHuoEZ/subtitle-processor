import importlib.util
import time
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


def test_healthcheck_tolerates_single_heartbeat_failure(monkeypatch):
    now = time.time()
    monkeypatch.setattr(telegram_app, "last_activity", now)
    monkeypatch.setattr(telegram_app, "is_bot_healthy", True)
    monkeypatch.setattr(telegram_app, "last_heartbeat_ok", False)
    monkeypatch.setattr(telegram_app, "last_heartbeat_at", now - 90)
    monkeypatch.setattr(telegram_app, "consecutive_heartbeat_failures", 1)
    monkeypatch.setattr(telegram_app, "heartbeat_unhealthy_since", 0.0)

    response = telegram_app.health_app.test_client().get("/health?deep=1")

    assert response.status_code == 200
    assert response.get_json()["status"] == "healthy"


def test_healthcheck_reports_sustained_heartbeat_failure(monkeypatch):
    now = time.time()
    monkeypatch.setattr(telegram_app, "last_activity", now)
    monkeypatch.setattr(telegram_app, "is_bot_healthy", False)
    monkeypatch.setattr(telegram_app, "last_heartbeat_ok", False)
    monkeypatch.setattr(telegram_app, "last_heartbeat_at", now - 90)
    monkeypatch.setattr(
        telegram_app,
        "consecutive_heartbeat_failures",
        telegram_app.TELEGRAM_HEARTBEAT_FAILURE_THRESHOLD,
    )
    monkeypatch.setattr(telegram_app, "heartbeat_unhealthy_since", now - 30)

    response = telegram_app.health_app.test_client().get("/health?deep=1")

    payload = response.get_json()
    assert response.status_code == 503
    assert payload["status"] == "unhealthy"
    assert "heartbeat_failed" in payload["reasons"]


def test_healthcheck_allows_initial_heartbeat_during_startup_grace(monkeypatch):
    now = time.time()
    monkeypatch.setattr(telegram_app, "last_activity", now)
    monkeypatch.setattr(telegram_app, "is_bot_healthy", True)
    monkeypatch.setattr(telegram_app, "last_heartbeat_ok", None)
    monkeypatch.setattr(telegram_app, "last_heartbeat_at", 0.0)
    monkeypatch.setattr(telegram_app, "heartbeat_unhealthy_since", 0.0)
    monkeypatch.setattr(
        telegram_app,
        "heartbeat_monitor_started_at",
        now - 60,
        raising=False,
    )
    monkeypatch.setattr(
        telegram_app,
        "TELEGRAM_HEARTBEAT_STARTUP_GRACE_SECONDS",
        300,
        raising=False,
    )

    response = telegram_app.health_app.test_client().get("/health?deep=1")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["status"] == "healthy"
    assert payload["time_since_heartbeat_sec"] is None
    assert "heartbeat_missing" not in payload["reasons"]


def test_healthcheck_rejects_missing_initial_heartbeat_after_grace(monkeypatch):
    now = time.time()
    monkeypatch.setattr(telegram_app, "last_activity", now)
    monkeypatch.setattr(telegram_app, "is_bot_healthy", True)
    monkeypatch.setattr(telegram_app, "last_heartbeat_ok", None)
    monkeypatch.setattr(telegram_app, "last_heartbeat_at", 0.0)
    monkeypatch.setattr(telegram_app, "heartbeat_unhealthy_since", 0.0)
    monkeypatch.setattr(
        telegram_app,
        "heartbeat_monitor_started_at",
        now - 301,
        raising=False,
    )
    monkeypatch.setattr(
        telegram_app,
        "TELEGRAM_HEARTBEAT_STARTUP_GRACE_SECONDS",
        300,
        raising=False,
    )

    response = telegram_app.health_app.test_client().get("/health?deep=1")

    payload = response.get_json()
    assert response.status_code == 503
    assert payload["status"] == "unhealthy"
    assert payload["time_since_heartbeat_sec"] is None
    assert payload["heartbeat_monitor_age_sec"] >= 300
    assert "heartbeat_missing" in payload["reasons"]


def test_healthcheck_only_mode_does_not_require_telegram_heartbeat(monkeypatch):
    now = time.time()
    monkeypatch.setattr(telegram_app, "TELEGRAM_ENABLED", False)
    monkeypatch.setattr(telegram_app, "last_activity", now)
    monkeypatch.setattr(telegram_app, "is_bot_healthy", True)
    monkeypatch.setattr(telegram_app, "last_heartbeat_ok", None)
    monkeypatch.setattr(telegram_app, "last_heartbeat_at", 0.0)
    monkeypatch.setattr(telegram_app, "heartbeat_unhealthy_since", 0.0)
    monkeypatch.setattr(telegram_app, "heartbeat_monitor_started_at", now - 3600)

    response = telegram_app.health_app.test_client().get("/health?deep=1")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["status"] == "healthy"
    assert "heartbeat_missing" not in payload["reasons"]
