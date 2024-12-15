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
import tempfile
import shutil
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
import yaml

# 配置日志格式和级别
class ColoredFormatter(logging.Formatter):
    """自定义的日志格式化器，添加颜色"""
    
    # 颜色代码
    grey = "\x1b[38;21m"
    blue = "\x1b[36m"
    yellow = "\x1b[33;21m"
    red = "\x1b[31;21m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    
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

# 创建logger
logger = logging.getLogger('subtitle-processor')
logger.setLevel(logging.DEBUG)  # 设置为DEBUG级别以捕获所有日志
logger.propagate = True  # 确保日志可以传播

# 先移除所有已存在的处理器
for handler in logger.handlers[:]:
    logger.removeHandler(handler)

# 创建控制台处理器
console_handler = logging.StreamHandler(sys.stdout)  # 明确指定输出到 stdout
console_handler.setLevel(logging.DEBUG)  # 控制台显示所有级别
console_handler.setFormatter(ColoredFormatter())

# 创建文件处理器
file_handler = logging.FileHandler('subtitle_processor.log')
file_handler.setLevel(logging.INFO)  # 文件只记录INFO及以上级别
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# 添加处理器到logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# 配置文件路径
CONTAINER_CONFIG_PATH = '/app/config/config.yml'
LOCAL_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
LOCAL_CONFIG_PATH = os.path.join(LOCAL_CONFIG_DIR, 'config.yml')

# 优先使用容器内配置路径
CONFIG_PATH = CONTAINER_CONFIG_PATH if os.path.exists(CONTAINER_CONFIG_PATH) else LOCAL_CONFIG_PATH
CONFIG_DIR = os.path.dirname(CONFIG_PATH)

# 确保配置目录存在
if not os.path.exists(CONFIG_DIR):
    try:
        os.makedirs(CONFIG_DIR)
        logger.info(f"创建配置目录: {CONFIG_DIR}")
    except Exception as e:
        logger.error(f"创建配置目录失败: {str(e)}")

logger.info(f"配置文件路径: {CONFIG_PATH}")

# 获取配置值的辅助函数
def get_config_value(key_path, default=None):
    """从配置中获取值，支持点号分隔的路径，如 'tokens.openai.api_key'"""
    try:
        if not config:
            logger.warning("配置对象为空")
            return default
            
        value = config
        keys = key_path.split('.')
        for i, key in enumerate(keys):
            if not isinstance(value, dict):
                logger.warning(f"配置路径 {'.'.join(keys[:i])} 的值不是字典: {value}")
                return default
            if key not in value:
                logger.warning(f"配置路径 {'.'.join(keys[:i+1])} 不存在")
                return default
            value = value[key]
            
        logger.debug(f"获取配置 {key_path}: {value}")
        return value
    except Exception as e:
        logger.warning(f"获取配置 {key_path} 时出错: {str(e)}, 使用默认值: {default}")
        return default

def load_config():
    """加载YAML配置文件"""
    global config
    try:
        logger.info(f"尝试加载配置文件: {CONFIG_PATH}")
        if not os.path.exists(CONFIG_PATH):
            logger.error(f"配置文件不存在: {CONFIG_PATH}")
            return {}
            
        # 检查文件权限
        if not os.access(CONFIG_PATH, os.R_OK):
            logger.error(f"配置文件无读取权限: {CONFIG_PATH}")
            return {}
        logger.info("配置文件可读")
            
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
            logger.debug(f"配置文件内容:\n{content}")
            try:
                loaded_config = yaml.safe_load(content)
                if not loaded_config:
                    logger.error("配置文件为空或格式错误")
                    return {}
                if not isinstance(loaded_config, dict):
                    logger.error(f"配置文件格式错误，应为字典，实际为: {type(loaded_config)}")
                    return {}
                logger.info("成功加载配置文件")
                logger.debug(f"解析后的配置: {loaded_config}")
                return loaded_config
            except yaml.YAMLError as e:
                logger.error(f"YAML解析错误: {str(e)}")
                return {}
    except Exception as e:
        logger.error(f"加载配置文件失败: {str(e)}")
        return {}

# 加载配置
config = load_config()
if not config:
    logger.error("配置加载失败，使用空配置")
    config = {}
else:
    logger.info(f"配置加载成功，包含以下部分: {list(config.keys())}")
    for section in config.keys():
        logger.debug(f"配置部分 {section}: {config[section]}")

# DeepLX API 配置
DEEPLX_API_URL = get_config_value('deeplx.api_url', "http://deeplx:1188/translate")
DEEPLX_API_V2_URL = get_config_value('deeplx.api_v2_url', "http://deeplx:1188/v2/translate")

# 翻译相关配置
TRANSLATE_MAX_RETRIES = get_config_value('translation.max_retries', 3)
TRANSLATE_BASE_DELAY = get_config_value('translation.base_delay', 3)
TRANSLATE_REQUEST_INTERVAL = get_config_value('translation.request_interval', 1.0)
TRANSLATE_TARGET_LENGTH = get_config_value('translation.chunk_size', 2000)

# 创建Flask应用
app = Flask(__name__)
CORS(app)

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
        logger.debug(f"输入结果类型: {type(result)}")
        logger.debug(f"输入结果内容: {result}")
        
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
                if isinstance(result['text'], str):
                    text_content = result['text']
                    logger.debug(f"获取到文本内容: {text_content[:200]}...")
                else:
                    logger.error(f"text字段不是字符串类型: {type(result['text'])}")
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
                
                if timestamps:
                    logger.debug(f"时间戳数量: {len(timestamps)}")
        
        if not text_content:
            logger.error("未找到有效的文本内容")
            return None
        
        # 分割文本为句子
        sentences = split_into_sentences(text_content)
        
        # 如果有时间戳，使用时间戳生成字幕
        if timestamps and isinstance(timestamps, list) and len(timestamps) > 0:
            logger.info("使用时间戳生成字幕")
            subtitles = []
            current_text = []
            current_start = timestamps[0][0]
            
            for i, (start, end) in enumerate(timestamps):
                if i < len(text_content):
                    char = text_content[i]
                    current_text.append(char)
                    
                    # 判断是否需要结束当前字幕
                    is_sentence_end = char in '.。!！?？;；'
                    is_too_long = len(''.join(current_text)) >= 25
                    is_long_pause = i < len(timestamps) - 1 and timestamps[i+1][0] - end > 800
                    is_natural_break = char in '，,、' and len(''.join(current_text)) >= 15
                    is_last_char = i == len(text_content) - 1
                    
                    if is_sentence_end or is_too_long or is_long_pause or is_natural_break or is_last_char:
                        if current_text:
                            subtitle = {
                                'start': current_start / 1000.0,
                                'duration': (end - current_start) / 1000.0,
                                'text': ''.join(current_text).strip()
                            }
                            subtitles.append(subtitle)
                            logger.debug(f"添加字幕: {subtitle}")
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
                logger.debug(f"添加最后的字幕: {subtitle}")
            
            logger.info(f"使用时间戳生成了 {len(subtitles)} 条字幕")
            return subtitles
        
        # 如果没有时间戳，使用估算的时间戳
        logger.info("使用估算的时间戳生成字幕")
        return generate_srt_timestamps(sentences, duration)
    
    except Exception as e:
        logger.error(f"解析字幕时出错: {str(e)}")
        logger.error(f"错误的输入内容: {result}")
        return None

