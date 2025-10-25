"""Runtime hotword settings management shared across services.

默认会把运行时热词配置持久化到 ``HOTWORD_SETTINGS_PATH`` 指定的文件
（默认 ``/app/config/hotword_settings.json``），确保容器重启后仍能保留状态。
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

ALLOWED_MODES = {"user_only", "curated", "experiment"}
DEFAULT_SETTINGS_PATH = "/app/config/hotword_settings.json"

logger = logging.getLogger(__name__)


def _to_bool(value: Any, default: bool = False) -> bool:
    """Normalize truthy values coming from env/config."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


class HotwordSettingsManager:
    """Singleton-style manager keeping hotword toggles in sync."""

    _instance: Optional["HotwordSettingsManager"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings_path = Path(
            os.getenv("HOTWORD_SETTINGS_PATH", DEFAULT_SETTINGS_PATH)
        )
        file_state = self._load_from_file()
        if file_state is not None:
            self._state = file_state
        else:
            self._state = self._state_from_env()
            self._persist_to_file()
        logger.info("热词设置管理器初始化，持久化路径: %s", self._settings_path)

    @classmethod
    def get_instance(cls) -> "HotwordSettingsManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    def _state_from_env(self) -> Dict[str, Any]:
        return {
            "auto_hotwords": _to_bool(os.getenv("ENABLE_AUTO_HOTWORDS"), False),
            "post_process": _to_bool(os.getenv("ENABLE_HOTWORD_POST_PROCESS"), False),
            "mode": self._normalize_mode(os.getenv("HOTWORD_MODE", "user_only")),
            "max_count": self._normalize_max_count(os.getenv("HOTWORD_MAX_COUNT", "20")),
        }

    def _normalize_mode(self, mode: Any) -> str:
        candidate = str(mode).strip().lower() if mode is not None else "user_only"
        if candidate not in ALLOWED_MODES:
            return "user_only"
        return candidate

    def _normalize_max_count(self, value: Any) -> int:
        try:
            count = int(value)
            return max(0, min(count, 100))
        except (TypeError, ValueError):
            return 20

    def _normalize_state(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "auto_hotwords": _to_bool(state.get("auto_hotwords"), False),
            "post_process": _to_bool(state.get("post_process"), False),
            "mode": self._normalize_mode(state.get("mode")),
            "max_count": self._normalize_max_count(state.get("max_count")),
        }

    def _load_from_file(self) -> Optional[Dict[str, Any]]:
        try:
            if self._settings_path.is_file():
                with self._settings_path.open("r", encoding="utf-8") as fp:
                    data = json.load(fp)
                    logger.debug(
                        "从文件加载热词设置: %s",
                        {k: ("***" if k.endswith("word") else v) for k, v in data.items()},
                    )
                    return self._normalize_state(data)
        except Exception as exc:
            logger.warning("读取热词设置文件失败，将使用环境变量: %s", exc)
        return None

    def _persist_to_file(self) -> None:
        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._settings_path.with_name(
                f"{self._settings_path.name}.tmp"
            )
            with tmp_path.open("w", encoding="utf-8") as fp:
                json.dump(self._state, fp, ensure_ascii=False, indent=2)
            tmp_path.replace(self._settings_path)
        except Exception as exc:
            logger.error("写入热词设置文件失败: %s", exc)

    def get_state(self) -> Dict[str, Any]:
        """Return a shallow copy of current settings."""
        with self._lock:
            return dict(self._state)

    def set_auto_hotwords(self, enabled: bool) -> Dict[str, Any]:
        return self.update_state(auto_hotwords=bool(enabled))

    def set_post_process(self, enabled: bool) -> Dict[str, Any]:
        return self.update_state(post_process=bool(enabled))

    def set_mode(self, mode: str) -> Dict[str, Any]:
        return self.update_state(mode=self._normalize_mode(mode))

    def set_max_count(self, max_count: Any) -> Dict[str, Any]:
        return self.update_state(max_count=self._normalize_max_count(max_count))

    def update_state(self, **changes: Any) -> Dict[str, Any]:
        """Update multiple fields atomically."""
        with self._lock:
            for key, value in changes.items():
                if key == "auto_hotwords":
                    self._state["auto_hotwords"] = _to_bool(
                        value, self._state["auto_hotwords"]
                    )
                elif key == "post_process":
                    self._state["post_process"] = _to_bool(
                        value, self._state["post_process"]
                    )
                elif key == "mode":
                    self._state["mode"] = self._normalize_mode(value)
                elif key == "max_count":
                    self._state["max_count"] = self._normalize_max_count(value)
            self._persist_to_file()
            return dict(self._state)

    def reset_from_env(self) -> Dict[str, Any]:
        """Reset settings back to environment defaults."""
        with self._lock:
            self._state = self._state_from_env()
            self._persist_to_file()
            return dict(self._state)
