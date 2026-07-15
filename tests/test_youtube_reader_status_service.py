import threading
import time

from app.services.youtube_reader_status_service import YouTubeReaderStatusService


class FakeFileService:
    def __init__(self, tasks=None):
        self.tasks = tasks or {}

    def list_files(self):
        return self.tasks


class FakeReadwiseService:
    enabled = True

    def __init__(self, *, document_lookups=None, library_lookup=None):
        self.document_lookups = document_lookups or {}
        self.library_lookup = library_lookup or {
            "status": "complete",
            "documents": [],
        }
        self.document_calls = []
        self.list_calls = 0

    def lookup_reader_document(self, document_id):
        self.document_calls.append(document_id)
        return self.document_lookups.get(
            document_id,
            {"status": "missing", "document": None},
        )

    def list_reader_documents(self, **kwargs):
        self.list_calls += 1
        return self.library_lookup


def build_service(
    file_service,
    readwise_service,
    clock=lambda: 100.0,
    *,
    async_library_refresh=False,
    **service_kwargs,
):
    return YouTubeReaderStatusService(
        file_service=file_service,
        readwise_service=readwise_service,
        status_cache_ttl_seconds=300,
        library_cache_ttl_seconds=900,
        library_max_pages=5,
        async_library_refresh=async_library_refresh,
        clock=clock,
        **service_kwargs,
    )


def test_local_task_document_is_verified_before_reporting_saved():
    files = FakeFileService(
        {
            "task-1": {
                "video_id": "abcdefghijk",
                "readwise_article_id": "reader-1",
                "readwise_url": "https://read.readwise.io/read/reader-1",
                "updated_time": "2026-07-15T10:00:00+00:00",
            }
        }
    )
    readwise = FakeReadwiseService(
        document_lookups={
            "reader-1": {
                "status": "found",
                "document": {
                    "id": "reader-1",
                    "url": "https://read.readwise.io/read/reader-1",
                    "title": "Existing article",
                },
            }
        }
    )

    result = build_service(files, readwise).get_status("abcdefghijk")

    assert result["status"] == "saved"
    assert result["reader_url"] == "https://read.readwise.io/read/reader-1"
    assert result["matched_by"] == "local_task_document_id"
    assert result["task_id"] == "task-1"
    assert readwise.document_calls == ["reader-1"]
    assert readwise.list_calls == 0


def test_deleted_local_document_falls_through_to_complete_reader_index():
    files = FakeFileService(
        {
            "task-1": {
                "url": "https://youtu.be/abcdefghijk",
                "readwise_article_id": "reader-deleted",
            }
        }
    )
    readwise = FakeReadwiseService(
        document_lookups={
            "reader-deleted": {"status": "missing", "document": None}
        }
    )

    result = build_service(files, readwise).get_status("abcdefghijk")

    assert result["status"] == "not_saved"
    assert result["saved"] is False
    assert readwise.list_calls == 1


def test_missing_local_document_is_not_resurrected_by_stale_reader_index():
    files = FakeFileService(
        {
            "task-1": {
                "video_id": "abcdefghijk",
                "readwise_article_id": "reader-deleted",
            }
        }
    )
    readwise = FakeReadwiseService(
        document_lookups={
            "reader-deleted": {"status": "missing", "document": None}
        },
        library_lookup={
            "status": "complete",
            "documents": [
                {
                    "id": "reader-deleted",
                    "url": "https://read.readwise.io/read/reader-deleted",
                    "source_url": "https://youtu.be/abcdefghijk",
                }
            ],
        },
    )

    result = build_service(files, readwise).get_status("abcdefghijk")

    assert result["status"] == "not_saved"
    assert result["saved"] is False


def test_explicitly_deleted_document_is_not_restored_from_reader_index():
    files = FakeFileService(
        {
            "task-1": {
                "video_id": "abcdefghijk",
                "readwise_article_id": "reader-deleted",
                "readwise_deleted_article_id": "reader-deleted",
            }
        }
    )
    readwise = FakeReadwiseService(
        library_lookup={
            "status": "complete",
            "documents": [
                {
                    "id": "reader-deleted",
                    "url": "https://read.readwise.io/read/reader-deleted",
                    "source_url": "https://youtu.be/abcdefghijk",
                }
            ],
        },
    )

    result = build_service(files, readwise).get_status("abcdefghijk")

    assert result["status"] == "not_saved"
    assert result["saved"] is False
    assert readwise.document_calls == []


