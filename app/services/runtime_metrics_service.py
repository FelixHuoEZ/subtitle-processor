"""Persistent, content-free runtime metrics for operational reviews."""

import logging
import math
import os
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


class RuntimeMetricsService:
    """Store bounded operational events in Redis and aggregate them on demand."""

    def __init__(
        self,
        redis_client=None,
        key_prefix: str = "subtitle_processor",
        upload_folder: str = "/app/uploads",
        output_folder: str = "/app/outputs",
        enabled: Optional[bool] = None,
        max_events: Optional[int] = None,
    ):
        self.redis_client = redis_client
        self.key_prefix = (key_prefix or "subtitle_processor").strip()
        self.stream_key = f"{self.key_prefix}:runtime_metrics"
        self.start_sequence_key = f"{self.key_prefix}:service_start_sequence"
        self.upload_folder = upload_folder
        self.output_folder = output_folder
        self.enabled = (
            self._parse_bool_env("RUNTIME_METRICS_ENABLED", True)
            if enabled is None
            else bool(enabled)
        )
        self.enabled = self.enabled and self.redis_client is not None
        self.max_events = max(
            1000,
            max_events
            if max_events is not None
            else self._parse_int_env("RUNTIME_METRICS_MAX_EVENTS", 50000),
        )

        if not self.enabled:
            logger.info("持久化运行指标未启用或 Redis 不可用")

    @staticmethod
    def _parse_bool_env(key: str, default: bool) -> bool:
        raw = os.getenv(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _parse_int_env(key: str, default: int) -> int:
        try:
            return int(os.getenv(key, str(default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _serialize_fields(fields: Dict[str, Any]) -> Dict[str, str]:
        serialized = {}
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = int(value)
            serialized[str(key)] = str(value)
        return serialized

    def record_event(self, event: str, **fields: Any) -> bool:
        """Append one bounded event. Metrics failures never break task processing."""
        if not self.enabled:
            return False

        payload = self._serialize_fields(
            {
                "event": event,
                "recorded_at": time.time(),
                **fields,
            }
        )
        try:
            self.redis_client.xadd(
                self.stream_key,
                payload,
                maxlen=self.max_events,
                approximate=True,
            )
            return True
        except Exception as exc:
            logger.warning("写入持久化运行指标失败: %s", exc)
            return False

    def record_service_start(self) -> bool:
        if not self.enabled:
            return False
        try:
            sequence = self.redis_client.incr(self.start_sequence_key)
        except Exception as exc:
            logger.warning("更新服务启动序号失败: %s", exc)
            sequence = None
        return self.record_event("service_start", sequence=sequence)

    def record_download(
        self,
        outcome: str,
        duration_seconds: float,
        queue_state: Optional[Dict[str, Any]] = None,
        signals: Optional[Dict[str, Any]] = None,
    ) -> bool:
        queue_state = queue_state or {}
        signals = signals or {}
        return self.record_event(
            "download_final",
            outcome=outcome,
            duration_seconds=round(max(0.0, float(duration_seconds)), 3),
            queue_wait_seconds=round(
                max(0.0, float(queue_state.get("wait_seconds") or 0.0)), 3
            ),
            max_queue_position=max(
                0, int(queue_state.get("max_queue_position") or 0)
            ),
            active_downloads=max(
                0, int(queue_state.get("active_downloads") or 0)
            ),
            download_limit=max(0, int(queue_state.get("download_limit") or 0)),
            attempt_failures=max(0, int(signals.get("attempt_failures") or 0)),
            http_403=max(0, int(signals.get("http_403") or 0)),
            http_429=max(0, int(signals.get("http_429") or 0)),
            bot_challenge=max(0, int(signals.get("bot_challenge") or 0)),
            total_timeout=max(0, int(signals.get("total_timeout") or 0)),
            socket_timeout=max(0, int(signals.get("socket_timeout") or 0)),
        )

    def record_auto_restart_retry(self, outcome: str, status_code: int) -> bool:
        return self.record_event(
            "auto_restart_retry",
            outcome=outcome,
            status_code=int(status_code),
        )

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _percentile(values: Iterable[float], percentile: float) -> Optional[float]:
        ordered = sorted(float(value) for value in values)
        if not ordered:
            return None
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return round(ordered[index], 3)

    @classmethod
    def _distribution(cls, values: List[float]) -> Dict[str, Optional[float]]:
        return {
            "p50": cls._percentile(values, 0.50),
            "p75": cls._percentile(values, 0.75),
            "max": round(max(values), 3) if values else None,
        }

    def _read_events(self, start_epoch: float) -> List[Dict[str, str]]:
        if not self.enabled:
            return []
        minimum_id = f"{max(0, int(start_epoch * 1000))}-0"
        rows = self.redis_client.xrange(self.stream_key, min=minimum_id, max="+")
        events = []
        for stream_id, fields in rows:
            item = dict(fields)
            item["stream_id"] = stream_id
            events.append(item)
        return events

    def _current_start_sequence(self) -> Optional[int]:
        if not self.enabled:
            return None
        try:
            value = self.redis_client.get(self.start_sequence_key)
            return self._to_int(value) or None
        except Exception as exc:
            logger.warning("读取服务启动序号失败: %s", exc)
            return None

    @staticmethod
    def _directory_stats(path: str) -> Dict[str, Any]:
        stats = {"path": path, "exists": os.path.isdir(path), "bytes": 0, "files": 0}
        if not stats["exists"]:
            return stats

        stack = [path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                stats["bytes"] += entry.stat(follow_symlinks=False).st_size
                                stats["files"] += 1
                        except OSError:
                            continue
            except OSError:
                continue
        return stats

    def _disk_summary(self) -> Dict[str, Any]:
        result = {
            "temp": self._directory_stats(os.path.join(self.upload_folder, "temp")),
            "cache": self._directory_stats(os.path.join(self.upload_folder, "cache")),
            "outputs": self._directory_stats(self.output_folder),
        }
        try:
            usage = shutil.disk_usage(self.upload_folder)
            result["filesystem"] = {
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            }
        except OSError as exc:
            result["filesystem"] = {"error": str(exc)}
        return result

    def get_summary(self, hours: float = 24.0) -> Dict[str, Any]:
        """Aggregate the requested window without reading task bodies or URLs."""
        hours = min(168.0, max(1.0, float(hours)))
        end_epoch = time.time()
        start_epoch = end_epoch - hours * 3600
        read_error = None
        try:
            events = self._read_events(start_epoch)
        except Exception as exc:
            logger.warning("读取持久化运行指标失败: %s", exc)
            events = []
            read_error = str(exc)

        starts = [item for item in events if item.get("event") == "service_start"]
        downloads = [item for item in events if item.get("event") == "download_final"]
        retries = [
            item for item in events if item.get("event") == "auto_restart_retry"
        ]

        outcomes = {"success": 0, "failure": 0, "cache_hit": 0}
        for item in downloads:
            outcome = item.get("outcome")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

        retry_outcomes: Dict[str, int] = {}
        for item in retries:
            outcome = item.get("outcome", "unknown")
            retry_outcomes[outcome] = retry_outcomes.get(outcome, 0) + 1

        active_downloads = [
            self._to_int(item.get("active_downloads")) for item in downloads
        ]
        limits = [self._to_int(item.get("download_limit")) for item in downloads]
        queue_positions = [
            self._to_int(item.get("max_queue_position")) for item in downloads
        ]
        queue_waits = [
            self._to_float(item.get("queue_wait_seconds"))
            for item in downloads
            if item.get("outcome") != "cache_hit"
        ]
        durations = [
            self._to_float(item.get("duration_seconds"))
            for item in downloads
            if item.get("outcome") != "cache_hit"
        ]
        signal_names = (
            "attempt_failures",
            "http_403",
            "http_429",
            "bot_challenge",
            "total_timeout",
            "socket_timeout",
        )
        signal_counts = {
            name: sum(self._to_int(item.get(name)) for item in downloads)
            for name in signal_names
        }
        recorded_times = [
            self._to_float(item.get("recorded_at"))
            for item in events
            if self._to_float(item.get("recorded_at")) > 0
        ]
        completed_downloads = outcomes.get("success", 0) + outcomes.get("cache_hit", 0)

        return {
            "available": self.enabled and read_error is None,
            "persistent": self.enabled,
            "error": read_error,
            "window": {
                "hours": hours,
                "start": datetime.fromtimestamp(start_epoch, timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(end_epoch, timezone.utc).isoformat(),
                "event_count": len(events),
                "first_event_at": (
                    datetime.fromtimestamp(min(recorded_times), timezone.utc).isoformat()
                    if recorded_times
                    else None
                ),
                "last_event_at": (
                    datetime.fromtimestamp(max(recorded_times), timezone.utc).isoformat()
                    if recorded_times
                    else None
                ),
            },
            "service": {
                "starts": len(starts),
                "latest_start_sequence": self._current_start_sequence(),
            },
            "downloads": {
                "final_results": len(downloads),
                "outcomes": outcomes,
                "success_rate": (
                    round(completed_downloads / len(downloads), 4)
                    if downloads
                    else None
                ),
                "signals": signal_counts,
                "duration_seconds": self._distribution(durations),
                "queue_wait_seconds": self._distribution(queue_waits),
                "max_active_downloads": max(active_downloads, default=0),
                "max_download_limit": max(limits, default=0),
                "max_queue_position": max(queue_positions, default=0),
            },
            "auto_restart_retry": {
                "total": len(retries),
                "outcomes": retry_outcomes,
            },
            "disk": self._disk_summary(),
        }
