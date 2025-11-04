"""File utility functions."""

import hashlib
import logging
import os
import re

import chardet

logger = logging.getLogger(__name__)


def detect_file_encoding(raw_bytes):
    """使用多种方法检测文件编码"""
    # 尝试使用chardet检测
    result = chardet.detect(raw_bytes)
    if result['confidence'] > 0.7:
        return result['encoding']

    # 尝试常见编码
    encodings = ['utf-8', 'gbk', 'gb2312', 'utf-16', 'ascii']
    for encoding in encodings:
        try:
            raw_bytes.decode(encoding)
            return encoding
        except:
            continue

    return 'utf-8'  # 默认使用UTF-8


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """按UTF-8字节长度安全截断字符串。

    Args:
        text: 原始文本
        max_bytes: 允许的最大字节数

    Returns:
        str: 截断后的字符串（不会切断多字节字符）
    """
    if max_bytes <= 0:
        return ""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    current_bytes = 0
    result_chars = []
    for char in text:
        char_bytes = char.encode("utf-8")
        if current_bytes + len(char_bytes) > max_bytes:
            break
        result_chars.append(char)
        current_bytes += len(char_bytes)
    return "".join(result_chars)


def sanitize_filename(filename):
    """清理文件名，移除不安全字符并限制长度"""
    # 替换Windows下的非法字符，包含常用全角符号
    illegal_chars = r'[<>:"/\\\\|?*\uff0f\uff1a\uff5c]'
    # 移除控制字符
    control_chars = ''.join(map(chr, list(range(0, 32)) + list(range(127, 160))))
    
    # 创建翻译表
    trans = str.maketrans('', '', control_chars)
    
    # 处理文件名
    clean_name = re.sub(illegal_chars, '_', filename)  # 替换非法字符为下划线
    clean_name = clean_name.translate(trans)  # 移除控制字符
    clean_name = re.sub(r'\s+', ' ', clean_name).strip()  # 规范空白字符
    clean_name = clean_name.replace('\\', '_').replace('/', '_')
    clean_name = clean_name.replace('\u3000', ' ')  # 全角空格
    clean_name = clean_name.replace('\uff5e', '~')
    hash_source = clean_name
    
    # 如果文件名为空，使用默认名称
    if not clean_name:
        clean_name = 'unnamed_file'
    
    # 限制文件名长度（不包括扩展名）
    name, ext = os.path.splitext(clean_name)
    if len(name) > 100:  # 设置合理的长度限制
        name = name[:97] + '...'  # 保留前97个字符，加上'...'

    # 限制文件名总字节长度（兼容多字节字符文件系统）
    max_total_bytes = 200
    ext_bytes = ext.encode("utf-8")
    base_bytes = name.encode("utf-8")

    if len(base_bytes) + len(ext_bytes) > max_total_bytes:
        ellipsis = '...'
        hash_suffix = '_' + hashlib.sha1(hash_source.encode('utf-8')).hexdigest()[:6]
        reserved_bytes = (
            len(ext_bytes)
            + len(ellipsis.encode("utf-8"))
            + len(hash_suffix.encode("utf-8"))
        )
        max_base_bytes = max_total_bytes - reserved_bytes
        if max_base_bytes <= 0:
            # 极端情况下，只能使用哈希作为文件名主体
            return f"{hashlib.sha1(hash_source.encode('utf-8')).hexdigest()[:16]}{ext}"
        truncated = _truncate_utf8(name, max_base_bytes)
        truncated = truncated.rstrip(' _-.')
        if not truncated:
            truncated = 'file'
        name = f"{truncated}{ellipsis}{hash_suffix}"

    clean_name = name + ext
        
    return clean_name


def split_into_sentences(text):
    """将文本分割成句子"""
    try:
        if not text:
            return []
        
        # 使用正则表达式分割句子
        # 匹配中文和英文的句号、问号、感叹号
        sentence_endings = r'[.!?。！？]+'
        sentences = re.split(sentence_endings, text)
        
        # 移除空字符串并清理空白
        sentences = [s.strip() for s in sentences if s.strip()]
        
        # 如果分割结果为空，返回原文本
        if not sentences:
            return [text.strip()]
        
        logger.debug(f"将文本分割为 {len(sentences)} 个句子")
        return sentences
        
    except Exception as e:
        logger.error(f"分割句子时出错: {str(e)}")
        return [text] if text else []
