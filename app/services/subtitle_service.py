"""Subtitle processing service for handling SRT files and transcription results."""

import json
import re
import logging
from typing import List, Dict, Any, Optional
from ..utils.time_utils import format_time, parse_time, generate_srt_timestamps
from ..utils.file_utils import split_into_sentences

logger = logging.getLogger(__name__)


class SubtitleService:
    """字幕处理服务"""
    
    def __init__(self):
        """初始化字幕服务"""
        pass
    
    def parse_srt(self, result, hotwords=None):
        """解析FunASR的结果为SRT格式
        
        Args:
            result: FunASR的识别结果
            hotwords: 热词列表，用于日志记录和调试
            
        Returns:
            str: SRT格式的字幕内容
        """
        try:
            logger.info("开始解析字幕内容")
            logger.info(f"输入结果类型: {type(result)}")
            logger.info(f"输入结果是否为None: {result is None}")
            if result is not None:
                logger.info(f"输入结果内容: {json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else str(result)}")
            
            text_content = None
            timestamps = None
            duration = None
            
            # 如果结果是字符串，尝试解析为字典
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                    logger.debug("成功将字符串解析为字典")
                except json.JSONDecodeError:
                    logger.debug("输入是纯文本，直接使用")
                    text_content = result
            
            # 从字典中提取信息
            if isinstance(result, dict):
                # 获取音频时长
                if 'audio_info' in result and 'duration_seconds' in result['audio_info']:
                    duration = result['audio_info']['duration_seconds']
                    logger.debug(f"获取到音频时长: {duration}秒")
                
                # 获取文本内容
                if 'text' in result:
                    logger.info(f"找到text字段，类型: {type(result['text'])}")
                    logger.info(f"text字段原始值: {repr(result['text'])}")
                    if isinstance(result['text'], str):
                        text_content = result['text']
                        logger.info(f"成功提取文本内容，长度: {len(text_content)}")
                        logger.info(f"文本内容前200字符: {text_content[:200]}")
                    else:
                        logger.error(f"text字段不是字符串类型: {type(result['text'])}")
                        logger.error(f"text字段值: {result['text']}")
                        return None
                else:
                    logger.error(f"结果中没有text字段，可用字段: {list(result.keys())}")
                    logger.error(f"完整结果内容: {result}")
                    return None
                
                # 获取时间戳
                if 'timestamp' in result:
                    timestamps = result['timestamp']
                    if isinstance(timestamps, str):
                        try:
                            timestamps = json.loads(timestamps)
                            logger.debug("成功解析时间戳字符串")
                        except json.JSONDecodeError:
                            logger.warning("时间戳解析失败，将不使用时间戳")
                            timestamps = None
            
            # 如果没有文本内容，无法生成字幕
            if not text_content:
                logger.error("无法获取有效的文本内容")
                logger.error(f"text_content值: {repr(text_content)}")
                logger.error(f"原始result类型: {type(result)}")
                logger.error(f"原始result内容: {result}")
                return None
            
            # 清理文本内容
            logger.info(f"清理前文本内容长度: {len(text_content)}")
            text_content = text_content.strip()
            logger.info(f"清理后文本内容长度: {len(text_content)}")
            if not text_content:
                logger.error("清理后文本内容为空")
                logger.error(f"清理后text_content值: {repr(text_content)}")
                return None
            
            # 生成SRT格式
            return self._generate_srt_from_text(text_content, timestamps, duration, hotwords)
            
        except Exception as e:
            logger.error(f"解析字幕时出错: {str(e)}")
            return None
    
    def _generate_srt_from_text(self, text_content, timestamps=None, duration=None, hotwords=None):
        """从文本内容生成SRT格式字幕
        
        Args:
            text_content: 文本内容
            timestamps: 时间戳信息
            duration: 音频总时长
            hotwords: 热词列表
            
        Returns:
            str: SRT格式字幕
        """
        try:
            # 分割成句子
            sentences = split_into_sentences(text_content)
            if not sentences:
                logger.error("无法分割句子")
                return None
            
            logger.info(f"分割得到 {len(sentences)} 个句子")
            
            # 生成时间戳
            subtitles = generate_srt_timestamps(sentences, duration)
            if not subtitles:
                logger.error("生成时间戳失败")
                return None
            
            # 转换为SRT格式
            srt_lines = []
            for subtitle in subtitles:
                srt_lines.extend([
                    str(subtitle['index']),
                    f"{format_time(subtitle['start'])} --> {format_time(subtitle['end'])}",
                    subtitle['text'],
                    ""
                ])
            
            srt_content = "\\n".join(srt_lines)
            logger.info(f"成功生成SRT格式字幕，共 {len(subtitles)} 条")
            return srt_content
            
        except Exception as e:
            logger.error(f"生成SRT格式字幕时出错: {str(e)}")
            return None
    
    def parse_srt_content(self, srt_content):
        """解析SRT格式字幕内容
        
        Args:
            srt_content (str): SRT格式的字幕内容或转录结果
            
        Returns:
            list: 解析后的字幕列表，每个字幕包含id、start、end、duration和text字段
        """
        if not srt_content or not isinstance(srt_content, str):
            logger.error("无效的字幕内容")
            return []
        
        # 记录原始内容
        logger.info(f"开始解析字幕内容，长度：{len(srt_content)}")
        logger.debug(f"字幕内容前100个字符: {srt_content[:100]}")
        
        # 检查是否是转录结果（没有时间戳）
        if not re.search(r'\\d+:\\d+:\\d+', srt_content):
            logger.info("检测到内容是转录结果，需要生成时间戳")
            return self._parse_transcript_content(srt_content)
        
        # 解析标准SRT格式
        return self._parse_standard_srt(srt_content)
    
    def _parse_transcript_content(self, content):
        """解析转录内容并生成时间戳"""
        try:
            # 将文本分割成句子
            sentences = split_into_sentences(content)
            if not sentences:
                logger.error("无法分割句子或句子列表为空")
                return []
            
            logger.info(f"分割得到 {len(sentences)} 个句子")
            
            # 生成时间戳
            subtitles = generate_srt_timestamps(sentences)
            
            # 转换为标准格式
            result = []
            for subtitle in subtitles:
                result.append({
                    'id': subtitle['index'],
                    'start': subtitle['start'],
                    'end': subtitle['end'],
                    'duration': subtitle['duration'],
                    'text': subtitle['text']
                })
            
            logger.info(f"成功解析转录内容为 {len(result)} 条字幕")
            return result
            
        except Exception as e:
            logger.error(f"解析转录内容时出错: {str(e)}")
            return []
    
    def _parse_standard_srt(self, srt_content):
        """解析标准SRT格式内容"""
        try:
            subtitles = []
            blocks = re.split(r'\\n\\s*\\n', srt_content.strip())
            
            for i, block in enumerate(blocks, 1):
                if not block.strip():
                    continue
                
                lines = block.strip().split('\\n')
                if len(lines) < 3:
                    logger.warning(f"字幕块 {i} 格式不完整，跳过")
                    continue
                
                try:
                    # 解析序号
                    subtitle_id = int(lines[0].strip())
                    
                    # 解析时间轴
                    time_line = lines[1].strip()
                    time_match = re.match(r'([\\d:,]+)\\s*-->\\s*([\\d:,]+)', time_line)
                    if not time_match:
                        logger.warning(f"字幕块 {i} 时间轴格式错误，跳过")
                        continue
                    
                    start_time = parse_time(time_match.group(1))
                    end_time = parse_time(time_match.group(2))
                    duration = end_time - start_time
                    
                    # 解析文本内容
                    text_lines = lines[2:]
                    text = '\\n'.join(text_lines).strip()
                    
                    if text:
                        subtitles.append({
                            'id': subtitle_id,
                            'start': start_time,
                            'end': end_time,
                            'duration': duration,
                            'text': text
                        })
                    
                except (ValueError, IndexError) as e:
                    logger.warning(f"解析字幕块 {i} 时出错: {str(e)}")
                    continue
            
            logger.info(f"成功解析SRT内容为 {len(subtitles)} 条字幕")
            return subtitles
            
        except Exception as e:
            logger.error(f"解析SRT内容时出错: {str(e)}")
            return []
    
    def convert_to_srt(self, content, format_type='json3'):
        """将不同格式的字幕内容转换为SRT格式
        
        Args:
            content: 字幕内容
            format_type: 内容格式类型
            
        Returns:
            str: SRT格式字幕内容
        """
        try:
            if format_type == 'json3':
                # 处理JSON3格式（YouTube等平台）
                return self._convert_json3_to_srt(content)
            else:
                logger.warning(f"不支持的格式类型: {format_type}")
                return None
        except Exception as e:
            logger.error(f"转换字幕格式时出错: {str(e)}")
            return None
    
    def _convert_json3_to_srt(self, content):
        """将JSON3格式转换为SRT格式"""
        try:
            if isinstance(content, str):
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    # 如果不是JSON，当作纯文本处理
                    return self.parse_srt(content)
            else:
                data = content
            
            if not isinstance(data, dict) or 'events' not in data:
                logger.error("JSON3格式数据无效")
                return None
            
            events = data['events']
            srt_lines = []
            subtitle_index = 1
            
            for event in events:
                if 'segs' not in event:
                    continue
                
                start_time = event.get('tStartMs', 0) / 1000.0
                duration = event.get('dDurationMs', 3000) / 1000.0
                end_time = start_time + duration
                
                # 合并所有文本段
                text_parts = []
                for seg in event['segs']:
                    if 'utf8' in seg:
                        text_parts.append(seg['utf8'])
                
                text = ''.join(text_parts).strip()
                if text:
                    srt_lines.extend([
                        str(subtitle_index),
                        f"{format_time(start_time)} --> {format_time(end_time)}",
                        text,
                        ""
                    ])
                    subtitle_index += 1
            
            if srt_lines:
                return "\\n".join(srt_lines)
            else:
                logger.warning("JSON3转换后没有有效内容")
                return None
                
        except Exception as e:
            logger.error(f"转换JSON3格式时出错: {str(e)}")
            return None
    
    def clean_subtitle_content(self, content, is_funasr=False):
        """清理字幕内容"""
        try:
            if not content:
                return None
                
            # 如果是 FunASR 的结果，需要特殊处理
            if is_funasr:
                # 移除多余的标点符号
                content = re.sub(r'[,.，。]+(?=[,.，。])', '', content)
                # 移除重复的空格
                content = re.sub(r'\\s+', ' ', content)
                # 移除空行
                content = '\\n'.join(line for line in content.split('\\n') if line.strip())
                return content
                
            # 移除空行
            lines = content.split('\\n')
            cleaned_lines = []
            for line in lines:
                line = line.strip()
                if line:
                    cleaned_lines.append(line)
            
            # 重新组合
            return '\\n'.join(cleaned_lines)
        except Exception as e:
            logger.error(f"清理字幕内容时出错: {str(e)}")
            return content
    
    def process_subtitle_content(self, content, **kwargs):
        """处理字幕内容的通用接口
        
        Args:
            content: 字幕内容
            **kwargs: 其他处理参数
            
        Returns:
            str: 处理后的字幕内容
        """
        try:
            is_funasr = kwargs.get('is_funasr', False)
            translate = kwargs.get('translate', False)
            language = kwargs.get('language')
            hotwords = kwargs.get('hotwords')
            
            # 清理内容
            cleaned_content = self.clean_subtitle_content(content, is_funasr)
            
            # 如果需要翻译，这里可以集成翻译服务
            if translate and language:
                logger.info(f"需要翻译内容，语言: {language}")
                # TODO: 集成翻译服务
            
            return cleaned_content
            
        except Exception as e:
            logger.error(f"处理字幕内容时出错: {str(e)}")
            return content