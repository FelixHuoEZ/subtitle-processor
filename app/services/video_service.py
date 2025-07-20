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
    
    def get_subtitle_strategy(self, video_info: Dict[str, Any]) -> str:
        """
        Determine subtitle extraction strategy based on video info.
        
        Args:
            video_info: Video information dictionary
            
        Returns:
            Strategy string: 'direct' or 'transcribe'
        """
        try:
            # Check if subtitles are available
            subtitles = video_info.get('subtitles', [])
            auto_captions = video_info.get('automatic_captions', [])
            
            if subtitles or auto_captions:
                logger.info("Subtitles available - using direct extraction")
                return 'direct'
            else:
                logger.info("No subtitles available - will use transcription")
                return 'transcribe'
                
        except Exception as e:
            logger.error(f"Error determining subtitle strategy: {str(e)}")
            return 'transcribe'  # Default to transcription
    
    def get_video_language(self, video_info: Dict[str, Any]) -> str:
        """
        Determine video language from video info.
        
        Args:
            video_info: Video information dictionary
            
        Returns:
            Language code (e.g., 'en', 'zh')
        """
        try:
            # Try to get language from video info
            language = video_info.get('language', '')
            
            if language:
                logger.info(f"Detected video language: {language}")
                return language
            
            # Fallback: guess from title or description
            title = video_info.get('title', '').lower()
            description = video_info.get('description', '').lower()
            
            # Simple language detection based on content
            chinese_chars = re.findall(r'[\u4e00-\u9fff]', title + description)
            if len(chinese_chars) > 10:
                logger.info("Detected Chinese language from content")
                return 'zh'
            else:
                logger.info("Defaulting to English language")
                return 'en'
                
        except Exception as e:
            logger.error(f"Error detecting video language: {str(e)}")
            return 'en'  # Default to English