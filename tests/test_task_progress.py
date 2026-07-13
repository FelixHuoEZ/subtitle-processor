from datetime import datetime, timedelta

from app.services.task_progress import TaskProgressService


class FakeFileService:
    def __init__(self, tasks=None):
        self.tasks = tasks or {}
        self.updates = []

    def list_files(self):
        return self.tasks

    def update_file_info(self, task_id, updates):
        self.updates.append((task_id, dict(updates)))
        self.tasks.setdefault(task_id, {}).update(updates)


class Clock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def _completed_stage_task(index, stage_seconds, media_seconds=600):
    started_at = datetime(2026, 7, 1, 10, 0, index).isoformat()
    finished_at = (
        datetime(2026, 7, 1, 10, 0, index) + timedelta(seconds=stage_seconds)
    ).isoformat()
    return {
        "id": f"history-{index}",
        "status": "completed",
        "progress_runs": [
            {
                "run_id": f"run-{index}",
                "status": "completed",
                "path": "transcription",
                "started_at": started_at,
                "finished_at": finished_at,
                "plan": ["transcribe_audio"],
                "stages": [
                    {
                        "code": "transcribe_audio",
                        "status": "completed",
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "duration_seconds": stage_seconds,
                        "media_duration_seconds": media_seconds,
                    }
                ],
            }
        ],
    }


def test_stage_estimate_uses_similar_history_after_minimum_sample_count():
    tasks = {
        f"history-{index}": _completed_stage_task(index, 50 + index)
        for index in range(10)
    }
    service = TaskProgressService(FakeFileService(tasks))

    estimate = service.estimate_stage("transcribe_audio", media_duration_seconds=1200)

    assert estimate["source"] == "stage_history"
    assert estimate["sample_count"] == 10
    assert estimate["confidence"] == "medium"
    assert 100 <= estimate["typical_seconds"] <= 120
    assert estimate["upper_seconds"] >= estimate["typical_seconds"]


def test_dynamic_plan_reports_current_and_remaining_stages():
    clock = Clock(datetime(2026, 7, 10, 12, 0, 0))
    task = {
        "id": "task-1",
        "status": "processing",
        "video_info": {"duration": 900},
    }
    file_service = FakeFileService({"task-1": task})
    service = TaskProgressService(file_service, now=clock.now)

    service.start_run(task, path="unknown")
    service.transition(task, "download_prepare")
    clock.advance(20)
    service.set_plan(
        task,
        path="transcription",
        stage_codes=[
            "download_prepare",
            "transcribe_audio",
            "generate_subtitles",
            "send_readwise",
        ],
    )
    service.transition(task, "transcribe_audio")

    snapshot = service.snapshot(task)

    assert snapshot["current_stage"]["code"] == "transcribe_audio"
    assert snapshot["current_stage"]["position"] == 2
    assert snapshot["stage_count"] == 4
    assert snapshot["remaining_stage_count"] == 2
    assert snapshot["stages"][0]["status"] == "completed"
    assert snapshot["stages"][1]["status"] == "running"
    assert snapshot["eta"]["typical_seconds"] > 0
    assert 0 < snapshot["progress"] < 100


def test_download_queue_stage_reports_position_without_overdue_warning():
    clock = Clock(datetime(2026, 7, 10, 12, 0, 0))
    task = {"id": "task-queue", "status": "processing"}
    service = TaskProgressService(
        FakeFileService({"task-queue": task}),
        now=clock.now,
    )
    service.start_run(task, path="unknown")
    service.transition(task, "source_analysis")
    service.transition(
        task,
        "wait_download_slot",
        context={"queue_position": 3, "active_downloads": 2, "download_limit": 2},
    )
    clock.advance(600)
    service.transition(
        task,
        "wait_download_slot",
        context={"queue_position": 1, "active_downloads": 2, "download_limit": 2},
    )

    snapshot = service.snapshot(task)

    assert snapshot["current_stage"]["code"] == "wait_download_slot"
    assert snapshot["current_stage"]["context"]["queue_position"] == 1
    assert snapshot["current_stage"]["elapsed_seconds"] == 600
    assert snapshot["current_stage"]["overdue"] is False
    assert snapshot["current_stage"]["estimate"]["source"] == "unavailable"


