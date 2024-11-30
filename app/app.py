from flask import Flask, request, render_template_string, jsonify
import os
from datetime import datetime
import uuid
import codecs
import logging
import sys
import binascii
import chardet
import json
import requests
from yt_dlp import YoutubeDL
import subprocess
import re
from flask_cors import CORS

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
    r"/process_youtube": {"origins": "*"}
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
        json.dump([], f, ensure_ascii=False)

def load_files_info():
    try:
        with open(FILES_INFO, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return []

def save_files_info(files_info):
    with open(FILES_INFO, 'w', encoding='utf-8') as f:
        json.dump(files_info, f, ensure_ascii=False, indent=2)

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

def download_youtube_subtitles(url):
    """从YouTube下载字幕文件，优先下载中文字幕，其次是英文字幕"""
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            ydl_opts = {
                'skip_download': True,
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': ['zh-Hans', 'zh-CN', 'zh', 'en'],
                'quiet': True,
                'socket_timeout': 30,  # 增加超时时间
                'nocheckcertificate': True,  # 忽略SSL证书验证
            }
            
            with YoutubeDL(ydl_opts) as ydl:
                logger.info(f"尝试下载字幕 (第{retry_count + 1}次尝试)")
                info = ydl.extract_info(url, download=False)
                
                # 检查是否有字幕
                if not info.get('subtitles') and not info.get('automatic_captions'):
                    logger.warning("没有找到字幕")
                    return None, info
                
                # 优先使用手动上传的字幕
                subtitles = info.get('subtitles', {})
                if not subtitles:
                    subtitles = info.get('automatic_captions', {})
                
                # 按优先级尝试不同语言
                for lang in ['zh-Hans', 'zh-CN', 'zh', 'en']:
                    if lang in subtitles:
                        formats = subtitles[lang]
                        # 优先使用srt格式
                        for fmt in formats:
                            if fmt['ext'] == 'srt':
                                logger.info(f"找到{lang}语言的SRT字幕")
                                try:
                                    # 下载字幕内容
                                    subtitle_url = fmt['url']
                                    logger.info(f"正在从URL下载字幕: {subtitle_url}")
                                    response = requests.get(subtitle_url, timeout=30)
                                    response.raise_for_status()  # 检查响应状态
                                    
                                    # 获取字幕内容的字节数据
                                    content_bytes = response.content
                                    
                                    # 检查内容是否为空
                                    if not content_bytes:
                                        logger.warning(f"{lang}语言字幕内容为空，尝试下一个")
                                        continue
                                    
                                    # 检测编码并解码
                                    encoding = detect_file_encoding(content_bytes)
                                    subtitle_content = content_bytes.decode(encoding)
                                    
                                    # 验证字幕内容是否有效
                                    if not subtitle_content.strip() or '-->' not in subtitle_content:
                                        logger.warning(f"{lang}语言字幕内容无效，尝试下一个")
                                        continue
                                    
                                    logger.info(f"成功下载{lang}语言字幕，使用编码: {encoding}")
                                    logger.debug(f"字幕内容预览: {subtitle_content[:200]}...")
                                    return subtitle_content, info
                                except Exception as e:
                                    logger.error(f"下载{lang}语言字幕内容时出错: {str(e)}")
                                    continue
                        
                        # 如果没有srt格式，使用第一个可用格式
                        logger.info(f"找到{lang}语言的字幕，但不是SRT格式")
                        try:
                            subtitle_url = formats[0]['url']
                            logger.info(f"正在从URL下载字幕: {subtitle_url}")
                            response = requests.get(subtitle_url, timeout=30)
                            response.raise_for_status()
                            
                            # 获取字幕内容的字节数据
                            content_bytes = response.content
                            
                            # 检查内容是否为空
                            if not content_bytes:
                                logger.warning(f"{lang}语言字幕内容为空，尝试下一个")
                                continue
                            
                            # 检测编码并解码
                            encoding = detect_file_encoding(content_bytes)
                            subtitle_content = content_bytes.decode(encoding)
                            
                            # 验证字幕内容是否有效
                            if not subtitle_content.strip() or '-->' not in subtitle_content:
                                logger.warning(f"{lang}语言字幕内容无效，尝试下一个")
                                continue
                            
                            logger.info(f"成功下载{lang}语言字幕，使用编码: {encoding}")
                            logger.debug(f"字幕内容预览: {subtitle_content[:200]}...")
                            return subtitle_content, info
                        except Exception as e:
                            logger.error(f"下载{lang}语言字幕内容时出错: {str(e)}")
                            continue
                
                logger.warning("未找到有效的字幕内容")
                return None, info
                
        except Exception as e:
            retry_count += 1
            error_msg = str(e)
            logger.error(f"下载YouTube字幕时出错 (尝试 {retry_count}/{max_retries}): {error_msg}")
            logger.exception(e)  # 输出完整的错误堆栈
            
            if retry_count >= max_retries:
                logger.error("达到最大重试次数，放弃下载")
                return None, None
            
            # 在重试之前等待一段时间
            import time
            time.sleep(2 * retry_count)  # 递增等待时间
    
    return None, None

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
        with YoutubeDL(ydl_opts) as ydl:
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

def transcribe_audio(audio_path):
    """使用FunASR转录音频"""
    try:
        # 使用transcribe-audio服务的内部网络地址
        funasr_url = 'http://transcribe-audio:10095/recognize'
        logger.info(f"发送请求到FunASR服务: {funasr_url}")
        logger.info(f"音频文件路径: {audio_path}")
        
        # 检查音频文件
        if not os.path.exists(audio_path):
            logger.error(f"音频文件不存在: {audio_path}")
            return None
            
        file_size = os.path.getsize(audio_path)
        logger.info(f"音频文件大小: {file_size} bytes")
        
        with open(audio_path, 'rb') as audio_file:
            files = {'audio': audio_file}
            try:
                # 尝试DNS解析
                import socket
                try:
                    transcribe_ip = socket.gethostbyname('transcribe-audio')
                    logger.info(f"transcribe-audio DNS解析结果: {transcribe_ip}")
                except socket.gaierror as e:
                    logger.error(f"DNS解析失败: {str(e)}")
                
                # 禁用代理，使用容器网络直接通信
                local_proxies = {
                    'http': None,
                    'https': None
                }
                
                # 添加超时设置
                logger.info("开始发送请求...")
                response = requests.post(
                    funasr_url,
                    files=files,
                    proxies=local_proxies,
                    timeout=300,
                    verify=False  # 禁用SSL验证
                )
                logger.info(f"FunASR响应状态码: {response.status_code}")
                logger.info(f"FunASR响应头: {response.headers}")
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info("成功获取转录结果")
                    return result
                else:
                    logger.error(f"FunASR服务返回错误: {response.status_code}")
                    logger.error(f"错误响应内容: {response.text}")
                    # 尝试ping transcribe-audio
                    try:
                        import subprocess
                        result = subprocess.run(['ping', 'transcribe-audio'], capture_output=True, text=True)
                        logger.info(f"Ping结果: {result.stdout}")
                    except Exception as e:
                        logger.error(f"Ping失败: {str(e)}")
                    return None
                    
            except requests.exceptions.ConnectionError as e:
                logger.error(f"连接FunASR服务失败: {str(e)}")
                return None
            except requests.exceptions.Timeout as e:
                logger.error(f"FunASR服务请求超时: {str(e)}")
                return None
            except Exception as e:
                logger.error(f"调用FunASR服务时发生错误: {str(e)}")
                logger.error(f"错误类型: {type(e)}")
                return None
    except Exception as e:
        logger.error(f"转录音频时发生错误: {str(e)}")
        logger.error(f"错误类型: {type(e)}")
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
        with YoutubeDL(ydl_opts) as ydl:
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

def save_to_readwise(title, content, url=None, published_date=None):
    """保存内容到Readwise，支持长文本分段"""
    try:
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
                "tags": ["youtube", "subtitles", "transcription"]
            }

            # 添加发布日期（如果有）
            if published_date:
                data["published_date"] = published_date

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
            <div class="time">{{ "%.3f"|format(sub.start) }} - {{ "%.3f"|format(sub.start + sub.duration) }}</div>
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
            if (!url) {
                showError('请输入YouTube URL');
                return false;
            }
            
            // 显示进度
            document.getElementById('progress').style.display = 'block';
            document.getElementById('progress').innerText = '正在处理...';
            document.getElementById('error-message').style.display = 'none';
            
            fetch('/process_youtube', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({url: url})
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
            <h3>处理YouTube视频字幕</h3>
            <input type="text" id="youtube-url" placeholder="输入YouTube视频URL">
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
            files_info.append({
                'id': file_id,
                'filename': file.filename,
                'path': filepath,
                'url': f'/view/{file_id}',
                'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'show_timeline': show_timeline,
                'subtitles': subtitles
            })
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
        for file_info in files_info:
            if file_info['id'] == file_id:
                return render_template_string(
                    HTML_TEMPLATE,
                    filename=file_info['filename'],
                    subtitles=file_info['subtitles'],
                    show_timeline=file_info['show_timeline']
                )
        return "File not found", 404
    except Exception as e:
        logger.error(f"查看文件时出错: {str(e)}")
        return str(e), 500

@app.route('/view/')
def view_files():
    """查看所有字幕文件列表"""
    files_info = load_files_info()
    files_info.sort(key=lambda x: x['upload_time'], reverse=True)
    return render_template_string(FILES_LIST_TEMPLATE, files=files_info)

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
        logger.info(f"处理YouTube URL: {url}")
        
        # 下载字幕
        srt_content, video_info = download_youtube_subtitles(url)
        if not srt_content:
            # 如果没有字幕，尝试下载视频并转录
            audio_path = download_video(url)
            if not audio_path:
                return jsonify({"error": "Failed to download video", "success": False}), 500
                
            # 使用FunASR转录
            result = transcribe_audio(audio_path)
            if not result:
                return jsonify({"error": "Failed to transcribe audio", "success": False}), 500
                
            # 解析转录结果生成字幕
            subtitles = parse_srt(result)
            if not subtitles:
                return jsonify({"error": "Failed to parse subtitles", "success": False}), 500
                
            # 生成SRT格式内容
            srt_content = ""
            for i, subtitle in enumerate(subtitles, 1):
                start_time = format_time(subtitle['start'])
                end_time = format_time(subtitle['start'] + subtitle['duration'])
                srt_content += f"{i}\n{start_time} --> {end_time}\n{subtitle['text']}\n\n"
            
            # 保存SRT文件
            output_filename = os.path.join(app.config['OUTPUT_FOLDER'], f"{os.path.splitext(os.path.basename(audio_path))[0]}.srt")
            with open(output_filename, 'w', encoding='utf-8') as f:
                f.write(srt_content)
            
            logger.info(f"转录结果已保存到: {output_filename}")
            
            # 删除临时音频文件
            try:
                os.remove(audio_path)
                logger.info(f"已删除临时文件: {audio_path}")
            except Exception as e:
                logger.warning(f"删除临时文件失败: {str(e)}")
        
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
                current_subtitle['start'] = parse_time(start)
                current_subtitle['duration'] = parse_time(end) - parse_time(start)
            elif current_subtitle.get('start') is not None:  # 文本行
                current_subtitle['text'] = line
        
        # 添加最后一个字幕条目
        if current_subtitle:
            subtitles_list.append(current_subtitle)
        
        # 保存文件信息
        file_info = {
            'id': file_id,
            'filename': f"{video_info.get('title', 'youtube_video')}.srt",
            'path': output_filename if 'output_filename' in locals() else None,
            'url': f'/view/{file_id}',
            'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'show_timeline': True,
            'subtitles': subtitles_list
        }
        files_info.append(file_info)
        save_files_info(files_info)
        
        # 发送到Readwise
        try:
            if video_info:
                save_to_readwise(
                    title=video_info.get('title', 'YouTube Video Transcript'),
                    content='\n'.join(s['text'] for s in subtitles_list),
                    url=url,
                    published_date=video_info.get('published_date')
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

if __name__ == '__main__':
    logger.info("启动Flask服务器")
    app.run(host='0.0.0.0', port=5000)
