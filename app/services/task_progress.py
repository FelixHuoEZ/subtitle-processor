"""Task stage tracking and history-based ETA estimation."""

import math
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional


MIN_HISTORY_SAMPLES = 10
RECENT_HISTORY_DAYS = 90
MAX_LEGACY_TASK_SECONDS = 6 * 60 * 60
ACTIVE_TASK_STATUSES = {"processing", "waiting_for_language_confirmation"}
TERMINAL_TASK_STATUSES = {
    "completed",
    "failed",
    "interrupted",
    "readwise_parse_failed",
}


@dataclass(frozen=True)
class StageDefinition:
    label: str
    duration_related: bool
    default_typical_seconds: float
    default_upper_seconds: float
    typical_realtime_factor: float = 0.0
    upper_realtime_factor: float = 0.0


STAGE_DEFINITIONS = {
    "download_prepare": StageDefinition(
        "下载与预处理", True, 45, 150, typical_realtime_factor=0.02, upper_realtime_factor=0.08
    ),
    "language_confirmation": StageDefinition("等待语言确认", False, 30, 180),
    "transcribe_audio": StageDefinition(
        "音频转录", True, 30, 120, typical_realtime_factor=0.06, upper_realtime_factor=0.18
    ),
    "generate_subtitles": StageDefinition(
        "生成字幕", True, 5, 20, typical_realtime_factor=0.002, upper_realtime_factor=0.008
    ),
    "normalize_subtitles": StageDefinition(
        "整理字幕", True, 3, 12, typical_realtime_factor=0.001, upper_realtime_factor=0.004
    ),
    "send_readwise": StageDefinition("发送到 Readwise", False, 8, 25),
    "verify_readwise": StageDefinition("确认 Reader 解析", False, 15, 35),
    "delete_url_only": StageDefinition("删除原 URL-only 文档", False, 5, 15),
}

UNKNOWN_STAGE_PLAN = [
    "download_prepare",
    "transcribe_audio",
    "generate_subtitles",
    "send_readwise",
    "verify_readwise",
]
UNKNOWN_CONDITIONAL_STAGES = [
    "transcribe_audio",
    "generate_subtitles",
    "verify_readwise",
]


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _elapsed_seconds(started_at: Any, finished_at: Any) -> Optional[float]:
    started = _parse_datetime(started_at)
    finished = _parse_datetime(finished_at)
    if not started or not finished:
        return None
    try:
        return max(0.0, (finished - started).total_seconds())
    except TypeError:
        return None


