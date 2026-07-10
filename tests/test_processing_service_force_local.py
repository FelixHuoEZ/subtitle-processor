from app.services.processing_service import ProcessingService
from app.services.subtitle_service import SubtitleService


class FakeFileService:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.files = {}
        self.claims = {}

    def get_file_info(self, file_id):
        return self.files.get(file_id)

    def update_file_info(self, file_id, updates):
        existing = self.files.setdefault(file_id, {})
        existing.update(updates)

    def save_file(self, content, filename):
        path = self.tmp_path / filename
        path.write_text(content, encoding="utf-8")
        return str(path)

    def claim_task_operation(self, file_id, operation, ttl_seconds=7200):
        key = (file_id, operation)
        if key in self.claims:
            return None
        token = f"claim-{file_id}-{operation}"
        self.claims[key] = token
        return token

    def release_task_operation(self, file_id, operation, owner_token):
        key = (file_id, operation)
        if self.claims.get(key) != owner_token:
            return False
        del self.claims[key]
        return True


class FakeVideoService:
    def __init__(self, events=None):
        self.calls = []
        self.cleaned = []
        self.events = events if events is not None else []

    def process_video_for_transcription(self, url, platform, force_local_processing=False):
        self.events.append("video")
        self.calls.append(
            {
                "url": url,
                "platform": platform,
                "force_local_processing": force_local_processing,
            }
        )
        return {
            "video_info": {
                "title": "Demo Video",
                "webpage_url": url,
                "uploader": "Demo Channel",
            },
            "language": "zh",
            "language_details": {"language": "zh", "confidence": 0.95},
            "content_locale": "zh",
            "content_locale_details": {"language": "zh", "confidence": 0.95},
            "subtitle_content": (
                "1\n"
                "00:00:00,000 --> 00:00:02,000\n"
                "这是一段本地字幕。\n"
            ),
            "subtitle_metadata": {"track_type": "asr_original"},
            "audio_file": None,
            "temp_dir": "/tmp/subtitle-force-local-test",
            "needs_transcription": False,
            "readwise_mode": "url_only",
            "readwise_reason": "original_zh_track_available",
            "readwise_url_only": True,
            "skip_processing_for_url_only": True,
            "spoken_pattern": None,
        }

    def cleanup_task_artifacts(self, temp_dir):
        self.cleaned.append(temp_dir)


class FakeReadwiseService:
    def __init__(self, events=None, delete_result=True):
        self.payloads = []
        self.deleted = []
        self.events = events if events is not None else []
        self.delete_result = delete_result

    def delete_article(self, article_id):
        self.events.append("delete")
        self.deleted.append(article_id)
        return self.delete_result

    def create_article_from_subtitle(self, payload):
        self.events.append("create")
        self.payloads.append(dict(payload))
        return {"id": "full-text-id", "url": "https://read.readwise.io/read/full-text-id"}


class AutoFallbackReadwiseService:
    def __init__(self, events):
        self.events = events
        self.payloads = []
        self.deleted = []
        self.create_count = 0

    def create_article_from_subtitle(self, payload):
        self.create_count += 1
        self.payloads.append(dict(payload))
        if self.create_count == 1:
            self.events.append("create_url")
            return {
                "id": "url-only-id",
                "url": "https://read.readwise.io/read/url-only-id",
            }
        self.events.append("create_full")
        return {
            "id": "full-text-id",
            "url": "https://read.readwise.io/read/full-text-id",
        }

    def check_reader_parse_result(self, article_id):
        self.events.append("check")
        return {
            "status": "failed",
            "reason": "youtube_subtitles_unavailable",
            "message": "Readwise Reader 未能从 YouTube 抓到字幕。",
            "checked_at": "2026-07-06T23:00:00",
        }

    def delete_article(self, article_id):
        self.events.append("delete")
        self.deleted.append(article_id)
        return True


def test_force_local_retry_preserves_url_only_item_and_sends_full_text(tmp_path):
    file_service = FakeFileService(tmp_path)
    events = []
    video_service = FakeVideoService(events)
    readwise_service = FakeReadwiseService(events)
    service = ProcessingService(
        file_service=file_service,
        video_service=video_service,
        transcription_service=None,
        subtitle_service=SubtitleService(),
        readwise_service=readwise_service,
    )
    file_service.files["task-1"] = {
        "id": "task-1",
        "url": "https://www.youtube.com/watch?v=demo",
        "platform": "youtube",
        "status": "readwise_parse_failed",
        "readwise_article_id": "url-only-id",
        "readwise_url": "https://read.readwise.io/read/url-only-id",
        "readwise_url_only": True,
        "skip_processing_for_url_only": True,
        "tags": ["youtube"],
    }

    result = service.retry_readwise_with_local_content("task-1")
    stored = file_service.files["task-1"]

    assert result["success"] is True
    assert events == ["delete", "video", "create"]
    assert readwise_service.deleted == ["url-only-id"]
    assert video_service.calls[0]["force_local_processing"] is True
    assert video_service.cleaned == ["/tmp/subtitle-force-local-test"]
    assert readwise_service.payloads[0]["readwise_mode"] == "full_text"
    assert readwise_service.payloads[0]["readwise_url_only"] is False
    assert readwise_service.payloads[0]["skip_processing_for_url_only"] is False
    assert stored["status"] == "completed"
    assert stored["readwise_url_only_article_id"] == "url-only-id"
    assert stored["readwise_url_only_url"] == "https://read.readwise.io/read/url-only-id"
    assert stored["readwise_url_only_delete_status"] == "deleted"
    assert stored["readwise_deleted_article_id"] == "url-only-id"
    assert stored["readwise_fallback_from_article_id"] == "url-only-id"
    assert stored["readwise_fallback_article_id"] == "full-text-id"
    assert stored["readwise_parse_status"] == "recovered"
    fallback_run = stored["progress_runs"][-1]
    assert fallback_run["path"] == "fallback"
    assert fallback_run["status"] == "completed"
    assert [stage["code"] for stage in fallback_run["stages"]] == [
        "delete_url_only",
        "download_prepare",
        "normalize_subtitles",
        "send_readwise",
    ]