def test_reader_library_index_finds_articles_saved_outside_this_app():
    readwise = FakeReadwiseService(
        library_lookup={
            "status": "complete",
            "documents": [
                {
                    "id": "reader-external",
                    "url": "https://read.readwise.io/read/reader-external",
                    "source_url": "https://www.youtube.com/shorts/abcdefghijk",
                    "title": "External save",
                    "updated_at": "2026-07-15T10:00:00+00:00",
                }
            ],
        }
    )

    result = build_service(FakeFileService(), readwise).get_status("abcdefghijk")

    assert result["status"] == "saved"
    assert result["reader_document_id"] == "reader-external"
    assert result["matched_by"] == "reader_source_url"


def test_incomplete_reader_index_never_reports_not_saved():
    readwise = FakeReadwiseService(
        library_lookup={
            "status": "partial",
            "reason": "reader_list_page_limit_reached",
            "documents": [],
        }
    )

    result = build_service(FakeFileService(), readwise).get_status("abcdefghijk")

    assert result["status"] == "unknown"
    assert result["saved"] is None
    assert result["reason"] == "reader_list_page_limit_reached"


def test_cached_not_saved_is_rechecked_when_local_task_appears():
    files = FakeFileService()
    readwise = FakeReadwiseService()
    service = build_service(files, readwise)

    first = service.get_status("abcdefghijk")
    files.tasks["task-2"] = {
        "video_id": "abcdefghijk",
        "readwise_article_id": "reader-2",
    }
    readwise.document_lookups["reader-2"] = {
        "status": "found",
        "document": {
            "id": "reader-2",
            "url": "https://read.readwise.io/read/reader-2",
        },
    }
    second = service.get_status("abcdefghijk")

    assert first["status"] == "not_saved"
    assert second["status"] == "saved"
    assert readwise.document_calls == ["reader-2"]


def test_invalid_video_id_is_rejected():
    service = build_service(FakeFileService(), FakeReadwiseService())

    try:
        service.get_status("../bad")
    except ValueError as exc:
        assert str(exc) == "invalid YouTube video ID"
    else:
        raise AssertionError("invalid ID should fail")


def test_async_library_warming_is_not_cached_and_resolves_after_refresh():
    refresh_started = threading.Event()
    allow_refresh = threading.Event()

    class BlockingReadwiseService(FakeReadwiseService):
        def list_reader_documents(self, **kwargs):
            self.list_calls += 1
            refresh_started.set()
            assert allow_refresh.wait(timeout=1)
            return {
                "status": "complete",
                "documents": [
                    {
                        "id": "reader-async",
                        "url": "https://read.readwise.io/read/reader-async",
                        "source_url": "https://youtu.be/abcdefghijk",
                    }
                ],
            }

    readwise = BlockingReadwiseService()
    service = build_service(
        FakeFileService(),
        readwise,
        async_library_refresh=True,
    )

    warming = service.get_status("abcdefghijk")

    assert warming["status"] == "unknown"
    assert warming["reason"] == "reader_index_warming"
    assert "abcdefghijk" not in service._status_cache
    assert refresh_started.wait(timeout=1)

    allow_refresh.set()
    deadline = time.monotonic() + 1
    while service._library_refresh_in_progress and time.monotonic() < deadline:
        time.sleep(0.01)

    resolved = service.get_status("abcdefghijk")

    assert resolved["status"] == "saved"
    assert resolved["reader_url"] == (
        "https://read.readwise.io/read/reader-async"
    )
    assert readwise.list_calls == 1


def test_complete_library_index_is_persisted_and_reused(tmp_path):
    cache_path = tmp_path / "reader-index.json"
    reader_document = {
        "id": "reader-persisted",
        "url": "https://read.readwise.io/read/reader-persisted",
        "source_url": "https://www.youtube.com/watch?v=abcdefghijk",
    }
    first_readwise = FakeReadwiseService(
        library_lookup={"status": "complete", "documents": [reader_document]}
    )
    first_service = build_service(
        FakeFileService(),
        first_readwise,
        library_cache_path=str(cache_path),
    )

    first_result = first_service.get_status("abcdefghijk")

    assert first_result["status"] == "saved"
    assert cache_path.is_file()

    second_readwise = FakeReadwiseService(
        library_lookup={
            "status": "unavailable",
            "reason": "reader_list_request_failed",
            "documents": [],
        }
    )
    second_service = build_service(
        FakeFileService(),
        second_readwise,
        library_cache_path=str(cache_path),
    )

    second_result = second_service.get_status("abcdefghijk")

    assert second_result["status"] == "saved"
    assert second_result["reader_document_id"] == "reader-persisted"
    assert second_readwise.list_calls == 0
