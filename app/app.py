import os
import json
import logging
import requests
import chardet
import traceback
import subprocess
import re
import yt_dlp
import sys
from flask import Flask, request, jsonify, send_file, render_template, render_template_string
from flask_cors import CORS
from datetime import datetime
import pytz
import uuid
import codecs
import binascii
import ast
import socket
import time
import math
from pydub import AudioSegment
import wave

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# 启用CORS，允许所有域名访问
CORS(app, resources={
    r"/upload": {"origins": "*"},
    r"/view/*": {"origins": "*"},
    r"/process_youtube": {"origins": "*"},
    r"/process": {"origins": "*"}
})

app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max-limit
app.config['UPLOAD_FOLDER'] = '/app/uploads'  # 上传文件存储路径
app.config['OUTPUT_FOLDER'] = '/app/outputs'  # 输出文件存储路径

# 确保目录存在
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# Store uploaded files and their corresponding URLs
UPLOAD_FOLDER = app.config['UPLOAD_FOLDER']
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# 存储文件信息的JSON文件
FILES_INFO = 'uploads/files_info.json'
if not os.path.exists(FILES_INFO):
    with open(FILES_INFO, 'w', encoding='utf-8') as f:
        json.dump({}, f, ensure_ascii=False)

def migrate_files_info():
    """将文件信息从列表格式迁移到字典格式"""
    try:
        with open(FILES_INFO, 'r', encoding='utf-8') as f:
            old_files_info = json.load(f)
            
        if isinstance(old_files_info, list):
            new_files_info = {}
            for file_info in old_files_info:
                if 'id' in file_info:
                    new_files_info[file_info['id']] = file_info
            
            with open(FILES_INFO, 'w', encoding='utf-8') as f:
                json.dump(new_files_info, f, ensure_ascii=False, indent=2)
                
            logger.info("成功将文件信息从列表格式迁移到字典格式")
            return new_files_info
    except Exception as e:
        logger.error(f"迁移文件信息时出错: {str(e)}")
        return {}

def load_files_info():
    """加载文件信息"""
    try:
        with open(FILES_INFO, 'r', encoding='utf-8') as f:
            files_info = json.load(f)
            
        # 如果是旧的列表格式，进行迁移
        if isinstance(files_info, list):
            files_info = migrate_files_info()
            
        return files_info
    except Exception as e:
        logger.error(f"加载文件信息时出错: {str(e)}")
        return {}

