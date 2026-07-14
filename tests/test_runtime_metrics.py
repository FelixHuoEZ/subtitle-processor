from app.main import create_app
from app.services.runtime_metrics_service import RuntimeMetricsService


class _FakeRedis:
    def __init__(self):
        self.events = []
        self.values = {}

    def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def get(self, key):
        return self.values.get(key)

    def xadd(self, key, fields, maxlen=None, approximate=True):
        stream_id = f"{len(self.events) + 1}-0"
        self.events.append((stream_id, dict(fields)))
        return stream_id

    def xrange(self, key, min="-", max="+"):
        return list(self.events)


def test_runtime_metrics_aggregates_downloads_retries_and_disk(tmp_path):
    upload_folder = tmp_path / "uploads"
    output_folder = tmp_path / "outputs"
    (upload_folder / "temp").mkdir(parents=True)
    (upload_folder / "cache" / "media").mkdir(parents=True)
    output_folder.mkdir()
    (upload_folder / "temp" / "partial.bin").write_bytes(b"12345")
    (upload_folder / "cache" / "media" / "audio.wav").write_bytes(b"123")
    (output_folder / "subtitle.srt").write_bytes(b"12")

    redis_client = _FakeRedis()
    service = RuntimeMetricsService(
        redis_client=redis_client,
        upload_folder=str(upload_folder),
        output_folder=str(output_folder),
        enabled=True,
    )

    service.record_service_start()
    service.record_download(
        "success",
        100,
        queue_state={
            "wait_seconds": 10,
            "max_queue_position": 2,
            "active_downloads": 3,
            "download_limit": 3,
        },
        signals={"attempt_failures": 2, "http_403": 2},
    )
    service.record_download(
        "failure",
        200,
        queue_state={"wait_seconds": 20, "download_limit": 3},
        signals={"attempt_failures": 1, "http_429": 1, "total_timeout": 1},
    )
    service.record_download("cache_hit", 0.2)
    service.record_auto_restart_retry("scheduled", 202)
    service.record_auto_restart_retry("skipped", 409)

    summary = service.get_summary(hours=24)

    assert summary["available"] is True
    assert summary["service"] == {"starts": 1, "latest_start_sequence": 1}
    assert summary["downloads"]["final_results"] == 3
    assert summary["downloads"]["outcomes"] == {
        "success": 1,
        "failure": 1,
        "cache_hit": 1,
    }
    assert summary["downloads"]["success_rate"] == 0.6667
    assert summary["downloads"]["signals"]["http_403"] == 2
    assert summary["downloads"]["signals"]["http_429"] == 1
    assert summary["downloads"]["signals"]["total_timeout"] == 1
    assert summary["downloads"]["duration_seconds"] == {
        "p50": 100.0,
        "p75": 200.0,
        "max": 200.0,
    }
    assert summary["downloads"]["queue_wait_seconds"]["p75"] == 20.0
    assert summary["downloads"]["max_active_downloads"] == 3
    assert summary["downloads"]["max_queue_position"] == 2
    assert summary["auto_restart_retry"]["outcomes"] == {
        "scheduled": 1,
        "skipped": 1,
    }
    assert summary["disk"]["temp"]["bytes"] == 5
    assert summary["disk"]["cache"]["bytes"] == 3
    assert summary["disk"]["outputs"]["bytes"] == 2

    all_fields = [fields for _, fields in redis_client.events]
    assert not any("url" in fields or "content" in fields for fields in all_fields)


def test_runtime_metrics_endpoint_validates_window(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "json")
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                "app:",
                f"  upload_folder: {tmp_path / 'uploads'}",
                f"  output_folder: {tmp_path / 'outputs'}",
                "tokens:",
                "  readwise:",
                "    api_token: ''",
                "servers:",
                "  transcribe:",
                "    default_url: http://transcribe-audio:10095",
            ]
        ),
        encoding="utf-8",
    )
    app = create_app(str(config_path))
    client = app.test_client()

    response = client.get("/health/metrics?hours=24")
    invalid = client.get("/health/metrics?hours=0")

    assert response.status_code == 200
    assert response.get_json()["persistent"] is False
    assert invalid.status_code == 400
