from app.main import create_app
from app.routes import process_routes, upload_routes, view_routes


def test_create_app_binds_route_modules_to_app_services(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "json")
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                "app:",
                f"  upload_folder: {tmp_path / 'uploads'}",
                f"  output_folder: {tmp_path / 'outputs'}",
                "tokens:",
                "  readwise:",
                "    api_token: ''",
                "servers:",
                "  transcribe:",
                "    default_url: http://transcribe-audio:10095",
            ]
        ),
        encoding="utf-8",
    )

    app = create_app(str(config_path))

    assert app.services.file_service is app.file_service
    assert upload_routes.file_service is app.file_service
    assert process_routes.file_service is app.file_service
    assert view_routes.file_service is app.file_service
    assert upload_routes.video_service is app.video_service
    assert process_routes.video_service is app.video_service