def save_files_info(files_info):
    """保存文件信息"""
    try:
        with open(FILES_INFO, 'w', encoding='utf-8') as f:
            json.dump(files_info, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存文件信息时出错: {str(e)}")

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

def parse_srt(result):
    """解析FunASR的结果为SRT格式"""
    try:
        logger.info("开始解析字幕内容")
        
        # 获取text内容
        if isinstance(result, dict):
            text_content = None
            timestamps = None
            
            # 尝试获取text内容
            if 'text' in result:
                if isinstance(result['text'], str):
                    try:
                        # 尝试解析字符串形式的字典
                        import ast
                        text_dict = ast.literal_eval(result['text'])
                        if isinstance(text_dict, dict) and 'text' in text_dict:
                            text_content = text_dict['text']
                        else:
                            text_content = result['text']
                    except:
                        text_content = result['text']
                else:
                    text_content = result['text']
            
            # 尝试获取timestamp
            if 'timestamp' in result:
                timestamps = result['timestamp']
                if isinstance(timestamps, str):
                    try:
                        timestamps = ast.literal_eval(timestamps)
                    except:
                        timestamps = None
            
            if not text_content:
                logger.error("未找到有效的文本内容")
                return None
            
            # 如果有有效的时间戳，使用时间戳生成字幕
            if isinstance(timestamps, list) and len(timestamps) > 0:
                subtitles = []
                current_text = []
                current_start = timestamps[0][0]
                current_end = timestamps[0][1]
                
                for i, (start, end) in enumerate(timestamps):
                    # 将毫秒转换为秒
                    start_sec = start / 1000.0
                    end_sec = end / 1000.0
                    
                    # 获取当前时间段的文本
                    if i < len(text_content):
                        char = text_content[i]
                        current_text.append(char)
                        
                        # 判断是否需要结束当前字幕
                        is_sentence_end = char in '.。!！?？;；'
                        is_too_long = len(''.join(current_text)) >= 25
                        is_long_pause = i < len(timestamps) - 1 and timestamps[i+1][0] - end > 800
                        is_natural_break = char in '，,、' and len(''.join(current_text)) >= 10
                        
                        if is_sentence_end or is_too_long or is_long_pause or is_natural_break:
                            if current_text:  # 确保有文本内容
                                subtitle = {
                                    'start': current_start / 1000.0,
                                    'duration': (end - current_start) / 1000.0,
                                    'text': ''.join(current_text).strip()
                                }
                                subtitles.append(subtitle)
                                current_text = []
                                if i < len(timestamps) - 1:
                                    current_start = timestamps[i+1][0]
                
                # 处理最后剩余的文本
                if current_text:
                    subtitle = {
                        'start': current_start / 1000.0,
                        'duration': (timestamps[-1][1] - current_start) / 1000.0,
                        'text': ''.join(current_text).strip()
                    }
                    subtitles.append(subtitle)
                
                logger.info(f"使用时间戳生成了 {len(subtitles)} 条字幕")
                return subtitles
            
            # 如果没有时间戳或解析失败，使用默认的分割方法
            sentences = split_into_sentences(text_content)
            subtitles = []
            
            total_duration = 5.0 * len(sentences)  # 估计总时长
            current_time = 0.0
            
            for sentence in sentences:
                # 根据句子长度动态计算持续时间
                duration = max(2.0, min(5.0, len(sentence) * 0.25))
                
                subtitle = {
                    'start': current_time,
                    'duration': duration,
                    'text': sentence.strip()
                }
                subtitles.append(subtitle)
                current_time += duration
            
            logger.info(f"使用默认分割方法生成了 {len(subtitles)} 条字幕")
            # 只显示前两条和最后两条字幕作为示例
            if subtitles:
                logger.debug("生成的SRT内容示例:")
                for i, sub in enumerate(subtitles[:2], 1):
                    logger.debug(f"{i}\n{format_time(sub['start'])} --> {format_time(sub['start'] + sub['duration'])}\n{sub['text']}\n")
                if len(subtitles) > 4:
                    logger.debug("...... 中间内容省略 ......")
                for i, sub in enumerate(subtitles[-2:], len(subtitles)-1):
                    logger.debug(f"{i}\n{format_time(sub['start'])} --> {format_time(sub['start'] + sub['duration'])}\n{sub['text']}\n")
            return subtitles
        
        logger.error("结果格式不正确")
        return None
        
    except Exception as e:
        logger.error(f"解析字幕时出错: {str(e)}")
        return None

def split_into_sentences(text):
    """将文本分割成句子"""
    # 定义句子结束的标点符号
    sentence_ends = '.。!！?？;；'
    natural_breaks = '，,、'
    
    # 如果文本很短，直接返回
    if len(text) < 15:  # 减小最小长度阈值
        return [text]
    
    sentences = []
    current_sentence = []
    
    for i, char in enumerate(text):
        current_sentence.append(char)
        current_text = ''.join(current_sentence)
        
        # 判断是否需要在这里分割句子
        is_sentence_end = char in sentence_ends
        is_natural_break = char in natural_breaks and len(current_text) >= 15
        is_too_long = len(current_text) >= 25
        is_last_char = i == len(text) - 1
        
        if is_sentence_end or is_natural_break or is_too_long or is_last_char:
            if len(current_text.strip()) >= 2:  # 确保句子至少有2个字符
                sentences.append(current_text)
                current_sentence = []
                continue
            
    # 处理最后剩余的文本
    if current_sentence and len(''.join(current_sentence).strip()) >= 2:
        sentences.append(''.join(current_sentence))
    
    # 如果没有找到任何有效句子，返回原始文本
    return sentences if sentences else [text]

def download_youtube_subtitles(url, video_info=None):
    """下载YouTube视频字幕
    
    Args:
        url: YouTube视频URL
        video_info: 预先获取的视频信息（可选）
    
    Returns:
        tuple: (字幕内容, 视频信息)
    """
    try:
        logger.info(f"开始下载YouTube字幕: {url}")
        
        # 如果没有提供视频信息，获取视频信息
        if not video_info:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                video_info = ydl.extract_info(url, download=False)
        
        # 获取视频语言和字幕策略
        language = get_video_language(video_info)
        should_download, target_languages = get_subtitle_strategy(language, video_info)
        
        if not should_download:
            logger.info(f"根据策略决定不下载字幕，视频语言: {language}")
            return None, video_info
            
        logger.info(f"视频语言: {language}, 目标字幕语言: {target_languages}")
        
        # 尝试下载字幕
        for target_lang in target_languages:
            # 首先尝试下载手动字幕
            if video_info.get('subtitles'):
                manual_subs = video_info['subtitles']
                for lang, sub_info in manual_subs.items():
                    if lang.startswith(target_lang):
                        for fmt in sub_info:
                            if fmt.get('ext') in ['vtt', 'srt', 'json3']:
                                subtitle_url = fmt['url']
                                content = download_subtitle_content(subtitle_url)
                                if content:
                                    logger.info(f"成功下载手动字幕，语言: {lang}")
                                    return content, video_info
            
            # 如果是英文视频且没有手动字幕，尝试下载自动字幕
            if language == 'en' and video_info.get('automatic_captions'):
                auto_subs = video_info['automatic_captions']
                for lang, sub_info in auto_subs.items():
                    if lang.startswith(target_lang):
                        for fmt in sub_info:
                            if fmt.get('ext') in ['vtt', 'srt', 'json3']:
                                subtitle_url = fmt['url']
                                content = download_subtitle_content(subtitle_url)
                                if content:
                                    logger.info(f"成功下载自动字幕，语言: {lang}")
                                    return content, video_info
        
        logger.info("未找到合适的字幕")
        return None, video_info
        
    except Exception as e:
        logger.error(f"下载YouTube字幕时出错: {str(e)}")
        return None, video_info

def download_subtitles(url, platform, video_info=None):
    """统一的字幕下载入口
    
    Args:
        url: 视频URL
        platform: 平台（'youtube' 或 'bilibili'）
        video_info: 预先获取的视频信息（可选）
    
    Returns:
        tuple: (字幕内容, 视频信息)
    """
    try:
        if platform == 'youtube':
            return download_youtube_subtitles(url, video_info)
        elif platform == 'bilibili':
            return download_bilibili_subtitles(url, video_info)
        else:
            logger.error(f"不支持的平台: {platform}")
            return None, None
    except Exception as e:
        logger.error(f"下载字幕时出错: {str(e)}")
        return None, None

def download_subtitle_content(subtitle_url):
    """下载并处理字幕内容"""
    # 优先使用srt格式
    try:
        # 下载字幕内容
        logger.info(f"正在从URL下载字幕: {subtitle_url}")
        response = requests.get(subtitle_url, timeout=30)
        response.raise_for_status()
        
        # 获取字幕内容的字节数据
        content_bytes = response.content
        
        # 检查内容是否为空
        if not content_bytes:
            logger.warning("字幕内容为空")
            return None
        
        # 检测编码并解码
        encoding = detect_file_encoding(content_bytes)
        subtitle_content = content_bytes.decode(encoding)
        
        # 清理字幕内容
        subtitle_content = clean_subtitle_content(subtitle_content)
        
        # 如果不是SRT格式，尝试转换
        if subtitle_url.endswith('.vtt'):
            logger.info("将VTT格式转换为SRT格式")
            subtitle_content = convert_to_srt(subtitle_content, 'vtt')
        
        # 验证字幕内容
        is_valid, message = validate_subtitle_content(subtitle_content)
        if not is_valid:
            logger.warning(f"字幕内容无效: {message}")
            return None
        
        logger.info("成功下载并处理字幕")
        logger.debug(f"字幕内容预览: {subtitle_content[:200]}...")
        return subtitle_content
        
    except Exception as e:
        logger.error(f"处理字幕时出错: {str(e)}")
        return None

def clean_subtitle_content(content):
    """清理字幕内容，去除无用的标记和空行"""
    # 移除 BOM
    content = content.replace('\ufeff', '')
    
    # 移除 HTML 标签
    content = re.sub(r'<[^>]+>', '', content)
    
    # 移除多余的空行
    lines = [line.strip() for line in content.splitlines()]
    lines = [line for line in lines if line]
    
    # 移除字幕中的样式标记 (比如 {\\an8} 这样的标记)
    lines = [re.sub(r'\{\\[^}]+\}', '', line) for line in lines]
    
    return '\n'.join(lines)

def convert_to_srt(content, input_format):
    """将其他格式的字幕转换为SRT格式"""
    if input_format == 'vtt':
        # 移除 WEBVTT 头部
        content = re.sub(r'^WEBVTT\n', '', content)
        # 移除 VTT 特有的样式信息
        content = re.sub(r'STYLE\n.*?\n\n', '', content, flags=re.DOTALL)
        # 转换时间戳格式 (如果需要)
        content = re.sub(r'(\d{2}):(\d{2}):(\d{2})\.(\d{3})', r'\1:\2:\3,\4', content)
        return content
    elif input_format == 'json3':
        try:
            # 解析JSON内容
            data = json.loads(content)
            if not isinstance(data, dict) or 'events' not in data:
                logger.error("无效的json3格式：缺少events字段")
                return None
            
            events = data['events']
            if not events:
                logger.error("无效的json3格式：events为空")
                return None
            
            # 转换为SRT格式
            srt_lines = []
            for i, event in enumerate(events, 1):
                # 检查必要的字段
                if 'tStartMs' not in event or 'dDurationMs' not in event or 'segs' not in event:
                    continue
                
                # 获取开始时间和持续时间
                start_ms = event['tStartMs']
                duration_ms = event['dDurationMs']
                end_ms = start_ms + duration_ms
                
                # 转换为SRT时间格式
                start_time = format_time(start_ms / 1000)
                end_time = format_time(end_ms / 1000)
                
                # 获取文本内容
                text = ''.join(seg.get('utf8', '') for seg in event['segs'] if 'utf8' in seg)
                if not text.strip():
                    continue
                
                # 添加SRT条目
                srt_lines.extend([
                    str(i),
                    f"{start_time} --> {end_time}",
                    text.strip(),
                    ""
                ])
            
            if not srt_lines:
                logger.error("转换后的SRT内容为空")
                return None
            
            return "\n".join(srt_lines)
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析错误: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"转换json3格式时出错: {str(e)}")
            return None
    
    logger.error(f"不支持的字幕格式: {input_format}")
    return None