def parse_srt_content(srt_content):
    """解析SRT格式字幕内容
    
    Args:
        srt_content (str): SRT格式的字幕内容
        
    Returns:
        list: 解析后的字幕列表，每个字幕包含id、start、end、duration和text字段
        
    Raises:
        ValueError: 当字幕内容格式无效时
    """
    if not srt_content or not isinstance(srt_content, str):
        logger.error("无效的字幕内容")
        return []
        
    subtitles_list = []
    current_subtitle = {}
    expected_id = 1
    
    try:
        lines = srt_content.strip().split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # 跳过空行
            if not line:
                i += 1
                continue
            
            try:
                # 字幕序号
                subtitle_id = int(line)
                if subtitle_id != expected_id:
                    logger.warning(f"字幕序号不连续: 期望 {expected_id}, 实际 {subtitle_id}")
                
                current_subtitle = {'id': subtitle_id}
                i += 1
                
                # 时间轴
                if i >= len(lines):
                    raise ValueError("字幕格式错误：缺少时间戳行")
                    
                time_line = lines[i].strip()
                if '-->' not in time_line:
                    raise ValueError(f"无效的时间戳格式: {time_line}")
                    
                try:
                    start_time, end_time = time_line.split(' --> ')
                    current_subtitle['start'] = parse_time_str(start_time.strip())
                    current_subtitle['end'] = parse_time_str(end_time.strip())
                    
                    if current_subtitle['start'] is None or current_subtitle['end'] is None:
                        raise ValueError("无效的时间戳值")
                    if current_subtitle['start'] >= current_subtitle['end']:
                        raise ValueError("结束时间早于开始时间")
                        
                    current_subtitle['duration'] = current_subtitle['end'] - current_subtitle['start']
                except Exception as e:
                    logger.error(f"解析时间戳出错: {str(e)}, 行内容: {time_line}")
                    raise ValueError(f"时间戳解析失败: {str(e)}")
                
                i += 1
                
                # 字幕文本
                text_lines = []
                while i < len(lines) and lines[i].strip():
                    text_lines.append(lines[i].strip())
                    i += 1
                
                if not text_lines:
                    logger.warning(f"字幕 {subtitle_id} 没有文本内容")
                    continue
                    
                current_subtitle['text'] = ' '.join(text_lines)  # 使用空格合并多行
                if len(current_subtitle['text']) > 0:
                    subtitles_list.append(current_subtitle)
                    expected_id += 1
                
            except ValueError as e:
                logger.error(f"解析字幕行时出错: {str(e)}, 行内容: {line}")
                # 尝试跳到下一个字幕块
                while i < len(lines) and lines[i].strip():
                    i += 1
                i += 1
                expected_id += 1
                continue
    
    except Exception as e:
        logger.error(f"解析SRT内容时出错: {str(e)}")
        logger.error(f"SRT内容前100个字符: {srt_content[:100]}")
        logger.exception("详细错误信息:")
    
    if not subtitles_list:
        logger.warning("没有解析出任何有效字幕")
    
    return subtitles_list

def parse_time_str(time_str):
    """解析SRT时间字符串为秒数"""
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

def split_into_sentences(text):
    """将文本分割成句子"""
    try:
        if not text:
            logger.error("输入文本为空")
            return []
            
        # 分割句子的标点符号
        sentence_endings = r'[。！？!?]+'
        
        # 按标点符号分割
        sentences = []
        current_sentence = ""
        
        for char in text:
            current_sentence += char
            
            # 如果遇到句子结束标点，且当前句子不为空
            if re.search(sentence_endings, char) and current_sentence.strip():
                sentences.append(current_sentence.strip())
                current_sentence = ""
                
        # 处理最后一个句子
        if current_sentence.strip():
            sentences.append(current_sentence.strip())
            
        # 过滤掉太短的句子
        sentences = [s for s in sentences if len(s) > 1]
        
        # 记录处理结果
        total_sentences = len(sentences)
        logger.info(f"分割完成，共 {total_sentences} 个句子")
        
        # 只显示前10行和后10行的句子
        if total_sentences > 20:
            for i, sentence in enumerate(sentences[:10]):
                logger.debug(f"句子[{i+1}]: {sentence[:50]}...")
            logger.debug("...")
            for i, sentence in enumerate(sentences[-10:]):
                logger.debug(f"句子[{total_sentences-10+i+1}]: {sentence[:50]}...")
        else:
            for i, sentence in enumerate(sentences):
                logger.debug(f"句子[{i+1}]: {sentence[:50]}...")
        
        return sentences
            
    except Exception as e:
        logger.error(f"分割句子时出错: {str(e)}")
        return []

def generate_srt_timestamps(sentences, total_duration=None):
    """为句子生成时间戳"""
    try:
        if not sentences:
            logger.error("没有句子需要生成时间戳")
            return []
            
        logger.info("开始生成时间戳")
        
        # 如果没有提供总时长，使用估算值
        if not total_duration:
            # 假设每个字符0.3秒
            total_duration = sum(len(s) * 0.3 for s in sentences)
            logger.info("使用估算的时间戳生成字幕")
            
        logger.debug(f"总时长: {total_duration}秒")
        logger.debug(f"句子数量: {len(sentences)}")
        
        # 计算每个句子的时长
        total_chars = sum(len(s) for s in sentences)
        timestamps = []
        current_time = 0
        
        # 只显示前10个和后10个时间戳的生成
        total_sentences = len(sentences)
        for i, sentence in enumerate(sentences, 1):
            duration = (len(sentence) / total_chars) * total_duration
            end_time = min(current_time + duration, total_duration)
            
            if i < 10 or i >= total_sentences - 10:
                logger.debug(f"生成字幕[{i}/{total_sentences}]: {current_time:.1f}s - {sentence[:50]}...")
            elif i == 10:
                logger.debug("...")
            
            timestamps.append({
                'start': current_time,
                'end': end_time,
                'duration': duration,  # 添加duration字段
                'text': sentence
            })
            
            current_time = end_time
            
        return timestamps
            
    except Exception as e:
        logger.error(f"生成时间戳时出错: {str(e)}")
        return []

def download_youtube_subtitles(url, video_info=None, lang_priority=None):
    """下载YouTube视频字幕"""
    try:
        # 创建临时目录
        temp_dir = tempfile.mkdtemp()
        logger.info(f"创建临时目录: {temp_dir}")
        
        # 如果没有提供video_info，获取视频信息
        if not video_info:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': lang_priority or ['zh-Hans', 'zh-Hant', 'zh', 'en'],
                'skip_download': True,
                'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
                'subtitlesformat': 'srt/ass/vtt/best'
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                video_info = ydl.extract_info(url, download=False)
                # 创建简化的信息对象，只包含必要字段
                simplified_info = {
                    'title': video_info.get('title', ''),
                    'uploader': video_info.get('uploader', ''),
                    'upload_date': video_info.get('upload_date', ''),
                    'subtitles': list(video_info.get('subtitles', {}).keys()),
                    'automatic_captions': list(video_info.get('automatic_captions', {}).keys())
                }
                logger.info(f"获取到的视频信息: {json.dumps(simplified_info, ensure_ascii=False)}")
        
        # 使用提供的语言优先级或默认值
        if lang_priority:
            logger.info(f"使用指定的语言优先级: {lang_priority}")
        else:
            lang_priority = ['zh-Hans', 'zh-Hant', 'zh', 'en']
            logger.info(f"使用默认语言优先级: {lang_priority}")
        
        # 检查是否有手动上传的字幕
        subtitles = video_info.get('subtitles', {})
        logger.info(f"手动上传的字幕: {list(subtitles.keys())}")
        
        # 检查是否有自动生成的字幕
        auto_captions = video_info.get('automatic_captions', {})
        logger.info(f"自动生成的字幕: {list(auto_captions.keys())}")
        
        # 优先使用手动上传的字幕
        subtitle_url = None
        subtitle_lang = None
        
        # 先检查手动上传的字幕
        for lang in lang_priority:
            if lang in subtitles:
                formats = subtitles[lang]
                # 优先选择srt格式
                for fmt in formats:
                    if fmt.get('ext') == 'srt':
                        subtitle_url = fmt['url']
                        subtitle_lang = lang
                        logger.info(f"找到手动上传的{lang}字幕，格式为srt")
                        break
                # 如果没有srt格式，使用第一个可用格式
                if not subtitle_url and formats:
                    subtitle_url = formats[0]['url']
                    subtitle_lang = lang
                    logger.info(f"找到手动上传的{lang}字幕，格式为{formats[0].get('ext')}")
                if subtitle_url:
                    break
        
        # 如果没有找到手动上传的字幕，检查自动生成的字幕
        if not subtitle_url:
            for lang in lang_priority:
                # 检查原始语言和翻译后的语言
                check_langs = [lang]
                if lang == 'en':
                    check_langs.append('en-orig')
                
                for check_lang in check_langs:
                    if check_lang in auto_captions:
                        formats = auto_captions[check_lang]
                        # 优先选择json3格式，因为它包含完整的字幕信息
                        for fmt in formats:
                            if fmt.get('ext') == 'json3':
                                subtitle_url = fmt['url']
                                subtitle_lang = check_lang
                                logger.info(f"找到自动生成的{check_lang}字幕，格式为json3")
                                break
                        # 如果没有json3格式，尝试其他格式（按优先级）
                        if not subtitle_url:
                            preferred_formats = ['vtt', 'ttml', 'srv3', 'srv2', 'srv1']
                            for pref_fmt in preferred_formats:
                                for fmt in formats:
                                    if fmt.get('ext') == pref_fmt:
                                        subtitle_url = fmt['url']
                                        subtitle_lang = check_lang
                                        logger.info(f"找到自动生成的{check_lang}字幕，格式为{pref_fmt}")
                                        break
                            if subtitle_url:
                                break
            if not subtitle_url:
                logger.info("未找到首选语言的字幕文件")
                return None
            
        # 下载字幕
        logger.info(f"开始下载字幕: {subtitle_url}")
        response = requests.get(subtitle_url)
        if response.status_code != 200:
            logger.error(f"下载字幕失败: {response.status_code}")
            return None
        
        # 检测并处理文件编码
        content = response.content
        encoding = detect_file_encoding(content)
        logger.info(f"检测到字幕文件编码: {encoding}")
        
        try:
            subtitle_content = content.decode(encoding)
        except UnicodeDecodeError:
            logger.warning(f"使用 {encoding} 解码失败，尝试使用 utf-8")
            try:
                subtitle_content = content.decode('utf-8')
            except UnicodeDecodeError:
                logger.warning("使用 utf-8 解码失败，尝试使用 utf-8-sig")
                subtitle_content = content.decode('utf-8-sig')
        
        # 清理临时目录
        shutil.rmtree(temp_dir)
        logger.info("清理临时目录")
        
        return subtitle_content, video_info
            
    except Exception as e:
        logger.error(f"下载YouTube字幕时出错: {str(e)}")
        if 'temp_dir' in locals():
            shutil.rmtree(temp_dir)
            logger.info("清理临时目录")
        raise

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

