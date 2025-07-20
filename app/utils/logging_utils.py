"""Logging utilities and formatters."""

import logging
import sys
from typing import Optional


class ColoredFormatter(logging.Formatter):
    """Custom logging formatter with color coding."""
    
    # Color codes
    grey = "\x1b[38;21m"
    blue = "\x1b[36m"
    yellow = "\x1b[33;21m"
    red = "\x1b[31;21m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    
    # Log format
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


def setup_logging(name: str = 'subtitle-processor', 
                 log_file: Optional[str] = 'subtitle_processor.log',
                 console_level: int = logging.DEBUG,
                 file_level: int = logging.INFO) -> logging.Logger:
    """
    Set up logging with console and file handlers.
    
    Args:
        name: Logger name
        log_file: Log file path (None to disable file logging)
        console_level: Console logging level
        file_level: File logging level
        
    Returns:
        Configured logger instance
    """
    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    
    # Remove existing handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(ColoredFormatter())
    logger.addHandler(console_handler)
    
    # Create file handler if specified
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(file_level)
        file_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        )
        logger.addHandler(file_handler)
    
    return logger