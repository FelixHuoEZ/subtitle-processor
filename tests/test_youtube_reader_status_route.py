from flask import Flask

from app.routes import process_routes


class FakeYouTubeReaderStatusService:
    def __init__(self):
        self.calls = []

    def get_status(self, video_id, *, force_refresh=False):
        self.calls.append((video_id, force_refresh))
        return {
            "success": True,
            "video_id": video_id,
            "status": "saved",
            "saved": True,
            "reader_url": "https://read.readwise.io/read/reader-id",
        }


def test_reader_status_route_returns_link_and_supports_force_refresh(monkeypatch):
    service = FakeYouTubeReaderStatusService()
    monkeypatch.setattr(process_routes, "youtube_reader_status_service", service)
    app = Flask(__name__)
    app.register_blueprint(process_routes.process_bp)

    response = app.test_client().get(
        "/process/reader-status/youtube/abcdefghijk?refresh=1"
    )

    assert response.status_code == 200
    assert response.get_json()["reader_url"] == (
        "https://read.readwise.io/read/reader-id"
    )
    assert service.calls == [("abcdefghijk", True)]


def test_reader_status_route_rejects_invalid_video_id(monkeypatch):
    class RejectingService:
        def get_status(self, video_id, *, force_refresh=False):
            raise ValueError("invalid YouTube video ID")

    monkeypatch.setattr(
        process_routes,
        "youtube_reader_status_service",
        RejectingService(),
    )
    app = Flask(__name__)
    app.register_blueprint(process_routes.process_bp)

    response = app.test_client().get(
        "/process/reader-status/youtube/bad-id"
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid YouTube video ID"
