"""Video processing service for downloading and processing videos."""

import os
import json
import logging
import requests
import tempfile
import shutil
import subprocess
import re
import yt_dlp
import uuid
import configparser
import time
import math
import chardet
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple
from pydub import AudioSegment
import wave

try:
    from ..config.config_manager import get_config_value
    from ..utils.file_utils import detect_file_encoding, sanitize_filename
except ImportError:
    from config.config_manager import get_config_value
    from utils.file_utils import detect_file_encoding, sanitize_filename

logger = logging.getLogger(__name__)


class VideoService:
    """Service for video downloading, processing, and subtitle extraction."""
    
    def __init__(self):
        self.upload_folder = get_config_value('app.upload_folder', '/app/uploads')
        self.output_folder = get_config_value('app.output_folder', '/app/outputs')
        self.video_domain = get_config_value('servers.video_domain', 'http://video.domain.com')
        
        # Ensure directories exist
        os.makedirs(self.upload_folder, exist_ok=True)
        os.makedirs(self.output_folder, exist_ok=True)
    
    def get_firefox_profile_path(self) -> Optional[str]:
        """
        Get Firefox profile path for cookie access.
        
        Returns:
            Firefox profile path or None if not found
        """
        try:
            # Try container path first
            container_path = '/root/.mozilla/firefox/Profiles/3tfynuxa.default-release'
            if os.path.exists(container_path):
                logger.info(f"Using container Firefox profile: {container_path}")
                return container_path
            
            # Try local Firefox profile paths
            possible_paths = [
                os.path.expanduser('~/.mozilla/firefox'),
                os.path.expanduser('~/AppData/Roaming/Mozilla/Firefox/Profiles'),
                './firefox_profile/Profiles'
            ]
            
            for base_path in possible_paths:
                if os.path.exists(base_path):
                    # Look for profiles.ini
                    profiles_ini = os.path.join(base_path, 'profiles.ini')
                    if os.path.exists(profiles_ini):
                        try:
                            config = configparser.ConfigParser()
                            config.read(profiles_ini)
                            for section in config.sections():
                                if section.startswith('Profile'):
                                    if config.get(section, 'Default', fallback='0') == '1':
                                        profile_path = config.get(section, 'Path')
                                        if config.get(section, 'IsRelative', fallback='1') == '1':
                                            profile_path = os.path.join(base_path, profile_path)
                                        if os.path.exists(profile_path):
                                            logger.info(f"Found Firefox profile: {profile_path}")
                                            return profile_path
                        except Exception as e:
                            logger.warning(f"Error reading profiles.ini: {str(e)}")
                    
                    # Fallback: look for any profile directory
                    for item in os.listdir(base_path):
                        if item.endswith('.default-release') or item.endswith('.default'):
                            profile_path = os.path.join(base_path, item)
                            if os.path.isdir(profile_path):
                                logger.info(f"Found Firefox profile (fallback): {profile_path}")
                                return profile_path
            
            logger.warning("No Firefox profile found")
            return None
            
        except Exception as e:
            logger.error(f"Error finding Firefox profile: {str(e)}")
            return None
    
    def convert_youtube_url(self, url: str) -> str:
        """
        Convert YouTube URL to custom domain format.
        
        Args:
            url: Original YouTube URL
            
        Returns:
            Converted URL
        """
        try:
            # Convert various YouTube URL formats
            patterns = [
                (r'https://www\.youtube\.com/watch\?v=([a-zA-Z0-9_-]+)', f'{self.video_domain}/watch/\\1'),
                (r'https://youtu\.be/([a-zA-Z0-9_-]+)', f'{self.video_domain}/watch/\\1'),
                (r'https://m\.youtube\.com/watch\?v=([a-zA-Z0-9_-]+)', f'{self.video_domain}/watch/\\1'),
                (r'https://youtube\.com/watch\?v=([a-zA-Z0-9_-]+)', f'{self.video_domain}/watch/\\1')
            ]
            
            for pattern, replacement in patterns:
                if re.match(pattern, url):
                    converted = re.sub(pattern, replacement, url)
                    logger.info(f"Converted URL: {url} -> {converted}")
                    return converted
            
            # If no pattern matches, return original URL
            logger.info(f"URL conversion not needed: {url}")
            return url
            
        except Exception as e:
            logger.error(f"Error converting URL: {str(e)}")
            return url
    
    def get_video_info(self, url: str, platform: str) -> Optional[Dict[str, Any]]:
        """
        Get video information for different platforms.
        
        Args:
            url: Video URL
            platform: Platform name (youtube, bilibili, acfun)
            
        Returns:
            Video information dictionary
        """
        try:
            if platform == 'youtube':
                return self.get_youtube_info(url)
            elif platform == 'bilibili':
                return self.get_bilibili_info(url)
            elif platform == 'acfun':
                return self.get_acfun_info(url)
            else:
                logger.error(f"Unsupported platform: {platform}")
                return None
        except Exception as e:
            logger.error(f"Error getting video info: {str(e)}")
            return None
    
    def get_youtube_info(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Get YouTube video information.
        
        Args:
            url: YouTube URL
            
        Returns:
            Video information dictionary
        """
        try:
            firefox_profile = self.get_firefox_profile_path()
            
            ydl_opts = {
                'format': 'best',
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0',
                'http_headers': {'Referer': 'https://www.youtube.com/'}
            }
            
            if firefox_profile:
                ydl_opts['cookiesfrombrowser'] = ('firefox', firefox_profile)
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                # Extract relevant information
                video_info = {
                    'id': info.get('id', ''),
                    'title': info.get('title', ''),
                    'uploader': info.get('uploader', ''),
                    'upload_date': info.get('upload_date', ''),
                    'duration': info.get('duration', 0),
                    'view_count': info.get('view_count', 0),
                    'description': info.get('description', '')[:500],  # Limit description length
                    'thumbnail': info.get('thumbnail', ''),
                    'subtitles': list(info.get('subtitles', {}).keys()),
                    'automatic_captions': list(info.get('automatic_captions', {}).keys()),
                    'language': info.get('language', 'en'),
                    'webpage_url': info.get('webpage_url', url)
                }
                
                logger.info(f"Retrieved YouTube video info: {video_info['title']}")
                return video_info
                
        except Exception as e:
            logger.error(f"Error getting YouTube info: {str(e)}")
            return None
    
    def get_bilibili_info(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Get Bilibili video information.
        
        Args:
            url: Bilibili URL
            
        Returns:
            Video information dictionary
        """
        try:
            ydl_opts = {
                'format': 'best',
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                video_info = {
                    'id': info.get('id', ''),
                    'title': info.get('title', ''),
                    'uploader': info.get('uploader', ''),
                    'upload_date': info.get('upload_date', ''),
                    'duration': info.get('duration', 0),
                    'view_count': info.get('view_count', 0),
                    'description': info.get('description', '')[:500],
                    'thumbnail': info.get('thumbnail', ''),
                    'subtitles': list(info.get('subtitles', {}).keys()),
                    'language': 'zh',  # Assume Chinese for Bilibili
                    'webpage_url': info.get('webpage_url', url)
                }
                
                logger.info(f"Retrieved Bilibili video info: {video_info['title']}")
                return video_info
                
        except Exception as e:
            logger.error(f"Error getting Bilibili info: {str(e)}")
            return None
    
    def get_acfun_info(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Get AcFun video information.
        
        Args:
            url: AcFun URL
            
        Returns:
            Video information dictionary
        """
        try:
            ydl_opts = {
                'format': 'best',
                'quiet': True,
                'no_warnings': True,
                'extract_flat': False,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                video_info = {
                    'id': info.get('id', ''),
                    'title': info.get('title', ''),
                    'uploader': info.get('uploader', ''),
                    'upload_date': info.get('upload_date', ''),
                    'duration': info.get('duration', 0),
                    'view_count': info.get('view_count', 0),
                    'description': info.get('description', '')[:500],
                    'thumbnail': info.get('thumbnail', ''),
                    'subtitles': list(info.get('subtitles', {}).keys()),
                    'language': 'zh',  # Assume Chinese for AcFun
                    'webpage_url': info.get('webpage_url', url)
                }
                
                logger.info(f"Retrieved AcFun video info: {video_info['title']}")
                return video_info
                
        except Exception as e:
            logger.error(f"Error getting AcFun info: {str(e)}")
            return None
    
    def download_video(self, url: str) -> Optional[str]:
        """
        Download video and convert to WAV audio.
        
        Args:
            url: Video URL
            
        Returns:
            Path to the downloaded WAV file or None if failed
        """
        try:
            # Create unique filename
            video_id = str(uuid.uuid4())
            temp_video_path = os.path.join(self.upload_folder, f"temp_{video_id}")
            final_audio_path = os.path.join(self.upload_folder, f"{video_id}.wav")
            
            logger.info(f"Starting video download: {url}")
            
            firefox_profile = self.get_firefox_profile_path()
            
            # yt-dlp options
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': temp_video_path + '.%(ext)s',
                'quiet': True,
                'no_warnings': True,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0',
                'http_headers': {'Referer': 'https://www.youtube.com/'}
            }
            
            if firefox_profile and 'youtube.com' in url:
                ydl_opts['cookiesfrombrowser'] = ('firefox', firefox_profile)
            
            # Download video
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
            # Find downloaded file
            downloaded_file = None
            for file in os.listdir(self.upload_folder):
                if file.startswith(f"temp_{video_id}"):
                    downloaded_file = os.path.join(self.upload_folder, file)
                    break
            
            if not downloaded_file or not os.path.exists(downloaded_file):
                logger.error("Downloaded file not found")
                return None
            
            logger.info(f"Downloaded file: {downloaded_file}")
            
            # Convert to WAV using ffmpeg
            try:
                cmd = [
                    'ffmpeg', '-i', downloaded_file,
                    '-acodec', 'pcm_s16le',
                    '-ar', '16000',
                    '-ac', '1',
                    '-y',  # Overwrite output file
                    final_audio_path
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                
                if result.returncode != 0:
                    logger.error(f"FFmpeg error: {result.stderr}")
                    return None
                
                logger.info(f"Successfully converted to WAV: {final_audio_path}")
                
                # Clean up temp file
                try:
                    os.remove(downloaded_file)
                    logger.debug(f"Cleaned up temp file: {downloaded_file}")
                except:
                    pass
                
                return final_audio_path
                
            except subprocess.TimeoutExpired:
                logger.error("FFmpeg conversion timeout")
                return None
            except FileNotFoundError:
                logger.error("FFmpeg not found. Please install FFmpeg.")
                return None
            
        except Exception as e:
            logger.error(f"Error downloading video: {str(e)}")
            return None
    
    def split_audio(self, audio_path: str, max_duration: int = 600, 
                   max_size: int = 100*1024*1024) -> List[str]:
        """
        Split audio file into smaller segments.
        
        Args:
            audio_path: Path to audio file
            max_duration: Maximum duration per segment in seconds
            max_size: Maximum file size per segment in bytes
            
        Returns:
            List of segment file paths
        """
        try:
            if not os.path.exists(audio_path):
                logger.error(f"Audio file not found: {audio_path}")
                return []
            
            file_size = os.path.getsize(audio_path)
            logger.info(f"Audio file size: {file_size / (1024*1024):.2f} MB")
            
            # If file is small enough, return as-is
            if file_size <= max_size:
                try:
                    audio = AudioSegment.from_wav(audio_path)
                    duration = len(audio) / 1000  # Convert to seconds
                    
                    if duration <= max_duration:
                        logger.info("Audio file doesn't need splitting")
                        return [audio_path]
                except Exception as e:
                    logger.warning(f"Could not check audio duration: {str(e)}")
                    return [audio_path]
            
            # Split the audio file
            logger.info(f"Splitting audio file into segments (max {max_duration}s each)")
            
            audio = AudioSegment.from_wav(audio_path)
            duration = len(audio)  # Duration in milliseconds
            segment_duration = max_duration * 1000  # Convert to milliseconds
            
            segments = []
            base_name = os.path.splitext(audio_path)[0]
            
            for i in range(0, duration, segment_duration):
                end_time = min(i + segment_duration, duration)
                segment = audio[i:end_time]
                
                segment_path = f"{base_name}_part{len(segments)+1}.wav"
                segment.export(segment_path, format="wav")
                segments.append(segment_path)
                
                logger.debug(f"Created segment {len(segments)}: {segment_path}")
            
            logger.info(f"Split audio into {len(segments)} segments")
            return segments
            
        except Exception as e:
            logger.error(f"Error splitting audio: {str(e)}")
            return [audio_path]  # Return original file as fallback
    
    
    def get_video_language(self, video_info: Dict[str, Any]) -> Optional[str]:
        """
        Comprehensive video language detection.
        
        Args:
            video_info: Video information dictionary
            
        Returns:
            Language code ('en', 'zh') or None
        """
        try:
            logger.info(f"开始语言检测，video_info keys: {list(video_info.keys()) if video_info else 'None'}")
            
            if not video_info:
                logger.warning("video_info为空，跳过语言检测")
                return None
            
            # 1. 分析标题
            title = video_info.get('title', '')
            logger.info(f"标题: {title[:100]}..." if len(title) > 100 else f"标题: {title}")
            
            # 中文字符检测
            chinese_chars = re.findall(r'[\u4e00-\u9fff]', title)
            logger.info(f"标题中的中文字符数量: {len(chinese_chars)}")
            
            # 英文单词检测  
            english_words = re.findall(r'\b[a-zA-Z]+\b', title)
            logger.info(f"标题中的英文单词数量: {len(english_words)}")
            
            # 如果中文字符较多，判断为中文
            if len(chinese_chars) >= 3:
                logger.info("根据标题判断为中文视频")
                return 'zh'
            
            # 如果主要是英文单词，判断为英文
            if len(english_words) >= 3 and len(chinese_chars) == 0:
                logger.info("根据标题判断为英文视频")
                return 'en'
            
            # 2. 检查手动字幕
            subtitles = video_info.get('subtitles', {})
            if isinstance(subtitles, dict):
                subtitle_langs = list(subtitles.keys())
            elif isinstance(subtitles, list):
                subtitle_langs = subtitles
            else:
                subtitle_langs = []
                
            logger.info(f"可用的手动字幕语言: {subtitle_langs}")
            
            # 检查是否有中文字幕
            chinese_subtitle_langs = [lang for lang in subtitle_langs 
                                    if any(x in lang.lower() for x in ['zh', 'chinese', 'china'])]
            if chinese_subtitle_langs:
                logger.info(f"发现中文字幕: {chinese_subtitle_langs}，判断为中文视频")
                return 'zh'
            
            # 检查是否有英文字幕
            english_subtitle_langs = [lang for lang in subtitle_langs 
                                    if any(x in lang.lower() for x in ['en', 'english'])]
            if english_subtitle_langs:
                logger.info(f"发现英文字幕: {english_subtitle_langs}，判断为英文视频")
                return 'en'
            
            # 3. 检查自动字幕
            automatic_captions = video_info.get('automatic_captions', {})
            if isinstance(automatic_captions, dict):
                auto_caption_langs = list(automatic_captions.keys())
            elif isinstance(automatic_captions, list):
                auto_caption_langs = automatic_captions
            else:
                auto_caption_langs = []
                
            logger.info(f"可用的自动字幕语言: {auto_caption_langs}")
            
            # 检查自动字幕中的中文
            chinese_auto_langs = [lang for lang in auto_caption_langs 
                                if any(x in lang.lower() for x in ['zh', 'chinese', 'china'])]
            if chinese_auto_langs:
                logger.info(f"发现中文自动字幕: {chinese_auto_langs}，判断为中文视频")
                return 'zh'
            
            # 检查自动字幕中的英文
            english_auto_langs = [lang for lang in auto_caption_langs 
                                if any(x in lang.lower() for x in ['en', 'english'])]
            if english_auto_langs:
                logger.info(f"发现英文自动字幕: {english_auto_langs}，判断为英文视频")
                return 'en'
            
            # 4. 检查视频的language字段
            video_language = video_info.get('language')
            if video_language:
                logger.info(f"视频language字段: {video_language}")
                if 'zh' in video_language.lower() or 'chinese' in video_language.lower():
                    logger.info("根据language字段判断为中文视频")
                    return 'zh'
                elif 'en' in video_language.lower() or 'english' in video_language.lower():
                    logger.info("根据language字段判断为英文视频")
                    return 'en'
            
            # 5. 如果都没有检测到，返回None
            logger.info("未能确定视频语言")
            return None
            
        except Exception as e:
            logger.error(f"语言检测出错: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    def get_subtitle_strategy(self, language: Optional[str], video_info: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Determine subtitle download strategy based on language.
        
        Args:
            language: Detected video language
            video_info: Video information dictionary
            
        Returns:
            Tuple of (should_download_subtitles, language_priority_list)
        """
        try:
            logger.info(f"确定字幕策略，语言: {language}")
            
            if language == 'zh':
                # 中文视频：只下载手动字幕，不使用自动字幕
                logger.info("中文视频：仅尝试下载手动字幕")
                return True, ['zh-Hans', 'zh-Hant', 'zh', 'zh-CN']
            elif language == 'en':
                # 英文视频：优先手动字幕，也可以使用自动字幕
                logger.info("英文视频：优先手动字幕，可使用自动字幕")
                return True, ['en', 'en-US', 'en-GB']
            else:
                # 其他语言暂不处理
                logger.info(f"不支持的语言: {language}，跳过字幕下载")
                return False, []
                
        except Exception as e:
            logger.error(f"确定字幕策略时出错: {str(e)}")
            return False, []
    
    def download_subtitles(self, url: str, platform: str, video_info: Optional[Dict[str, Any]] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        Download subtitles for different platforms.
        
        Args:
            url: Video URL
            platform: Platform name (youtube, bilibili, acfun)
            video_info: Video information dictionary
            
        Returns:
            Tuple of (subtitle_content, video_info) or None
        """
        try:
            if platform == 'youtube':
                return self.download_youtube_subtitles(url, video_info)
            elif platform == 'bilibili':
                return self.download_bilibili_subtitles(url, video_info)
            elif platform == 'acfun':
                return self.download_acfun_subtitles(url, video_info)
            else:
                logger.error(f"不支持的平台: {platform}")
                return None
                
        except Exception as e:
            logger.error(f"下载字幕时出错: {str(e)}")
            return None
    
    def download_youtube_subtitles(self, url: str, video_info: Optional[Dict[str, Any]] = None, 
                                  lang_priority: Optional[List[str]] = None) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        Download YouTube subtitles with comprehensive language support.
        
        Args:
            url: YouTube URL
            video_info: Video information dictionary
            lang_priority: Language priority list
            
        Returns:
            Tuple of (subtitle_content, video_info) or None
        """
        try:
            logger.info(f"开始下载YouTube字幕: {url}")
            
            # 创建临时目录
            temp_dir = tempfile.mkdtemp()
            logger.info(f"创建临时目录: {temp_dir}")
            
            try:
                # 默认语言优先级
                if not lang_priority:
                    lang_priority = ['zh-Hans', 'zh-Hant', 'zh', 'en']
                
                firefox_profile = self.get_firefox_profile_path()
                
                # 配置yt-dlp选项
                ydl_opts = {
                    'writesubtitles': True,
                    'writeautomaticsub': True,
                    'subtitleslangs': lang_priority,
                    'subtitlesformat': 'srt/vtt/json3/ttml',
                    'skip_download': True,
                    'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
                    'quiet': True,
                    'no_warnings': True,
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0',
                    'http_headers': {'Referer': 'https://www.youtube.com/'}
                }
                
                if firefox_profile:
                    ydl_opts['cookiesfrombrowser'] = ('firefox', firefox_profile)
                    logger.info("使用Firefox cookie进行认证")
                
                # 下载字幕
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    
                    if not video_info:
                        video_info = {
                            'id': info.get('id', ''),
                            'title': info.get('title', ''),
                            'uploader': info.get('uploader', ''),
                            'upload_date': info.get('upload_date', ''),
                            'duration': info.get('duration', 0),
                            'view_count': info.get('view_count', 0),
                            'description': info.get('description', '')[:500],
                            'thumbnail': info.get('thumbnail', ''),
                            'subtitles': list(info.get('subtitles', {}).keys()),
                            'automatic_captions': list(info.get('automatic_captions', {}).keys()),
                            'language': info.get('language', 'en'),
                            'webpage_url': info.get('webpage_url', url)
                        }
                
                # 查找下载的字幕文件
                subtitle_files = []
                for file in os.listdir(temp_dir):
                    if any(file.endswith(f'.{lang}.{ext}') 
                          for lang in lang_priority 
                          for ext in ['srt', 'vtt', 'json3', 'ttml']):
                        subtitle_files.append(os.path.join(temp_dir, file))
                
                logger.info(f"找到字幕文件: {subtitle_files}")
                
                # 优先选择手动字幕，然后选择自动字幕
                selected_file = None
                for lang in lang_priority:
                    # 先找手动字幕
                    for file in subtitle_files:
                        if f'.{lang}.' in file and 'auto' not in file:
                            selected_file = file
                            logger.info(f"选择手动字幕文件: {selected_file}")
                            break
                    if selected_file:
                        break
                
                # 如果没有手动字幕，选择自动字幕
                if not selected_file:
                    for lang in lang_priority:
                        for file in subtitle_files:
                            if f'.{lang}.' in file:
                                selected_file = file
                                logger.info(f"选择自动字幕文件: {selected_file}")
                                break
                        if selected_file:
                            break
                
                if not selected_file:
                    logger.warning("未找到合适的字幕文件")
                    return None
                
                # 读取字幕内容
                with open(selected_file, 'rb') as f:
                    raw_content = f.read()
                
                # 检测编码
                encoding = detect_file_encoding(raw_content)
                if not encoding:
                    encoding = 'utf-8'
                
                try:
                    content = raw_content.decode(encoding)
                except UnicodeDecodeError:
                    try:
                        content = raw_content.decode('utf-8')
                    except UnicodeDecodeError:
                        content = raw_content.decode('utf-8-sig')
                
                logger.info(f"成功读取字幕内容，长度: {len(content)}")
                return content, video_info
                
            finally:
                # 清理临时目录
                try:
                    shutil.rmtree(temp_dir)
                    logger.debug(f"清理临时目录: {temp_dir}")
                except:
                    pass
                
        except Exception as e:
            logger.error(f"下载YouTube字幕时出错: {str(e)}")
            return None
    
    def download_bilibili_subtitles(self, url: str, video_info: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        Download Bilibili subtitles.
        
        Args:
            url: Bilibili URL
            video_info: Video information dictionary
            
        Returns:
            Tuple of (subtitle_content, video_info) or None
        """
        try:
            logger.info(f"开始下载Bilibili字幕: {url}")
            
            # 中文字幕优先级
            lang_priority = ['zh-CN', 'zh-Hans', 'zh']
            
            firefox_profile = self.get_firefox_profile_path()
            temp_dir = tempfile.mkdtemp()
            
            try:
                ydl_opts = {
                    'writesubtitles': True,
                    'subtitleslangs': lang_priority,
                    'subtitlesformat': 'srt/vtt/json3',
                    'skip_download': True,
                    'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
                    'quiet': True,
                    'no_warnings': True,
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'http_headers': {'Referer': 'https://www.bilibili.com/'}
                }
                
                if firefox_profile:
                    ydl_opts['cookiesfrombrowser'] = ('firefox', firefox_profile)
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(url, download=True)
                
                # 查找字幕文件
                subtitle_files = []
                for file in os.listdir(temp_dir):
                    if any(file.endswith(f'.{ext}') for ext in ['srt', 'vtt', 'json3']):
                        subtitle_files.append(os.path.join(temp_dir, file))
                
                if not subtitle_files:
                    logger.warning("Bilibili未找到字幕文件")
                    return None
                
                # 选择第一个可用的字幕文件
                selected_file = subtitle_files[0]
                logger.info(f"选择Bilibili字幕文件: {selected_file}")
                
                # 读取内容
                with open(selected_file, 'rb') as f:
                    raw_content = f.read()
                
                encoding = detect_file_encoding(raw_content)
                content = raw_content.decode(encoding or 'utf-8')
                
                logger.info(f"成功读取Bilibili字幕，长度: {len(content)}")
                return content, video_info
                
            finally:
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"下载Bilibili字幕时出错: {str(e)}")
            return None
    
    def download_acfun_subtitles(self, url: str, video_info: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        Download AcFun subtitles.
        
        Args:
            url: AcFun URL
            video_info: Video information dictionary
            
        Returns:
            Tuple of (subtitle_content, video_info) or None
        """
        try:
            logger.info(f"开始下载AcFun字幕: {url}")
            
            lang_priority = ['zh-CN', 'zh-Hans', 'zh']
            firefox_profile = self.get_firefox_profile_path()
            temp_dir = tempfile.mkdtemp()
            
            try:
                ydl_opts = {
                    'writesubtitles': True,
                    'subtitleslangs': lang_priority,
                    'subtitlesformat': 'srt/vtt/json3',
                    'skip_download': True,
                    'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
                    'quiet': True,
                    'no_warnings': True,
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'http_headers': {'Referer': 'https://www.acfun.cn/'}
                }
                
                if firefox_profile:
                    ydl_opts['cookiesfrombrowser'] = ('firefox', firefox_profile)
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(url, download=True)
                
                # 查找字幕文件
                subtitle_files = []
                for file in os.listdir(temp_dir):
                    if any(file.endswith(f'.{ext}') for ext in ['srt', 'vtt', 'json3']):
                        subtitle_files.append(os.path.join(temp_dir, file))
                
                if not subtitle_files:
                    logger.warning("AcFun未找到字幕文件")
                    return None
                
                selected_file = subtitle_files[0]
                logger.info(f"选择AcFun字幕文件: {selected_file}")
                
                with open(selected_file, 'rb') as f:
                    raw_content = f.read()
                
                encoding = detect_file_encoding(raw_content)
                content = raw_content.decode(encoding or 'utf-8')
                
                logger.info(f"成功读取AcFun字幕，长度: {len(content)}")
                return content, video_info
                
            finally:
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"下载AcFun字幕时出错: {str(e)}")
            return None
    
    def sanitize_filename(self, filename: str) -> str:
        """
        Sanitize filename for safe filesystem operations.
        
        Args:
            filename: Original filename
            
        Returns:
            Sanitized filename
        """
        try:
            return sanitize_filename(filename)
        except:
            # Fallback implementation
            import re
            # 移除不安全字符
            filename = re.sub(r'[<>:"/\\|?*]', '', filename)
            # 限制长度
            if len(filename) > 200:
                name, ext = os.path.splitext(filename)
                filename = name[:200-len(ext)] + ext
            return filename or 'subtitle'