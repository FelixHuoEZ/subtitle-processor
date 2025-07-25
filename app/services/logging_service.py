"""Logging service for the subtitle processing application."""

import logging
import sys


class ColoredFormatter(logging.Formatter):
    """自定义的日志格式化器，添加颜色"""
    
    # 颜色代码
    grey = "\\x1b[38;21m"
    blue = "\\x1b[36m"
    yellow = "\\x1b[33;21m"
    red = "\\x1b[31;21m"
    bold_red = "\\x1b[31;1m"
    reset = "\\x1b[0m"
    
    # 日志格式
    format_str = '%(asctime)s - %(levelname)s - %(message)s'
    
    FORMATS = {
        logging.DEBUG: blue + format_str + reset,
        logging.INFO: grey + format_str + reset,
        logging.WARNING: yellow + format_str + reset,
        logging.ERROR: red + format_str + reset,
        logging.CRITICAL: bold_red + format_str + reset
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt='%Y-%m-%d %H:%M:%S')
        return formatter.format(record)


class LoggingService:
    """日志服务管理器"""
    
    def __init__(self, logger_name='subtitle-processor', log_file='subtitle_processor.log'):
        """初始化日志服务
        
        Args:
            logger_name: logger名称
            log_file: 日志文件路径
        """
        self.logger_name = logger_name
        self.log_file = log_file
        self.logger = None
        self._setup_logger()
    
    def _setup_logger(self):
        """设置logger"""
        # 创建logger
        self.logger = logging.getLogger(self.logger_name)
        self.logger.setLevel(logging.DEBUG)  # 设置为DEBUG级别以捕获所有日志
        self.logger.propagate = True  # 确保日志可以传播
        
        # 先移除所有已存在的处理器
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
        
        # 创建控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)  # 明确指定输出到 stdout
        console_handler.setLevel(logging.DEBUG)  # 控制台显示所有级别
        console_handler.setFormatter(ColoredFormatter())
        
        # 创建文件处理器
        file_handler = logging.FileHandler(self.log_file)
        file_handler.setLevel(logging.INFO)  # 文件只记录INFO及以上级别
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        
        # 添加处理器到logger
        self.logger.addHandler(console_handler)
        self.logger.addHandler(file_handler)
    
    def get_logger(self):
        """获取配置好的logger"""
        return self.logger
    
    def set_level(self, level):
        """设置日志级别
        
        Args:
            level: 日志级别 (logging.DEBUG, logging.INFO, etc.)
        """
        self.logger.setLevel(level)
    
    def add_handler(self, handler):
        """添加日志处理器
        
        Args:
            handler: logging.Handler实例
        """
        self.logger.addHandler(handler)
    
    def remove_handler(self, handler):
        """移除日志处理器
        
        Args:
            handler: logging.Handler实例
        """
        self.logger.removeHandler(handler)


# 全局日志服务实例
_logging_service = None


def get_logging_service(logger_name='subtitle-processor', log_file='subtitle_processor.log'):
    """获取全局日志服务实例"""
    global _logging_service
    if _logging_service is None:
        _logging_service = LoggingService(logger_name, log_file)
    return _logging_service


def setup_logging(logger_name='subtitle-processor', log_file='subtitle_processor.log'):
    """便捷函数：设置日志并返回logger"""
    service = get_logging_service(logger_name, log_file)
    return service.get_logger()


# 创建默认logger供向后兼容
logger = setup_logging()