def validate_subtitle_content(content):
    """验证字幕内容是否有效"""
    # 检查基本内容
    if not content or not content.strip():
        return False, "字幕内容为空"
    
    # 检查是否包含时间戳
    if '-->' not in content:
        return False, "未找到时间戳"
    
    # 检查时间戳格式
    timestamp_pattern = r'\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}'
    if not re.search(timestamp_pattern, content):
        return False, "时间戳格式无效"
    
    # 检查是否有实际的文本内容
    text_lines = [line.strip() for line in content.split('\n') if line.strip() and '-->' not in line and not line.strip().isdigit()]
    if not text_lines:
        return False, "没有有效的字幕文本"
    
    return True, "字幕内容有效"

def download_video(url):
    """下载视频并提取音频"""
    try:
        # 创建临时目录
        temp_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        
        # 设置下载选项
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
            }],
            'outtmpl': os.path.join(temp_dir, '%(id)s.%(ext)s'),
            'quiet': True
        }
        
        # 下载视频并提取音频
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info['id']
            audio_path = os.path.join(temp_dir, f"{video_id}.wav")
            
            if not os.path.exists(audio_path):
                logger.error(f"音频文件不存在: {audio_path}")
                return None
                
            return audio_path
            
    except Exception as e:
        logger.error(f"下载视频时出错: {str(e)}")
        return None

def split_audio(audio_path, max_duration=600, max_size=100*1024*1024):
    """将音频文件分割成更小的片段
    
    Args:
        audio_path: 音频文件路径
        max_duration: 每个片段的最大时长（秒）
        max_size: 每个片段的最大大小（字节）
    
    Returns:
        分割后的音频文件路径列表
    """
    try:
        import wave
        import math
        from pydub import AudioSegment
        
        # 获取音频文件信息
        audio = AudioSegment.from_wav(audio_path)
        duration_ms = len(audio)
        file_size = os.path.getsize(audio_path)
        
        # 计算需要分割的片段数
        num_segments = max(
            math.ceil(duration_ms / (max_duration * 1000)),  # 基于时长
            math.ceil(file_size / max_size)  # 基于文件大小
        )
        
        if num_segments <= 1:
            return [audio_path]
        
        # 计算每个片段的时长
        segment_duration = duration_ms / num_segments
        
        # 分割音频
        output_paths = []
        for i in range(num_segments):
            start_ms = int(i * segment_duration)
            end_ms = int((i + 1) * segment_duration)
            
            # 提取片段
            segment = audio[start_ms:end_ms]
            
            # 生成输出路径
            base_path = os.path.splitext(audio_path)[0]
            output_path = f"{base_path}_part{i+1}.wav"
            
            # 导出片段
            segment.export(output_path, format="wav")
            output_paths.append(output_path)
            
            logger.info(f"生成音频片段 {i+1}/{num_segments}: {output_path}")
        
        return output_paths
        
    except Exception as e:
        logger.error(f"分割音频文件时出错: {str(e)}")
        return [audio_path]

def transcribe_audio(audio_path):
    """使用FunASR转录音频"""
    try:
        # 检查音频文件大小
        file_size = os.path.getsize(audio_path)
        logger.info(f"音频文件大小: {file_size} bytes")
        
        # 如果文件太大，先分割
        if file_size > 100*1024*1024:  # 100MB
            logger.info("音频文件过大，进行分割处理")
            audio_segments = split_audio(audio_path)
        else:
            audio_segments = [audio_path]
        
        all_results = []
        
        # 处理每个音频片段
        for i, segment_path in enumerate(audio_segments, 1):
            logger.info(f"处理音频片段 {i}/{len(audio_segments)}: {segment_path}")
            
            # 准备请求
            url = 'http://transcribe-audio:10095/recognize'
            
            # 解析域名
            import socket
            try:
                ip = socket.gethostbyname('transcribe-audio')
                logger.info(f"transcribe-audio DNS解析结果: {ip}")
            except Exception as e:
                logger.error(f"DNS解析失败: {str(e)}")
            
            logger.info("开始发送请求...")
            
            # 发送文件
            with open(segment_path, 'rb') as f:
                files = {'audio': f}
                response = requests.post(url, files=files)
            
            logger.info(f"FunASR响应状态码: {response.status_code}")
            logger.info(f"FunASR响应头: {dict(response.headers)}")
            
            if response.status_code != 200:
                logger.error(f"FunASR服务返回错误: {response.status_code}")
                logger.error(f"错误响应内容: {response.text}")
                continue
            
            try:
                result = response.json()
                all_results.append(result)
            except Exception as e:
                logger.error(f"解析JSON响应时出错: {str(e)}")
                continue
        
        # 合并所有结果
        if not all_results:
            return None
            
        # 如果只有一个结果，直接返回
        if len(all_results) == 1:
            return all_results[0]
        
        # 合并多个结果
        merged_result = {
            'text': '',
            'timestamp': []
        }
        
        last_end_time = 0
        for result in all_results:
            if isinstance(result, dict):
                # 合并文本
                if 'text' in result:
                    merged_result['text'] += result['text']
                
                # 合并时间戳
                if 'timestamp' in result and isinstance(result['timestamp'], list):
                    # 调整时间戳
                    adjusted_timestamps = []
                    for start, end in result['timestamp']:
                        adjusted_timestamps.append([start + last_end_time, end + last_end_time])
                    merged_result['timestamp'].extend(adjusted_timestamps)
                    
                    # 更新最后的结束时间
                    if adjusted_timestamps:
                        last_end_time = adjusted_timestamps[-1][1]
        
        return merged_result
            
    except Exception as e:
        logger.error(f"音频转录时出错: {str(e)}")
        return None

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
        h, m, s = time_str.replace(',', '.').split(':')
        return float(h) * 3600 + float(m) * 60 + float(s)
    except:
        return 0

