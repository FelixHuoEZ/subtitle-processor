from pathlib import Path

from flask import Flask

from app.routes import process_routes, view_routes


class _FakeFileService:
    def __init__(self, task_info):
        self.task_info = task_info

    def get_file_info(self, file_id):
        if file_id == self.task_info["id"]:
            return self.task_info
        return None


def _build_view_client(monkeypatch, task_info):
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parents[1] / "app" / "templates"),
    )
    fake_file_service = _FakeFileService(task_info)
    monkeypatch.setattr(view_routes, "file_service", fake_file_service)
    monkeypatch.setattr(process_routes, "file_service", fake_file_service)
    app.register_blueprint(view_routes.view_bp)
    app.register_blueprint(process_routes.process_bp)
    return app.test_client()


def test_file_detail_renders_progress_bar(monkeypatch):
    task_info = {
        "id": "task-1",
        "filename": "Demo task",
        "status": "processing",
        "progress": 42,
    }
    client = _build_view_client(monkeypatch, task_info)

    response = client.get("/view/task-1")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="taskProgress"' in html
    assert 'id="taskProgressStage"' in html
    assert 'id="taskProgressEta"' in html
    assert 'id="taskStageList"' in html
    assert 'data-status-url="/process/status/task-1"' in html
    assert 'aria-valuenow="42"' in html
    assert "42%" in html
    assert "/process/status/task-1" in html


def test_file_detail_defaults_completed_progress_to_100(monkeypatch):
    task_info = {
        "id": "task-2",
        "filename": "Completed task",
        "status": "completed",
    }
    client = _build_view_client(monkeypatch, task_info)

    response = client.get("/view/task-2")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'aria-valuenow="100"' in html
    assert "100%" in html


def test_status_endpoint_returns_stage_plan_and_eta(monkeypatch):
    task_info = {
        "id": "task-3",
        "filename": "Running task",
        "status": "processing",
        "progress": 37,
    }

    class FakeProgressService:
        def get_progress_snapshot(self, current_task):
            assert current_task is task_info
            return {
                "progress": 37,
                "current_stage": {
                    "code": "transcribe_audio",
                    "label": "音频转录",
                    "position": 2,
                },
                "stage_count": 4,
                "remaining_stage_count": 2,
                "stages": [],
                "eta": {
                    "typical_seconds": 60,
                    "upper_seconds": 120,
                    "sample_count": 42,
                    "confidence": "high",
                    "source": "stage_history",
                },
            }

    monkeypatch.setattr(process_routes, "processing_service", FakeProgressService())
    client = _build_view_client(monkeypatch, task_info)

    response = client.get("/process/status/task-3")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["stage"] == "transcribe_audio"
    assert payload["stage_label"] == "音频转录"
    assert payload["progress_details"]["remaining_stage_count"] == 2
    assert payload["progress_details"]["eta"]["sample_count"] == 42