def clean_subtitle_content(content, is_funasr=False):
    """清理字幕内容
    
    Args:
        content: 字幕内容
        is_funasr: 是否是FunASR转换的字幕
    """
    try:
        if not content:
            return ""
            
        # 移除WEBVTT头部
        content = re.sub(r'^WEBVTT\s*\n', '', content)
        
        # 移除序号和时间轴
        lines = []
        current_text = []
        skip_next = False
        
        for line in content.split('\n'):
            line = line.strip()
            
            # 跳过序号行（纯数字）
            if re.match(r'^\d+$', line):
                continue
                
            # 跳过时间轴行
            if re.match(r'^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}', line):
                continue
                
            # 跳过空行
            if not line:
                if current_text:
                    if is_funasr:
                        # FunASR转换的字幕：合并所有文本，不保留换行
                        lines.append(' '.join(current_text))
                    else:
                        # 直接提取的字幕：保留原有换行
                        lines.append('\n'.join(current_text))
                    current_text = []
                continue
                
            current_text.append(line)
            
        # 处理最后一段文本
        if current_text:
            if is_funasr:
                lines.append(' '.join(current_text))
            else:
                lines.append('\n'.join(current_text))
        
        # 合并处理后的文本
        if is_funasr:
            # FunASR转换的字幕：所有段落用空格连接
            return ' '.join(lines)
        else:
            # 直接提取的字幕：段落之间用两个换行符分隔
            return '\n\n'.join(lines)
            
    except Exception as e:
        logger.error(f"清理字幕内容时出错: {str(e)}")
        return content

def convert_to_srt(content, input_format):
    if input_format == 'vtt':
        try:
            # 移除 BOM 标记（如果存在）
            content = content.strip('\ufeff')
            
            # 移除 WEBVTT 头部和注释
            content = re.sub(r'^WEBVTT.*?\n', '', content, flags=re.DOTALL)
            content = re.sub(r'NOTE.*?\n', '', content, flags=re.DOTALL)
            
            # 移除 VTT 特有的样式信息
            content = re.sub(r'STYLE\n.*?\n\n', '', content, flags=re.DOTALL)
            content = re.sub(r'REGION\n.*?\n\n', '', content, flags=re.DOTALL)
            
            # 清理空行和多余的空格
            lines = [line.strip() for line in content.split('\n')]
            lines = [line for line in lines if line]
            
            # 转换时间戳格式并添加序号
            srt_lines = []
            current_index = 1
            i = 0
            while i < len(lines):
                # 检查是否是时间戳行
                if re.match(r'^\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}', lines[i]):
                    # 添加序号
                    srt_lines.append(str(current_index))
                    
                    # 转换时间戳格式（将 . 替换为 ,）
                    timestamp = re.sub(r'\.', ',', lines[i])
                    srt_lines.append(timestamp)
                    
                    # 收集字幕文本直到下一个时间戳或结束
                    text_lines = []
                    i += 1
                    while i < len(lines) and not re.match(r'^\d{2}:\d{2}:\d{2}[.,]\d{3}', lines[i]):
                        text_lines.append(lines[i])
                        i += 1
                    
                    # 添加字幕文本和空行
                    if text_lines:
                        srt_lines.append(' '.join(text_lines))
                        srt_lines.append('')
                        current_index += 1
                    continue
                i += 1
            
            if not srt_lines:
                logger.error("转换后的SRT内容为空")
                return None
                
            return '\n'.join(srt_lines)
            
        except Exception as e:
            logger.error(f"转换VTT格式时出错: {str(e)}")
            return None
    elif input_format == 'json3':
        try:
            # 处理可能的多行 JSON
            content = content.strip()
            if content.startswith('[') and content.endswith(']'):
                # 如果内容是 JSON 数组
                data_list = json.loads(content)
                if not data_list:
                    logger.error("JSON数组为空")
                    return None
                # 使用第一个有效的 JSON 对象
                for data in data_list:
                    if isinstance(data, dict) and 'events' in data:
                        break
                else:
                    logger.error("JSON数组中没有找到有效的字幕数据")
                    return None
            else:
                # 尝试处理多行独立的 JSON 对象
                try:
                    # 首先尝试作为单个 JSON 对象解析
                    data = json.loads(content)
                except json.JSONDecodeError:
                    # 如果失败，尝试分行处理
                    lines = content.split('\n')
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            if isinstance(data, dict) and 'events' in data:
                                break
                        except json.JSONDecodeError:
                            continue
                    else:
                        logger.error("未找到有效的字幕JSON数据")
                        return None
            
            # 验证数据结构
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
        
        # 获取可用的转录服务器
        server_url = get_available_transcribe_server()
        logger.info(f"使用转录服务器: {server_url}")
        
        # 处理每个音频片段
        for i, segment_path in enumerate(audio_segments, 1):
            logger.info(f"处理音频片段 {i}/{len(audio_segments)}: {segment_path}")
            
            # 准备请求
            url = f"{server_url}/recognize"
            
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
            logger.error("没有有效的转录结果")
            return None
            
        # 记录所有结果的格式
        logger.info(f"收到 {len(all_results)} 个转录结果")
        for i, result in enumerate(all_results):
            logger.info(f"结果 {i+1} 的类型: {type(result)}")
            logger.info(f"结果 {i+1} 的内容: {result}")
            
        # 如果只有一个结果，直接返回
        if len(all_results) == 1:
            result = all_results[0]
            if isinstance(result, dict) and 'text' in result:
                logger.info(f"单个结果的文本内容: {result['text']}")
                return result['text']
            else:
                logger.error(f"单个结果格式错误: {result}")
                return None
        
        # 合并多个结果
        merged_result = {
            'text': '',
            'timestamp': []
        }
        
        last_end_time = 0
        for i, result in enumerate(all_results):
            logger.info(f"处理第 {i+1} 个结果")
            if isinstance(result, dict) and 'text' in result:
                # 合并文本
                merged_result['text'] += result['text']
                logger.info(f"合并后的文本长度: {len(merged_result['text'])}")
                
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
                        logger.info(f"更新最后的结束时间: {last_end_time}")
            else:
                logger.error(f"结果 {i+1} 格式错误: {result}")
        
        logger.info(f"最终合并的文本长度: {len(merged_result['text'])}")
        logger.info(f"最终合并的文本内容: {merged_result['text'][:200]}...")  # 只显示前200个字符
        
        processed_text = process_subtitle_content(merged_result['text'], is_funasr=True)
        logger.info(f"处理后的文本长度: {len(processed_text)}")
        logger.info(f"处理后的文本内容: {processed_text[:200]}...")  # 只显示前200个字符
        
        return processed_text
            
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

