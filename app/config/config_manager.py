"""Configuration management for the subtitle processing application."""

import os
import yaml
import logging

logger = logging.getLogger(__name__)


class ConfigManager:
    """Central configuration manager for the application."""
    
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
                logger.debug(f"配置文件内容:\\n{content}")
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
                    logger.debug(f"解析后的配置: {loaded_config}")
                    
                    if self.config:
                        logger.info(f"配置加载成功，包含以下部分: {list(self.config.keys())}")
                        for section in self.config.keys():
                            logger.debug(f"配置部分 {section}: {self.config[section]}")
                    
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
                if not isinstance(value, dict):
                    logger.warning(f"配置路径 {'.'.join(keys[:i])} 的值不是字典: {value}")
                    return default
                if key not in value:
                    logger.warning(f"配置路径 {'.'.join(keys[:i+1])} 不存在")
                    return default
                value = value[key]
                
            logger.debug(f"获取配置 {key_path}: {value}")
            return value
        except Exception as e:
            logger.warning(f"获取配置 {key_path} 时出错: {str(e)}, 使用默认值: {default}")
            return default
    
    def get_config(self):
        """获取完整配置字典"""
        return self.config.copy()
    
    def reload_config(self):
        """重新加载配置文件"""
        self.load_config()


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