def test_orphaned_running_task_is_marked_interrupted():
    task = {
        "id": "task-1",
        "status": "processing",
        "progress": 42,
        "progress_runs": [
            {
                "run_id": "old-run",
                "runtime_id": "old-runtime",
                "status": "running",
                "path": "transcription",
                "started_at": "2026-07-10T10:00:00",
                "updated_at": "2026-07-10T10:01:00",
                "plan": ["download_prepare", "transcribe_audio"],
                "stages": [
                    {
                        "code": "transcribe_audio",
                        "status": "running",
                        "started_at": "2026-07-10T10:01:00",
                    }
                ],
            }
        ],
    }
    file_service = FakeFileService({"task-1": task})
    service = TaskProgressService(
        file_service,
        now=lambda: datetime(2026, 7, 10, 11, 0, 0),
        runtime_id="new-runtime",
    )

    interrupted_count = service.mark_orphaned_runs_interrupted()

    stored = file_service.tasks["task-1"]
    assert interrupted_count == 1
    assert stored["status"] == "interrupted"
    assert stored["stage"] == "interrupted"
    assert stored["status_updated_at"] == "2026-07-10T11:00:00"
    assert stored["progress_runs"][-1]["status"] == "interrupted"
    assert stored["progress_runs"][-1]["stages"][-1]["status"] == "interrupted"
    assert service.last_interrupted_task_ids == ["task-1"]


def test_legacy_active_task_without_run_is_not_reclassified_on_startup():
    task = {
        "id": "legacy-task",
        "status": "processing",
        "created_time": "2026-01-05T10:00:00",
        "updated_time": "2026-01-05T10:01:00",
    }
    file_service = FakeFileService({"legacy-task": task})
    service = TaskProgressService(
        file_service,
        now=lambda: datetime(2026, 7, 10, 11, 0, 0),
        runtime_id="new-runtime",
    )

    interrupted_count = service.mark_orphaned_runs_interrupted()

    assert interrupted_count == 0
    assert task["status"] == "processing"
    assert task["updated_time"] == "2026-01-05T10:01:00"
    assert file_service.updates == []


def test_running_task_without_runtime_owner_is_not_reclassified_on_startup():
    task = {
        "id": "unowned-task",
        "status": "processing",
        "progress_runs": [{"run_id": "legacy-run", "status": "running"}],
    }
    file_service = FakeFileService({"unowned-task": task})
    service = TaskProgressService(file_service, runtime_id="new-runtime")

    interrupted_count = service.mark_orphaned_runs_interrupted()

    assert interrupted_count == 0
    assert task["status"] == "processing"
    assert file_service.updates == []


def test_legacy_completed_tasks_seed_total_eta_baseline():
    tasks = {}
    for index in range(12):
        started = datetime(2026, 7, 1, 8, index, 0)
        tasks[f"legacy-{index}"] = {
            "id": f"legacy-{index}",
            "status": "completed",
            "created_time": started.isoformat(),
            "updated_time": (started + timedelta(seconds=60 + index)).isoformat(),
            "needs_transcription": True,
            "video_info": {"duration": 600},
        }
    service = TaskProgressService(FakeFileService(tasks))

    estimate = service.estimate_legacy_total(
        media_duration_seconds=1200,
        needs_transcription=True,
    )

    assert estimate["source"] == "legacy_task_history"
    assert estimate["sample_count"] == 12
    assert estimate["typical_seconds"] >= 120
    assert estimate["upper_seconds"] >= estimate["typical_seconds"]


def test_language_confirmation_is_inserted_after_current_stage():
    task = {"id": "task-1", "status": "processing"}
    service = TaskProgressService(FakeFileService({"task-1": task}))
    service.start_run(
        task,
        path="transcription",
        stage_codes=[
            "download_prepare",
            "transcribe_audio",
            "generate_subtitles",
            "send_readwise",
        ],
    )
    service.transition(task, "download_prepare")

    service.transition(task, "language_confirmation")

    snapshot = service.snapshot(task)
    assert [stage["code"] for stage in snapshot["stages"]] == [
        "download_prepare",
        "language_confirmation",
        "transcribe_audio",
        "generate_subtitles",
        "send_readwise",
    ]
    assert snapshot["current_stage"]["position"] == 2
    assert snapshot["remaining_stage_count"] == 3


