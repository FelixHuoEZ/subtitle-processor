"""Video processing service for handling multiple video platforms."""

import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import yt_dlp
from yt_dlp.utils import DownloadError

from ..config.config_manager import get_config_value
from ..utils.file_utils import sanitize_filename

logger = logging.getLogger(__name__)


class VideoService:
    """视频处理服务 - 支持YouTube、Bilibili、AcFun等平台"""

    def __init__(self):
        """初始化视频服务"""
        self.supported_platforms = ["youtube", "bilibili", "acfun"]
        self.bgutil_provider_url = self._normalize_bgutil_url(
            os.getenv("BGUTIL_PROVIDER_URL", "http://bgutil-provider:4416")
        )
        self._setup_yt_dlp_options()

    @staticmethod
    def _normalize_bgutil_url(url: Optional[str]) -> str:
        """确保bgutil provider的URL合法并带有协议"""
        default_url = "http://bgutil-provider:4416"
        if not url:
            return default_url
        parsed = urlparse(url if "://" in url else f"http://{url.strip()}")
        if not parsed.scheme or not parsed.netloc:
            return default_url
        return parsed.geturl().rstrip("/") or default_url

    def _setup_yt_dlp_options(self):
        """设置yt-dlp默认选项"""

        # 自定义日志处理器
        class QuietLogger:
            def debug(self, msg):
                # 忽略调试信息
                pass

            def warning(self, msg):
                logger.warning(msg)

            def error(self, msg):
                logger.error(msg)

        base_opts = {
            "logger": QuietLogger(),
            "quiet": True,
            "no_warnings": True,
            "cachedir": False,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
            "http_headers": {"Referer": "https://www.youtube.com/"},
            "noplaylist": True,
            "skip_unavailable_fragments": True,
            "format": "bestaudio/best",
            "extractor_args": {
                "youtube": {
                    "player_client": ["default", "mweb"],
                    "fetch_pot": ["always"],
                },
                "youtubepot-bgutilhttp": {
                    "base_url": [self.bgutil_provider_url],
                },
            },
        }
        logger.info("yt-dlp 将使用 bgutil provider: %s", self.bgutil_provider_url)
        self._configure_cookie_support(base_opts)
        self.yt_dlp_opts = base_opts

    def _get_platform_headers(
        self, platform: Optional[str], url: Optional[str] = None
    ) -> Dict[str, str]:
        """构建平台所需的请求头，避免跨站Referer触发拦截"""
        origin = None
        if platform == "youtube":
            origin = "https://www.youtube.com"
        elif platform == "bilibili":
            origin = "https://www.bilibili.com"
        elif platform == "acfun":
            origin = "https://www.acfun.cn"
        elif url:
            parsed = urlparse(url)
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"

        if not origin:
            return {}

        return {"Origin": origin, "Referer": f"{origin}/"}

    def _get_yt_dlp_opts_for_platform(
        self, platform: str, url: Optional[str] = None
    ) -> Dict[str, Any]:
        """为指定平台生成yt-dlp选项，覆盖可能导致403的请求头"""
        opts = dict(self.yt_dlp_opts)
        headers = dict(opts.get("http_headers", {}))
        platform_headers = self._get_platform_headers(platform, url)
        if platform_headers:
            headers.update(platform_headers)
            opts["http_headers"] = headers
        return opts

    def _configure_cookie_support(self, base_opts: Dict[str, Any]) -> None:
        """为yt-dlp配置cookie，优先使用显式配置"""
        cookie_file_env = os.getenv("YTDLP_COOKIE_FILE")
        if cookie_file_env:
            if os.path.isfile(cookie_file_env):
                base_opts["cookiefile"] = cookie_file_env
                logger.info("使用环境变量指定的cookie文件: %s", cookie_file_env)
                return
            logger.warning(
                "环境变量 YTDLP_COOKIE_FILE 指定的路径 %s 不存在或不可读，请确认容器内已挂载正确的 cookie 文件。",
                cookie_file_env,
            )

        config_cookie_path = get_config_value("cookies")
        if config_cookie_path:
            if os.path.isfile(config_cookie_path):
                base_opts["cookiefile"] = config_cookie_path
                logger.info("使用配置文件指定的cookie文件: %s", config_cookie_path)
                return
            if os.path.isdir(config_cookie_path):
                cookie_db = os.path.join(config_cookie_path, "cookies.sqlite")
                if os.path.exists(cookie_db):
                    base_opts["cookiesfrombrowser"] = ("firefox", config_cookie_path)
                    logger.info(
                        "使用配置文件指定的Firefox cookie目录: %s", config_cookie_path
                    )
                    return
                logger.warning(
                    "配置文件中的 cookies 目录 %s 缺少 cookies.sqlite，"
                    "请运行 scripts/update_firefox_cookies.sh 同步或更新 config.yml。",
                    cookie_db,
                )
            else:
                logger.warning(
                    "配置文件中的 cookies 路径 %s 不存在，请检查 config/config.yml 并确保该路径已挂载到容器。",
                    config_cookie_path,
                )

        firefox_profile = self._get_firefox_profile_path()
        if firefox_profile:
            base_opts["cookiesfrombrowser"] = ("firefox", firefox_profile)
            logger.info("使用自动发现的Firefox cookie目录: %s", firefox_profile)
        else:
            logger.warning(
                "未找到可用的 YouTube cookie，后续请求可能触发验证。"
                "请同步 firefox_profile 目录或设置 YTDLP_COOKIE_FILE。",
            )

    def _extract_youtube_info(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            with yt_dlp.YoutubeDL(self.yt_dlp_opts) as ydl:
                return ydl.extract_info(url, download=False)
        except DownloadError as err:
            logger.warning(f"初次获取YouTube信息失败，尝试元数据回退: {err}")
            fallback_opts = dict(self.yt_dlp_opts)
            fallback_opts.pop("format", None)
            fallback_opts.pop("skip_unavailable_fragments", None)
            fallback_opts["quiet"] = True
            try:
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    return ydl.extract_info(url, download=False, process=False)
            except DownloadError as fallback_err:
                logger.error(f"YouTube元数据回退失败: {fallback_err}")
                raise

    def get_video_info(self, url: str, platform: str) -> Optional[Dict[str, Any]]:
        """获取视频信息

        Args:
            url: 视频URL
            platform: 平台名称 ('youtube', 'bilibili', 'acfun')

        Returns:
            dict: 视频信息，失败返回None
        """
        try:
            logger.info(f"获取{platform}视频信息: {url}")

            if platform == "youtube":
                return self.get_youtube_info(url)
            elif platform == "bilibili":
                return self.get_bilibili_info(url)
            elif platform == "acfun":
                return self.get_acfun_info(url)
            else:
                logger.error(f"不支持的平台: {platform}")
                return None

        except Exception as e:
            logger.error(f"获取{platform}视频信息失败: {str(e)}")
            return None

    def get_youtube_info(self, url: str) -> Optional[Dict[str, Any]]:
        """获取YouTube视频信息"""
        try:
            # 添加率限制防止IP被封
            time.sleep(2)
            info = self._extract_youtube_info(url)
            if not info:
                logger.error("未能获取YouTube元数据")
                return None

            # ��ϸ��¼���п��ܰ������ڵ��ֶ�
            date_fields = {
                "upload_date": info.get("upload_date"),
                "release_date": info.get("release_date"),
                "modified_date": info.get("modified_date"),
                "timestamp": info.get("timestamp"),
            }
            logger.info(
                f"YouTube��Ƶ��������ֶ�: {json.dumps(date_fields, indent=2, ensure_ascii=False)}"
            )

            # ���Զ�������ֶ�
            published_date = None
            if info.get("upload_date"):
                published_date = f"{info['upload_date'][:4]}-{info['upload_date'][4:6]}-{info['upload_date'][6:]}T00:00:00Z"
            elif info.get("release_date"):
                published_date = info["release_date"]
            elif info.get("modified_date"):
                published_date = info["modified_date"]
            elif info.get("timestamp"):
                published_date = datetime.fromtimestamp(info["timestamp"]).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )

            logger.info(f"����ȷ���ķ�������: {published_date}")

            video_info = {
                "id": info.get("id"),
                "title": info.get("title"),
                "description": info.get("description"),
                "uploader": info.get("uploader") or info.get("channel"),
                "duration": info.get("duration"),
                "view_count": info.get("view_count"),
                "like_count": info.get("like_count"),
                "upload_date": info.get("upload_date"),
                "published_date": published_date,
                "webpage_url": info.get("webpage_url", url),
                "thumbnail": info.get("thumbnail"),
                "language": info.get("language", "en"),
                "subtitles": list(info.get("subtitles", {}).keys())
                if info.get("subtitles")
                else [],
                "automatic_captions": list(info.get("automatic_captions", {}).keys())
                if info.get("automatic_captions")
                else [],
            }

            logger.info(f"��ȡYouTube��Ƶ��Ϣ�ɹ�: {video_info['title']}")
            return video_info

        except Exception as e:
            logger.error(f"获取YouTube视频信息失败: {str(e)}")
            return None

    def get_bilibili_info(self, url: str) -> Optional[Dict[str, Any]]:
        """获取Bilibili视频信息"""
        try:
            opts = self._get_yt_dlp_opts_for_platform("bilibili", url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

                video_info = {
                    "id": info.get("id"),
                    "title": info.get("title"),
                    "description": info.get("description"),
                    "uploader": info.get("uploader"),
                    "duration": info.get("duration"),
                    "view_count": info.get("view_count"),
                    "upload_date": info.get("upload_date"),
                    "published_date": info.get("upload_date"),
                    "webpage_url": info.get("webpage_url", url),
                    "thumbnail": info.get("thumbnail"),
                    "language": "zh-CN",
                    "subtitles": list(info.get("subtitles", {}).keys())
                    if info.get("subtitles")
                    else [],
                    "automatic_captions": [],
                }

                logger.info(f"获取Bilibili视频信息成功: {video_info['title']}")
                return video_info

        except Exception as e:
            logger.error(f"获取Bilibili视频信息失败: {str(e)}")
            return None

    def get_acfun_info(self, url: str) -> Optional[Dict[str, Any]]:
        """获取AcFun视频信息"""
        try:
            opts = self._get_yt_dlp_opts_for_platform("acfun", url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

                video_info = {
                    "id": info.get("id"),
                    "title": info.get("title"),
                    "description": info.get("description"),
                    "uploader": info.get("uploader"),
                    "duration": info.get("duration"),
                    "view_count": info.get("view_count"),
                    "upload_date": info.get("upload_date"),
                    "published_date": info.get("upload_date"),
                    "webpage_url": info.get("webpage_url", url),
                    "thumbnail": info.get("thumbnail"),
                    "language": "zh-CN",
                    "subtitles": list(info.get("subtitles", {}).keys())
                    if info.get("subtitles")
                    else [],
                    "automatic_captions": [],
                }

                logger.info(f"获取AcFun视频信息成功: {video_info['title']}")
                return video_info

        except Exception as e:
            logger.error(f"获取AcFun视频信息失败: {str(e)}")
            return None

    def get_video_language(self, info: Dict[str, Any]) -> Optional[str]:
        """检测视频语言

        Args:
            info: 视频信息字典

        Returns:
            str: 语言代码 ('zh', 'en', etc.) 或 None
        """
        try:
            if not info:
                return None

            # 1. 优先使用视频信息中的语言字段
            if "language" in info and info["language"]:
                lang = info["language"].lower()
                if lang.startswith("zh"):
                    return "zh"
                elif lang.startswith("en"):
                    return "en"
                else:
                    return lang[:2]

            # 2. 根据标题和描述中的字符特征判断
            title = info.get("title", "")
            description = info.get("description", "")
            text_sample = (title + " " + description)[:500]  # 取前500字符作为样本

            if not text_sample:
                return None

            # 统计中文字符数量
            chinese_chars = len(re.findall(r"[\\u4e00-\\u9fff]", text_sample))
            total_chars = len([c for c in text_sample if c.isalnum()])

            if total_chars == 0:
                return None

            chinese_ratio = chinese_chars / total_chars

            # 如果中文字符占比超过30%，认为是中文视频
            if chinese_ratio > 0.3:
                return "zh"
            elif chinese_ratio < 0.1:
                return "en"
            else:
                return "mixed"  # 混合语言

        except Exception as e:
            logger.error(f"检测视频语言时出错: {str(e)}")
            return None

    def get_subtitle_strategy(
        self, language: Optional[str], info: Dict[str, Any]
    ) -> Tuple[bool, List[str]]:
        """确定字幕获取策略

        Args:
            language: 检测到的视频语言
            info: 视频信息

        Returns:
            tuple: (是否应该下载字幕, 语言优先级列表)
        """
        try:
            # 获取可用字幕语言，确保是字典类型
            available_subtitles = info.get("subtitles", {})
            available_auto = info.get("automatic_captions", {})

            # 如果不是字典类型，设为空字典
            if not isinstance(available_subtitles, dict):
                available_subtitles = {}
            if not isinstance(available_auto, dict):
                available_auto = {}

            logger.info(f"可用字幕: {list(available_subtitles.keys())}")
            logger.info(f"可用自动字幕: {list(available_auto.keys())}")

            if language == "zh":
                # 中文视频：优先中文字幕
                lang_priority = ["zh-CN", "zh", "zh-TW", "zh-Hans", "zh-Hant"]
            elif language == "en":
                # 英文视频：优先英文字幕
                lang_priority = ["en", "en-US", "en-GB"]
            else:
                # 其他语言：暂不处理
                return False, []

            # 检查是否有对应语言的字幕
            for lang in lang_priority:
                if lang in available_subtitles or lang in available_auto:
                    logger.info(f"找到{lang}字幕，将尝试下载")
                    return True, lang_priority

            logger.info("未找到匹配的字幕语言")
            return False, lang_priority

        except Exception as e:
            logger.error(f"确定字幕策略时出错: {str(e)}")
            return False, []

    def convert_youtube_url(self, url: str) -> str:
        """将YouTube URL转换为自定义domain"""
        try:
            # 处理不同格式的YouTube URL
            if "youtu.be/" in url:
                # 短链接格式
                video_id = url.split("youtu.be/")[-1].split("?")[0]
            elif "youtube.com/watch?v=" in url:
                # 标准格式
                video_id = url.split("v=")[1].split("&")[0]
            else:
                return url  # 如果不是YouTube URL，直接返回

            # 获取自定义域名配置
            custom_domain = get_config_value(
                "servers.video_domain", "http://localhost:5000"
            )
            return f"{custom_domain}/view/{video_id}"

        except Exception as e:
            logger.error(f"转换YouTube URL时出错: {str(e)}")
            return url

    def download_video(
        self,
        url: str,
        output_folder: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> Optional[str]:
        """下载视频并提取音频

        Args:
            url: 视频URL
            output_folder: 输出目录，默认使用配置的上传目录
            platform: 平台名称，用于设置正确的请求头

        Returns:
            str: 下载的音频文件路径，失败返回None
        """
        try:
            # 创建临时目录
            temp_dir = output_folder or os.path.join(
                get_config_value("app.upload_folder", "/app/uploads"), "temp"
            )
            os.makedirs(temp_dir, exist_ok=True)
            logger.info(f"开始下载视频: {url}")

            # 先尝试检查视频信息
            info = None
            try:
                # 添加率限制防止IP被封
                time.sleep(2)
                temp_opts = {
                    "quiet": True,
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["default", "mweb"],
                            "fetch_pot": ["always"],
                        },
                        "youtubepot-bgutilhttp": {
                            "base_url": [self.bgutil_provider_url],
                        },
                    },
                }
                with yt_dlp.YoutubeDL(temp_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    logger.info(f"视频标题: {info.get('title')}")
                    if info.get("age_limit", 0) > 0:
                        logger.info(f"视频有年龄限制: {info.get('age_limit')}+")
                    if info.get("is_live", False):
                        logger.info("这是一个直播视频")
                    if info.get("availability", "") != "public":
                        logger.info(
                            f"视频可用性: {info.get('availability', 'unknown')}"
                        )
            except Exception as e:
                logger.info(f"无法获取视频信息，可能需要登录: {str(e)}")
                info = None

            # 记录预期的视频ID（用于后续文件查找）
            expected_video_id = None
            if info:
                expected_video_id = info.get("id")
            else:
                # 尝试从URL中提取视频ID
                try:
                    if "youtu.be/" in url:
                        expected_video_id = url.split("youtu.be/")[-1].split("?")[0]
                    elif "youtube.com/watch?v=" in url:
                        expected_video_id = url.split("v=")[1].split("&")[0]
                except:
                    pass

            logger.info(f"预期视频ID: {expected_video_id}")

            # 基础下载选项
            base_opts = {
                "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "quiet": True,
                "no_warnings": True,
                "geo_bypass": True,
                "no_check_certificate": True,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["default", "mweb"],
                        "fetch_pot": ["always"],
                    },
                    "youtubepot-bgutilhttp": {
                        "base_url": [self.bgutil_provider_url],
                    },
                },
            }
            base_headers = {
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.5",
            }
            platform_headers = self._get_platform_headers(platform, url)
            if not platform_headers:
                platform_headers = self._get_platform_headers("youtube", url)
            base_opts["http_headers"] = {**base_headers, **platform_headers}

            # 尝试获取Firefox配置文件路径
            firefox_profile = self._get_firefox_profile_path()
            if firefox_profile:
                logger.info(f"使用Firefox配置文件: {firefox_profile}")
                base_opts["cookiesfrombrowser"] = ("firefox", firefox_profile)
            else:
                logger.warning(
                    "未在 /root/.mozilla/firefox 中找到可用 profile，将临时跳过 cookie 下载。"
                    "请确认已挂载 firefox_profile 或在 config.yml 中配置 cookies 字段。",
                )

            # 按优先级尝试不同的格式
            format_attempts = [
                {
                    "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio",
                    "desc": "最佳音频格式",
                },
                {
                    "format": "worst[height<=480]/worst",
                    "desc": "低质量视频（提取音频）",
                },
                {
                    "format": "best[height<=720]/best",
                    "desc": "中等质量视频（提取音频）",
                },
            ]

            downloaded_file = None
            for attempt in format_attempts:
                try:
                    logger.info(f"尝试下载: {attempt['desc']}")
                    # 添加率限制防止IP被封
                    time.sleep(3)
                    opts = base_opts.copy()
                    opts["format"] = attempt["format"]

                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([url])

                    # 改进的文件查找逻辑
                    downloaded_file = self._find_downloaded_file(
                        temp_dir, expected_video_id
                    )

                    if downloaded_file and os.path.exists(downloaded_file):
                        logger.info(f"下载成功: {downloaded_file}")
                        break

                except Exception as e:
                    logger.warning(f"下载失败 ({attempt['desc']}): {str(e)}")
                    continue

            if not downloaded_file:
                logger.error("所有下载尝试都失败了")
                # 列出临时目录中的文件用于调试
                try:
                    files = os.listdir(temp_dir)
                    logger.error(f"临时目录中的文件: {files}")
                    if files:
                        logger.error(
                            "文件存在但未被正确识别，这可能是文件查找逻辑的问题"
                        )
                except Exception as e:
                    logger.error(f"无法列出临时目录文件: {str(e)}")
                return None

            # 转换为音频格式
            return self._convert_to_audio(downloaded_file, temp_dir)

        except Exception as e:
            logger.error(f"下载视频时出错: {str(e)}")
            return None

    def _find_downloaded_file(
        self, temp_dir: str, expected_video_id: Optional[str]
    ) -> Optional[str]:
        """改进的下载文件查找逻辑"""
        try:
            if not os.path.exists(temp_dir):
                logger.error(f"临时目录不存在: {temp_dir}")
                return None

            files = os.listdir(temp_dir)
            logger.info(f"临时目录中的文件: {files}")

            if not files:
                logger.warning("临时目录中没有文件")
                return None

            # 策略1: 如果有预期的视频ID，优先匹配
            if expected_video_id:
                # 先查找精确匹配（不带_part_的文件）
                for file in files:
                    base_name, _ = os.path.splitext(file)
                    if base_name == expected_video_id:
                        file_path = os.path.join(temp_dir, file)
                        logger.info(f"通过精确匹配到文件: {file_path}")
                        return file_path

                for file in files:
                    if file.startswith(expected_video_id):
                        file_path = os.path.join(temp_dir, file)
                        logger.info(f"通过视频ID匹配到文件: {file_path}")
                        return file_path

            # 策略2: 查找最新创建的文件
            files_with_time = []
            for file in files:
                file_path = os.path.join(temp_dir, file)
                try:
                    mtime = os.path.getmtime(file_path)
                    files_with_time.append((file_path, mtime))
                except OSError:
                    continue

            if files_with_time:
                # 按修改时间排序，选择最新的文件
                files_with_time.sort(key=lambda x: x[1], reverse=True)
                newest_file = files_with_time[0][0]
                logger.info(f"选择最新的文件: {newest_file}")
                return newest_file

            # 策略3: 如果都失败了，返回第一个文件
            first_file = os.path.join(temp_dir, files[0])
            logger.info(f"回退到第一个文件: {first_file}")
            return first_file

        except Exception as e:
            logger.error(f"查找下载文件时发生错误: {str(e)}")
            return None

    def _convert_to_audio(self, video_file: str, output_dir: str) -> Optional[str]:
        """将视频转换为音频格式"""
        try:
            import subprocess

            from pydub import AudioSegment

            # 生成音频文件路径
            base_name = os.path.splitext(os.path.basename(video_file))[0]
            audio_file = os.path.join(output_dir, f"{base_name}.wav")

            # 检查输入文件是否已经是正确格式的wav文件
            if video_file == audio_file:
                logger.info(f"输入文件已经是目标格式: {audio_file}")
                # 验证音频格式是否符合要求
                try:
                    audio = AudioSegment.from_file(video_file)
                    current_rate = audio.frame_rate
                    current_channels = audio.channels
                    logger.info(
                        f"当前音频格式: {current_rate}Hz, {current_channels}声道"
                    )

                    if current_rate == 16000 and current_channels == 1:
                        logger.info(f"音频格式已符合要求，无需转换: {audio_file}")
                        return audio_file
                    else:
                        logger.info(
                            f"需要调整音频格式: {current_rate}Hz -> 16000Hz, {current_channels}声道 -> 1声道"
                        )

                        # 使用安全的临时文件转换方案
                        import shutil
                        import uuid

                        temp_file = os.path.join(
                            output_dir,
                            f"{base_name}_format_temp_{uuid.uuid4().hex[:8]}.wav",
                        )
                        backup_file = audio_file + f"_backup_{uuid.uuid4().hex[:8]}"

                        try:
                            # 备份原文件
                            shutil.copy2(audio_file, backup_file)
                            logger.info(f"原文件已备份: {backup_file}")

                            # 格式转换
                            converted_audio = audio.set_frame_rate(16000).set_channels(
                                1
                            )
                            converted_audio.export(temp_file, format="wav")

                            # 验证转换结果
                            if (
                                not os.path.exists(temp_file)
                                or os.path.getsize(temp_file) == 0
                            ):
                                raise Exception("格式转换失败，临时文件无效")

                            # 替换原文件
                            os.remove(audio_file)
                            shutil.move(temp_file, audio_file)

                            # 清理备份
                            if os.path.exists(backup_file):
                                os.remove(backup_file)

                            logger.info(f"音频格式调整完成: {audio_file}")
                            return audio_file

                        except Exception as conversion_error:
                            logger.error(f"格式调整失败: {str(conversion_error)}")

                            # 恢复备份
                            if os.path.exists(backup_file):
                                try:
                                    if os.path.exists(audio_file):
                                        os.remove(audio_file)
                                    shutil.move(backup_file, audio_file)
                                    logger.info("已恢复原文件")
                                except Exception as restore_error:
                                    logger.error(
                                        f"恢复原文件失败: {str(restore_error)}"
                                    )

                            # 清理临时文件
                            for cleanup_file in [temp_file, backup_file]:
                                if os.path.exists(cleanup_file):
                                    try:
                                        os.remove(cleanup_file)
                                    except:
                                        pass

                            raise conversion_error

                except Exception as check_error:
                    logger.warning(f"检查音频格式时出错: {str(check_error)}")
                    # 格式检查失败，继续正常的转换流程

            # 对于同名文件，跳过FFmpeg直接使用pydub（避免FFmpeg的同名文件问题）
            if video_file == audio_file:
                logger.info(
                    f"同名文件检测到，跳过FFmpeg直接使用pydub处理: {audio_file}"
                )
            else:
                # 使用ffmpeg转换（仅对不同名文件）
                try:
                    cmd = [
                        "ffmpeg",
                        "-i",
                        video_file,
                        "-vn",
                        "-acodec",
                        "pcm_s16le",
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        audio_file,
                        "-y",
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    if result.returncode == 0:
                        logger.info(f"ffmpeg音频转换成功: {audio_file}")
                        # 只在文件不同时删除原文件
                        if video_file != audio_file and os.path.exists(video_file):
                            os.remove(video_file)
                        return audio_file
                    else:
                        logger.error(f"ffmpeg转换失败: {result.stderr}")
                except Exception as ffmpeg_error:
                    logger.warning(f"ffmpeg转换失败: {str(ffmpeg_error)}")

            # 尝试使用pydub
            try:
                audio = AudioSegment.from_file(video_file)

                # 检查是否需要转换格式
                needs_conversion = audio.frame_rate != 16000 or audio.channels != 1
                logger.info(f"当前音频格式: {audio.frame_rate}Hz, {audio.channels}声道")
                logger.info(f"需要格式转换: {needs_conversion}")

                if needs_conversion:
                    audio = audio.set_frame_rate(16000).set_channels(1)

                    if video_file == audio_file:
                        # 如果输入输出文件相同，使用更安全的临时文件处理方案
                        import shutil
                        import tempfile
                        import uuid

                        # 生成唯一的临时文件名，避免冲突
                        temp_suffix = f"_temp_{uuid.uuid4().hex[:8]}"
                        temp_audio_file = audio_file + temp_suffix
                        backup_file = audio_file + "_backup_" + uuid.uuid4().hex[:8]

                        logger.info(f"同名文件转换: {audio_file}")
                        logger.info(f"临时文件: {temp_audio_file}")
                        logger.info(f"备份文件: {backup_file}")

                        success = False
                        try:
                            # 步骤1: 先备份原文件
                            if os.path.exists(audio_file):
                                shutil.copy2(audio_file, backup_file)
                                logger.info(f"原文件已备份: {backup_file}")

                            # 步骤2: 导出到临时文件
                            logger.info("开始导出到临时文件...")
                            audio.export(temp_audio_file, format="wav")

                            # 步骤3: 验证临时文件
                            if not os.path.exists(temp_audio_file):
                                raise Exception(f"临时文件创建失败: {temp_audio_file}")

                            temp_size = os.path.getsize(temp_audio_file)
                            if temp_size == 0:
                                raise Exception(f"临时文件为空: {temp_audio_file}")

                            logger.info(
                                f"临时文件创建成功: {temp_audio_file} ({temp_size} bytes)"
                            )

                            # 步骤4: 多重替换策略
                            replacement_success = False

                            # 策略1: 直接os.replace
                            try:
                                if os.path.exists(audio_file):
                                    os.remove(audio_file)
                                os.rename(temp_audio_file, audio_file)
                                replacement_success = True
                                logger.info(
                                    f"pydub转换成功(同名文件,os.rename): {audio_file}"
                                )
                            except Exception as rename_error:
                                logger.warning(f"os.rename失败: {str(rename_error)}")

                                # 策略2: shutil.move
                                try:
                                    if os.path.exists(audio_file):
                                        os.remove(audio_file)
                                    shutil.move(temp_audio_file, audio_file)
                                    replacement_success = True
                                    logger.info(
                                        f"pydub转换成功(同名文件,shutil.move): {audio_file}"
                                    )
                                except Exception as move_error:
                                    logger.warning(
                                        f"shutil.move失败: {str(move_error)}"
                                    )

                                    # 策略3: 复制+删除
                                    try:
                                        if os.path.exists(audio_file):
                                            os.remove(audio_file)
                                        shutil.copy2(temp_audio_file, audio_file)
                                        os.remove(temp_audio_file)
                                        replacement_success = True
                                        logger.info(
                                            f"pydub转换成功(同名文件,copy+delete): {audio_file}"
                                        )
                                    except Exception as copy_error:
                                        logger.error(
                                            f"所有替换策略均失败: {str(copy_error)}"
                                        )

                            if not replacement_success:
                                raise Exception("所有文件替换策略均失败")

                            # 步骤5: 验证最终文件
                            if not os.path.exists(audio_file):
                                raise Exception(f"最终音频文件不存在: {audio_file}")

                            final_size = os.path.getsize(audio_file)
                            if final_size == 0:
                                raise Exception(f"最终音频文件为空: {audio_file}")

                            logger.info(
                                f"最终文件验证成功: {audio_file} ({final_size} bytes)"
                            )
                            success = True

                        except Exception as temp_error:
                            logger.error(f"同名文件处理失败: {str(temp_error)}")

                            # 恢复备份文件
                            if os.path.exists(backup_file):
                                try:
                                    if os.path.exists(audio_file):
                                        os.remove(audio_file)
                                    shutil.move(backup_file, audio_file)
                                    logger.info(f"已恢复备份文件: {audio_file}")
                                except Exception as restore_error:
                                    logger.error(
                                        f"恢复备份文件失败: {str(restore_error)}"
                                    )

                            raise temp_error

                        finally:
                            # 清理临时文件和备份文件
                            for cleanup_file in [temp_audio_file, backup_file]:
                                if os.path.exists(cleanup_file):
                                    try:
                                        os.remove(cleanup_file)
                                        logger.debug(f"清理临时文件: {cleanup_file}")
                                    except Exception as cleanup_error:
                                        logger.warning(
                                            f"清理文件失败 {cleanup_file}: {str(cleanup_error)}"
                                        )

                            if success:
                                logger.info(f"同名文件转换完成: {audio_file}")
                    else:
                        # 正常导出到不同文件
                        audio.export(audio_file, format="wav")

                        # 验证音频文件是否创建成功
                        if not os.path.exists(audio_file):
                            raise Exception(f"音频文件创建失败: {audio_file}")

                        logger.info(f"pydub转换成功: {audio_file}")
                        if os.path.exists(video_file):
                            os.remove(video_file)
                else:
                    # 格式已经正确，如果是不同文件则复制
                    if video_file != audio_file:
                        import shutil

                        shutil.copy2(video_file, audio_file)
                        os.remove(video_file)
                        logger.info(f"音频格式正确，文件复制完成: {audio_file}")
                    else:
                        logger.info(f"音频格式正确，无需处理: {audio_file}")

                return audio_file
            except Exception as pydub_error:
                logger.error(f"pydub转换失败: {str(pydub_error)}")

            return None

        except Exception as e:
            logger.error(f"音频转换时出错: {str(e)}")
            return None

    def _get_firefox_profile_path(self) -> Optional[str]:
        """获取Firefox配置文件路径"""
        try:
            import configparser

            # 检查配置文件中是否有指定的cookie路径
            cookie_path = get_config_value("cookies")
            if cookie_path:
                if os.path.isdir(cookie_path):
                    cookie_db = os.path.join(cookie_path, "cookies.sqlite")
                    if os.path.exists(cookie_db):
                        logger.info(f"使用配置的cookie目录: {cookie_path}")
                        return cookie_path
                    logger.warning(
                        "配置的 cookies 目录 %s 缺少 cookies.sqlite，请确认同步的 Firefox profile 完整或重新导出。",
                        cookie_db,
                    )
                else:
                    logger.warning(
                        "配置的 cookies 路径 %s 不存在，请检查 config.yml 中的 cookies 字段。",
                        cookie_path,
                    )

            # 在Docker容器中，Firefox配置文件路径
            firefox_config = "/root/.mozilla/firefox/profiles.ini"
            if os.path.exists(firefox_config):
                config = configparser.ConfigParser()
                config.read(firefox_config)

                # 优先查找default-release配置文件
                for section in config.sections():
                    if section.startswith("Profile"):
                        if config.has_option(section, "Path"):
                            profile_path = config.get(section, "Path")
                            # 检查是否为相对路径
                            if (
                                config.has_option(section, "IsRelative")
                                and config.getint(section, "IsRelative", fallback=1)
                                == 1
                            ):
                                profile_path = os.path.join(
                                    "/root/.mozilla/firefox", profile_path
                                )

                            # 检查是否为默认配置文件
                            if (
                                config.has_option(section, "Name")
                                and config.get(section, "Name") == "default-release"
                            ):
                                if os.path.isdir(profile_path) and os.path.exists(
                                    os.path.join(profile_path, "cookies.sqlite")
                                ):
                                    logger.info(
                                        f"使用default-release配置文件: {profile_path}"
                                    )
                                    return profile_path
                                logger.warning(
                                    "default-release 配置目录 %s 缺少 cookies.sqlite，请重新同步 Firefox profile。",
                                    profile_path,
                                )

                            # 如果标记为默认配置文件
                            if (
                                config.has_option(section, "Default")
                                and config.getint(section, "Default", fallback=0) == 1
                            ):
                                if os.path.isdir(profile_path) and os.path.exists(
                                    os.path.join(profile_path, "cookies.sqlite")
                                ):
                                    logger.info(f"使用默认配置文件: {profile_path}")
                                    return profile_path
                                logger.warning(
                                    "默认 Firefox 配置目录 %s 缺少 cookies.sqlite，请确认 profile 是否完整。",
                                    profile_path,
                                )

            logger.warning(
                "未在 /root/.mozilla/firefox 下找到可用的 Firefox 配置，请挂载 firefox_profile 目录或执行 scripts/update_firefox_cookies.sh 同步。"
            )
            return None
        except Exception as e:
            logger.error(f"获取Firefox配置文件路径时出错: {str(e)}")
            return None

    def download_subtitles(
        self, url: str, platform: str, lang_priority: List[str]
    ) -> Optional[str]:
        """下载字幕文件

        Args:
            url: 视频URL
            platform: 平台名称
            lang_priority: 语言优先级列表

        Returns:
            str: 字幕内容，失败返回None
        """
        try:
            logger.info(f"开始下载{platform}字幕: {url}")

            if platform == "youtube":
                return self.download_youtube_subtitles(url, lang_priority)
            elif platform == "bilibili":
                return self.download_bilibili_subtitles(url, lang_priority)
            elif platform == "acfun":
                return self.download_acfun_subtitles(url, lang_priority)
            else:
                logger.error(f"不支持的平台字幕下载: {platform}")
                return None

        except Exception as e:
            logger.error(f"下载{platform}字幕失败: {str(e)}")
            return None

    def download_youtube_subtitles(
        self, url: str, lang_priority: List[str]
    ) -> Optional[str]:
        """下载YouTube字幕"""
        try:
            # 添加率限制防止IP被封
            time.sleep(2)
            with yt_dlp.YoutubeDL(self.yt_dlp_opts) as ydl:
                info = ydl.extract_info(url, download=False)

                available_subtitles = info.get("subtitles", {})
                available_auto = info.get("automatic_captions", {})

                # 按优先级查找字幕
                for lang in lang_priority:
                    # 优先使用人工字幕
                    if lang in available_subtitles:
                        logger.info(f"找到{lang}人工字幕")
                        return self._extract_subtitle_content(available_subtitles[lang])

                    # 如果没有人工字幕，使用自动字幕
                    if lang in available_auto:
                        logger.info(f"找到{lang}自动字幕")
                        return self._extract_subtitle_content(available_auto[lang])

                logger.warning("未找到匹配语言的字幕")
                return None

        except Exception as e:
            logger.error(f"下载YouTube字幕失败: {str(e)}")
            return None

    def download_bilibili_subtitles(
        self, url: str, lang_priority: List[str]
    ) -> Optional[str]:
        """下载Bilibili字幕"""
        try:
            opts = self._get_yt_dlp_opts_for_platform("bilibili", url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

                available_subtitles = info.get("subtitles", {})

                # Bilibili通常只有中文字幕
                for lang in lang_priority:
                    if lang in available_subtitles:
                        logger.info(f"找到{lang}字幕")
                        return self._extract_subtitle_content(available_subtitles[lang])

                # 如果没有指定语言，尝试任何可用的字幕
                if available_subtitles:
                    first_lang = list(available_subtitles.keys())[0]
                    logger.info(f"使用第一个可用字幕: {first_lang}")
                    return self._extract_subtitle_content(
                        available_subtitles[first_lang]
                    )

                logger.warning("未找到Bilibili字幕")
                return None

        except Exception as e:
            logger.error(f"下载Bilibili字幕失败: {str(e)}")
            return None

    def download_acfun_subtitles(
        self, url: str, lang_priority: List[str]
    ) -> Optional[str]:
        """下载AcFun字幕"""
        try:
            opts = self._get_yt_dlp_opts_for_platform("acfun", url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

                available_subtitles = info.get("subtitles", {})

                # AcFun通常只有中文字幕
                for lang in lang_priority:
                    if lang in available_subtitles:
                        logger.info(f"找到{lang}字幕")
                        return self._extract_subtitle_content(available_subtitles[lang])

                # 如果没有指定语言，尝试任何可用的字幕
                if available_subtitles:
                    first_lang = list(available_subtitles.keys())[0]
                    logger.info(f"使用第一个可用字幕: {first_lang}")
                    return self._extract_subtitle_content(
                        available_subtitles[first_lang]
                    )

                logger.warning("未找到AcFun字幕")
                return None

        except Exception as e:
            logger.error(f"下载AcFun字幕失败: {str(e)}")
            return None

    def _extract_subtitle_content(
        self, subtitle_formats: List[Dict[str, Any]]
    ) -> Optional[str]:
        """从字幕格式列表中提取内容"""
        try:
            # 按优先级尝试不同格式
            format_priority = ["json3", "srv3", "srv2", "srv1", "ttml", "vtt", "srt"]

            for format_name in format_priority:
                for subtitle_format in subtitle_formats:
                    if subtitle_format.get("ext") == format_name:
                        subtitle_url = subtitle_format.get("url")
                        if subtitle_url:
                            logger.info(f"下载{format_name}格式字幕: {subtitle_url}")
                            response = requests.get(subtitle_url, timeout=30)
                            if response.status_code == 200:
                                return response.text

            # 如果没有找到优先格式，使用第一个可用的
            if subtitle_formats:
                first_format = subtitle_formats[0]
                subtitle_url = first_format.get("url")
                if subtitle_url:
                    logger.info(f"使用第一个可用格式: {first_format.get('ext')}")
                    response = requests.get(subtitle_url, timeout=30)
                    if response.status_code == 200:
                        return response.text

            logger.warning("无法提取字幕内容")
            return None

        except Exception as e:
            logger.error(f"提取字幕内容失败: {str(e)}")
            return None

    def process_video_for_transcription(
        self, url: str, platform: str
    ) -> Optional[Dict[str, Any]]:
        """处理视频用于转录

        Args:
            url: 视频URL
            platform: 平台名称

        Returns:
            dict: 处理结果，包含视频信息和音频文件路径
        """
        try:
            logger.info(f"处理{platform}视频用于转录: {url}")

            # 1. 获取视频信息
            video_info = self.get_video_info(url, platform)
            if not video_info:
                logger.error("获取视频信息失败")
                return None

            # 2. 检测语言和字幕策略
            language = self.get_video_language(video_info)
            should_download_subs, lang_priority = self.get_subtitle_strategy(
                language, video_info
            )

            # 3. 尝试下载字幕
            subtitle_content = None
            if should_download_subs:
                subtitle_content = self.download_subtitles(url, platform, lang_priority)

            # 4. 如果没有字幕，下载音频用于转录
            audio_file = None
            if not subtitle_content:
                logger.info("未找到字幕，开始下载音频用于转录")
                audio_file = self.download_video(url, platform=platform)

            return {
                "video_info": video_info,
                "language": language,
                "subtitle_content": subtitle_content,
                "audio_file": audio_file,
                "needs_transcription": subtitle_content is None,
            }

        except Exception as e:
            logger.error(f"处理视频用于转录失败: {str(e)}")
            return None