def test_url_only_parse_failure_auto_fallbacks_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("READWISE_AUTO_FALLBACK_ON_PARSE_FAILED", "true")
    file_service = FakeFileService(tmp_path)
    events = []
    video_service = FakeVideoService(events)
    readwise_service = AutoFallbackReadwiseService(events)
    service = ProcessingService(
        file_service=file_service,
        video_service=video_service,
        transcription_service=None,
        subtitle_service=SubtitleService(),
        readwise_service=readwise_service,
    )
    task_info = {
        "id": "task-1",
        "url": "https://www.youtube.com/watch?v=demo",
        "platform": "youtube",
        "readwise_mode": "url_only",
        "readwise_url_only": True,
        "skip_processing_for_url_only": True,
        "video_info": {
            "title": "Demo Video",
            "webpage_url": "https://www.youtube.com/watch?v=demo",
        },
    }
    file_service.files["task-1"] = dict(task_info)

    service._handle_url_only_readwise("task-1", task_info)
    stored = file_service.files["task-1"]

    assert events == ["create_url", "check", "delete", "video", "create_full"]
    assert readwise_service.deleted == ["url-only-id"]
    assert stored["status"] == "completed"
    assert stored["readwise_auto_fallback_enabled"] is True
    assert stored["readwise_auto_fallback_requested_at"]
    assert stored["readwise_url_only_delete_status"] == "deleted"
    assert stored["readwise_fallback_from_article_id"] == "url-only-id"
    assert stored["readwise_fallback_article_id"] == "full-text-id"
    assert task_info["readwise_article_id"] == "full-text-id"
    assert stored["progress_runs"][-1]["path"] == "fallback"
    assert stored["progress_runs"][-1]["status"] == "completed"


def test_url_only_parse_failure_waits_for_manual_fallback_by_default(tmp_path):
    file_service = FakeFileService(tmp_path)
    events = []
    video_service = FakeVideoService(events)
    readwise_service = AutoFallbackReadwiseService(events)
    service = ProcessingService(
        file_service=file_service,
        video_service=video_service,
        transcription_service=None,
        subtitle_service=SubtitleService(),
        readwise_service=readwise_service,
    )
    task_info = {
        "id": "task-1",
        "url": "https://www.youtube.com/watch?v=demo",
        "platform": "youtube",
        "readwise_mode": "url_only",
        "readwise_url_only": True,
        "skip_processing_for_url_only": True,
        "video_info": {
            "title": "Demo Video",
            "webpage_url": "https://www.youtube.com/watch?v=demo",
        },
    }
    file_service.files["task-1"] = dict(task_info)

    service._handle_url_only_readwise("task-1", task_info)
    stored = file_service.files["task-1"]

    assert events == ["create_url", "check"]
    assert video_service.calls == []
    assert readwise_service.deleted == []
    assert stored["status"] == "readwise_parse_failed"
    assert stored["readwise_auto_fallback_enabled"] is False
    assert stored["force_local_readwise_available"] is True
    url_only_run = stored["progress_runs"][-1]
    assert url_only_run["path"] == "url_only"
    assert [stage["code"] for stage in url_only_run["stages"]] == [
        "send_readwise",
        "verify_readwise",
    ]


def test_force_local_retry_stops_when_url_only_delete_fails(tmp_path):
    file_service = FakeFileService(tmp_path)
    events = []
    video_service = FakeVideoService(events)
    readwise_service = FakeReadwiseService(events, delete_result=False)
    service = ProcessingService(
        file_service=file_service,
        video_service=video_service,
        transcription_service=None,
        subtitle_service=SubtitleService(),
        readwise_service=readwise_service,
    )
    file_service.files["task-1"] = {
        "id": "task-1",
        "url": "https://www.youtube.com/watch?v=demo",
        "platform": "youtube",
        "status": "readwise_parse_failed",
        "readwise_article_id": "url-only-id",
        "readwise_url": "https://read.readwise.io/read/url-only-id",
        "readwise_url_only": True,
        "skip_processing_for_url_only": True,
    }

    result = service.retry_readwise_with_local_content("task-1")
    stored = file_service.files["task-1"]

    assert result["success"] is False
    assert events == ["delete"]
    assert video_service.calls == []
    assert readwise_service.payloads == []
    assert stored["status"] == "failed"
    assert stored["readwise_error"] == "readwise_url_only_delete_failed"
    assert stored["readwise_url_only_delete_status"] == "failed"