def convert_youtube_url(url):
    """将YouTube URL转换为自定义domain"""
    try:
        # 处理不同格式的YouTube URL
        if 'youtu.be/' in url:
            video_id = url.split('youtu.be/')[-1].split('?')[0]
        elif 'youtube.com/watch' in url:
            video_id = url.split('v=')[-1].split('&')[0]
        else:
            video_id = url  # 假设直接传入了video_id

        # 从配置中获取视频域名
        video_domain = get_config_value('servers.video_domain')
        return f"{video_domain}/youtube/{video_id}"
    except Exception as e:
        logger.error(f"转换YouTube URL时出错: {str(e)}")
        return url

def get_youtube_info(url):
    """获取YouTube视频信息"""
    try:
        # 自定义日志处理器
        class QuietLogger:
            def debug(self, msg):
                # 忽略调试信息
                pass
            def warning(self, msg):
                logger.warning(msg)
            def error(self, msg):
                logger.error(msg)
        
        ydl_opts = {
            'logger': QuietLogger(),  # 使用自定义日志处理器
            'quiet': True,  # 减少输出
            'no_warnings': True,  # 不显示警告
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # 详细记录所有可能包含日期的字段
            date_fields = {
                'upload_date': info.get('upload_date'),
                'release_date': info.get('release_date'),
                'modified_date': info.get('modified_date'),
                'timestamp': info.get('timestamp')
            }
            logger.info(f"YouTube视频日期相关字段: {json.dumps(date_fields, indent=2, ensure_ascii=False)}")
            
            # 尝试多个日期字段
            published_date = None
            if info.get('upload_date'):
                published_date = f"{info['upload_date'][:4]}-{info['upload_date'][4:6]}-{info['upload_date'][6:]}T00:00:00Z"
            elif info.get('release_date'):
                published_date = info['release_date']
            elif info.get('modified_date'):
                published_date = info['modified_date']
            elif info.get('timestamp'):
                from datetime import datetime
                published_date = datetime.fromtimestamp(info['timestamp']).strftime('%Y-%m-%dT%H:%M:%SZ')
            
            logger.info(f"最终确定的发布日期: {published_date}")
            
            video_info = {
                'title': info.get('title', ''),
                'published_date': published_date,
                'uploader': info.get('uploader', ''),
                'subtitles': info.get('subtitles', {}),
                'automatic_captions': info.get('automatic_captions', {}),
                'language': info.get('language')
            }
            
            # 记录简化的返回信息
            simplified_info = {
                'title': video_info['title'],
                'uploader': video_info['uploader'],
                'published_date': video_info['published_date'],
                'language': video_info['language'],
                'subtitles': list(video_info['subtitles'].keys()),
                'automatic_captions': list(video_info['automatic_captions'].keys())
            }
            # logger.info(f"视频信息: {json.dumps(simplified_info, indent=2, ensure_ascii=False)}")
            
            return video_info
            
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
            
            # 详细记录所有可能包含日期的字段
            date_fields = {
                'upload_date': info.get('upload_date'),
                'release_date': info.get('release_date'),
                'modified_date': info.get('modified_date'),
                'timestamp': info.get('timestamp')
            }
            logger.info(f"Bilibili视频日期相关字段: {json.dumps(date_fields, indent=2, ensure_ascii=False)}")
            
            # 尝试多个日期字段
            published_date = None
            if info.get('upload_date'):
                published_date = f"{info['upload_date'][:4]}-{info['upload_date'][4:6]}-{info['upload_date'][6:]}T00:00:00Z"
            elif info.get('release_date'):
                published_date = info['release_date']
            elif info.get('modified_date'):
                published_date = info['modified_date']
            elif info.get('timestamp'):
                from datetime import datetime
                published_date = datetime.fromtimestamp(info['timestamp']).strftime('%Y-%m-%dT%H:%M:%SZ')
            
            logger.info(f"最终确定的发布日期: {published_date}")
            
            video_info = {
                'title': info.get('title', ''),
                'published_date': published_date,
                'uploader': info.get('uploader', ''),
                'subtitles': info.get('subtitles', {}),
                'automatic_captions': info.get('automatic_captions', {}),
                'language': info.get('language')
            }
            
            # 记录完整的返回信息
            logger.info(f"返回的视频信息: {json.dumps(video_info, indent=2, ensure_ascii=False)}")
            
            return video_info
            
    except Exception as e:
        logger.error(f"获取Bilibili视频信息失败: {str(e)}")
        raise

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

def download_subtitles(url, platform, video_info=None):
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

def save_to_readwise(title, content, url=None, published_date=None, author=None, location='new', tags=None, language=None):
    """保存内容到Readwise
    
    Args:
        title: 标题
        content: 内容
        url: 链接
        published_date: 发布日期
        author: 作者
        location: 保存位置 ('new' 或 'later')
        tags: 标签列表
        language: 视频语言
    """
    try:
        # 验证location参数
        valid_locations = ['new', 'later', 'archive', 'feed']
        if location not in valid_locations:
            logger.warning(f"无效的location值: {location}，使用默认值'new'")
            location = 'new'
            
        # 检查标题是否为英文
        if language == 'en':
            logger.info("检测到英文标题，开始翻译...")
            translated_title = translate_text(title)
            if translated_title and translated_title != title:
                title = f"{title} | {translated_title}"
                logger.info(f"标题已翻译: {title}")
            
        # 检查是否包含英文内容
        has_english = any(c.isalpha() for c in content)
        if has_english and language == 'en':
            # 添加翻译
            translated_content = translate_text(content)
            content = f"{content}\n\n中文翻译：\n{translated_content}"
        
        # 优先从配置文件获取token
        token = get_config_value('tokens.readwise')
        if not token:
            # 如果配置中没有token，从文件读取
            token_file = os.getenv('READWISE_API_TOKEN_FILE', '/app/config/readwise_token.txt')
            logger.info(f"配置中未找到token，尝试从文件读取: {token_file}")
            
            # 如果默认路径不存在，尝试在项目根目录下的config目录查找
            if not os.path.exists(token_file):
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                local_token_file = os.path.join(project_root, 'config', 'readwise_token.txt')
                logger.info(f"默认token文件不存在，尝试从本地路径读取: {local_token_file}")
                if os.path.exists(local_token_file):
                    token_file = local_token_file
                else:
                    logger.error("未找到Readwise token文件")
                    return None
                    
            try:
                with open(token_file, 'r', encoding='utf-8') as f:
                    token = f.read().strip()
            except Exception as e:
                logger.error(f"读取Readwise token失败: {str(e)}")
                return None
        
        if not token:
            logger.error("Readwise token为空")
            return None
            
        logger.info("成功读取Readwise token")
        
        headers = {
            'Authorization': f'Token {token}',
            'Content-Type': 'application/json'
        }

        # 转换YouTube URL
        if url:
            url = convert_youtube_url(url)
            logger.info(f"转换后的URL: {url}")

        # 记录原始内容长度
        logger.info(f"原始内容长度: {len(content)}")
        logger.info(f"作者信息: {author}")

        # 处理字幕内容，移除序号和时间轴
        content = process_subtitle_content(content, translate=False, language=language)
        logger.info(f"处理后的内容长度: {len(content)}")

        # 将内容转换为HTML格式
        content_with_br = content.replace('\n', '<br>')
        html_content = f'<div class="content">{content_with_br}</div>'

        data = {
            "url": url or f"{video_domain}/youtube/unknown",
            "title": title,
            "author": author,
            "html": html_content,
            "should_clean_html": True,
            "category": "article",
            "location": location,
            "tags": tags or []
        }

        # 添加发布日期（如果有）
        if published_date:
            data["published_date"] = published_date
            logger.info(f"添加发布日期: {published_date}")

        # 记录发送到Readwise的数据
        logger_data = {**data}
        logger_data['html'] = logger_data['html'][:200] + '...' if len(logger_data['html']) > 200 else logger_data['html']
        logger.info(f"发送到Readwise的数据: {json.dumps(logger_data, ensure_ascii=False)}")

        # 发送请求
        response = requests.post(
            'https://readwise.io/api/v3/save/',
            headers=headers,
            json=data
        )

        # 记录响应内容
        logger.info(f"Readwise响应状态码: {response.status_code}")
        logger.info(f"Readwise响应内容: {response.text}")

        if response.status_code not in [200, 201]:
            logger.error(f"发送到Readwise失败: {response.status_code} - {response.text}")
            return None
        else:
            logger.info("成功发送到Readwise")
            return response.json()

    except Exception as e:
        logger.error(f"保存到Readwise时出错: {str(e)}")
        logger.exception(e)
        return None

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
            border-radius: 5px;
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
        <a href="/" class="back-link">← 返回列表</a>
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

import json
import os

# 翻译文本长度限制
TRANSLATE_MIN_LENGTH = 1600  # 最小字符数
TRANSLATE_MAX_LENGTH = 2400  # 最大字符数
TRANSLATE_TARGET_LENGTH = get_config_value('translation.chunk_size', 2000)  # 目标字符数

# 重试配置
TRANSLATE_MAX_RETRIES = get_config_value('translation.max_retries', 3)
TRANSLATE_BASE_DELAY = get_config_value('translation.base_delay', 3)
TRANSLATE_REQUEST_INTERVAL = get_config_value('translation.request_interval', 1.0)

def load_transcribe_servers():
    """加载转录服务器配置"""
    servers = get_config_value('servers.transcribe.servers', [])
    if not servers:
        # 如果yml中没有配置，尝试从backup文件加载
        backup_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'transcribe_servers-backup.json')
        try:
            with open(backup_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                servers = data.get('servers', [])
        except Exception as e:
            logger.error(f"加载转录服务器配置失败: {str(e)}")
            servers = []
    
    # 按优先级排序
    return sorted(servers, key=lambda x: x.get('priority', 999))

def get_available_transcribe_server():
    """获取可用的转录服务器"""
    servers = load_transcribe_servers()
    available_servers = []
    
    message = "\n=== 开始检查转录服务器 ==="
    print(message)
    logger.info(message)
    
    message = f"发现 {len(servers)} 个配置的服务器"
    print(message)
    logger.info(message)
    
    for server in servers:
        message = f"\n正在检查服务器: {server['name']} ({server['url']})"
        print(message)
        logger.info(message)
        
        try:
            response = requests.get(f"{server['url']}/health", timeout=5)
            if response.status_code == 200:
                server_info = response.json()
                # 将服务器状态信息添加到配置中
                server.update(server_info)
                available_servers.append(server)
                
                message = f"✓ 服务器可用"
                print(message)
                logger.info(message)
                
                message = f"  - 设备类型: {server_info.get('device', 'unknown')}"
                print(message)
                logger.info(message)
                
                message = f"  - GPU状态: {'可用' if server_info.get('gpu_available', False) else '不可用'}"
                print(message)
                logger.info(message)
        except Exception as e:
            message = f"✗ 服务器不可用: {str(e)}"
            print(message)
            logger.error(message)
    
    if not available_servers:
        message = "\n❌ 错误: 没有可用的转录服务器"
        print(message)
        logger.error(message)
        raise Exception("没有可用的转录服务器")
    
    # 按优先级排序，优先使用GPU服务器
    available_servers.sort(key=lambda x: (
        x['priority'],  # 首先按配置的优先级排序
        0 if x.get('gpu_available', False) else 1  # 其次优先选择有GPU的服务器
    ))
    
    selected_server = available_servers[0]
    
    message = "\n=== 服务器选择结果 ==="
    print(message)
    logger.info(message)
    
    message = f"已选择: {selected_server['name']} ({selected_server['url']})"
    print(message)
    logger.info(message)
    
    message = f"  - 优先级: {selected_server['priority']}"
    print(message)
    logger.info(message)
    
    message = f"  - 设备类型: {selected_server.get('device', 'unknown')}"
    print(message)
    logger.info(message)
    
    message = f"  - GPU状态: {'可用' if selected_server.get('gpu_available', False) else '不可用'}"
    print(message)
    logger.info(message)
    
    message = "==================\n"
    print(message)
    logger.info(message)
    
    return selected_server['url']

def sanitize_filename(filename):
    """清理文件名，移除不安全字符"""
    # 替换Windows下的非法字符
    illegal_chars = r'[<>:"/\\|?*]'
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
        
    return clean_name

def process_subtitle_content(content, is_funasr=False, translate=False, language=None):
    """
    处理字幕内容，移除序号和时间轴，根据来源处理换行
    
    Args:
        content: 字幕内容
        is_funasr: 是否是FunASR转换的字幕
        translate: 是否需要翻译
        language: 视频语言
    """
    try:
        if not content:
            logger.error("输入内容为空")
            return ""
            
        logger.info(f"开始处理字幕内容 [长度: {len(content)}字符]")
        if is_funasr:
            logger.info("使用FunASR模式处理字幕")
        
        # 移除WEBVTT头部
        content = re.sub(r'^WEBVTT\s*\n', '', content)
        
        # 分割成行
        lines = content.split('\n')
        logger.info(f"字幕总行数: {len(lines)}")
        text_blocks = []
        current_block = []
        
        skipped_numbers = 0
        skipped_timestamps = 0
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # 跳过序号行（纯数字）
            if re.match(r'^\d+$', line):
                skipped_numbers += 1
                i += 1
                continue
                
            # 跳过时间轴行
            if re.match(r'^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}', line):
                skipped_timestamps += 1
                i += 1
                continue
                
            # 跳过空行
            if not line:
                if current_block:
                    if is_funasr:
                        # FunASR转换的字幕：合并所有文本，不保留换行
                        text_blocks.append(' '.join(current_block))
                    else:
                        # 直接提取的字幕：保留原有换行
                        text_blocks.append('\n'.join(current_block))
                    current_block = []
                i += 1
                continue
            
            current_block.append(line)
            i += 1
            
        # 处理最后一段文本
        if current_block:
            if is_funasr:
                text_blocks.append(' '.join(current_block))
            else:
                text_blocks.append('\n'.join(current_block))
        
        # 合并所有文本块
        if is_funasr:
            # FunASR转换的字幕：用空格连接所有块
            result = ' '.join(text_blocks)
        else:
            # 直接提取的字幕：段落之间用两个换行符分隔
            result = '\n\n'.join(text_blocks)
            
        logger.info(f"字幕处理完成:")
        logger.info(f"- 移除了 {skipped_numbers} 个序号标记")
        logger.info(f"- 移除了 {skipped_timestamps} 个时间轴")
        logger.info(f"- 处理后文本长度: {len(result)}字符")
        logger.debug(f"处理后内容预览: {result[:200]}...")
        
        # 只有当视频是英文且需要翻译时才进行翻译
        if translate and language == 'en':
            logger.info("检测到英文视频，开始翻译...")
            # 直接使用改进后的 translate_text 函数进行翻译
            translated = translate_text(result)
            if translated != result:  # 只有在翻译成功时才使用翻译结果
                # 再次清理翻译后的文本
                translated = re.sub(r'^\d+\s*$', '', translated, flags=re.MULTILINE)  # 移除序号行
                translated = re.sub(r'^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}\s*$', '', translated, flags=re.MULTILINE)  # 移除时间轴行
                translated = re.sub(r'\n{3,}', '\n\n', translated)  # 移除多余的空行
                translated = translated.strip()
                result = f"{result}\n\n{translated}"
            
        return result
            
    except Exception as e:
        logger.error(f"处理字幕内容时出错: {str(e)}")
        logger.error(f"错误的输入内容: {content}")
        raise

def translate_text(text, source_lang='en', target_lang='zh'):
    """翻译文本，首先尝试DeepLX，如果失败则使用OpenAI"""
    if not text:
        return text

    logger.info("开始翻译文本...")
    logger.info(f"原文前100个字符: {text[:100]}")
    
    try:
        # 移除HTML标签，保留换行符
        text = re.sub(r'<br\s*/?>', '\n', text)  # 将<br>转换为换行符
        text = re.sub(r'<[^>]+>', '', text)  # 移除其他HTML标签
        
        # 将文本按照100-150字符分段
        segments = []
        current_pos = 0
        text_length = len(text)
        
        while current_pos < text_length:
            # 找到目标字符长度的位置
            next_pos = min(current_pos + TRANSLATE_TARGET_LENGTH, text_length)
            
            # 如果不是文本末尾，查找最近的句子结束标记
            if next_pos < text_length:
                # 在目标范围内查找句子结束标记
                for end_pos in range(next_pos, min(next_pos + (TRANSLATE_MAX_LENGTH - TRANSLATE_TARGET_LENGTH), text_length)):
                    if text[end_pos] in '.!?。！？':
                        next_pos = end_pos + 1
                        break
            
            segment = text[current_pos:next_pos].strip()
            if segment:
                segments.append(segment)
            current_pos = next_pos
        
        logger.info(f"分割完成，共 {len(segments)} 个文本段")
        
        translated_segments = []
        last_request_time = 0
        min_request_interval = TRANSLATE_REQUEST_INTERVAL  # 最小请求间隔（秒）
        
        for i, segment in enumerate(segments, 1):
            if not segment.strip():
                continue
                
            logger.debug(f"文本段[{i}]: {segment[:50]}...")
            logger.debug(f"文本段长度: {len(segment)} 字符")
            
            # 控制请求频率
            current_time = time.time()
            time_since_last_request = current_time - last_request_time
            if time_since_last_request < min_request_interval:
                time.sleep(min_request_interval - time_since_last_request)
            
            # 尝试翻译，最多重试3次
            for attempt in range(TRANSLATE_MAX_RETRIES):
                try:
                    if attempt > 0:
                        # 503错误时使用更长的等待时间
                        delay = (attempt + 1) * TRANSLATE_BASE_DELAY  # 3秒、6秒、9秒
                        logger.info(f"第 {attempt + 1} 次重试，等待 {delay} 秒...")
                        time.sleep(delay)
                    
                    translated = _translate_with_retry(segment, source_lang, target_lang)
                    translated_segments.append(translated)
                    last_request_time = time.time()
                    break
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"翻译失败 (尝试 {attempt+1}/{TRANSLATE_MAX_RETRIES}): {error_msg}")
                    if attempt == TRANSLATE_MAX_RETRIES - 1:
                        logger.warning(f"翻译失败{TRANSLATE_MAX_RETRIES}次，使用原文: {segment[:50]}...")
                
            logger.info(f"翻译进度: {i}/{len(segments)}")
        
        # 合并翻译结果
        translated_text = " ".join(translated_segments)
        
        # 记录翻译后的前100个字符
        logger.info(f"翻译后前100个字符: {translated_text[:100]}")
        
        return translated_text
            
    except Exception as e:
        logger.error(f"翻译过程出错: {str(e)}")
        logger.error(traceback.format_exc())
        return text  # 出错时返回原文

