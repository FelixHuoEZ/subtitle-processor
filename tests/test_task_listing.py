from pathlib import Path

from flask import Flask

from app.main import register_main_routes
from app.routes import process_routes, view_routes
from app.services import file_service as file_service_module
from app.utils.file_utils import get_file_created_time


class FakeFileService:
    def __init__(self, tasks):
        self.tasks = tasks

    def list_files(self):
        return self.tasks

    def get_file_info(self, file_id):
        return self.tasks.get(file_id)


def _build_client(monkeypatch, tasks):
    fake_file_service = FakeFileService(tasks)
    monkeypatch.setattr(view_routes, "file_service", fake_file_service)
    monkeypatch.setattr(process_routes, "file_service", fake_file_service)
    monkeypatch.setattr(file_service_module, "FileService", lambda: fake_file_service)
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parents[1] / "app" / "templates"),
    )
    app.secret_key = "test"
    app.register_blueprint(view_routes.view_bp)
    app.register_blueprint(process_routes.process_bp)
    register_main_routes(app)
    return app.test_client()


def test_file_created_time_ignores_later_status_update():
    task = {
        "created_time": "2026-01-05T10:00:00",
        "updated_time": "2026-07-10T23:55:36",
    }

    assert get_file_created_time(task) == "2026-01-05T10:00:00"


def test_file_created_time_falls_back_for_legacy_uploads():
    task = {
        "upload_time": "2025-10-29T22:05:32",
        "updated_time": "2026-07-10T23:55:36",
    }

    assert get_file_created_time(task) == "2025-10-29T22:05:32"


def test_task_lists_sort_by_creation_instead_of_status_update(monkeypatch):
    tasks = {
        "new-task": {
            "id": "new-task",
            "filename": "New task",
            "status": "completed",
            "created_time": "2026-07-10T22:56:19",
            "updated_time": "2026-07-10T23:00:00",
        },
        "old-task": {
            "id": "old-task",
            "filename": "Old interrupted task",
            "status": "interrupted",
            "created_time": "2026-01-05T10:00:00",
            "updated_time": "2026-07-10T23:55:36",
        },
    }
    client = _build_client(monkeypatch, tasks)

    homepage = client.get("/")
    api_response = client.get("/view/api/files")

    assert homepage.status_code == 200
    html = homepage.get_data(as_text=True)
    assert html.index("New task") < html.index("Old interrupted task")
    assert "添加时间：2026-01-05T10:00:00" in html
    assert [item["id"] for item in api_response.get_json()["files"]] == [
        "new-task",
        "old-task",
    ]
