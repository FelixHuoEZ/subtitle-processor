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
        incremental_refresh_seconds: Optional[float] = None,
        full_refresh_seconds: Optional[float] = None,
        incremental_overlap_seconds: Optional[float] = None,
        library_max_pages: Optional[int] = None,
        library_page_interval_seconds: Optional[float] = None,
        library_request_retry_delay_seconds: Optional[float] = None,
        library_request_max_retries: Optional[int] = None,
        library_cache_path: Optional[str] = None,
        async_library_refresh: bool = True,
        clock=time.monotonic,
        wall_clock=time.time,
    ) -> None:
        self.file_service = file_service
        self.readwise_service = readwise_service
        self.status_cache_ttl_seconds = _positive_float(
            status_cache_ttl_seconds,
            os.getenv("YOUTUBE_READER_STATUS_CACHE_TTL_SECONDS"),
            300.0,
        )
        legacy_refresh_seconds = library_cache_ttl_seconds
        if legacy_refresh_seconds is None:
            legacy_refresh_seconds = os.getenv(
                "YOUTUBE_READER_LIBRARY_CACHE_TTL_SECONDS"
            )
        legacy_refresh_default = _positive_float(
            legacy_refresh_seconds,
            None,
            1800.0,
        )
        self.incremental_refresh_seconds = _positive_float(
            incremental_refresh_seconds,
            os.getenv("YOUTUBE_READER_INCREMENTAL_REFRESH_SECONDS"),
            legacy_refresh_default,
        )
        self.full_refresh_seconds = _positive_float(
            full_refresh_seconds,
            os.getenv("YOUTUBE_READER_FULL_REFRESH_SECONDS"),
            86400.0,
        )
        self.incremental_overlap_seconds = _nonnegative_float(
            incremental_overlap_seconds,
            os.getenv("YOUTUBE_READER_INCREMENTAL_OVERLAP_SECONDS"),
            300.0,
        )
        # Retain the old attribute for callers that still pass the legacy TTL.
        self.library_cache_ttl_seconds = self.incremental_refresh_seconds
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
        self._wall_clock = wall_clock
        self._async_library_refresh = bool(async_library_refresh)
        self._lock = threading.RLock()
        self._status_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._library_documents: Dict[str, Dict[str, Any]] = {}
        self._library_index: Dict[str, Dict[str, Any]] = {}
        self._library_index_status = "unavailable"
        self._library_index_reason = "reader_index_not_loaded"
        self._incremental_refresh_due_at = 0.0
        self._full_refresh_due_at = 0.0
        self._last_incremental_sync_at: Optional[str] = None
        self._last_full_refresh_at: Optional[str] = None
        self._library_refresh_in_progress = False
        self._library_refresh_mode: Optional[str] = None
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
        refresh_mode = self._refresh_mode(
            force_refresh=force_refresh,
            now=now,
        )
        if refresh_mode is None and not self._library_refresh_in_progress:
            return {
                "status": self._library_index_status,
                "reason": self._library_index_reason,
                "index": self._library_index,
            }

        if self._async_library_refresh:
            if not self._library_refresh_in_progress:
                self._library_refresh_in_progress = True
                self._library_refresh_mode = refresh_mode
                threading.Thread(
                    target=self._refresh_reader_library_index,
                    args=(refresh_mode,),
                    daemon=True,
                    name="youtube-reader-index-refresh",
                ).start()
            if self._library_index_status == "complete":
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

        self._library_refresh_in_progress = True
        self._library_refresh_mode = refresh_mode
        return self._refresh_reader_library_index(refresh_mode)

    def _refresh_mode(self, *, force_refresh: bool, now: float) -> Optional[str]:
        if self._library_index_status != "complete":
            if force_refresh or now >= self._full_refresh_due_at:
                return "full"
            return None
        if now >= self._full_refresh_due_at:
            return "full"
        if force_refresh or now >= self._incremental_refresh_due_at:
            return "incremental"
        return None

    def _refresh_reader_library_index(self, refresh_mode: str) -> Dict[str, Any]:
        sync_started_at = _timestamp_to_iso(self._wall_clock())
        updated_after = None
        if refresh_mode == "incremental":
            updated_after = _iso_with_offset_seconds(
                self._last_incremental_sync_at,
                -self.incremental_overlap_seconds,
            )
        try:
            result = self._fetch_reader_library_index(
                refresh_mode=refresh_mode,
                updated_after=updated_after,
            )
        except Exception:
            logger.exception(
                "Reader YouTube index refresh crashed: mode=%s",
                refresh_mode,
            )
            result = {
                "status": "unavailable",
                "reason": "reader_list_failed",
                "index": {},
                "documents": {},
            }

        persist_payload = None
        with self._lock:
            self._library_refresh_in_progress = False
            self._library_refresh_mode = None
            result_status = str(result.get("status") or "unavailable")
            if result_status == "complete":
                fetched_documents = dict(result.get("documents") or {})
                if refresh_mode == "incremental":
                    self._library_documents = _merge_library_documents(
                        self._library_documents,
                        fetched_documents,
                    )
                else:
                    self._library_documents = fetched_documents
                self._library_index = _build_library_index(
                    self._library_documents.values()
                )
                self._library_index_status = "complete"
                self._library_index_reason = None
                self._last_incremental_sync_at = sync_started_at
                if refresh_mode == "full":
                    self._last_full_refresh_at = sync_started_at
                    self._full_refresh_due_at = (
                        self._clock() + self.full_refresh_seconds
                    )
                self._incremental_refresh_due_at = (
                    self._clock() + self.incremental_refresh_seconds
                )
                self._status_cache.clear()
                result = {
                    **result,
                    "index": dict(self._library_index),
                }
                persist_payload = (
                    dict(self._library_index),
                    dict(self._library_documents),
                    self._last_full_refresh_at,
                    self._last_incremental_sync_at,
                )
            elif self._library_index_status != "complete":
                self._library_documents = dict(result.get("documents") or {})
                self._library_index = dict(result.get("index") or {})
                self._library_index_status = result_status
                self._library_index_reason = result.get("reason")
            else:
                logger.warning(
                    "Reader YouTube index refresh failed; keeping last-known-good cache: %s",
                    result.get("reason") or result_status,
                )

            if result_status != "complete":
                retry_at = self._clock() + min(
                    self.incremental_refresh_seconds,
                    30.0,
                )
                self._incremental_refresh_due_at = retry_at
                if refresh_mode == "full":
                    self._full_refresh_due_at = retry_at
        if persist_payload is not None:
            self._persist_library_index(*persist_payload)
        return result

    def _fetch_reader_library_index(
        self,
        *,
        refresh_mode: str,
        updated_after: Optional[str],
    ) -> Dict[str, Any]:
        lookup = self.readwise_service.list_reader_documents(
            category="video",
            updated_after=updated_after,
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
                "documents": {},
            }

        documents: Dict[str, Dict[str, Any]] = {}
        for document in lookup.get("documents") or []:
            if not isinstance(document, dict) or document.get("parent_id"):
                continue
            document_id = str(document.get("id") or "").strip()
            if not document_id:
                continue
            compact_document = _compact_reader_document(document)
            documents[document_id] = compact_document

        index = _build_library_index(documents.values())

        is_complete = lookup.get("status") == "complete"
        logger.info(
            "Reader YouTube index refresh finished: mode=%s status=%s pages=%s "
            "reader_documents=%s youtube_documents=%s",
            refresh_mode,
            lookup.get("status"),
            lookup.get("pages_read"),
            len(lookup.get("documents") or []),
            len(index),
        )
        return {
            "status": "complete" if is_complete else "partial",
            "reason": None if is_complete else "reader_list_page_limit_reached",
            "index": index,
            "documents": documents,
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
            raw_documents = payload.get("documents")
            if isinstance(raw_documents, dict):
                documents = {
                    str(document_id): document
                    for document_id, document in raw_documents.items()
                    if str(document_id).strip() and isinstance(document, dict)
                }
            else:
                documents = {
                    str(document.get("id")): document
                    for document in index.values()
                    if str(document.get("id") or "").strip()
                }
            generated_at = str(payload.get("generated_at") or "").strip()
            fallback_timestamp = _timestamp_to_iso(os.path.getmtime(path))
            self._last_full_refresh_at = str(
                payload.get("last_full_refresh_at") or generated_at or fallback_timestamp
            )
            self._last_incremental_sync_at = str(
                payload.get("last_incremental_sync_at")
                or generated_at
                or fallback_timestamp
            )
            now_wall = self._wall_clock()
            self._library_index = index
            self._library_documents = documents
            self._library_index_status = "complete"
            self._library_index_reason = None
            incremental_remaining = _remaining_interval_seconds(
                self._last_incremental_sync_at,
                self.incremental_refresh_seconds,
                now_wall,
            )
            full_remaining = _remaining_interval_seconds(
                self._last_full_refresh_at,
                self.full_refresh_seconds,
                now_wall,
            )
            self._incremental_refresh_due_at = self._clock() + incremental_remaining
            self._full_refresh_due_at = self._clock() + full_remaining
            logger.info(
                "Loaded persisted Reader YouTube index: documents=%s "
                "incremental_due_seconds=%.1f full_due_seconds=%.1f",
                len(index),
                incremental_remaining,
                full_remaining,
            )
        except Exception as exc:
            logger.warning("Failed to load persisted Reader YouTube index: %s", exc)

    def _persist_library_index(
        self,
        index: Dict[str, Dict[str, Any]],
        documents: Dict[str, Dict[str, Any]],
        last_full_refresh_at: Optional[str],
        last_incremental_sync_at: Optional[str],
    ) -> None:
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
                        "version": 2,
                        "generated_at": _timestamp_to_iso(self._wall_clock()),
                        "last_full_refresh_at": last_full_refresh_at,
                        "last_incremental_sync_at": last_incremental_sync_at,
                        "documents": documents,
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


def _build_library_index(
    documents: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for document in documents:
        document_video_id = _document_video_id(document)
        if not document_video_id:
            continue
        existing = index.get(document_video_id)
        if existing is None or _document_sort_key(document) > _document_sort_key(
            existing
        ):
            index[document_video_id] = document
    return index


def _merge_library_documents(
    current: Dict[str, Dict[str, Any]],
    updates: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    merged = dict(current)
    for document_id, document in updates.items():
        merged.pop(document_id, None)
        if _document_video_id(document):
            merged[document_id] = document
    return merged


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


def _timestamp_to_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat(
        timespec="seconds"
    )


def _parse_iso_timestamp(value: Optional[str]) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso_with_offset_seconds(
    value: Optional[str],
    offset_seconds: float,
) -> Optional[str]:
    timestamp = _parse_iso_timestamp(value)
    if timestamp is None:
        return None
    return _timestamp_to_iso(timestamp + float(offset_seconds))


def _remaining_interval_seconds(
    last_completed_at: Optional[str],
    interval_seconds: float,
    now_timestamp: float,
) -> float:
    completed_timestamp = _parse_iso_timestamp(last_completed_at)
    if completed_timestamp is None:
        return 0.0
    age_seconds = max(0.0, float(now_timestamp) - completed_timestamp)
    return max(0.0, float(interval_seconds) - age_seconds)


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