def _translate_with_retry(text, source_lang='en', target_lang='zh'):
    """
    带重试机制的翻译函数，支持DeepLX和OpenAI
    每个接口会重试到最大次数后，再切换到下一个接口
    """
    # 获取翻译服务配置
    services = get_config_value('translation.services', [
        {'name': 'deeplx_v2', 'enabled': True, 'priority': 1},
        {'name': 'openai', 'enabled': True, 'priority': 2},
        {'name': 'deeplx', 'enabled': True, 'priority': 3}
    ])
    
    # 按优先级排序服务
    services.sort(key=lambda x: x.get('priority', 999))
    enabled_services = [s['name'] for s in services if s.get('enabled', True)]
    
    # 创建服务配置字典，方便后续查找
    services_dict = {s['name']: s for s in services if s.get('enabled', True)}
    
    logger.info(f"可用翻译服务（按优先级）: {enabled_services}")
    
    TRANSLATE_MAX_RETRIES = get_config_value('translation.max_retries', 3)
    TRANSLATE_BASE_DELAY = get_config_value('translation.base_delay', 3)
    
    last_error = None
    
    # 遍历所有启用的服务
    for service in enabled_services:
        logger.info(f"尝试使用 {service} 进行翻译")
        service_success = False
        
        # 对每个服务进行最大重试次数的尝试
        for attempt in range(TRANSLATE_MAX_RETRIES):
            try:
                if attempt > 0:
                    # 503错误时使用更长的等待时间
                    delay = (attempt + 1) * TRANSLATE_BASE_DELAY  # 3秒、6秒、9秒
                    logger.info(f"第 {attempt + 1} 次重试，等待 {delay} 秒...")
                    time.sleep(delay)
                
                # OpenAI服务的处理
                if service == "openai":
                    openai_configs = get_config_value('tokens.openai', [])
                    for config in openai_configs:
                        logger.info(f"尝试使用OpenAI配置: {config.get('name', 'unnamed')}")
                        translation = translate_with_openai(text, target_lang, config)
                        if translation:
                            return translation
                    continue
                
                # 指定OpenAI配置的处理
                elif service.startswith("openai_"):
                    config_name = services_dict[service].get('config_name')
                    if not config_name:
                        logger.warning(f"服务 {service} 未指定config_name，跳过")
                        continue
                        
                    openai_configs = get_config_value('tokens.openai', [])
                    config = next((c for c in openai_configs if c.get('name') == config_name), None)
                    if not config:
                        logger.warning(f"未找到名为 {config_name} 的OpenAI配置，跳过")
                        continue
                        
                    logger.info(f"使用OpenAI配置: {config_name}")
                    translation = translate_with_openai(text, target_lang, config)
                    if translation:
                        return translation
                    continue
                
                # DeepL V2服务的处理
                elif service == "deeplx_v2":
                    api_url = get_config_value('deeplx.api_v2_url', 'http://deeplx:1188/v2/translate')
                    lang_map = {
                        'zh': 'zh', 'en': 'en', 'ja': 'ja', 'ko': 'ko',
                        'fr': 'fr', 'de': 'de', 'es': 'es', 'pt': 'pt',
                        'it': 'it', 'nl': 'nl', 'pl': 'pl', 'ru': 'ru',
                    }
                    source_lang_code = lang_map.get(source_lang, source_lang.lower())
                    target_lang_code = lang_map.get(target_lang, target_lang.lower())
                    
                    v2_data = {
                        "text": text,
                        "source_lang": source_lang_code,
                        "target_lang": target_lang_code
                    }
                    logger.info(f"发送请求到DeepLX V2: {api_url}")
                    logger.debug(f"请求数据: {json.dumps(v2_data, ensure_ascii=False)}")
                    
                    api_key = get_config_value('tokens.deepl', '')
                    headers = {
                        'Content-Type': 'application/json'
                    }
                    if api_key:
                        headers['Authorization'] = f'DeepL-Auth-Key {api_key}'
                    
                    response = requests.post(
                        api_url,
                        json=v2_data,
                        headers=headers,
                        timeout=10
                    )
                
                # DeepL V1服务的处理
                elif service == "deeplx":
                    api_url = get_config_value('deeplx.api_url', 'http://deeplx:1188/translate')
                    v1_data = {
                        "text": text,
                        "source_lang": source_lang,
                        "target_lang": target_lang
                    }
                    response = requests.post(
                        api_url,
                        json=v1_data,
                        timeout=10
                    )
                
                # 处理DeepL服务的响应
                if service in ["deeplx_v2", "deeplx"]:
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("code") == 200 and result.get("data"):
                            return result["data"]
                        elif "translations" in result:
                            return result['translations'][0]['text']
                        elif "data" in result:
                            return result["data"]
                        
                    logger.warning(f"{service} 返回状态码: {response.status_code}")
                    raise Exception(f"{service} 返回状态码: {response.status_code}")
                
                service_success = True
                break
                
            except Exception as e:
                last_error = str(e)
                logger.error(f"{service} 请求失败: {last_error}")
                if attempt == TRANSLATE_MAX_RETRIES - 1:
                    logger.warning(f"{service} 已达到最大重试次数，准备切换到下一个服务")
        
        if service_success:
            break
    
    logger.error(f"所有翻译服务都已尝试失败，最后的错误: {last_error}")
    return text

