"""Reader presence lookup for YouTube pages."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse

from ..utils.video_utils import extract_youtube_video_id


logger = logging.getLogger(__name__)

YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
READER_ID_URL_FIELDS = (
    ("readwise_fallback_article_id", "readwise_fallback_url"),
    ("readwise_article_id", "readwise_url"),
    ("readwise_url_only_article_id", "readwise_url_only_url"),
)


class YouTubeReaderStatusService:
    """Resolve whether a YouTube video currently exists in Reader."""

    def __init__(
        self,
        *,
        file_service,
        readwise_service,
        status_cache_ttl_seconds: Optional[float] = None,
        library_cache_ttl_seconds: Optional[float] = None,
        library_max_pages: Optional[int] = None,
        library_page_interval_seconds: Optional[float] = None,
        library_request_retry_delay_seconds: Optional[float] = None,
        library_request_max_retries: Optional[int] = None,
        library_cache_path: Optional[str] = None,
        async_library_refresh: bool = True,
        clock=time.monotonic,
    ) -> None:
        self.file_service = file_service
        self.readwise_service = readwise_service
        self.status_cache_ttl_seconds = _positive_float(
            status_cache_ttl_seconds,
            os.getenv("YOUTUBE_READER_STATUS_CACHE_TTL_SECONDS"),
            300.0,
        )
        self.library_cache_ttl_seconds = _positive_float(
            library_cache_ttl_seconds,
            os.getenv("YOUTUBE_READER_LIBRARY_CACHE_TTL_SECONDS"),
            21600.0,
        )
        self.library_max_pages = _positive_int(
            library_max_pages,
            os.getenv("YOUTUBE_READER_LIBRARY_MAX_PAGES"),
            100,
        )
        self.library_page_interval_seconds = _nonnegative_float(
            library_page_interval_seconds,
            os.getenv("YOUTUBE_READER_LIBRARY_PAGE_INTERVAL_SECONDS"),
            3.25,
        )
        self.library_request_retry_delay_seconds = _nonnegative_float(
            library_request_retry_delay_seconds,
            os.getenv("YOUTUBE_READER_LIBRARY_RETRY_DELAY_SECONDS"),
            30.0,
        )
        self.library_request_max_retries = _nonnegative_int(
            library_request_max_retries,
            os.getenv("YOUTUBE_READER_LIBRARY_MAX_RETRIES"),
            2,
        )
        self.library_cache_path = library_cache_path or os.getenv(
            "YOUTUBE_READER_LIBRARY_CACHE_PATH"
        ) or _default_library_cache_path(file_service)
        self._clock = clock
        self._async_library_refresh = bool(async_library_refresh)
        self._lock = threading.RLock()
        self._status_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._library_index: Dict[str, Dict[str, Any]] = {}
        self._library_index_status = "unavailable"
        self._library_index_reason = "reader_index_not_loaded"
        self._library_cache_expires_at = 0.0
        self._library_refresh_in_progress = False
        self._load_persisted_library_index()

    def get_status(
        self, video_id: str, *, force_refresh: bool = False
    ) -> Dict[str, Any]:
        normalized_video_id = str(video_id or "").strip()
        if not YOUTUBE_VIDEO_ID_PATTERN.fullmatch(normalized_video_id):
            raise ValueError("invalid YouTube video ID")

        with self._lock:
            now = self._clock()
            cached = self._status_cache.get(normalized_video_id)
            if not force_refresh and cached and cached[0] > now:
                cached_result = cached[1]
                local_candidate_now_exists = (
                    cached_result.get("status") == "not_saved"
                    and bool(self._local_reader_candidates(normalized_video_id))
                )
                if not local_candidate_now_exists:
                    return dict(cached_result)

            result = self._resolve_status(
                normalized_video_id,
                force_refresh=force_refresh,
            )
            if (
                result.get("status") == "unknown"
                and result.get("reason") == "reader_index_warming"
            ):
                self._status_cache.pop(normalized_video_id, None)
                return result

            ttl = self.status_cache_ttl_seconds
            if result.get("status") == "unknown":
                ttl = min(ttl, 30.0)
            elif result.get("status") == "not_saved":
                ttl = min(ttl, 60.0)
            self._status_cache[normalized_video_id] = (
                self._clock() + ttl,
                dict(result),
            )
            return result

    def invalidate(self, video_id: str) -> None:
        normalized_video_id = str(video_id or "").strip()
        with self._lock:
            self._status_cache.pop(normalized_video_id, None)

    def _resolve_status(
        self, video_id: str, *, force_refresh: bool
    ) -> Dict[str, Any]:
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not getattr(self.readwise_service, "enabled", False):
            return _unknown_status(
                video_id,
                checked_at=checked_at,
                reason="reader_not_configured",
            )

        local_candidates, excluded_document_ids = self._local_reader_state(video_id)
        unavailable_document_ids = set()
        for candidate in local_candidates:
            lookup = self.readwise_service.lookup_reader_document(
                candidate["reader_document_id"]
            )
            lookup_status = lookup.get("status")
            if lookup_status == "missing":
                excluded_document_ids.add(candidate["reader_document_id"])
                continue
            if lookup_status != "found":
                unavailable_document_ids.add(candidate["reader_document_id"])
                continue
            document = lookup.get("document") or {}
            return _saved_status(
                video_id,
                document,
                checked_at=checked_at,
                matched_by="local_task_document_id",
                task_id=candidate.get("task_id"),
                fallback_url=candidate.get("reader_url"),
            )

        library = self._reader_library_index(force_refresh=force_refresh)
        document = library.get("index", {}).get(video_id)
        if document:
            document_id = str(document.get("id") or "").strip()
            if document_id in unavailable_document_ids:
                return _unknown_status(
                    video_id,
                    checked_at=checked_at,
                    reason="reader_document_lookup_unavailable",
                )
            if document_id in excluded_document_ids:
                document = None

        if document:
            return _saved_status(
                video_id,
                document,
                checked_at=checked_at,
                matched_by="reader_source_url",
            )

        if library.get("status") != "complete":
            return _unknown_status(
                video_id,
                checked_at=checked_at,
                reason=library.get("reason") or "reader_lookup_unavailable",
            )

        return {
            "success": True,
            "video_id": video_id,
            "status": "not_saved",
            "saved": False,
            "reader_document_id": None,
            "reader_url": None,
            "checked_at": checked_at,
            "matched_by": None,
        }

    def _local_reader_candidates(self, video_id: str) -> list[Dict[str, Any]]:
        candidates, _ = self._local_reader_state(video_id)
        return candidates

    def _local_reader_state(
        self, video_id: str
    ) -> tuple[list[Dict[str, Any]], set[str]]:
        candidates: list[Dict[str, Any]] = []
        excluded_document_ids: set[str] = set()
        for task_id, task in (self.file_service.list_files() or {}).items():
            if not isinstance(task, dict) or _task_video_id(task) != video_id:
                continue

            deleted_document_id = str(
                task.get("readwise_deleted_article_id") or ""
            ).strip()
            if deleted_document_id:
                excluded_document_ids.add(deleted_document_id)
            for document_field, url_field in READER_ID_URL_FIELDS:
                document_id = str(task.get(document_field) or "").strip()
                if not document_id or document_id == deleted_document_id:
                    continue
                candidates.append(
                    {
                        "task_id": str(task_id),
                        "reader_document_id": document_id,
                        "reader_url": task.get(url_field),
                        "updated_at": task.get("status_updated_at")
                        or task.get("updated_time")
                        or task.get("created_time")
                        or "",
                    }
                )

        candidates.sort(
            key=lambda candidate: str(candidate.get("updated_at") or ""),
            reverse=True,
        )
        seen_document_ids = set()
        unique_candidates = []
        for candidate in candidates:
            document_id = candidate["reader_document_id"]
            if document_id in seen_document_ids:
                continue
            seen_document_ids.add(document_id)
            unique_candidates.append(candidate)
        return unique_candidates, excluded_document_ids

    def _reader_library_index(self, *, force_refresh: bool) -> Dict[str, Any]:
        now = self._clock()
        if (
            not force_refresh
            and not self._library_refresh_in_progress
            and self._library_cache_expires_at > now
        ):
            return {
                "status": self._library_index_status,
                "reason": self._library_index_reason,
                "index": self._library_index,
            }

        if self._async_library_refresh:
            if not self._library_refresh_in_progress:
                self._library_refresh_in_progress = True
                threading.Thread(
                    target=self._refresh_reader_library_index,
                    daemon=True,
                    name="youtube-reader-index-refresh",
                ).start()
            if self._library_index_status == "complete" and self._library_index:
                return {
                    "status": "partial",
                    "reason": "reader_index_warming",
                    "index": self._library_index,
                }
            return {
                "status": "unavailable",
                "reason": "reader_index_warming",
                "index": {},
            }

        return self._refresh_reader_library_index()

    def _refresh_reader_library_index(self) -> Dict[str, Any]:
        try:
            result = self._fetch_reader_library_index()
        except Exception:
            logger.exception("Reader YouTube index refresh crashed")
            result = {
                "status": "unavailable",
                "reason": "reader_list_failed",
                "index": {},
            }

        persist_index = None
        with self._lock:
            self._library_refresh_in_progress = False
            result_status = str(result.get("status") or "unavailable")
            if result_status == "complete":
                self._library_index = dict(result.get("index") or {})
                self._library_index_status = "complete"
                self._library_index_reason = None
                persist_index = dict(self._library_index)
            elif self._library_index_status != "complete":
                self._library_index = dict(result.get("index") or {})
                self._library_index_status = result_status
                self._library_index_reason = result.get("reason")
            else:
                logger.warning(
                    "Reader YouTube index refresh failed; keeping last-known-good cache: %s",
                    result.get("reason") or result_status,
                )

            ttl = self.library_cache_ttl_seconds
            if result_status != "complete":
                ttl = min(ttl, 30.0)
            self._library_cache_expires_at = self._clock() + ttl
        if persist_index is not None:
            self._persist_library_index(persist_index)
        return result

    def _fetch_reader_library_index(self) -> Dict[str, Any]:
        lookup = self.readwise_service.list_reader_documents(
            category="video",
            limit=100,
            max_pages=self.library_max_pages,
            page_interval_seconds=self.library_page_interval_seconds,
            request_retry_delay_seconds=self.library_request_retry_delay_seconds,
            max_request_retries=self.library_request_max_retries,
        )
        if lookup.get("status") not in {"complete", "partial"}:
            return {
                "status": "unavailable",
                "reason": lookup.get("reason") or "reader_list_failed",
                "index": {},
            }

        index: Dict[str, Dict[str, Any]] = {}
        for document in lookup.get("documents") or []:
            if not isinstance(document, dict) or document.get("parent_id"):
                continue
            document_video_id = _document_video_id(document)
            if not document_video_id:
                continue
            compact_document = _compact_reader_document(document)
            existing = index.get(document_video_id)
            if existing is None or _document_sort_key(
                compact_document
            ) > _document_sort_key(
                existing
            ):
                index[document_video_id] = compact_document

        is_complete = lookup.get("status") == "complete"
        logger.info(
            "Reader YouTube index refresh finished: status=%s pages=%s "
            "reader_documents=%s youtube_documents=%s",
            lookup.get("status"),
            lookup.get("pages_read"),
            len(lookup.get("documents") or []),
            len(index),
        )
        return {
            "status": "complete" if is_complete else "partial",
            "reason": None if is_complete else "reader_list_page_limit_reached",
            "index": index,
        }

    def _load_persisted_library_index(self) -> None:
        path = self.library_cache_path
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            raw_index = payload.get("index") if isinstance(payload, dict) else None
            if not isinstance(raw_index, dict):
                return
            index = {
                video_id: document
                for video_id, document in raw_index.items()
                if YOUTUBE_VIDEO_ID_PATTERN.fullmatch(str(video_id))
                and isinstance(document, dict)
            }
            self._library_index = index
            self._library_index_status = "complete"
            self._library_index_reason = None
            age_seconds = max(0.0, time.time() - os.path.getmtime(path))
            remaining_ttl = max(0.0, self.library_cache_ttl_seconds - age_seconds)
            self._library_cache_expires_at = self._clock() + remaining_ttl
            logger.info(
                "Loaded persisted Reader YouTube index: documents=%s age_seconds=%.1f",
                len(index),
                age_seconds,
            )
        except Exception as exc:
            logger.warning("Failed to load persisted Reader YouTube index: %s", exc)

    def _persist_library_index(self, index: Dict[str, Dict[str, Any]]) -> None:
        path = self.library_cache_path
        if not path:
            return
        directory = os.path.dirname(path) or "."
        temp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=directory,
                prefix="reader-youtube-index-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(
                    {
                        "version": 1,
                        "generated_at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                        "index": index,
                    },
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except Exception as exc:
            logger.warning("Failed to persist Reader YouTube index: %s", exc)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass


def _task_video_id(task: Dict[str, Any]) -> Optional[str]:
    direct_video_id = str(task.get("video_id") or "").strip()
    if direct_video_id:
        return direct_video_id

    video_info = task.get("video_info")
    candidate_urls: Iterable[Any] = (
        task.get("url"),
        video_info.get("webpage_url") if isinstance(video_info, dict) else None,
        video_info.get("url") if isinstance(video_info, dict) else None,
    )
    for candidate_url in candidate_urls:
        video_id = extract_youtube_video_id(candidate_url)
        if video_id:
            return video_id
    return None


def _document_video_id(document: Dict[str, Any]) -> Optional[str]:
    for field in ("source_url", "raw_source_url"):
        video_id = extract_youtube_video_id(document.get(field))
        if video_id:
            return video_id
    return None


def _document_sort_key(document: Dict[str, Any]) -> str:
    return str(
        document.get("updated_at")
        or document.get("saved_at")
        or document.get("created_at")
        or ""
    )


def _compact_reader_document(document: Dict[str, Any]) -> Dict[str, Any]:
    fields = (
        "id",
        "url",
        "source_url",
        "raw_source_url",
        "title",
        "updated_at",
        "saved_at",
        "created_at",
    )
    return {field: document.get(field) for field in fields if document.get(field)}


def _saved_status(
    video_id: str,
    document: Dict[str, Any],
    *,
    checked_at: str,
    matched_by: str,
    task_id: Optional[str] = None,
    fallback_url: Optional[str] = None,
) -> Dict[str, Any]:
    document_id = str(document.get("id") or "").strip()
    reader_url = _validated_reader_url(document.get("url")) or _validated_reader_url(
        fallback_url
    )
    if not reader_url and document_id:
        reader_url = f"https://read.readwise.io/read/{document_id}"
    return {
        "success": True,
        "video_id": video_id,
        "status": "saved",
        "saved": True,
        "reader_document_id": document_id or None,
        "reader_url": reader_url,
        "title": document.get("title"),
        "checked_at": checked_at,
        "matched_by": matched_by,
        "task_id": task_id,
    }


def _unknown_status(video_id: str, *, checked_at: str, reason: str) -> Dict[str, Any]:
    return {
        "success": True,
        "video_id": video_id,
        "status": "unknown",
        "saved": None,
        "reader_document_id": None,
        "reader_url": None,
        "checked_at": checked_at,
        "reason": reason,
    }


def _validated_reader_url(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme != "https":
        return None
    if parsed.hostname not in {"read.readwise.io", "readwise.io"}:
        return None
    return text


def _positive_float(
    explicit_value: Optional[float], env_value: Optional[str], default: float
) -> float:
    value = explicit_value if explicit_value is not None else env_value
    try:
        return max(float(value), 1.0) if value is not None else default
    except (TypeError, ValueError):
        return default


def _nonnegative_float(
    explicit_value: Optional[float], env_value: Optional[str], default: float
) -> float:
    value = explicit_value if explicit_value is not None else env_value
    try:
        return max(float(value), 0.0) if value is not None else default
    except (TypeError, ValueError):
        return default


def _positive_int(
    explicit_value: Optional[int], env_value: Optional[str], default: int
) -> int:
    value = explicit_value if explicit_value is not None else env_value
    try:
        return max(int(value), 1) if value is not None else default
    except (TypeError, ValueError):
        return default


def _nonnegative_int(
    explicit_value: Optional[int], env_value: Optional[str], default: int
) -> int:
    value = explicit_value if explicit_value is not None else env_value
    try:
        return max(int(value), 0) if value is not None else default
    except (TypeError, ValueError):
        return default


def _default_library_cache_path(file_service) -> Optional[str]:
    upload_folder = str(getattr(file_service, "upload_folder", "") or "").strip()
    if not upload_folder:
        return None
    return os.path.join(upload_folder, "cache", "reader-youtube-index-v1.json")