def convert_youtube_url(url):
    """将YouTube URL转换为Gauss Surf格式"""
    try:
        # 处理不同格式的YouTube URL
        if 'youtu.be/' in url:
            video_id = url.split('youtu.be/')[-1].split('?')[0]
        elif 'youtube.com/watch' in url:
            video_id = url.split('v=')[-1].split('&')[0]
        else:
            video_id = url  # 假设直接传入了video_id

        return f"http://read.gauss.surf/youtube/{video_id}"
    except Exception as e:
        logger.error(f"转换YouTube URL时出错: {str(e)}")
        return url

def get_youtube_info(url):
    """获取YouTube视频信息，包括标题和发布日期"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False  # 需要完整的元数据
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # 获取发布日期并转换为ISO 8601格式
            upload_date = info.get('upload_date')  # 格式：YYYYMMDD
            if upload_date:
                published_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}T00:00:00+00:00"
            else:
                published_date = None
                
            return {
                'title': info.get('title', ''),
                'published_date': published_date
            }
    except Exception as e:
        logger.error(f"获取YouTube信息时出错: {str(e)}")
        return {'title': None, 'published_date': None}

def get_video_info(url, platform):
    """获取视频信息"""
    try:
        if platform == 'youtube':
            return get_youtube_info(url)
        elif platform == 'bilibili':
            return get_bilibili_info(url)
        else:
            raise ValueError(f"不支持的平台: {platform}")
    except Exception as e:
        logger.error(f"获取视频信息失败: {str(e)}")
        raise

def get_youtube_info(url):
    """获取YouTube视频信息"""
    try:
        with yt_dlp.YoutubeDL({
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True
        }) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                'title': info.get('title'),
                'language': get_video_language(info),
                'duration': info.get('duration'),
                'upload_date': info.get('upload_date'),
                'uploader': info.get('uploader'),
                'view_count': info.get('view_count'),
                'platform': 'youtube'
            }
    except Exception as e:
        logger.error(f"获取YouTube视频信息失败: {str(e)}")
        raise

def get_bilibili_info(url):
    """获取Bilibili视频信息"""
    try:
        with yt_dlp.YoutubeDL({
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True
        }) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                'title': info.get('title'),
                'language': 'zh',  # Bilibili默认为中文
                'duration': info.get('duration'),
                'upload_date': info.get('upload_date'),
                'uploader': info.get('uploader'),
                'view_count': info.get('view_count'),
                'platform': 'bilibili'
            }
    except Exception as e:
        logger.error(f"获取Bilibili视频信息失败: {str(e)}")
        raise

def download_subtitles(url, platform, video_info):
    """下载字幕"""
    try:
        if platform == 'youtube':
            return download_youtube_subtitles(url, video_info)
        elif platform == 'bilibili':
            return download_bilibili_subtitles(url, video_info)
        else:
            raise ValueError(f"不支持的平台: {platform}")
    except Exception as e:
        logger.error(f"下载字幕失败: {str(e)}")
        raise

def download_bilibili_subtitles(url, video_info):
    """下载Bilibili字幕"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['zh-CN', 'zh-Hans', 'zh'],  # Bilibili主要是中文字幕
            'skip_download': True,
            'format': 'best',
            # 添加重试和超时设置
            'retries': 10,
            'fragment_retries': 10,
            'socket_timeout': 30,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # 检查是否有字幕
            if not info.get('subtitles') and not info.get('automatic_captions'):
                logger.info("未找到字幕")
                return None
            
            # 优先使用手动上传的字幕
            subtitles = info.get('subtitles', {})
            if not subtitles:
                subtitles = info.get('automatic_captions', {})
            
            # 按优先级尝试不同的中文字幕
            for lang in ['zh-CN', 'zh-Hans', 'zh']:
                if lang in subtitles:
                    subtitle_info = subtitles[lang]
                    for fmt in subtitle_info:
                        if fmt.get('ext') in ['vtt', 'srt', 'json3']:
                            subtitle_url = fmt['url']
                            return download_subtitle_content(subtitle_url)
            
            logger.info("未找到合适格式的字幕")
            return None
            
    except Exception as e:
        logger.error(f"下载Bilibili字幕失败: {str(e)}")
        raise

def save_to_readwise(title, content, url=None, published_date=None, author=None, location='new', tags=None):
    """保存内容到Readwise，支持长文本分段"""
    try:
        # 验证location参数
        valid_locations = ['new', 'later', 'archive', 'feed']
        if location not in valid_locations:
            logger.warning(f"无效的location值: {location}，使用默认值'new'")
            location = 'new'
        
        # 从文件读取token
        token_file = os.getenv('READWISE_API_TOKEN_FILE', '/app/config/readwise_token.txt')
        if not os.path.exists(token_file):
            logger.error(f"Readwise token文件不存在: {token_file}")
            return False
            
        with open(token_file, 'r') as f:
            token = f.read().strip()
            
        if not token:
            logger.error("Readwise token为空")
            return False
            
        headers = {
            'Authorization': f'Token {token}',
            'Content-Type': 'application/json'
        }
        
        # 转换YouTube URL
        if url:
            url = convert_youtube_url(url)
            logger.info(f"转换后的URL: {url}")

        # 记录原始内容
        logger.info("原始内容:")
        logger.info(content[:1000])  # 记录前1000个字符

        # 将内容分段，每段最大300000字符
        MAX_LENGTH = 300000
        segments = []
        current_segment = []
        current_length = 0

        # 移除时间轴信息，只保留文本内容
        lines = content.split('\n')
        text_only_lines = []
        for line in lines:
            # 跳过时间轴行（通常包含 --> 或时间格式）
            if '-->' in line or re.match(r'^\d{2}:\d{2}:\d{2}', line):
                continue
            # 跳过纯数字的行（通常是字幕序号）
            if re.match(r'^\d+$', line.strip()):
                continue
            # 保留非空的文本行，移除方括号中的时间信息
            if line.strip():
                # 移除形如 [00:00:00,166] 的时间戳
                cleaned_line = re.sub(r'\[\d{2}:\d{2}:\d{2},\d{3}\]\s*', '', line.strip())
                if cleaned_line:  # 确保移除时间戳后还有内容
                    text_only_lines.append(cleaned_line)

        # 合并相邻的文本行
        merged_text = ' '.join(text_only_lines)
        
        # 按句子分割文本
        sentences = re.split(r'(?<=[.!?。！？])\s+', merged_text)
        
        for sentence in sentences:
            sentence_length = len(sentence)
            if current_length + sentence_length + 2 > MAX_LENGTH:  # +2 for '\n\n'
                if current_segment:  # 保存当前段
                    segments.append(' '.join(current_segment))
                current_segment = [sentence]
                current_length = sentence_length
            else:
                current_segment.append(sentence)
                current_length += sentence_length + 2  # 包括空格

        if current_segment:  # 保存最后一段
            segments.append(' '.join(current_segment))

        # 发送每个段落到Readwise
        success = True
        total_segments = len(segments)
        for i, segment in enumerate(segments, 1):
            # 构造HTML内容和标题
            if total_segments > 1:
                current_title = f"{title} (Part {i}/{total_segments})"
            else:
                current_title = title

            html_content = f'<article><h1>{current_title}</h1><div class="content">{segment}</div></article>'

            data = {
                "url": url or f"http://read.gauss.surf/youtube/unknown",  # 使用新的默认URL格式
                "html": html_content,
                "title": current_title,
                "category": "article",
                "should_clean_html": True,
                "saved_using": "YouTube Subtitles Tool",
                "tags": tags or [],
                "location": location
            }

            # 添加发布日期（如果有）
            if published_date:
                data["published_date"] = published_date

            # 添加作者信息（如果有）
            if author:
                data["author"] = author

            # 记录请求数据
            logger.info("发送到Readwise的数据:")
            logger.info(json.dumps(data, ensure_ascii=False, indent=2))

            response = requests.post(
                'https://readwise.io/api/v3/save/',
                headers=headers,
                json=data
            )

            # 记录响应
            logger.info(f"Readwise响应状态码: {response.status_code}")
            logger.info(f"Readwise响应内容: {response.text}")

            if response.status_code not in [200, 201]:
                logger.error(f"保存第{i}段到Readwise失败: {response.status_code} - {response.text}")
                success = False
            else:
                logger.info(f"成功保存第{i}段到Readwise")

        return success

    except Exception as e:
        logger.error(f"保存到Readwise时出错: {str(e)}")
        return False