def translate_with_openai(text, target_lang='zh', config=None):
    """使用OpenAI API翻译文本
    
    Args:
        text: 要翻译的文本
        target_lang: 目标语言代码，默认为'zh'（中文）
        config: OpenAI配置字典，包含api_key、api_endpoint等
    """
    if not config:
        logger.warning("未提供OpenAI配置")
        return None
        
    # 获取配置
    api_key = config.get('api_key')
    api_endpoint = config.get('api_endpoint')
    model = config.get('model', 'gpt-4o-mini')
    prompt_template = config.get('prompt', 'You are a professional translator. Please translate the following text to {target_lang}, maintaining the original meaning, style, and formatting. Pay special attention to context and nuance.')
    
    logger.debug(f"OpenAI配置 - endpoint: {api_endpoint}, model: {model}")
    logger.debug(f"API密钥长度: {len(api_key) if api_key else 0}")
    logger.debug(f"翻译目标语言: {target_lang}")
    
    if not api_key:
        logger.warning("未配置OpenAI API密钥，跳过OpenAI翻译")
        return None

    try:
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }

        data = {
            'model': model,
            'messages': [
                {
                    'role': 'system',
                    'content': prompt_template.format(target_lang=target_lang)
                },
                {
                    'role': 'user',
                    'content': text
                }
            ],
            'temperature': 0.7
        }

        logger.debug(f"准备发送请求到OpenAI - URL: {api_endpoint}")
        response = requests.post(
            api_endpoint,
            headers=headers,
            json=data,
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            if 'choices' in result and len(result['choices']) > 0:
                translation = result['choices'][0]['message']['content'].strip()
                logger.info("OpenAI翻译成功")
                return translation
            
        logger.error(f"OpenAI翻译失败，状态码：{response.status_code}，响应：{response.text}")
        return None
            
    except Exception as e:
        logger.error(f"OpenAI翻译出错: {str(e)}")
        return None

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
            subtitles = parse_srt_content(content)
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
        subtitles = parse_srt_content(srt_content)
        
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
        url = data.get('url')
        location = data.get('location', 'new')  # 获取location参数，默认为'new'
        tags_str = data.get('tags', '')
        tags = [tag.strip() for tag in tags_str.split(',')] if tags_str else []
        logger.info(f"处理YouTube URL: {url}, location: {location}, tags: {tags}")
        
        # 先获取视频信息
        video_info = get_youtube_info(url)
        logger.info(f"获取到的视频信息: {json.dumps(video_info, indent=2, ensure_ascii=False)}")
        
        if not video_info or not video_info.get('title'):
            logger.error("无法获取视频信息")
            return jsonify({"error": "Failed to get video info", "success": False}), 400
            
        # 检测视频语言
        logger.info("准备调用 get_video_language 函数...")
        try:
            language = get_video_language(video_info)
            logger.info(f"get_video_language 返回结果: {language}")
        except Exception as e:
            logger.error(f"get_video_language 函数出错: {str(e)}")
            logger.error(traceback.format_exc())
            language = None
        
        # 确定字幕策略
        should_download, lang_priority = get_subtitle_strategy(language, video_info)
        logger.info(f"字幕策略: should_download={should_download}, lang_priority={lang_priority}")
            
        # 下载字幕或转录
        srt_content = None
        if should_download:
            try:
                srt_content, video_info = download_youtube_subtitles(url, video_info, lang_priority)
                if srt_content:
                    # 验证字幕内容
                    is_valid, error_msg = validate_subtitle_content(srt_content)
                    if not is_valid:
                        logger.error(f"字幕内容验证失败: {error_msg}")
                        srt_content = None
                    else:
                        # 清理字幕内容
                        srt_content = clean_subtitle_content(srt_content)
            except Exception as e:
                logger.error(f"下载字幕失败: {str(e)}")
                srt_content = None
        
        # 如果是中文视频且没有获取到字幕，尝试转录
        if not srt_content and language == 'zh':
            try:
                logger.info("开始转录视频音频...")
                # 下载视频音频
                audio_path = download_video(url)
                # 转录音频
                srt_content = transcribe_audio(audio_path)
                # 清理临时文件
                os.remove(audio_path)
                if srt_content:
                    srt_content = clean_subtitle_content(srt_content, is_funasr=True)
                    logger.info("转录完成并清理完成")
            except Exception as e:
                logger.error(f"转录失败: {str(e)}")
                logger.exception("详细错误信息:")
        
        # 解析字幕内容为列表格式
        subtitles = parse_srt_content(srt_content)
        if not subtitles:
            return jsonify({'error': '解析字幕失败'}), 500
            
        # 保存字幕文件
        title = video_info.get('title', '')
        if not title:
            title = f"{os.path.splitext(os.path.basename(url))[0]}"
                
        output_filename = sanitize_filename(f"{title}.srt")
        output_filepath = os.path.join(app.config['OUTPUT_FOLDER'], output_filename)
        with open(output_filepath, 'w', encoding='utf-8') as f:
            f.write(srt_content)
        
        # 生成文件信息并保存
        file_id = str(uuid.uuid4())
        files_info = load_files_info()
        files_info[file_id] = {
            'id': file_id,
            'filename': output_filename,
            'path': output_filepath,
            'url': f'/view/{file_id}',
            'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'show_timeline': True,
            'source': 'subtitle',
            'video_info': video_info
        }
        save_files_info(files_info)
        
        # 发送到Readwise
        try:
            # 将字幕列表转换为纯文本
            subtitle_text = []
            for subtitle in subtitles:
                if 'text' in subtitle:
                    subtitle_text.append(subtitle['text'])
            content = '\n'.join(subtitle_text)
            
            # 记录原始内容长度
            logger.info(f"原始内容长度: {len(content)}")
            logger.info(f"作者信息: {video_info.get('uploader')}")
            
            # 将内容转换为HTML格式
            content_with_br = content.replace('\n', '<br>')
            html_content = f'<div class="content">{content_with_br}</div>'
            
            # 准备视频信息
            video_title = video_info.get('title', 'YouTube Video Transcript')
            video_url = url
            video_author = video_info.get('uploader')
            video_date = video_info.get('published_date')
            
            logger.info(f"正在发送内容到Readwise: {video_title}")
            
            # 发送到Readwise
            success = save_to_readwise(
                title=video_title,
                content=html_content,
                url=video_url,
                published_date=video_date,
                author=video_author,
                location=location,
                tags=tags,
                language=language
            )
            
            if success:
                logger.info("成功发送到Readwise")
            else:
                logger.error("发送到Readwise失败")
            
        except Exception as e:
            logger.error(f"发送到Readwise失败: {str(e)}")
            logger.exception("详细错误信息:")
        
        # 返回结果
        return jsonify({
            "success": True,
            "srt_content": srt_content,
            "filename": output_filename,
            "view_url": f'/view/{file_id}'
        })
        
    except Exception as e:
        logger.error(f"处理YouTube URL时出错: {str(e)}")
        logger.exception("详细错误信息:")
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/process', methods=['POST'])
def process_video():
    """处理视频URL"""
    try:
        data = request.get_json()
        url = data.get('url')
        platform = data.get('platform', 'youtube')  # 默认为YouTube
        location = data.get('location', 'new')  # 默认为new
        tags = data.get('tags', [])  # 获取tags参数，默认为空列表
        
        # Log the request with location and tags
        logger.info(f'处理%s URL: %s, location: %s, tags: %s', 
                   platform, url, location, json.dumps(tags, ensure_ascii=False))
        
        if not url or not platform:
            return jsonify({'error': '缺少必要参数'}), 400
        
        # 获取视频信息
        video_info = get_video_info(url, platform)
        
        # 检测视频语言
        logger.info("准备调用 get_video_language 函数...")
        try:
            language = get_video_language(video_info)
            logger.info(f"get_video_language 返回结果: {language}")
        except Exception as e:
            logger.error(f"get_video_language 函数出错: {str(e)}")
            logger.error(traceback.format_exc())
            language = None
            
        # 确定字幕策略
        should_download, lang_priority = get_subtitle_strategy(language, video_info)
        logger.info(f"字幕策略: should_download={should_download}, lang_priority={lang_priority}")
        
        # 下载字幕
        subtitle_result = download_subtitles(url, platform, video_info) if should_download else None
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
                text = subtitle['text']
                # 如果是英文视频，添加翻译
                if language == 'en':
                    translation = translate_text(text)
                    text = f"{text}\n{translation}"
                srt_content += f"{i}\n{start_time} --> {end_time}\n{text}\n\n"
            
            # 保存字幕文件
            title = video_info.get('title', '') if video_info else ''
            if not title:
                title = f"{os.path.splitext(os.path.basename(url))[0]}"
                
            output_filename = sanitize_filename(f"{title}.srt")
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
                        tags=tags,  # 传递tags参数
                        language=language
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
            # 检查内容是否已经是SRT格式
            if subtitle_content and subtitle_content.strip().split('\n')[0].isdigit():
                logger.info("字幕内容已经是SRT格式")
                srt_content = subtitle_content
            else:
                logger.info("尝试将字幕转换为SRT格式")
                srt_content = convert_to_srt(subtitle_content, 'json3')
        elif platform == 'bilibili':
            # 检查内容是否已经是SRT格式
            if subtitle_content and subtitle_content.strip().split('\n')[0].isdigit():
                logger.info("字幕内容已经是SRT格式")
                srt_content = subtitle_content
            else:
                logger.info("尝试将字幕转换为SRT格式")
                srt_content = convert_to_srt(subtitle_content, 'json3')
        else:
            return jsonify({'error': '不支持的平台'}), 400
            
        if not srt_content:
            return jsonify({'error': '转换字幕失败'}), 500
            
        # 解析字幕内容为列表格式
        subtitles = parse_srt_content(srt_content)
        if not subtitles:
            return jsonify({'error': '解析字幕失败'}), 500
            
        # 保存字幕文件
        title = video_info.get('title', '') if video_info else ''
        if not title:
            title = f"{os.path.splitext(os.path.basename(url))[0]}"
                
        output_filename = sanitize_filename(f"{title}.srt")
        output_filepath = os.path.join(app.config['OUTPUT_FOLDER'], output_filename)
        with open(output_filepath, 'w', encoding='utf-8') as f:
            f.write(srt_content)
            
        logger.info(f"字幕文件已保存到: {output_filepath}")
        
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
        
        # 发送到Readwise
        try:
            # 将字幕列表转换为纯文本
            subtitle_text = []
            for subtitle in subtitles:
                if 'text' in subtitle:
                    subtitle_text.append(subtitle['text'])
            content = '\n'.join(subtitle_text)
            
            # 记录原始内容长度
            logger.info(f"原始内容长度: {len(content)}")
            logger.info(f"作者信息: {video_info.get('uploader')}")
            
            # 将内容转换为HTML格式
            content_with_br = content.replace('\n', '<br>')
            html_content = f'<div class="content">{content_with_br}</div>'
            
            # 准备视频信息
            video_title = video_info.get('title', 'Video Transcript') if video_info else title
            video_url = url
            video_author = video_info.get('uploader') if video_info else None
            video_date = video_info.get('published_date') if video_info else None
            
            logger.info(f"正在发送内容到Readwise: {video_title}")
            
            # 发送到Readwise
            success = save_to_readwise(
                title=video_title,
                content=html_content,
                url=video_url,
                published_date=video_date,
                author=video_author,
                location=location,
                tags=tags,
                language=language
            )
            
            if success:
                logger.info("成功发送到Readwise")
            else:
                logger.error("发送到Readwise失败")
            
        except Exception as e:
            logger.error(f"发送到Readwise失败: {str(e)}")
            logger.exception("详细错误信息:")
        
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
        # logger.info(f"开始进行视频语言检测，输入信息: {json.dumps(info, indent=2, ensure_ascii=False)}")
        
        # 1. 从标题判断语言
        if info.get('title'):
            title = info['title']
            logger.info(f"正在分析标题: {title}")
            
            # 检测标题是否包含中文字符
            has_chinese = any('\u4e00' <= char <= '\u9fff' for char in title)
            # 检查英文字母数量
            english_chars = sum(1 for char in title if char.isalpha())
            # 检查非ASCII字符（排除标点和空格）
            non_english = sum(1 for char in title if not char.isascii() and not char.isspace() and not char in "''""…—–-?!.,")
            # 如果标题包含足够多的英文字符（至少5个），且没有中文字符，判定为英文
            is_mainly_english = english_chars >= 5 and not has_chinese
            
            logger.info(f"标题特征: 包含中文={has_chinese}, 英文字符数={english_chars}, 非英文字符数={non_english}")
            
            if has_chinese:
                result = "✓ 通过标题检测：标题包含中文字符，判定为中文视频"
                logger.info(result)
                return 'zh'
            elif is_mainly_english:
                result = "✓ 通过标题检测：标题主要是英文，判定为英文视频"
                logger.info(result)
                return 'en'
            logger.info("标题语言无法确定，继续检查其他来源")
        else:
            logger.info("未找到视频标题，跳过标题语言检测")
        
        # 2. 从手动上传的字幕判断
        if info.get('subtitles'):
            manual_subs = info['subtitles']
            available_langs = list(manual_subs.keys())
            logger.info(f"发现手动上传的字幕，可用语言: {available_langs}")
            
            # 优先检查中文字幕
            if any(lang.startswith('zh') for lang in available_langs):
                result = "✓ 通过手动字幕检测：找到中文字幕，判定为中文视频"
                logger.info(result)
                return 'zh'
            # 其次检查英文字幕
            elif any(lang.startswith('en') for lang in available_langs):
                result = "✓ 通过手动字幕检测：找到英文字幕，判定为英文视频"
                logger.info(result)
                return 'en'
            logger.info("未找到中文或英文的手动字幕，继续检查其他来源")
        else:
            logger.info("未找到手动上传的字幕，跳过手动字幕语言检测")
        
        # 3. 从自动字幕判断
        if info.get('automatic_captions'):
            auto_subs = info['automatic_captions']
            available_langs = list(auto_subs.keys())
            logger.info(f"发现自动生成的字幕，可用语言: {available_langs}")
            
            # 检查是否有en-orig，这表示原始视频是英文的
            if 'en-orig' in available_langs:
                result = "✓ 通过自动字幕检测：找到英文原始字幕，判定为英文视频"
                logger.info(result)
                return 'en'
            # 其次检查是否有英文字幕
            elif any(lang == 'en' for lang in available_langs):
                result = "✓ 通过自动字幕检测：找到英文字幕，判定为英文视频"
                logger.info(result)
                return 'en'
            # 最后检查中文字幕
            elif any(lang.startswith('zh') for lang in available_langs):
                result = "✓ 通过自动字幕检测：找到中文字幕，判定为中文视频"
                logger.info(result)
                return 'zh'
            logger.info("未找到中文或英文的自动字幕，继续检查其他来源")
        else:
            logger.info("未找到自动生成的字幕，跳过自动字幕语言检测")
        
        # 4. 从视频语言字段获取
        if info.get('language'):
            lang = info['language']
            logger.info(f"从视频信息中获取到语言字段: {lang}")
            
            if lang.startswith('zh'):
                result = "✓ 通过视频语言字段检测：语言为中文"
                logger.info(result)
                return 'zh'
            elif lang.startswith('en'):
                result = "✓ 通过视频语言字段检测：语言为英文"
                logger.info(result)
                return 'en'
            logger.info(f"视频语言字段为 {lang}，无法确定是中文还是英文")
        else:
            logger.info("未找到视频语言字段")
        
        logger.warning("无法通过任何方式确定视频语言，返回 None")
        return None
        
    except Exception as e:
        logger.error(f"语言检测过程出错: {str(e)}")
        logger.error(traceback.format_exc())
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
            return True, ['zh-Hans', 'zh-Hant', 'zh']
        return False, []  # 返回False表示需要转录
        
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
    app.run(host='0.0.0.0', port=5000, debug=True)
