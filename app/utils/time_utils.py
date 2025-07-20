"""Time handling utilities for subtitle processing."""

import logging
from typing import Union

logger = logging.getLogger(__name__)


def format_time(seconds: float) -> str:
    """
    Convert seconds to HH:MM:SS,mmm format.
    
    Args:
        seconds: Time in seconds
        
    Returns:
        Formatted time string in SRT format
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = seconds % 60
    milliseconds = int((seconds % 1) * 1000)
    seconds = int(seconds)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def parse_time(time_str: str) -> float:
    """
    Convert HH:MM:SS,mmm format time to seconds.
    
    Args:
        time_str: Time string in SRT format
        
    Returns:
        Time in seconds
    """
    try:
        # Handle milliseconds
        if ',' in time_str:
            time_str = time_str.replace(',', '.')
        
        # Split hours, minutes, seconds
        hours, minutes, seconds = time_str.split(':')
        total_seconds = float(hours) * 3600 + float(minutes) * 60 + float(seconds)
        return total_seconds
        
    except Exception as e:
        logger.error(f"Error parsing time string: {str(e)}, time string: {time_str}")
        return 0.0


def parse_time_str(time_str: str) -> float:
    """
    Parse SRT time string to seconds.
    
    Args:
        time_str: Time string in SRT format
        
    Returns:
        Time in seconds
        
    Note:
        This function is identical to parse_time() and may be consolidated
    """
    try:
        # Handle milliseconds
        if ',' in time_str:
            time_str = time_str.replace(',', '.')
        
        # Split hours, minutes, seconds
        hours, minutes, seconds = time_str.split(':')
        total_seconds = float(hours) * 3600 + float(minutes) * 60 + float(seconds)
        return total_seconds
        
    except Exception as e:
        logger.error(f"Error parsing time string: {str(e)}, time string: {time_str}")
        return 0.0


def seconds_to_time_components(seconds: float) -> tuple[int, int, int, int]:
    """
    Convert seconds to individual time components.
    
    Args:
        seconds: Time in seconds
        
    Returns:
        Tuple of (hours, minutes, seconds, milliseconds)
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remaining_seconds = seconds % 60
    milliseconds = int((remaining_seconds % 1) * 1000)
    remaining_seconds = int(remaining_seconds)
    
    return hours, minutes, remaining_seconds, milliseconds


def time_components_to_seconds(hours: int, minutes: int, seconds: int, milliseconds: int = 0) -> float:
    """
    Convert time components to total seconds.
    
    Args:
        hours: Hours component
        minutes: Minutes component
        seconds: Seconds component
        milliseconds: Milliseconds component
        
    Returns:
        Total time in seconds
    """
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000.0