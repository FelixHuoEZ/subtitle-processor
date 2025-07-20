"""File handling utilities."""

import os
import re
import chardet
import logging

logger = logging.getLogger(__name__)


def detect_file_encoding(raw_bytes: bytes) -> str:
    """
    Detect file encoding using multiple methods.
    
    Args:
        raw_bytes: Raw file bytes
        
    Returns:
        Detected encoding string
    """
    # Try using chardet detection
    result = chardet.detect(raw_bytes)
    if result['confidence'] > 0.7:
        return result['encoding']

    # Try common encodings
    encodings = ['utf-8', 'gbk', 'gb2312', 'utf-16', 'ascii']
    for encoding in encodings:
        try:
            raw_bytes.decode(encoding)
            return encoding
        except:
            continue

    return 'utf-8'  # Default to UTF-8


def sanitize_filename(filename: str) -> str:
    """
    Clean filename by removing unsafe characters and limiting length.
    
    Args:
        filename: Original filename
        
    Returns:
        Sanitized filename
    """
    try:
        # If filename is empty, use default name
        if not filename:
            return 'unnamed_file'
        
        # Separate filename and extension
        name, ext = os.path.splitext(filename)
        
        # Convert full-width characters to half-width
        name = re.sub(r'[\u3000！-～]', lambda x: chr(ord(x.group(0)) - 0xfee0), name)
        name = re.sub(r'[\uff61-\uff9f]', lambda x: chr(ord(x.group(0)) - 0xfee0), name)
        
        # Remove Windows illegal characters and other unwanted characters
        name = re.sub(r'[<>:"/\\|?*\[\]\{\}]', '', name)  # Remove Windows illegal chars
        name = re.sub(r'[\s　]+', ' ', name)  # Merge whitespace characters
        name = re.sub(r'[\u2026—–\-_\|\uff5c]+', '-', name)  # Unify separators
        name = re.sub(r'[-\s]+', '-', name)  # Merge consecutive separators
        
        # Remove control characters
        control_chars = ''.join(map(chr, list(range(0, 32)) + list(range(127, 160))))
        trans = str.maketrans('', '', control_chars)
        name = name.translate(trans)
        
        # Remove leading/trailing special characters
        name = name.strip('.-')
        
        # Limit filename length (excluding extension)
        max_length = 50  # Stricter length limit
        if len(name) > max_length:
            # Keep first half and last half, connect with ...
            half_length = (max_length - 3) // 2
            name = f"{name[:half_length]}...{name[-half_length:]}"
        
        # If filename is empty, use default name
        if not name:
            name = 'unnamed_file'
        
        # Combine filename
        clean_name = name + ext.lower()  # Convert extension to lowercase
        
        logger.info(f"Original filename: {filename}")
        logger.info(f"Processed filename: {clean_name}")
        
        return clean_name
        
    except Exception as e:
        logger.error(f"Error processing filename: {str(e)}")
        return 'error_filename'