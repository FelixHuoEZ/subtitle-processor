import logging

from app.config.config_manager import ConfigManager


def test_config_manager_redacts_sensitive_values_in_logs(tmp_path, monkeypatch, caplog):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                "tokens:",
                "  openai:",
                "    api_key: sk-real-secret-value",
                "    base_url: https://api.openai.com/v1",
                "app:",
                "  name: subtitle-processor",
            ]
        ),
        encoding="utf-8",
    )

    def fake_setup_config_paths(self):
        self.container_config_path = str(config_path)
        self.local_config_path = str(config_path)
        self.config_path = str(config_path)
        self.config_dir = str(tmp_path)

    monkeypatch.setattr(ConfigManager, "_setup_config_paths", fake_setup_config_paths)

    with caplog.at_level(logging.DEBUG):
        manager = ConfigManager()
        assert manager.get_config_value("tokens.openai.api_key") == "sk-real-secret-value"
        assert manager.get_config_value("tokens.openai.base_url") == "https://api.openai.com/v1"

    logs = "\n".join(record.getMessage() for record in caplog.records)

    assert "sk-real-secret-value" not in logs
    assert "<redacted len=20>" in logs
    assert "https://api.openai.com/v1" in logs