def _quantile(values: Iterable[float], percentile: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    index = int(math.floor((len(ordered) - 1) * percentile))
    return ordered[index]


def _media_duration_seconds(task_info: Dict[str, Any]) -> Optional[float]:
    video_info = task_info.get("video_info") or {}
    duration = video_info.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        return None
    return float(duration)


class TaskProgressEstimator:
    """Estimate stage and total durations from comparable completed tasks."""

    def __init__(self, tasks: Iterable[Dict[str, Any]], now: Callable[[], datetime]):
        self._now = now
        self._lock = threading.Lock()
        self._stage_samples = defaultdict(list)
        self._legacy_samples = defaultdict(list)
        for task in tasks:
            self._add_task(task)

    def _add_task(self, task: Dict[str, Any]) -> None:
        self._add_legacy_sample(task)
        for run in task.get("progress_runs") or []:
            self.add_run(run)

    def _add_legacy_sample(self, task: Dict[str, Any]) -> None:
        if task.get("status") != "completed":
            return
        media_seconds = _media_duration_seconds(task)
        elapsed = _elapsed_seconds(task.get("created_time"), task.get("updated_time"))
        if not media_seconds or not elapsed or elapsed > MAX_LEGACY_TASK_SECONDS:
            return
        needs_transcription = task.get("needs_transcription")
        if not isinstance(needs_transcription, bool):
            return
        self._legacy_samples[needs_transcription].append(
            {
                "duration_seconds": elapsed,
                "media_duration_seconds": media_seconds,
                "finished_at": task.get("updated_time"),
            }
        )

    def add_run(self, run: Dict[str, Any]) -> None:
        if run.get("status") != "completed":
            return
        with self._lock:
            for stage in run.get("stages") or []:
                if stage.get("status") != "completed":
                    continue
                duration = stage.get("duration_seconds")
                if not isinstance(duration, (int, float)) or duration <= 0:
                    continue
                self._stage_samples[stage.get("code")].append(dict(stage))

    def estimate_stage(
        self,
        stage_code: str,
        media_duration_seconds: Optional[float] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        definition = STAGE_DEFINITIONS[stage_code]
        samples = self._recent_comparable_samples(stage_code, context or {})
        if len(samples) >= MIN_HISTORY_SAMPLES:
            estimate = self._estimate_from_stage_samples(
                definition,
                samples,
                media_duration_seconds,
            )
            if estimate:
                return estimate
        return self._default_stage_estimate(definition, media_duration_seconds)

    def _recent_comparable_samples(
        self, stage_code: str, context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        samples = list(self._stage_samples.get(stage_code) or [])
        cutoff = self._now() - timedelta(days=RECENT_HISTORY_DAYS)
        recent = []
        for sample in samples:
            finished_at = _parse_datetime(sample.get("finished_at"))
            if finished_at and finished_at >= cutoff:
                recent.append(sample)
        samples = sorted(
            recent,
            key=lambda sample: _parse_datetime(sample.get("finished_at")) or datetime.min,
        )

        if stage_code == "download_prepare" and "cache_hit" in context:
            matching_cache = [
                sample
                for sample in samples
                if (sample.get("context") or {}).get("cache_hit") == context["cache_hit"]
            ]
            if len(matching_cache) >= MIN_HISTORY_SAMPLES:
                samples = matching_cache
        return samples[-200:]

    @staticmethod
    def _estimate_from_stage_samples(
        definition: StageDefinition,
        samples: List[Dict[str, Any]],
        media_duration_seconds: Optional[float],
    ) -> Optional[Dict[str, Any]]:
        if definition.duration_related and media_duration_seconds:
            ratios = [
                sample["duration_seconds"] / sample["media_duration_seconds"]
                for sample in samples
                if isinstance(sample.get("media_duration_seconds"), (int, float))
                and sample["media_duration_seconds"] > 0
            ]
            if len(ratios) < MIN_HISTORY_SAMPLES:
                return None
            typical = media_duration_seconds * (_quantile(ratios, 0.5) or 0)
            upper = media_duration_seconds * (_quantile(ratios, 0.75) or 0)
            sample_count = len(ratios)
        else:
            durations = [sample["duration_seconds"] for sample in samples]
            typical = _quantile(durations, 0.5) or 0
            upper = _quantile(durations, 0.75) or typical
            sample_count = len(durations)

        return {
            "typical_seconds": max(1, round(typical)),
            "upper_seconds": max(1, round(max(typical, upper))),
            "sample_count": sample_count,
            "confidence": "high" if sample_count >= 30 else "medium",
            "source": "stage_history",
        }

    @staticmethod
    def _default_stage_estimate(
        definition: StageDefinition, media_duration_seconds: Optional[float]
    ) -> Dict[str, Any]:
        typical = definition.default_typical_seconds
        upper = definition.default_upper_seconds
        if definition.duration_related and media_duration_seconds:
            typical = max(typical, media_duration_seconds * definition.typical_realtime_factor)
            upper = max(upper, media_duration_seconds * definition.upper_realtime_factor)
        return {
            "typical_seconds": max(1, round(typical)),
            "upper_seconds": max(1, round(max(typical, upper))),
            "sample_count": 0,
            "confidence": "low",
            "source": "default",
        }

    def estimate_legacy_total(
        self,
        media_duration_seconds: Optional[float],
        needs_transcription: Optional[bool],
    ) -> Optional[Dict[str, Any]]:
        if not media_duration_seconds or not isinstance(needs_transcription, bool):
            return None
        samples = list(self._legacy_samples.get(needs_transcription) or [])
        if len(samples) < MIN_HISTORY_SAMPLES:
            return None

        if needs_transcription:
            ratios = [
                sample["duration_seconds"] / sample["media_duration_seconds"]
                for sample in samples
                if sample["media_duration_seconds"] > 0
            ]
            typical = media_duration_seconds * (_quantile(ratios, 0.5) or 0)
            upper = media_duration_seconds * (_quantile(ratios, 0.75) or 0)
            sample_count = len(ratios)
        else:
            durations = [sample["duration_seconds"] for sample in samples]
            typical = _quantile(durations, 0.5) or 0
            upper = _quantile(durations, 0.75) or typical
            sample_count = len(durations)

        return {
            "typical_seconds": max(1, round(typical)),
            "upper_seconds": max(1, round(max(typical, upper))),
            "sample_count": sample_count,
            "confidence": "high" if sample_count >= 30 else "medium",
            "source": "legacy_task_history",
        }


class TaskProgressService:
    """Persist task attempts, stage transitions, and progress snapshots."""

    def __init__(
        self,
        file_service,
        now: Optional[Callable[[], datetime]] = None,
        runtime_id: Optional[str] = None,
    ):
        self.file_service = file_service
        self._now = now or datetime.now
        self.runtime_id = runtime_id or str(uuid.uuid4())
        try:
            tasks = list((file_service.list_files() or {}).values())
        except (AttributeError, TypeError):
            tasks = []
        self.estimator = TaskProgressEstimator(tasks, self._now)

    def start_run(
        self,
        task_info: Dict[str, Any],
        path: str = "unknown",
        stage_codes: Optional[List[str]] = None,
        conditional_stages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        now = self._now().isoformat()
        runs = list(task_info.get("progress_runs") or [])
        if runs and runs[-1].get("status") == "running":
            self._finish_run_record(runs[-1], "superseded", now)

        run = {
            "run_id": str(uuid.uuid4()),
            "runtime_id": self.runtime_id,
            "status": "running",
            "path": path,
            "started_at": now,
            "updated_at": now,
            "plan": list(stage_codes or UNKNOWN_STAGE_PLAN),
            "conditional_stages": list(
                UNKNOWN_CONDITIONAL_STAGES
                if conditional_stages is None and path == "unknown"
                else (conditional_stages or [])
            ),
            "stages": [],
        }
        runs.append(run)
        task_info.update(
            {
                "progress_runs": runs,
                "status": "processing",
                "progress": 0,
                "stage": "pending",
                "stage_label": "准备处理",
                "stage_updated_at": now,
                "updated_time": now,
            }
        )
        self._persist(task_info)
        return run

    def set_plan(
        self,
        task_info: Dict[str, Any],
        path: str,
        stage_codes: List[str],
        conditional_stages: Optional[List[str]] = None,
    ) -> None:
        run = self._current_run(task_info)
        if not run:
            run = self.start_run(task_info, path=path, stage_codes=stage_codes)
        recorded_codes = [stage.get("code") for stage in run.get("stages") or []]
        plan = list(stage_codes)
        for index, code in enumerate(recorded_codes):
            if code in plan:
                continue
            previous_code = recorded_codes[index - 1] if index > 0 else None
            insert_at = plan.index(previous_code) + 1 if previous_code in plan else len(plan)
            plan.insert(insert_at, code)
        run["path"] = path
        run["plan"] = plan
        run["conditional_stages"] = list(conditional_stages or [])
        run["updated_at"] = self._now().isoformat()
        task_info["progress_runs"] = task_info.get("progress_runs") or [run]
        self._persist(task_info)

    def transition(
        self,
        task_info: Dict[str, Any],
        stage_code: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        if stage_code not in STAGE_DEFINITIONS:
            raise ValueError(f"Unknown task stage: {stage_code}")
        run = self._current_run(task_info)
        if not run:
            run = self.start_run(task_info)

        now = self._now().isoformat()
        current = self._running_stage(run)
        previous_stage_code = current.get("code") if current else None
        if current and current.get("code") == stage_code:
            current["updated_at"] = now
            run["updated_at"] = now
            task_info["stage_updated_at"] = now
            self._persist(task_info)
            return
        if current:
            self._complete_stage(current, "completed", now, task_info)

        self._ensure_stage_in_plan(run, stage_code, previous_stage_code)
        stage = {
            "code": stage_code,
            "label": STAGE_DEFINITIONS[stage_code].label,
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "media_duration_seconds": _media_duration_seconds(task_info),
            "context": dict(context or {}),
        }
        run.setdefault("stages", []).append(stage)
        run["updated_at"] = now
        task_info.update(
            {
                "stage": stage_code,
                "stage_label": stage["label"],
                "stage_started_at": now,
                "stage_updated_at": now,
            }
        )
        task_info["progress"] = self.snapshot(task_info)["progress"]
        self._persist(task_info)

    def update_current_stage_context(
        self, task_info: Dict[str, Any], context: Dict[str, Any]
    ) -> None:
        run = self._current_run(task_info)
        current = self._running_stage(run or {})
        if not current:
            return
        current.setdefault("context", {}).update(context)
        current["media_duration_seconds"] = (
            current.get("media_duration_seconds") or _media_duration_seconds(task_info)
        )
        current["updated_at"] = self._now().isoformat()
        run["updated_at"] = current["updated_at"]
        task_info["stage_updated_at"] = current["updated_at"]
        self._persist(task_info)

    def finish(self, task_info: Dict[str, Any], outcome: str) -> None:
        run = self._current_run(task_info)
        now = self._now().isoformat()
        if run and run.get("status") == "running":
            current = self._running_stage(run)
            if current:
                stage_outcome = "completed" if outcome == "completed" else outcome
                self._complete_stage(current, stage_outcome, now, task_info)
            run["status"] = outcome
            run["finished_at"] = now
            run["updated_at"] = now
            if outcome == "completed":
                self.estimator.add_run(run)

        task_info.update(
            {
                "status": outcome,
                "stage": outcome,
                "stage_label": self._terminal_label(outcome),
                "stage_updated_at": now,
                "updated_time": now,
            }
        )
        task_info["progress"] = (
            100 if outcome == "completed" else self.snapshot(task_info)["progress"]
        )
        self._persist(task_info)

    def snapshot(self, task_info: Dict[str, Any]) -> Dict[str, Any]:
        run = self._current_run(task_info)
        if not run:
            return self._legacy_snapshot(task_info)

        now = self._now()
        media_seconds = _media_duration_seconds(task_info)
        stage_records = {stage.get("code"): stage for stage in run.get("stages") or []}
        stages = []
        current_position = None
        typical_remaining = 0
        upper_remaining = 0
        history_sample_count = 0
        active_estimates = []

        for index, stage_code in enumerate(run.get("plan") or [], start=1):
            definition = STAGE_DEFINITIONS.get(stage_code)
            if not definition:
                continue
            record = stage_records.get(stage_code) or {}
            status = record.get("status", "pending")
            context = record.get("context") or {}
            estimate = self.estimator.estimate_stage(stage_code, media_seconds, context)
            elapsed = self._stage_elapsed(record, now)
            actual = record.get("duration_seconds") if status != "running" else None
            remaining_typical = estimate["typical_seconds"]
            remaining_upper = estimate["upper_seconds"]
            if status == "running":
                current_position = index
                remaining_typical = max(0, remaining_typical - elapsed)
                remaining_upper = max(0, remaining_upper - elapsed)
            elif status in {"completed", "skipped"}:
                remaining_typical = 0
                remaining_upper = 0

            if status in {"running", "pending"}:
                typical_remaining += remaining_typical
                upper_remaining += remaining_upper
                active_estimates.append(estimate)
                history_sample_count += estimate["sample_count"]
            stages.append(
                {
                    "code": stage_code,
                    "label": definition.label,
                    "status": status,
                    "position": index,
                    "conditional": stage_code in (run.get("conditional_stages") or []),
                    "elapsed_seconds": round(elapsed),
                    "actual_seconds": round(actual) if isinstance(actual, (int, float)) else None,
                    "estimate": estimate,
                    "remaining_typical_seconds": round(remaining_typical),
                    "remaining_upper_seconds": round(remaining_upper),
                    "overdue": status == "running" and elapsed > estimate["upper_seconds"],
                }
            )

        run_elapsed = self._run_elapsed(run, now)
        legacy_estimate = self.estimator.estimate_legacy_total(
            media_seconds,
            task_info.get("needs_transcription"),
        )
        active_sources = {estimate["source"] for estimate in active_estimates}
        source_components = set(active_sources)
        confidence_rank = {"low": 0, "medium": 1, "high": 2}
        eta_confidence = min(
            (estimate["confidence"] for estimate in active_estimates),
            key=lambda value: confidence_rank.get(value, 0),
            default="low",
        )
        eta_sample_count = history_sample_count
        legacy_used = False
        if legacy_estimate and run.get("status") == "running":
            legacy_typical_remaining = max(
                0, legacy_estimate["typical_seconds"] - run_elapsed
            )
            legacy_upper_remaining = max(0, legacy_estimate["upper_seconds"] - run_elapsed)
            if legacy_typical_remaining > typical_remaining:
                typical_remaining = legacy_typical_remaining
                legacy_used = True
            if legacy_upper_remaining > upper_remaining:
                upper_remaining = legacy_upper_remaining
                legacy_used = True
            if legacy_used:
                source_components.add(legacy_estimate["source"])
                eta_confidence = min(
                    eta_confidence,
                    legacy_estimate["confidence"],
                    key=lambda value: confidence_rank.get(value, 0),
                )
                eta_sample_count += legacy_estimate["sample_count"]

        if not source_components:
            eta_source = "unavailable"
        elif len(source_components) == 1:
            eta_source = next(iter(source_components))
        else:
            eta_source = "mixed"

        progress = self._calculate_progress(task_info, stages)
        current_stage = next((stage for stage in stages if stage["status"] == "running"), None)
        if not current_stage and task_info.get("status") != "completed":
            current_stage = next(
                (
                    stage
                    for stage in reversed(stages)
                    if stage["status"]
                    in {"failed", "interrupted", "readwise_parse_failed"}
                ),
                None,
            )
        if not current_stage and task_info.get("status") in TERMINAL_TASK_STATUSES:
            current_stage = {
                "code": task_info.get("status"),
                "label": self._terminal_label(task_info.get("status")),
                "status": task_info.get("status"),
                "position": len(stages),
            }

        future_stages = 0
        if current_position:
            future_stages = sum(
                1
                for stage in stages[current_position:]
                if stage["status"] == "pending"
            )
        elif not current_stage:
            future_stages = sum(1 for stage in stages if stage["status"] == "pending")

        return {
            "run_id": run.get("run_id"),
            "path": run.get("path"),
            "run_status": run.get("status"),
            "current_stage": current_stage,
            "stages": stages,
            "stage_count": len(stages),
            "remaining_stage_count": future_stages,
            "progress": progress,
            "eta": {
                "typical_seconds": round(max(0, typical_remaining)),
                "upper_seconds": round(max(typical_remaining, upper_remaining)),
                "sample_count": eta_sample_count,
                "confidence": eta_confidence,
                "source": eta_source,
                "source_components": sorted(source_components),
            },
        }

    def estimate_stage(
        self,
        stage_code: str,
        media_duration_seconds: Optional[float] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.estimator.estimate_stage(stage_code, media_duration_seconds, context)

    def estimate_legacy_total(
        self,
        media_duration_seconds: Optional[float],
        needs_transcription: Optional[bool],
    ) -> Optional[Dict[str, Any]]:
        return self.estimator.estimate_legacy_total(
            media_duration_seconds,
            needs_transcription,
        )

    def mark_orphaned_runs_interrupted(self) -> int:
        try:
            tasks = self.file_service.list_files() or {}
        except (AttributeError, TypeError):
            return 0
        interrupted_count = 0
        now = self._now().isoformat()
        for task_id, task_info in tasks.items():
            if task_info.get("status") not in ACTIVE_TASK_STATUSES:
                continue
            run = self._current_run(task_info)
            if run and run.get("status") == "running":
                if run.get("runtime_id") == self.runtime_id:
                    continue
                self._finish_run_record(run, "interrupted", now, task_info)

            task_info.update(
                {
                    "status": "interrupted",
                    "stage": "interrupted",
                    "stage_label": self._terminal_label("interrupted"),
                    "stage_updated_at": now,
                    "updated_time": now,
                    "error": task_info.get("error")
                    or "服务重启，后台任务已中断，请重新发起。",
                }
            )
            self.file_service.update_file_info(task_id, task_info)
            interrupted_count += 1
        return interrupted_count

    def _persist(self, task_info: Dict[str, Any]) -> None:
        task_id = task_info.get("id")
        if not task_id:
            return
        fields = {
            key: task_info.get(key)
            for key in (
                "progress_runs",
                "progress",
                "stage",
                "stage_label",
                "stage_started_at",
                "stage_updated_at",
                "status",
                "updated_time",
                "error",
            )
            if key in task_info
        }
        self.file_service.update_file_info(task_id, fields)

    @staticmethod
    def _current_run(task_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        runs = task_info.get("progress_runs") or []
        return runs[-1] if runs else None

    @staticmethod
    def _running_stage(run: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for stage in reversed(run.get("stages") or []):
            if stage.get("status") == "running":
                return stage
        return None

    @staticmethod
    def _ensure_stage_in_plan(
        run: Dict[str, Any],
        stage_code: str,
        previous_stage_code: Optional[str] = None,
    ) -> None:
        plan = run.setdefault("plan", [])
        if stage_code in plan:
            return
        if previous_stage_code in plan:
            plan.insert(plan.index(previous_stage_code) + 1, stage_code)
        else:
            plan.append(stage_code)

    @staticmethod
    def _complete_stage(
        stage: Dict[str, Any],
        outcome: str,
        finished_at: str,
        task_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        stage["status"] = outcome
        stage["finished_at"] = finished_at
        stage["updated_at"] = finished_at
        stage["duration_seconds"] = _elapsed_seconds(
            stage.get("started_at"), finished_at
        )
        if task_info and not stage.get("media_duration_seconds"):
            stage["media_duration_seconds"] = _media_duration_seconds(task_info)

    @staticmethod
    def _finish_run_record(
        run: Dict[str, Any],
        outcome: str,
        finished_at: str,
        task_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        current = TaskProgressService._running_stage(run)
        if current:
            TaskProgressService._complete_stage(
                current, outcome, finished_at, task_info
            )
        run["status"] = outcome
        run["finished_at"] = finished_at
        run["updated_at"] = finished_at

    @staticmethod
    def _stage_elapsed(stage: Dict[str, Any], now: datetime) -> float:
        if not stage:
            return 0.0
        if isinstance(stage.get("duration_seconds"), (int, float)):
            return float(stage["duration_seconds"])
        started = _parse_datetime(stage.get("started_at"))
        if not started:
            return 0.0
        try:
            return max(0.0, (now - started).total_seconds())
        except TypeError:
            return 0.0

    @staticmethod
    def _run_elapsed(run: Dict[str, Any], now: datetime) -> float:
        started = _parse_datetime(run.get("started_at"))
        if not started:
            return 0.0
        try:
            return max(0.0, (now - started).total_seconds())
        except TypeError:
            return 0.0

    @staticmethod
    def _calculate_progress(
        task_info: Dict[str, Any], stages: List[Dict[str, Any]]
    ) -> int:
        if task_info.get("status") == "completed":
            return 100
        weights = [max(1, stage["estimate"]["typical_seconds"]) for stage in stages]
        total = sum(weights)
        if not total:
            return int(task_info.get("progress") or 0)
        completed = 0.0
        for stage, weight in zip(stages, weights):
            if stage["status"] in {"completed", "skipped"}:
                completed += weight
            elif stage["status"] == "running":
                completed += weight * min(
                    0.9,
                    stage["elapsed_seconds"] / max(1, weight),
                )
        calculated = min(99, int(round(100 * completed / total)))
        return max(int(task_info.get("progress") or 0), calculated)

    @staticmethod
    def _terminal_label(status: Optional[str]) -> str:
        return {
            "completed": "处理完成",
            "failed": "处理失败",
            "interrupted": "处理已中断",
            "readwise_parse_failed": "Reader 解析失败",
            "superseded": "已开始新的处理尝试",
        }.get(status or "", status or "状态未知")

    def _legacy_snapshot(self, task_info: Dict[str, Any]) -> Dict[str, Any]:
        status = task_info.get("status", "unknown")
        progress = int(task_info.get("progress") or (100 if status == "completed" else 0))
        return {
            "run_id": None,
            "path": "legacy",
            "run_status": status,
            "current_stage": {
                "code": task_info.get("stage") or status,
                "label": task_info.get("stage_label") or self._terminal_label(status),
                "status": status,
                "position": 0,
            },
            "stages": [],
            "stage_count": 0,
            "remaining_stage_count": 0,
            "progress": max(0, min(100, progress)),
            "eta": {
                "typical_seconds": 0,
                "upper_seconds": 0,
                "sample_count": 0,
                "confidence": "low",
                "source": "unavailable",
                "source_components": [],
            },
        }
