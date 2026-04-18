import pytest

import app.services.file_service as file_service


def test_redis_fallbacks_to_json(monkeypatch, tmp_path):
    if file_service.redis is None:
        pytest.skip("redis dependency not installed")

    def fail(*_args, **_kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(file_service.redis.Redis, "from_url", staticmethod(fail))
    monkeypatch.setenv("STORAGE_BACKEND", "redis")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    service = file_service.FileService(
        upload_folder=tmp_path / "uploads",
        output_folder=tmp_path / "outputs",
    )

    assert service.storage_backend == "json"
    assert service.redis_client is None
    assert (tmp_path / "uploads" / "files_info.json").exists()
