"""Time utility functions for subtitle processing."""

import logging

logger = logging.getLogger(__name__)


def format_time(seconds):
    """将秒数转换为 HH:MM:SS,mmm 格式"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = seconds % 60
    milliseconds = int((seconds % 1) * 1000)
    seconds = int(seconds)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def parse_time(time_str):
    """将 HH:MM:SS,mmm 格式时间转换为秒数"""
    try:
        # 处理毫秒
        if ',' in time_str:
            time_str = time_str.replace(',', '.')
        
        # 分离时、分、秒
        hours, minutes, seconds = time_str.split(':')
        total_seconds = float(hours) * 3600 + float(minutes) * 60 + float(seconds)
        return total_seconds
        
    except Exception as e:
        logger.error(f"解析时间字符串出错: {str(e)}, 时间字符串: {time_str}")
        return 0.0


def parse_time_str(time_str):
    """解析SRT时间字符串为秒数（parse_time的别名，保持向后兼容）"""
    return parse_time(time_str)


def generate_srt_timestamps(sentences, total_duration=None):
    """为句子列表生成SRT时间戳
    
    Args:
        sentences: 句子列表
        total_duration: 总时长（秒），如果提供则平均分配时间
    
    Returns:
        list: 包含时间戳的字幕条目列表
    """
    try:
        if not sentences:
            return []
        
        subtitles = []
        num_sentences = len(sentences)
        
        if total_duration:
            # 根据总时长平均分配时间
            time_per_sentence = total_duration / num_sentences
        else:
            # 根据句子长度估算时间（每个字符约0.1秒）
            time_per_sentence = 3.0  # 默认3秒每句
        
        current_time = 0.0
        
        for i, sentence in enumerate(sentences):
            start_time = current_time
            
            # 根据句子长度调整时长
            if total_duration is None:
                # 估算阅读时间：中文约0.2秒/字，英文约0.05秒/字符
                char_count = len(sentence)
                estimated_duration = max(char_count * 0.15, 1.5)  # 最少1.5秒
                duration = min(estimated_duration, 8.0)  # 最多8秒
            else:
                duration = time_per_sentence
            
            end_time = start_time + duration
            
            subtitles.append({
                'index': i + 1,
                'start': start_time,
                'end': end_time,
                'duration': duration,
                'text': sentence.strip()
            })
            
            current_time = end_time
        
        logger.debug(f"为 {num_sentences} 个句子生成了时间戳，总时长: {current_time:.2f}秒")
        return subtitles
        
    except Exception as e:
        logger.error(f"生成SRT时间戳时出错: {str(e)}")
        return []