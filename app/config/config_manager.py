"""Configuration management for the subtitle processing application."""

import os
import yaml
import logging

logger = logging.getLogger(__name__)


class ConfigManager:
    """Central configuration manager for the application."""

    _SENSITIVE_SEGMENT_MARKERS = (
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "cookie",
        "authorization",
    )
    
    def __init__(self):
        """Initialize the configuration manager."""
        self.config = {}
        self._setup_config_paths()
        self.load_config()
    
    def _setup_config_paths(self):
        """Setup configuration file paths."""
        # 配置文件路径
        self.container_config_path = '/app/config/config.yml'
        local_config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
        self.local_config_path = os.path.join(local_config_dir, 'config.yml')
        
        # 优先使用容器内配置路径
        self.config_path = (self.container_config_path 
                           if os.path.exists(self.container_config_path) 
                           else self.local_config_path)
        self.config_dir = os.path.dirname(self.config_path)
        
        # 确保配置目录存在
        if not os.path.exists(self.config_dir):
            try:
                os.makedirs(self.config_dir)
                logger.info(f"创建配置目录: {self.config_dir}")
            except Exception as e:
                logger.error(f"创建配置目录失败: {str(e)}")
        
        logger.info(f"配置文件路径: {self.config_path}")
    
    def load_config(self):
        """加载YAML配置文件"""
        try:
            logger.info(f"尝试加载配置文件: {self.config_path}")
            if not os.path.exists(self.config_path):
                logger.error(f"配置文件不存在: {self.config_path}")
                self.config = {}
                return
                
            # 检查文件权限
            if not os.access(self.config_path, os.R_OK):
                logger.error(f"配置文件无读取权限: {self.config_path}")
                self.config = {}
                return
            logger.info("配置文件可读")
                
            with open(self.config_path, 'r', encoding='utf-8') as f:
                content = f.read()
                logger.debug("配置文件内容已读取，字符数: %s", len(content))
                try:
                    loaded_config = yaml.safe_load(content)
                    if not loaded_config:
                        logger.error("配置文件为空或格式错误")
                        self.config = {}
                        return
                    if not isinstance(loaded_config, dict):
                        logger.error(f"配置文件格式错误，应为字典，实际为: {type(loaded_config)}")
                        self.config = {}
                        return
                    self.config = loaded_config
                    logger.info("成功加载配置文件")
                    logger.debug(
                        "解析后的配置: %s",
                        self._sanitize_for_log(loaded_config),
                    )
                    
                    if self.config:
                        logger.info(f"配置加载成功，包含以下部分: {list(self.config.keys())}")
                        for section in self.config.keys():
                            logger.debug(
                                "配置部分 %s: %s",
                                section,
                                self._sanitize_for_log(self.config[section], section),
                            )
                    
                except yaml.YAMLError as e:
                    logger.error(f"YAML解析错误: {str(e)}")
                    self.config = {}
        except Exception as e:
            logger.error(f"加载配置文件失败: {str(e)}")
            self.config = {}

        if not self.config:
            logger.error("配置加载失败，使用空配置")
            self.config = {}
    
    def get_config_value(self, key_path, default=None):
        """从配置中获取值，支持点号分隔的路径，如 'tokens.openai.api_key'"""
        try:
            if not self.config:
                logger.warning("配置对象为空")
                return default
                
            value = self.config
            keys = key_path.split('.')
            for i, key in enumerate(keys):
                is_last = (i == len(keys) - 1)

                if isinstance(value, list) and not is_last:
                    value = self._list_to_dict(value)

                if isinstance(value, str) and not is_last:
                    value = {'value': value, 'api_key': value}

                if not isinstance(value, dict):
                    if is_last:
                        break
                    logger.warning(
                        "配置文件 %s 中的路径 '%s' 不是字典结构，当前值: %r",
                        self.config_path,
                        ".".join(keys[:i]),
                        value,
                    )
                    return default
                if key not in value:
                    missing_path = ".".join(keys[: i + 1])
                    logger.warning(
                        "配置文件 %s 中未找到路径 '%s'，请确认 config.yml 是否包含该字段或更新环境变量。",
                        self.config_path,
                        missing_path,
                    )
                    return default
                value = value[key]

            logger.debug(
                "获取配置 %s: %s",
                key_path,
                self._sanitize_for_log(value, key_path),
            )
            return value
        except Exception as e:
            logger.warning(
                "获取配置 %s 时出错: %s, 使用默认值: %s",
                key_path,
                str(e),
                self._sanitize_for_log(default, key_path),
            )
            return default
    
    def get_config(self):
        """获取完整配置字典"""
        return self.config.copy()
    
    def reload_config(self):
        """重新加载配置文件"""
        self.load_config()

    @classmethod
    def _is_sensitive_segment(cls, segment):
        normalized = str(segment or "").strip().lower()
        if not normalized or normalized == "tokens":
            return False
        return any(marker in normalized for marker in cls._SENSITIVE_SEGMENT_MARKERS)

    @classmethod
    def _is_sensitive_key_path(cls, key_path):
        if not key_path:
            return False
        segments = str(key_path).replace("-", ".").split(".")
        return any(cls._is_sensitive_segment(segment) for segment in segments)

    @classmethod
    def _redact_value(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            if not value:
                return ""
            return f"<redacted len={len(value)}>"
        if isinstance(value, bytes):
            return f"<redacted bytes={len(value)}>"
        if isinstance(value, (list, tuple, set)):
            return f"<redacted items={len(value)}>"
        if isinstance(value, dict):
            return f"<redacted keys={len(value)}>"
        return "<redacted>"

    @classmethod
    def _sanitize_for_log(cls, value, key_path=""):
        if cls._is_sensitive_key_path(key_path):
            return cls._redact_value(value)

        if isinstance(value, dict):
            sanitized = {}
            for key, nested_value in value.items():
                child_path = f"{key_path}.{key}" if key_path else str(key)
                sanitized[key] = cls._sanitize_for_log(nested_value, child_path)
            return sanitized

        if isinstance(value, list):
            return [
                cls._sanitize_for_log(item, key_path)
                for item in value
            ]

        if isinstance(value, tuple):
            return tuple(
                cls._sanitize_for_log(item, key_path)
                for item in value
            )

        if isinstance(value, set):
            return {
                cls._sanitize_for_log(item, key_path)
                for item in value
            }

        return value
    
    @staticmethod
    def _list_to_dict(value):
        """将配置中的列表转换为可通过名称访问的字典"""
        result = {}
        for index, item in enumerate(value):
            if isinstance(item, dict):
                key = item.get('name') or str(index)
                result[key] = item
        return result or {str(index): item for index, item in enumerate(value)}


# 全局配置管理器实例
_config_manager = None


def get_config_manager():
    """获取全局配置管理器实例"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager


def get_config_value(key_path, default=None):
    """便捷函数：获取配置值"""
    return get_config_manager().get_config_value(key_path, default)


def load_config():
    """便捷函数：重新加载配置"""
    return get_config_manager().reload_config()