# HTML模板
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>字幕查看器</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: Arial, sans-serif;
            line-height: 1.6;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background-color: white;
            padding: 20px;
            border-radius: 5px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .subtitle {
            margin-bottom: 15px;
            padding: 10px;
            border-bottom: 1px solid #eee;
        }
        .time {
            color: #666;
            font-size: 0.9em;
        }
        .text {
            margin-top: 5px;
        }
        h1 {
            color: #333;
            margin-bottom: 20px;
        }
        .back-link {
            display: inline-block;
            margin-bottom: 20px;
            color: #0066cc;
            text-decoration: none;
        }
        .back-link:hover {
            text-decoration: underline;
        }
        .meta-info {
            margin-bottom: 20px;
            color: #666;
            font-size: 0.9em;
        }
        .search-box {
            margin-bottom: 20px;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        .search-box input {
            width: 100%;
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
    </style>
    <script>
        function searchSubtitles() {
            const searchText = document.getElementById('search').value.toLowerCase();
            const subtitles = document.getElementsByClassName('subtitle');
            let foundCount = 0;
            
            for (let subtitle of subtitles) {
                const text = subtitle.getElementsByClassName('text')[0].innerText.toLowerCase();
                if (text.includes(searchText)) {
                    subtitle.style.display = 'block';
                    foundCount++;
                } else {
                    subtitle.style.display = 'none';
                }
            }
            
            document.getElementById('search-count').innerText = 
                searchText ? `找到 ${foundCount} 个匹配项` : '';
        }
    </script>
</head>
<body>
    <div class="container">
        <a href="/" class="back-link">← 返回列表</a>
        <h1>{{filename}}</h1>
        <div class="meta-info">
            总字幕数：{{ subtitles|length }} 条
        </div>
        <div class="search-box">
            <input type="text" id="search" placeholder="搜索字幕..." oninput="searchSubtitles()">
            <div id="search-count"></div>
        </div>
        {% for sub in subtitles %}
        <div class="subtitle">
            {% if show_timeline %}
            <div class="time">{{ "%.3f"|format(sub.start) }} - {{ "%.3f"|format(sub.end) }}</div>
            {% endif %}
            <div class="text">{{ sub.text }}</div>
        </div>
        {% endfor %}
    </div>
</body>
</html>
'''

FILES_LIST_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>字幕文件列表</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: Arial, sans-serif;
            line-height: 1.6;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background-color: white;
            padding: 20px;
            border-radius: 5px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        h1 {
            color: #333;
            margin-bottom: 20px;
        }
        .file-list {
            list-style: none;
            padding: 0;
        }
        .file-item {
            padding: 10px;
            border-bottom: 1px solid #eee;
        }
        .file-item:last-child {
            border-bottom: none;
        }
        .file-link {
            color: #0066cc;
            text-decoration: none;
        }
        .file-link:hover {
            text-decoration: underline;
        }
        .file-time {
            color: #666;
            font-size: 0.9em;
        }
        .youtube-form {
            margin-bottom: 20px;
            padding: 15px;
            background-color: #f8f9fa;
            border-radius: 5px;
        }
        .youtube-form input[type="text"] {
            width: 100%;
            padding: 8px;
            margin-bottom: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        .youtube-form select {
            width: 100%;
            padding: 8px;
            margin-bottom: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        .youtube-form button {
            background-color: #0066cc;
            color: white;
            padding: 8px 16px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
        }
        .youtube-form button:hover {
            background-color: #0052a3;
        }
        .tags-input {
            width: 100%;
            padding: 8px;
            margin-bottom: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        .tags-help {
            font-size: 0.9em;
            color: #666;
            margin-bottom: 10px;
        }
        #progress {
            display: none;
            margin-top: 10px;
            padding: 10px;
            background-color: #e9ecef;
            border-radius: 4px;
        }
        .error-message {
            color: #dc3545;
            margin-top: 10px;
            padding: 10px;
            background-color: #f8d7da;
            border-radius: 4px;
            display: none;
        }
    </style>
    <script>
        function submitYouTubeUrl() {
            var url = document.getElementById('youtube-url').value;
            var location = document.getElementById('save-location').value;
            var tags = document.getElementById('tags').value;
            if (!url) {
                showError('请输入YouTube URL');
                return false;
            }
            
            // 处理tags
            var tagsList = [];
            if (tags) {
                // 支持中英文逗号
                tagsList = tags.split(/[,，]/).map(tag => tag.trim()).filter(tag => tag);
            }
            
            // 显示进度
            document.getElementById('progress').style.display = 'block';
            document.getElementById('progress').innerText = '正在处理...';
            document.getElementById('error-message').style.display = 'none';
            
            // 获取video_id
            var videoId = extractVideoId(url);
            
            fetch('/process', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    url: url,
                    platform: 'youtube',
                    location: location,
                    video_id: videoId,
                    tags: tagsList
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    showError(data.error);
                } else if (data.view_url) {
                    window.location.href = data.view_url;
                } else {
                    showError('处理成功但未返回查看链接');
                }
            })
            .catch(error => {
                showError('请求失败: ' + error);
            });
            
            return false;
        }
        
        function extractVideoId(url) {
            var match = url.match(/[?&]v=([^&]+)/);
            if (match) {
                return match[1];
            }
            match = url.match(/youtu\.be\/([^?]+)/);
            if (match) {
                return match[1];
            }
            return null;
        }
        
        function showError(message) {
            var errorDiv = document.getElementById('error-message');
            errorDiv.innerText = message;
            errorDiv.style.display = 'block';
            document.getElementById('progress').style.display = 'none';
        }
    </script>
</head>
<body>
    <div class="container">
        <h1>字幕文件列表</h1>
        
        <form class="youtube-form" onsubmit="return submitYouTubeUrl()">
            <input type="text" id="youtube-url" placeholder="输入YouTube视频URL">
            <select id="save-location">
                <option value="new">New</option>
                <option value="later">Later</option>
                <option value="archive">Archive</option>
                <option value="feed">Feed</option>
            </select>
            <input type="text" id="tags" class="tags-input" placeholder="输入标签，用逗号分隔">
            <div class="tags-help">标签示例：youtube字幕,学习笔记,英语学习</div>
            <button type="submit">处理</button>
            <div id="progress"></div>
            <div id="error-message" class="error-message"></div>
        </form>

        <ul class="file-list">
        {% for file in files %}
            <li class="file-item">
                <a href="{{ file.url }}" class="file-link">{{ file.filename }}</a>
                <div class="file-time">{{ file.upload_time }}</div>
            </li>
        {% endfor %}
        </ul>
    </div>
</body>
</html>
'''

@app.route('/upload', methods=['POST'])
def upload_file():
    """处理文件上传"""
    logger.info("收到上传请求")
    
    try:
        # 检查是否有文件
        if 'file' not in request.files:
            logger.warning("请求中没有文件")
            return jsonify({"error": "No file part"}), 400
            
        file = request.files['file']
        if file.filename == '':
            logger.warning("未选择文件")
            return jsonify({"error": "No selected file"}), 400
            
        # 获取显示时间轴设置
        show_timeline = request.headers.get('Show-Timeline', 'false').lower() == 'true'
        logger.info(f"显示时间轴设置: {show_timeline}")
        
        # 保存文件
        if file:
            filename = str(uuid.uuid4()) + '.srt'
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            logger.info(f"文件已保存到: {filepath}")
            
            # 读取文件内容
            with open(filepath, 'rb') as f:
                raw_bytes = f.read()
                
            # 检测文件编码
            encoding = detect_file_encoding(raw_bytes)
            logger.info(f"检测到文件编码: {encoding}")
            
            # 读取文件内容
            with open(filepath, 'r', encoding=encoding) as f:
                content = f.read()
                
            # 解析字幕
            subtitles = parse_srt(content)
            if not subtitles:
                logger.error("解析字幕失败")
                return jsonify({"error": "Failed to parse subtitles"}), 400
                
            # 更新文件信息
            file_id = str(uuid.uuid4())
            files_info = load_files_info()
            files_info[file_id] = {
                'id': file_id,
                'filename': file.filename,
                'path': filepath,
                'url': f'/view/{file_id}',
                'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'show_timeline': show_timeline,
                'subtitles': subtitles
            }
            save_files_info(files_info)
            
            return jsonify({
                "message": "File uploaded successfully",
                "file_id": file_id,
                "url": f'/view/{file_id}'
            }), 200
            
    except Exception as e:
        logger.error(f"处理文件时发生错误: {str(e)}")
        return jsonify({'error': str(e)}), 500

def parse_time_str(time_str):
    """解析SRT时间戳为秒数"""
    try:
        hours, minutes, seconds = time_str.replace(',', '.').split(':')
        return float(hours) * 3600 + float(minutes) * 60 + float(seconds)
    except Exception as e:
        logger.error(f"解析时间戳失败: {time_str}, 错误: {str(e)}")
        return 0.0

@app.route('/view/<file_id>')
def view_file(file_id):
    """查看字幕文件内容"""
    try:
        files_info = load_files_info()
        if file_id not in files_info:
            return "文件不存在", 404
            
        file_info = files_info[file_id]
        if not os.path.exists(file_info['path']):
            return "文件不存在", 404
            
        # 读取字幕文件内容
        with open(file_info['path'], 'r', encoding='utf-8') as f:
            srt_content = f.read()
            
        # 解析SRT内容为字幕列表
        subtitles = []
        current_subtitle = {}
        for line in srt_content.strip().split('\n'):
            line = line.strip()
            if not line:  # 空行表示一个字幕结束
                if current_subtitle and 'id' in current_subtitle and 'time' in current_subtitle and 'text' in current_subtitle:
                    # 解析时间轴
                    time_parts = current_subtitle['time'].split(' --> ')
                    if len(time_parts) == 2:
                        start_time = parse_time_str(time_parts[0])
                        end_time = parse_time_str(time_parts[1])
                        current_subtitle['start'] = start_time
                        current_subtitle['end'] = end_time
                    subtitles.append(current_subtitle)
                current_subtitle = {}
            elif not current_subtitle:  # 字幕序号
                current_subtitle = {'id': line}
            elif 'time' not in current_subtitle:  # 时间轴
                current_subtitle['time'] = line
            elif 'text' not in current_subtitle:  # 字幕文本
                current_subtitle['text'] = line
            else:  # 多行字幕文本
                current_subtitle['text'] += '\n' + line
                
        # 添加最后一个字幕
        if current_subtitle and 'id' in current_subtitle and 'time' in current_subtitle and 'text' in current_subtitle:
            time_parts = current_subtitle['time'].split(' --> ')
            if len(time_parts) == 2:
                start_time = parse_time_str(time_parts[0])
                end_time = parse_time_str(time_parts[1])
                current_subtitle['start'] = start_time
                current_subtitle['end'] = end_time
            subtitles.append(current_subtitle)
            
        return render_template_string(
            HTML_TEMPLATE,
            filename=file_info['filename'],
            subtitles=subtitles,
            show_timeline=file_info.get('show_timeline', True)
        )
        
    except Exception as e:
        logger.error(f"查看文件时出错: {str(e)}")
        return str(e), 500

@app.route('/view/')
def view_files():
    """查看所有字幕文件列表"""
    files_info = load_files_info()
    files_info_list = list(files_info.values())
    files_info_list.sort(key=lambda x: x['upload_time'], reverse=True)
    return render_template_string(FILES_LIST_TEMPLATE, files=files_info_list)

@app.route('/')
def index():
    """主页"""
    return view_files()

@app.route('/process_youtube', methods=['POST'])
def process_youtube():
    """处理YouTube视频字幕"""
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({"error": "Missing URL parameter", "success": False}), 400
            
        url = data['url']
        location = data.get('location', 'new')  # 获取location参数，默认为'new'
        logger.info(f"处理YouTube URL: {url}, location: {location}")
        
        # 下载字幕
        srt_content, video_info = download_youtube_subtitles(url)
        if not srt_content:
            # 如果没有字幕，尝试下载视频并转录
            audio_path = download_video(url)
            if not audio_path or not os.path.exists(audio_path):
                return jsonify({"error": "无法下载视频或提取音频", "success": False}), 500
                
            # 使用FunASR转录
            result = transcribe_audio(audio_path)
            if not result:
                return jsonify({"error": "转录失败", "success": False}), 500
                
            # 解析转录结果生成字幕
            subtitles = parse_srt(result)
            if not subtitles:
                return jsonify({"error": "解析转录结果失败", "success": False}), 500
                
            # 生成SRT格式内容
            srt_content = ""
            for i, subtitle in enumerate(subtitles, 1):
                start_time = format_time(subtitle['start'])
                end_time = format_time(subtitle['start'] + subtitle['duration'])
                srt_content += f"{i}\n{start_time} --> {end_time}\n{subtitle['text']}\n\n"
            
            # 保存SRT文件
            title = video_info.get('title', '') if video_info else ''
            if not title:
                title = f"video_transcript_{os.path.splitext(os.path.basename(audio_path))[0]}"
                
            output_filename = f"{title}.srt"
            output_filepath = os.path.join(app.config['OUTPUT_FOLDER'], output_filename)
            with open(output_filepath, 'w', encoding='utf-8') as f:
                f.write(srt_content)
            
            logger.info(f"转录结果已保存到: {output_filepath}")
            
            # 删除临时音频文件
            try:
                os.remove(audio_path)
                logger.info(f"已删除临时文件: {audio_path}")
            except Exception as e:
                logger.warning(f"删除临时文件失败: {str(e)}")
                
            # 生成文件信息并保存
            file_id = str(uuid.uuid4())
            file_info = {
                'id': file_id,
                'filename': output_filename,
                'path': output_filepath,
                'url': f'/view/{file_id}',
                'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'show_timeline': True,
                'source': 'transcription',
                'video_info': video_info
            }
            files_info = load_files_info()
            files_info[file_id] = file_info
            save_files_info(files_info)
            
            # 发送到Readwise
            try:
                if video_info:
                    save_to_readwise(
                        title=video_info.get('title', 'Video Transcript'),
                        content=srt_content,
                        url=url,
                        published_date=video_info.get('published_date'),
                        author=video_info.get('uploader'),
                        location=location
                    )
                    logger.info("成功发送转录内容到Readwise")
            except Exception as e:
                logger.error(f"发送转录内容到Readwise失败: {str(e)}")
                
            return jsonify({
                'success': True,
                'video_info': video_info,
                'subtitle_content': srt_content,
                'filename': file_info['filename'],
                'view_url': file_info['url'],
                'source': 'transcription'
            })
            
        # 获取视频信息
        if not video_info:
            video_info = get_youtube_info(url)
        
        # 更新文件信息并生成网页
        file_id = str(uuid.uuid4())
        files_info = load_files_info()
        
        # 解析字幕内容为列表格式
        subtitles_list = []
        current_subtitle = {}
        for line in srt_content.strip().split('\n'):
            line = line.strip()
            if not line:  # 空行表示一个字幕条目的结束
                if current_subtitle:
                    subtitles_list.append(current_subtitle)
                    current_subtitle = {}
            elif '-->' in line:  # 时间戳行
                start, end = line.split(' --> ')
                current_subtitle['start'] = parse_time_str(start)
                current_subtitle['duration'] = parse_time_str(end) - parse_time_str(start)
            elif current_subtitle.get('start') is not None:  # 文本行
                current_subtitle['text'] = line
        
        # 添加最后一个字幕条目
        if current_subtitle:
            subtitles_list.append(current_subtitle)
        
        # 保存文件信息
        title = video_info.get('title', '') if video_info else ''
        if not title:
            title = f"youtube_video_{os.path.splitext(os.path.basename(url))[0]}"
                
        file_info = {
            'id': file_id,
            'filename': f"{title}.srt",
            'path': None,
            'url': f'/view/{file_id}',
            'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'show_timeline': True,
            'subtitles': subtitles_list
        }
        files_info[file_id] = file_info
        save_files_info(files_info)
        
        # 发送到Readwise
        try:
            if video_info:
                save_to_readwise(
                    title=video_info.get('title', 'YouTube Video Transcript'),
                    content='\n'.join(s['text'] for s in subtitles_list),
                    url=url,
                    published_date=video_info.get('published_date'),
                    author=video_info.get('uploader'),
                    location=location
                )
                logger.info("成功发送到Readwise")
        except Exception as e:
            logger.error(f"发送到Readwise失败: {str(e)}")
        
        # 返回结果
        response = {
            "success": True,
            "srt_content": srt_content,
            "filename": file_info['filename'],
            "view_url": file_info['url']
        }
        if video_info:
            response.update(video_info)
        
        return jsonify(response)
            
    except Exception as e:
        logger.error(f"处理YouTube URL时出错: {str(e)}")
        logger.exception(e)  # 输出完整的错误堆栈
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/process', methods=['POST'])
def process_video():
    """处理视频URL"""
    try:
        data = request.get_json()
        url = data.get('url')
        platform = data.get('platform')
        video_id = data.get('video_id')
        tags = data.get('tags', [])  # 获取tags参数，默认为空列表
        location = data.get('location', 'new')
        
        # Log the request with location and tags
        logger.info(f'处理{platform}URL: {url}, location: {location}, tags: {tags}')
        
        if not url or not platform or not video_id:
            return jsonify({'error': '缺少必要参数'}), 400
        
        # 获取视频信息
        video_info = get_video_info(url, platform)
        
        # 下载字幕
        subtitle_result = download_subtitles(url, platform, video_info)
        subtitle_content, video_info = subtitle_result if isinstance(subtitle_result, tuple) else (subtitle_result, video_info)
        
        if not subtitle_content:
            # 如果没有字幕，尝试下载视频并转录
            logger.info("未找到字幕，尝试下载视频并转录...")
            audio_path = download_video(url)
            if not audio_path:
                return jsonify({"error": "下载视频失败"}, {"success": False}), 500
                
            # 使用FunASR转录
            result = transcribe_audio(audio_path)
            if not result:
                return jsonify({"error": "转录失败"}, {"success": False}), 500
                
            # 解析转录结果生成字幕
            subtitles = parse_srt(result)
            if not subtitles:
                return jsonify({"error": "解析转录结果失败"}, {"success": False}), 500
                
            # 生成SRT格式内容
            srt_content = ""
            for i, subtitle in enumerate(subtitles, 1):
                start_time = format_time(subtitle['start'])
                end_time = format_time(subtitle['start'] + subtitle['duration'])
                srt_content += f"{i}\n{start_time} --> {end_time}\n{subtitle['text']}\n\n"
            
            # 保存字幕文件
            title = video_info.get('title', '') if video_info else ''
            if not title:
                title = f"video_transcript_{video_id}"
                
            output_filename = f"{title}.srt"
            output_filepath = os.path.join(app.config['OUTPUT_FOLDER'], output_filename)
            with open(output_filepath, 'w', encoding='utf-8') as f:
                f.write(srt_content)
            
            logger.info(f"转录结果已保存到: {output_filepath}")
            
            # 删除临时音频文件
            try:
                os.remove(audio_path)
                logger.info(f"已删除临时文件: {audio_path}")
            except Exception as e:
                logger.warning(f"删除临时文件失败: {str(e)}")
                
            # 生成文件信息并保存
            file_id = str(uuid.uuid4())
            file_info = {
                'id': file_id,
                'filename': output_filename,
                'path': output_filepath,
                'url': f'/view/{file_id}',
                'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'show_timeline': True,
                'source': 'transcription',
                'video_info': video_info
            }
            files_info = load_files_info()
            files_info[file_id] = file_info
            save_files_info(files_info)
            
            # 发送到Readwise
            try:
                if video_info:
                    save_to_readwise(
                        title=video_info.get('title', 'Video Transcript'),
                        content=srt_content,
                        url=url,
                        published_date=video_info.get('published_date'),
                        author=video_info.get('uploader'),
                        tags=tags  # 传递tags参数
                    )
                    logger.info("成功发送转录内容到Readwise")
            except Exception as e:
                logger.error(f"发送转录内容到Readwise失败: {str(e)}")
                
            return jsonify({
                'success': True,
                'video_info': video_info,
                'subtitle_content': srt_content,
                'filename': file_info['filename'],
                'view_url': file_info['url'],
                'source': 'transcription'
            })
            
        # 如果有字幕，根据平台确定字幕格式并转换
        if platform == 'youtube':
            srt_content = convert_to_srt(subtitle_content, 'json3')
        elif platform == 'bilibili':
            srt_content = convert_to_srt(subtitle_content, 'json3')
        else:
            return jsonify({'error': '不支持的平台'}), 400
            
        if not srt_content:
            return jsonify({'error': '字幕转换失败'}), 400
        
        # 保存字幕文件
        title = video_info.get('title', '') if video_info else ''
        if not title:
            title = f"{video_id}"
                
        output_filename = f"{title}.srt"
        output_filepath = os.path.join(app.config['OUTPUT_FOLDER'], output_filename)
        with open(output_filepath, 'w', encoding='utf-8') as f:
            f.write(srt_content)
        
        # 生成文件信息并保存
        file_id = str(uuid.uuid4())
        file_info = {
            'id': file_id,
            'filename': output_filename,
            'path': output_filepath,
            'url': f'/view/{file_id}',
            'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'show_timeline': True,
            'source': 'subtitle',
            'video_info': video_info
        }
        files_info = load_files_info()
        files_info[file_id] = file_info
        save_files_info(files_info)
        
        # 返回结果
        return jsonify({
            'success': True,
            'video_info': video_info,
            'subtitle_content': srt_content,
            'filename': file_info['filename'],
            'view_url': file_info['url'],
            'source': 'subtitle'
        })
        
    except Exception as e:
        logger.error(f"处理视频时出错: {str(e)}")
        return jsonify({'error': str(e)}), 500

def get_video_language(info):
    """获取视频的语言信息
    
    优先级：
    1. 从标题判断语言
    2. 从手动上传的字幕判断
    3. 从自动字幕判断
    4. 从视频语言字段获取
    
    返回：
        str: 'zh' 表示中文, 'en' 表示英文, None 表示其他语言
    """
    try:
        language = None
        
        # 1. 从标题判断语言
        if info.get('title'):
            title = info['title']
            # 检测标题是否包含中文字符
            if any('\u4e00' <= char <= '\u9fff' for char in title):
                return 'zh'
            # 如果标题全是英文字符和标点符号，判定为英文
            elif all(ord(char) < 128 for char in title):
                return 'en'
        
        # 2. 从手动上传的字幕判断
        if info.get('subtitles'):
            manual_subs = info['subtitles']
            # 优先检查中文字幕
            if any(lang.startswith('zh') for lang in manual_subs.keys()):
                return 'zh'
            # 其次检查英文字幕
            elif any(lang.startswith('en') for lang in manual_subs.keys()):
                return 'en'
        
        # 3. 从自动字幕判断
        if info.get('automatic_captions'):
            auto_subs = info['automatic_captions']
            # 优先检查中文自动字幕
            if any(lang.startswith('zh') for lang in auto_subs.keys()):
                return 'zh'
            # 其次检查英文自动字幕
            elif any(lang.startswith('en') for lang in auto_subs.keys()):
                return 'en'
        
        # 4. 从视频语言字段获取
        if info.get('language'):
            lang = info['language'].lower()
            if lang.startswith('zh'):
                return 'zh'
            elif lang.startswith('en'):
                return 'en'
        
        # 如果无法确定语言，返回 None
        return None
            
    except Exception as e:
        logger.error(f"获取视频语言时出错: {str(e)}")
        return None

def get_subtitle_strategy(language, info):
    """根据视频语言确定字幕下载策略
    
    Args：
        language: 视频语言 ('zh', 'en', 或 None)
        info: 视频信息字典
    
    Returns：
        tuple: (是否下载字幕, 优先下载的语言列表)
    """
    if language == 'zh':
        # 中文视频：只下载手动字幕
        if info.get('subtitles') and any(lang.startswith('zh') for lang in info['subtitles'].keys()):
            return True, ['zh']
        return False, []
        
    elif language == 'en':
        # 英文视频：优先手动字幕，其次自动字幕
        has_manual = info.get('subtitles') and any(lang.startswith('en') for lang in info['subtitles'].keys())
        has_auto = info.get('automatic_captions') and any(lang.startswith('en') for lang in info['automatic_captions'].keys())
        
        if has_manual or has_auto:
            return True, ['en']
        return False, []
        
    else:
        # 其他语言：暂不处理
        return False, []

if __name__ == '__main__':
    logger.info("启动Flask服务器")
    app.run(host='0.0.0.0', port=5000)