def test_finish_persists_terminal_status_with_run_record_atomically():
    task = {"id": "task-1", "status": "processing"}
    file_service = FakeFileService({"task-1": task})
    service = TaskProgressService(file_service)
    service.start_run(task, path="existing_subtitle", stage_codes=["send_readwise"])
    service.transition(task, "send_readwise")
    file_service.updates.clear()

    service.finish(task, "completed")

    _, terminal_update = file_service.updates[-1]
    assert terminal_update["status"] == "completed"
    assert terminal_update["progress"] == 100
    assert terminal_update["progress_runs"][-1]["status"] == "completed"
    assert terminal_update["updated_time"]


def test_failed_run_keeps_last_progress_instead_of_showing_100_percent():
    clock = Clock(datetime(2026, 7, 10, 12, 0, 0))
    task = {"id": "task-1", "status": "processing", "video_info": {"duration": 600}}
    service = TaskProgressService(
        FakeFileService({"task-1": task}),
        now=clock.now,
    )
    service.start_run(
        task,
        path="transcription",
        stage_codes=["download_prepare", "transcribe_audio", "send_readwise"],
    )
    service.transition(task, "download_prepare")
    clock.advance(20)
    service.transition(task, "transcribe_audio")
    clock.advance(10)

    service.finish(task, "failed")

    assert task["status"] == "failed"
    assert 0 < task["progress"] < 100


def test_active_task_with_finished_run_is_not_reclassified_as_interrupted():
    task = {
        "id": "task-1",
        "status": "processing",
        "progress_runs": [
            {
                "run_id": "finished-run",
                "runtime_id": "old-runtime",
                "status": "failed",
                "plan": ["download_prepare"],
                "stages": [],
            }
        ],
    }
    file_service = FakeFileService({"task-1": task})
    service = TaskProgressService(file_service, runtime_id="new-runtime")

    interrupted_count = service.mark_orphaned_runs_interrupted()

    assert interrupted_count == 0
    assert task["status"] == "processing"
    assert file_service.updates == []


def test_eta_source_only_counts_stages_that_are_still_active():
    tasks = {}
    for index in range(10):
        task = _completed_stage_task(index, 20 + index)
        task["progress_runs"][0]["plan"] = ["download_prepare"]
        task["progress_runs"][0]["stages"][0]["code"] = "download_prepare"
        tasks[task["id"]] = task
    current = {
        "id": "current",
        "status": "processing",
        "video_info": {"duration": 600},
    }
    tasks["current"] = current
    service = TaskProgressService(FakeFileService(tasks))
    service.start_run(
        current,
        path="existing_subtitle",
        stage_codes=["download_prepare", "send_readwise"],
    )
    service.transition(current, "download_prepare")
    service.transition(current, "send_readwise")

    eta = service.snapshot(current)["eta"]

    assert eta["source"] == "default"
    assert eta["sample_count"] == 0
    assert eta["confidence"] == "low"


def test_stage_history_older_than_90_days_does_not_drive_eta():
    tasks = {
        f"history-{index}": _completed_stage_task(index, 50 + index)
        for index in range(10)
    }
    for task in tasks.values():
        stage = task["progress_runs"][0]["stages"][0]
        stage["finished_at"] = "2026-01-01T10:00:00"
    service = TaskProgressService(
        FakeFileService(tasks),
        now=lambda: datetime(2026, 7, 10, 12, 0, 0),
    )

    estimate = service.estimate_stage("transcribe_audio", media_duration_seconds=1200)

    assert estimate["source"] == "default"
    assert estimate["sample_count"] == 0


def test_mixed_eta_reports_exact_source_components():
    tasks = {
        f"history-{index}": _completed_stage_task(index, 50 + index)
        for index in range(10)
    }
    current = {
        "id": "current",
        "status": "processing",
        "video_info": {"duration": 600},
    }
    tasks["current"] = current
    service = TaskProgressService(FakeFileService(tasks))
    service.start_run(
        current,
        path="transcription",
        stage_codes=["transcribe_audio", "send_readwise"],
    )
    service.transition(current, "transcribe_audio")

    eta = service.snapshot(current)["eta"]

    assert eta["source"] == "mixed"
    assert eta["source_components"] == ["default", "stage_history"]
    assert eta["sample_count"] == 10
    assert eta["confidence"] == "low"
