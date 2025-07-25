"""File utility functions."""

import os
import re
import chardet
import logging

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


def sanitize_filename(filename):
    """清理文件名，移除不安全字符并限制长度"""
    # 替换Windows下的非法字符
    illegal_chars = r'[<>:"/\\\\|?*]'
    # 移除控制字符
    control_chars = ''.join(map(chr, list(range(0, 32)) + list(range(127, 160))))
    
    # 创建翻译表
    trans = str.maketrans('', '', control_chars)
    
    # 处理文件名
    clean_name = re.sub(illegal_chars, '_', filename)  # 替换非法字符为下划线
    clean_name = clean_name.translate(trans)  # 移除控制字符
    clean_name = clean_name.strip()  # 移除首尾空白
    
    # 如果文件名为空，使用默认名称
    if not clean_name:
        clean_name = 'unnamed_file'
    
    # 限制文件名长度（不包括扩展名）
    name, ext = os.path.splitext(clean_name)
    if len(name) > 100:  # 设置合理的长度限制
        name = name[:97] + '...'  # 保留前97个字符，加上'...'
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