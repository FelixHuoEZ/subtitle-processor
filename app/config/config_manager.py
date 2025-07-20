"""Configuration management for the subtitle processing application."""

import os
import yaml
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ConfigManager:
    """Central configuration manager for the application."""
    
    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._config_path: Optional[str] = None
        self._initialize_config_path()
        self.load_config()
    
    def _initialize_config_path(self) -> None:
        """Initialize configuration file path with fallback logic."""
        container_config_path = '/app/config/config.yml'
        local_config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'config')
        local_config_path = os.path.join(local_config_dir, 'config.yml')
        
        # Prefer container config path
        self._config_path = container_config_path if os.path.exists(container_config_path) else local_config_path
        config_dir = os.path.dirname(self._config_path)
        
        # Ensure config directory exists
        if not os.path.exists(config_dir):
            try:
                os.makedirs(config_dir)
                logger.info(f"Created config directory: {config_dir}")
            except Exception as e:
                logger.error(f"Failed to create config directory: {str(e)}")
        
        logger.info(f"Config file path: {self._config_path}")
    
    def load_config(self) -> Dict[str, Any]:
        """Load YAML configuration file."""
        try:
            logger.info(f"Attempting to load config file: {self._config_path}")
            if not os.path.exists(self._config_path):
                logger.error(f"Config file does not exist: {self._config_path}")
                self._config = {}
                return self._config
                
            # Check file permissions
            if not os.access(self._config_path, os.R_OK):
                logger.error(f"Config file not readable: {self._config_path}")
                self._config = {}
                return self._config
            
            logger.info("Config file is readable")
                
            with open(self._config_path, 'r', encoding='utf-8') as f:
                content = f.read()
                logger.debug(f"Config file content:\n{content}")
                try:
                    loaded_config = yaml.safe_load(content)
                    if not loaded_config:
                        logger.error("Config file is empty or has invalid format")
                        self._config = {}
                        return self._config
                    if not isinstance(loaded_config, dict):
                        logger.error(f"Config file format error, expected dict, got: {type(loaded_config)}")
                        self._config = {}
                        return self._config
                    
                    self._config = loaded_config
                    logger.info("Successfully loaded config file")
                    logger.debug(f"Parsed config: {loaded_config}")
                    return self._config
                except yaml.YAMLError as e:
                    logger.error(f"YAML parsing error: {str(e)}")
                    self._config = {}
                    return self._config
        except Exception as e:
            logger.error(f"Failed to load config file: {str(e)}")
            self._config = {}
            return self._config
    
    def get(self, key_path: str, default: Any = None) -> Any:
        """
        Get configuration value using dot-separated path.
        
        Args:
            key_path: Dot-separated configuration path (e.g., 'tokens.openai.api_key')
            default: Default value if key not found
            
        Returns:
            Configuration value or default
        """
        try:
            if not self._config:
                logger.warning("Config object is empty")
                return default
                
            value = self._config
            keys = key_path.split('.')
            for i, key in enumerate(keys):
                if not isinstance(value, dict):
                    logger.warning(f"Config path {'.'.join(keys[:i])} value is not a dict: {value}")
                    return default
                if key not in value:
                    logger.warning(f"Config path {'.'.join(keys[:i+1])} does not exist")
                    return default
                value = value[key]
                
            logger.debug(f"Retrieved config {key_path}: {value}")
            return value
        except Exception as e:
            logger.warning(f"Error getting config {key_path}: {str(e)}, using default: {default}")
            return default
    
    def get_all(self) -> Dict[str, Any]:
        """Get all configuration as a dictionary."""
        return self._config.copy()
    
    def reload(self) -> None:
        """Reload configuration from file."""
        self.load_config()
    
    @property
    def config_path(self) -> Optional[str]:
        """Get the current configuration file path."""
        return self._config_path


# Global configuration manager instance
_config_manager = ConfigManager()


def get_config_value(key_path: str, default: Any = None) -> Any:
    """
    Get configuration value using dot-separated path.
    
    Args:
        key_path: Dot-separated configuration path (e.g., 'tokens.openai.api_key')
        default: Default value if key not found
        
    Returns:
        Configuration value or default
    """
    return _config_manager.get(key_path, default)


def load_config() -> Dict[str, Any]:
    """Load and return the full configuration."""
    return _config_manager.get_all()


def reload_config() -> None:
    """Reload configuration from file."""
    _config_manager.reload()


def get_config_manager() -> ConfigManager:
    """Get the global configuration manager instance."""
    return _config_manager