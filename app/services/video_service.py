"""Video processing service for handling multiple video platforms."""

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import yt_dlp
from yt_dlp.utils import DownloadError

from ..config.config_manager import get_config_value
from ..utils.language_detection import (
    add_language_score,
    blank_language_scores,
    decide_primary_language,
    detect_text_primary_language,
    normalize_primary_language,
)
from ..utils.file_utils import sanitize_filename
from ..utils.video_utils import extract_youtube_video_id, normalize_youtube_watch_url
from .subtitle_service import SubtitleService

logger = logging.getLogger(__name__)


class VideoService:
    """视频处理服务 - 支持YouTube、Bilibili、AcFun等平台"""

    def __init__(self):
        """初始化视频服务"""
        self.supported_platforms = ["youtube", "bilibili", "acfun"]
        self.subtitle_service = SubtitleService()
        self.bgutil_provider_url = self._normalize_bgutil_url(
            os.getenv("BGUTIL_PROVIDER_URL", "http://bgutil-provider:4416")
        )
        self._setup_yt_dlp_options()
        self.download_concurrency = self._parse_concurrency_env(
            "DOWNLOAD_CONCURRENCY", 2, "下载"
        )
        self._download_semaphore = threading.BoundedSemaphore(self.download_concurrency)
        self.download_retry_max = max(0, int(os.getenv("DOWNLOAD_MAX_RETRIES", "3")))
        self.download_retry_base_delay = max(
            0.1, float(os.getenv("DOWNLOAD_RETRY_BASE_DELAY", "2"))
        )
        self.download_retry_backoff = max(
            1.0, float(os.getenv("DOWNLOAD_RETRY_BACKOFF", "2"))
        )
        self.download_retry_max_delay = max(
            self.download_retry_base_delay,
            float(os.getenv("DOWNLOAD_RETRY_MAX_DELAY", "30")),
        )
        self.readwise_url_only_when_zh_subs = self._parse_bool_env(
            "READWISE_URL_ONLY_WHEN_ZH_SUBS", False
        )
        logger.info("下载并发限制: %s", self.download_concurrency)
        logger.info(
            "下载403重试参数: max=%s, base=%.1fs, backoff=%.2f, max_delay=%.1fs",
            self.download_retry_max,
            self.download_retry_base_delay,
            self.download_retry_backoff,
            self.download_retry_max_delay,
        )
        logger.info(
            "Readwise URL剪藏开关(中文字幕): %s", self.readwise_url_only_when_zh_subs
        )
        self._log_js_runtime_status()

    def _get_youtube_player_clients(self) -> List[str]:
        """获取YouTube player_client列表，支持环境变量覆盖。"""
        env_value = os.getenv("YTDLP_PLAYER_CLIENTS")
        if env_value:
            clients = [item.strip() for item in env_value.split(",") if item.strip()]
            if clients:
                return clients

        return ["tv", "web_safari", "web"]

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

    @staticmethod
    def _parse_concurrency_env(key: str, default: int, label: str) -> int:
        """解析并发环境变量，0/1 均视为串行。"""
        raw = os.getenv(key)
        if raw is None or not str(raw).strip():
            value = default
        else:
            try:
                value = int(raw)
            except ValueError:
                logger.warning(
                    "%s 并发设置 %s 无效，使用默认值 %s", label, raw, default
                )
                value = default

        if value <= 1:
            if value <= 0:
                logger.info("%s 并发设置为 %s，按串行处理", label, value)
            return 1

        return value

    @staticmethod
    def _parse_bool_env(key: str, default: bool = False) -> bool:
        raw = os.getenv(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _extract_languages(raw_value: Any) -> List[str]:
        if isinstance(raw_value, dict):
            return list(raw_value.keys())
        if isinstance(raw_value, list):
            return [str(item) for item in raw_value]
        return []

    @staticmethod
    def _language_available(target: str, candidates: List[str]) -> bool:
        if target in candidates:
            return True
        prefix = f"{target}-"
        suffix = f"-{target}"
        for candidate in candidates:
            if candidate.startswith(prefix) or candidate.endswith(suffix):
                return True
        return False

    @staticmethod
    def _match_language_key(target: str, candidates: List[str]) -> Optional[str]:
        if target in candidates:
            return target
        prefix = f"{target}-"
        suffix = f"-{target}"
        for candidate in candidates:
            if candidate.startswith(prefix) or candidate.endswith(suffix):
                return candidate
        return None

    @staticmethod
    def _normalize_language_code(language: Optional[str]) -> Optional[str]:
        return normalize_primary_language(language)

    def _collect_primary_languages(self, candidates: List[str]) -> List[str]:
        detected = []
        for language, priorities in (
            ("zh", self._get_zh_language_priority()),
            ("en", self._get_en_language_priority()),
        ):
            for candidate in priorities:
                if self._language_available(candidate, candidates):
                    detected.append(language)
                    break
        return detected

    def _get_exclusive_language_hint(self, candidates: List[str]) -> Optional[str]:
        detected = self._collect_primary_languages(candidates)
        if len(detected) == 1:
            return detected[0]
        return None

    def _build_language_details(
        self,
        language: Optional[str],
        confidence: float,
        scores: Dict[str, float],
        signals: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        normalized = self._normalize_language_code(language) or language
        return {
            "language": normalized,
            "confidence": round(float(confidence), 4),
            "scores": {
                key: round(float(value), 4)
                for key, value in (scores or {}).items()
                if key in {"zh", "en"}
            },
            "signals": signals,
        }

    def _infer_language_from_text(
        self, text: str, source: str, max_weight: float
    ) -> Optional[Dict[str, Any]]:
        detection = detect_text_primary_language(text)
        language = detection.get("language")
        confidence = float(detection.get("confidence", 0.0))
        if language not in {"zh", "en"}:
            return None
        weight = round(max_weight * confidence, 4)
        if weight <= 0:
            return None
        return {
            "language": language,
            "weight": weight,
            "source": source,
            "confidence": confidence,
            "stats": detection.get("stats", {}),
        }

    def get_video_language_details(
        self,
        info: Dict[str, Any],
        subtitle_result: Optional[Dict[str, Any]] = None,
        audio_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Return the detected primary language with confidence and signals."""
        try:
            if not info:
                return self._build_language_details(None, 0.0, {}, [])

            scores = blank_language_scores()
            signals: List[Dict[str, Any]] = []

            raw_language = info.get("language")
            normalized_language = self._normalize_language_code(raw_language)
            if normalized_language in {"zh", "en"}:
                add_language_score(scores, normalized_language, 0.55)
                signals.append(
                    {
                        "source": "metadata.language",
                        "language": normalized_language,
                        "weight": 0.55,
                        "value": raw_language,
                    }
                )
            elif normalized_language and normalized_language not in {"mixed", "unknown"}:
                return self._build_language_details(
                    normalized_language,
                    0.9,
                    {"zh": 0.0, "en": 0.0},
                    [
                        {
                            "source": "metadata.language",
                            "language": normalized_language,
                            "weight": 0.9,
                            "value": raw_language,
                        }
                    ],
                )

            title = info.get("title", "") or ""
            description = info.get("description", "") or ""
            title_text = "\n".join([title, description[:600]]).strip()
            title_hint = self._infer_language_from_text(
                title_text, "metadata.text", max_weight=0.22
            )
            if title_hint:
                add_language_score(
                    scores, title_hint["language"], title_hint["weight"]
                )
                signals.append(title_hint)

            auto_hint = self._get_exclusive_language_hint(
                self._extract_languages(info.get("automatic_captions", {}))
            )
            if auto_hint in {"zh", "en"}:
                add_language_score(scores, auto_hint, 0.3)
                signals.append(
                    {
                        "source": "tracks.automatic_captions",
                        "language": auto_hint,
                        "weight": 0.3,
                    }
                )

            subtitle_hint = self._get_exclusive_language_hint(
                self._extract_languages(info.get("subtitles", {}))
            )
            if subtitle_hint in {"zh", "en"}:
                add_language_score(scores, subtitle_hint, 0.1)
                signals.append(
                    {
                        "source": "tracks.subtitles",
                        "language": subtitle_hint,
                        "weight": 0.1,
                    }
                )

            if subtitle_result:
                claimed_language = self._normalize_language_code(
                    subtitle_result.get("matched_lang")
                )
                source_type = subtitle_result.get("source_type") or "subtitle"
                if claimed_language in {"zh", "en"}:
                    claimed_weight = 0.14 if source_type == "automatic_caption" else 0.06
                    add_language_score(scores, claimed_language, claimed_weight)
                    signals.append(
                        {
                            "source": f"subtitle.{source_type}.track",
                            "language": claimed_language,
                            "weight": claimed_weight,
                            "value": subtitle_result.get("matched_lang"),
                        }
                    )

                subtitle_text = subtitle_result.get("content") or ""
                normalized_subtitle = (
                    self.subtitle_service.normalize_external_subtitle_content(
                        subtitle_text
                    )
                    or subtitle_text
                )
                subtitle_text_hint = self._infer_language_from_text(
                    normalized_subtitle,
                    f"subtitle.{source_type}.text",
                    max_weight=0.45 if source_type == "automatic_caption" else 0.16,
                )
                if subtitle_text_hint:
                    add_language_score(
                        scores,
                        subtitle_text_hint["language"],
                        subtitle_text_hint["weight"],
                    )
                    signals.append(subtitle_text_hint)

            if audio_result:
                audio_language = self._normalize_language_code(
                    audio_result.get("language")
                )
                audio_confidence = float(audio_result.get("confidence", 0.0))
                if audio_language in {"zh", "en"} and audio_confidence > 0:
                    audio_weight = round(min(0.85, 0.55 + audio_confidence * 0.3), 4)
                    add_language_score(scores, audio_language, audio_weight)
                    signals.append(
                        {
                            "source": "audio_probe",
                            "language": audio_language,
                            "weight": audio_weight,
                            "confidence": audio_confidence,
                        }
                    )

            decision = decide_primary_language(scores)
            language = decision.get("language")
            if language is None and normalized_language:
                language = normalized_language

            return self._build_language_details(
                language,
                decision.get("confidence", 0.0),
                decision.get("scores", scores),
                signals,
            )

        except Exception as e:
            logger.error(f"检测视频语言详情时出错: {str(e)}")
            return self._build_language_details(None, 0.0, {}, [])

    @staticmethod
    def _get_zh_language_priority() -> List[str]:
        return ["zh-CN", "zh", "zh-TW", "zh-Hans", "zh-Hant"]

    @staticmethod
    def _get_en_language_priority() -> List[str]:
        return ["en", "en-US", "en-GB"]

    def _has_language_subtitles(
        self, info: Dict[str, Any], lang_priority: List[str]
    ) -> bool:
        available_subtitles = self._extract_languages(info.get("subtitles", {}))
        available_auto = self._extract_languages(info.get("automatic_captions", {}))
        for lang in lang_priority:
            if self._language_available(
                lang, available_subtitles
            ) or self._language_available(lang, available_auto):
                return True
        return False

    def _should_clip_url_only(self, info: Dict[str, Any]) -> bool:
        if not self.readwise_url_only_when_zh_subs:
            return False
        return self._has_language_subtitles(info, self._get_zh_language_priority())

    def _setup_yt_dlp_options(self):
        """设置yt-dlp默认选项"""
        player_clients = self._get_youtube_player_clients()

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
                    "player_client": player_clients,
                    "fetch_pot": ["auto"],
                },
                "youtubepot-bgutilhttp": {
                    "base_url": [self.bgutil_provider_url],
                },
            },
        }
        logger.info("yt-dlp 将使用 bgutil provider: %s", self.bgutil_provider_url)
        logger.info("yt-dlp YouTube player clients: %s", player_clients)
        self._configure_cookie_support(base_opts)
        self.yt_dlp_opts = base_opts

    def _log_js_runtime_status(self) -> None:
        """快速检测JS运行时，辅助排查YouTube挑战失败问题。"""
        runtime = self._detect_js_runtime()
        if runtime:
            name, version = runtime
            if version:
                logger.info("检测到JS运行时: %s (%s)", name, version)
            else:
                logger.info("检测到JS运行时: %s", name)
            return

        logger.warning(
            "未检测到JS运行时(Node/QuickJS)，可能导致YouTube n challenge失败。"
        )

    def _detect_js_runtime(self) -> Optional[Tuple[str, Optional[str]]]:
        """检测可用的JS运行时，并尽量获取版本信息。"""
        try:
            import shutil
            import subprocess
        except Exception:
            return None

        candidates = [
            ("deno", ["deno", "--version"]),
            ("node", ["node", "-v"]),
            ("qjs", ["qjs", "--version"]),
            ("quickjs", ["quickjs", "--version"]),
        ]

        for name, cmd in candidates:
            if not shutil.which(cmd[0]):
                continue

            version = None
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                output = (result.stdout or result.stderr or "").strip()
                if output:
                    version = output.splitlines()[0]
            except Exception:
                version = None

            return name, version

        return None

    def _is_http_403_error(self, error: Exception) -> bool:
        """判断下载错误是否为403"""
        message = str(error)
        if "HTTP Error 403" in message:
            return True
        lowered = message.lower()
        return "403" in lowered and "forbidden" in lowered

    def _calculate_download_backoff(self, attempt: int) -> float:
        """计算下载403重试的退避时间"""
        delay = self.download_retry_base_delay * (
            self.download_retry_backoff ** max(0, attempt - 1)
        )
        return min(delay, self.download_retry_max_delay)

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

    def _build_download_base_opts(
        self, temp_dir: str, platform: Optional[str], url: str
    ) -> Dict[str, Any]:
        """构建媒体下载选项，确保复用统一的yt-dlp/cookie配置。"""
        resolved_platform = platform or "youtube"
        opts = deepcopy(self._get_yt_dlp_opts_for_platform(resolved_platform, url))
        opts["outtmpl"] = os.path.join(temp_dir, "%(id)s.%(ext)s")
        opts["quiet"] = True
        opts["no_warnings"] = True
        opts["geo_bypass"] = True
        opts["no_check_certificate"] = True

        headers = dict(opts.get("http_headers", {}))
        headers.update(
            {
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.5",
            }
        )
        platform_headers = self._get_platform_headers(platform, url)
        if not platform_headers:
            platform_headers = self._get_platform_headers("youtube", url)
        headers.update(platform_headers)
        opts["http_headers"] = headers

        if "cookiefile" in opts or "cookiesfrombrowser" in opts:
            logger.info("音频下载将复用统一cookie配置")
            return opts

        firefox_profile = self._get_firefox_profile_path()
        if firefox_profile:
            logger.info("音频下载补充Firefox配置文件: %s", firefox_profile)
            opts["cookiesfrombrowser"] = ("firefox", firefox_profile)
        else:
            logger.warning(
                "音频下载阶段未找到可用 cookie，可能触发 YouTube 验证。"
                "请确认已挂载 firefox_profile 或设置 YTDLP_COOKIE_FILE。",
            )

        return opts

    def _build_public_youtube_download_opts(self, temp_dir: str) -> Dict[str, Any]:
        """为公开视频构建接近裸 yt-dlp 的下载参数，避免额外配置改变可用格式。"""
        opts: Dict[str, Any] = {
            "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
            "geo_bypass": True,
            "no_check_certificate": True,
        }
        for key in (
            "logger",
            "quiet",
            "no_warnings",
            "cachedir",
            "noplaylist",
            "skip_unavailable_fragments",
        ):
            if key in self.yt_dlp_opts:
                opts[key] = self.yt_dlp_opts[key]
        return opts

    def _build_download_option_profiles(
        self, temp_dir: str, platform: Optional[str], url: str
    ) -> List[Dict[str, Any]]:
        """构建媒体下载参数组合，优先使用更接近裸 yt-dlp 的路径。"""
        resolved_platform = platform or "youtube"
        if resolved_platform != "youtube":
            return [
                {
                    "desc": "平台下载参数",
                    "opts": self._build_download_base_opts(
                        temp_dir, resolved_platform, url
                    ),
                }
            ]

        profiles = [
            {
                "desc": "默认公开视频参数",
                "opts": self._build_public_youtube_download_opts(temp_dir),
            }
        ]

        authenticated_opts = self._build_download_base_opts(
            temp_dir, resolved_platform, url
        )
        profiles.append(
            {
                "desc": "登录态/兼容参数",
                "opts": authenticated_opts,
            }
        )
        return profiles

    @staticmethod
    def _normalize_download_error_text(message: Any) -> Optional[str]:
        if message is None:
            return None
        normalized = " ".join(str(message).split()).strip()
        return normalized or None

    def _summarize_download_errors(self, errors: List[str]) -> str:
        normalized_errors = []
        for error in errors:
            normalized = self._normalize_download_error_text(error)
            if normalized:
                normalized_errors.append(normalized)

        if not normalized_errors:
            return "音频下载失败"

        combined = " ".join(normalized_errors).lower()
        if "po token" in combined or "bgutil" in combined:
            return (
                "YouTube 音频下载失败：PO Token 获取失败，请检查 bgutil provider "
                "是否可用，并确认 cookies 仍有效"
            )
        if "n challenge" in combined:
            return (
                "YouTube 音频下载失败：JS challenge 解析失败，请确认 Deno / "
                "yt-dlp-ejs 可用，并检查 cookies 配置"
            )
        if (
            "http error 403" in combined
            or "forbidden" in combined
            or "fragment 1 not found" in combined
        ):
            return (
                "YouTube 音频下载失败：媒体流返回 HTTP 403，请检查 cookies 是否有效，"
                "或确认 bgutil provider / JS challenge solver 是否正常"
            )
        if "requested format is not available" in combined:
            return "YouTube 音频下载失败：当前没有可用的下载格式，请稍后重试或更新 yt-dlp"

        return normalized_errors[-1][:240]

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
                "language": info.get("language"),
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

    def _probe_audio_language(
        self, audio_file: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Use the transcription service's lightweight probe when available."""
        if not audio_file:
            return None

        try:
            from .transcription_service import TranscriptionService

            probe_service = TranscriptionService()
            return probe_service.detect_audio_language(audio_file)
        except Exception as e:
            logger.warning(f"音频语言探测失败: {str(e)}")
            return None

    def get_video_language(self, info: Dict[str, Any]) -> Optional[str]:
        """检测视频语言

        Args:
            info: 视频信息字典

        Returns:
            str: 语言代码 ('zh', 'en', etc.) 或 None
        """
        try:
            details = self.get_video_language_details(info)
            return details.get("language")

        except Exception as e:
            logger.error(f"检测视频语言时出错: {str(e)}")
            return None

    def get_subtitle_strategy(
        self,
        language: Optional[str],
        info: Dict[str, Any],
        language_confidence: float = 0.0,
    ) -> Tuple[bool, List[str]]:
        """确定字幕获取策略

        Args:
            language: 检测到的视频语言
            info: 视频信息
            language_confidence: 语言置信度

        Returns:
            tuple: (是否应该下载字幕, 语言优先级列表)
        """
        try:

            def _summarize_languages(
                languages: List[str], limit: int = 12
            ) -> List[str]:
                if len(languages) <= limit:
                    return languages
                return languages[:limit] + [f"...(+{len(languages) - limit})"]

            available_subtitles = self._extract_languages(info.get("subtitles", {}))
            available_auto = self._extract_languages(info.get("automatic_captions", {}))

            logger.info(f"可用字幕: {_summarize_languages(available_subtitles)}")
            logger.info(f"可用自动字幕: {_summarize_languages(available_auto)}")

            auto_hint = self._get_exclusive_language_hint(available_auto)
            subtitle_hint = self._get_exclusive_language_hint(available_subtitles)

            if language == "zh" and language_confidence >= 0.6:
                # 中文视频：优先中文字幕
                lang_priority = self._get_zh_language_priority()
            elif language == "en" and language_confidence >= 0.6:
                # 英文视频：优先英文字幕
                lang_priority = self._get_en_language_priority()
            elif auto_hint == "zh":
                logger.info("自动字幕轨道更偏向中文，优先下载中文字幕")
                return True, self._get_zh_language_priority()
            elif auto_hint == "en":
                logger.info("自动字幕轨道更偏向英文，优先下载英文字幕")
                return True, self._get_en_language_priority()
            elif language == "zh":
                lang_priority = self._get_zh_language_priority()
            elif language == "en":
                lang_priority = self._get_en_language_priority()
            elif subtitle_hint == "zh":
                logger.info("人工字幕轨道更偏向中文，优先下载中文字幕")
                return True, self._get_zh_language_priority()
            elif subtitle_hint == "en":
                logger.info("人工字幕轨道更偏向英文，优先下载英文字幕")
                return True, self._get_en_language_priority()
            else:
                if self._has_language_subtitles(info, self._get_zh_language_priority()):
                    logger.info("检测到中文字幕，优先下载中文字幕")
                    return True, self._get_zh_language_priority()
                if self._has_language_subtitles(info, self._get_en_language_priority()):
                    logger.info("检测到英文字幕，优先下载英文字幕")
                    return True, self._get_en_language_priority()
                return False, []

            # 检查是否有对应语言的字幕
            for lang in lang_priority:
                if self._language_available(
                    lang, available_subtitles
                ) or self._language_available(lang, available_auto):
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
            video_id = extract_youtube_video_id(url)
            if not video_id:
                return url  # 如果不是YouTube URL，直接返回

            # 获取自定义域名配置
            custom_domain = get_config_value(
                "servers.video_domain", "http://localhost:5000"
            )
            return f"{custom_domain}/view/{video_id}"

        except Exception as e:
            logger.error(f"转换YouTube URL时出错: {str(e)}")
            return url

    def _normalize_youtube_watch_url(self, url: str) -> Optional[str]:
        """将YouTube短链或特殊页面URL转换为标准 watch URL。"""
        try:
            return normalize_youtube_watch_url(url)
        except Exception as e:
            logger.warning(f"解析YouTube URL失败: {str(e)}")
            return None

    def _prepare_task_temp_dir(self, output_folder: Optional[str] = None) -> str:
        """为单次下载任务创建独立临时目录，避免任务之间互相污染。"""
        temp_root = output_folder or os.path.join(
            get_config_value("app.upload_folder", "/app/uploads"), "temp"
        )
        os.makedirs(temp_root, exist_ok=True)
        task_temp_dir = tempfile.mkdtemp(prefix="download_", dir=temp_root)
        logger.info("创建任务临时目录: %s", task_temp_dir)
        return task_temp_dir

    @staticmethod
    def _cleanup_task_temp_dir(temp_dir: Optional[str]) -> None:
        """清理单次下载任务的临时目录。"""
        if not temp_dir:
            return
        try:
            shutil.rmtree(temp_dir)
            logger.info("已清理任务临时目录: %s", temp_dir)
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning("清理任务临时目录失败 %s: %s", temp_dir, e)

    def cleanup_task_artifacts(self, temp_dir: Optional[str]) -> None:
        """公开的任务清理入口，供路由层在处理完成后回收下载产物。"""
        if not temp_dir:
            return

        temp_root = os.path.realpath(
            os.path.join(get_config_value("app.upload_folder", "/app/uploads"), "temp")
        )
        target_dir = os.path.realpath(temp_dir)

        if os.path.basename(target_dir).startswith("download_") is False:
            logger.warning("跳过清理非任务临时目录: %s", temp_dir)
            return

        if os.path.commonpath([temp_root, target_dir]) != temp_root:
            logger.warning("跳过清理目录，路径不在临时目录根下: %s", temp_dir)
            return

        self._cleanup_task_temp_dir(temp_dir)

    def download_video(
        self,
        url: str,
        output_folder: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> Optional[Dict[str, str]]:
        """下载视频并提取音频

        Args:
            url: 视频URL
            output_folder: 输出目录，默认使用配置的上传目录
            platform: 平台名称，用于设置正确的请求头

        Returns:
            dict: 成功时返回音频文件路径和临时目录，失败返回None
        """
        semaphore = self._download_semaphore
        if semaphore:
            logger.info(
                "等待下载并发许可 (limit=%s): %s", self.download_concurrency, url
            )
            semaphore.acquire()
        temp_dir = None
        should_cleanup_temp_dir = True
        try:
            temp_dir = self._prepare_task_temp_dir(output_folder)
            logger.info(f"开始下载视频: {url}")
            download_profiles = self._build_download_option_profiles(
                temp_dir, platform, url
            )

            # 先尝试检查视频信息
            info = None
            for profile in download_profiles:
                try:
                    time.sleep(2)
                    info_opts = deepcopy(profile["opts"])
                    info_opts.pop("outtmpl", None)
                    info_opts.pop("format", None)
                    with yt_dlp.YoutubeDL(info_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                        logger.info(
                            "使用%s获取视频信息成功: %s",
                            profile["desc"],
                            info.get("title"),
                        )
                        if info.get("age_limit", 0) > 0:
                            logger.info(f"视频有年龄限制: {info.get('age_limit')}+")
                        if info.get("is_live", False):
                            logger.info("这是一个直播视频")
                        if info.get("availability", "") != "public":
                            logger.info(
                                f"视频可用性: {info.get('availability', 'unknown')}"
                            )
                        break
                except Exception as e:
                    logger.info(
                        "使用%s获取视频信息失败，尝试下一组参数: %s",
                        profile["desc"],
                        str(e),
                    )
                    info = None

            # 记录预期的视频ID（用于后续文件查找）
            expected_video_id = None
            if info:
                expected_video_id = info.get("id")
            else:
                expected_video_id = extract_youtube_video_id(url)

            logger.info(f"预期视频ID: {expected_video_id}")

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
            download_errors = []
            retry_limit = max(0, self.download_retry_max)
            for profile in download_profiles:
                profile_desc = profile["desc"]
                base_opts = profile["opts"]
                for format_attempt in format_attempts:
                    retry_count = 0
                    while True:
                        try:
                            logger.info(
                                "尝试下载: %s / %s",
                                profile_desc,
                                format_attempt["desc"],
                            )
                            # 添加率限制防止IP被封
                            time.sleep(3)
                            before_files = set(os.listdir(temp_dir))
                            opts = deepcopy(base_opts)
                            opts["format"] = format_attempt["format"]

                            with yt_dlp.YoutubeDL(opts) as ydl:
                                ydl.download([url])

                            # 改进的文件查找逻辑
                            downloaded_file = self._find_downloaded_file(
                                temp_dir, expected_video_id, baseline_files=before_files
                            )
                            if not downloaded_file:
                                downloaded_file = self._find_downloaded_file(
                                    temp_dir, expected_video_id
                                )

                            if downloaded_file and os.path.exists(downloaded_file):
                                logger.info(f"下载成功: {downloaded_file}")
                                break

                            logger.warning(
                                "下载完成但未找到文件: %s / %s",
                                profile_desc,
                                format_attempt["desc"],
                            )
                            download_errors.append(
                                f"{profile_desc} / {format_attempt['desc']}: 下载完成但未找到输出文件"
                            )
                            break

                        except DownloadError as e:
                            download_errors.append(
                                f"{profile_desc} / {format_attempt['desc']}: {str(e)}"
                            )
                            if self._is_http_403_error(e) and retry_count < retry_limit:
                                retry_count += 1
                                delay = self._calculate_download_backoff(retry_count)
                                logger.warning(
                                    "下载遇到403，%ss后重试 (%s/%s): %s / %s",
                                    delay,
                                    retry_count,
                                    retry_limit,
                                    profile_desc,
                                    format_attempt["desc"],
                                )
                                time.sleep(delay)
                                continue
                            logger.warning(
                                "下载失败 (%s / %s): %s",
                                profile_desc,
                                format_attempt["desc"],
                                str(e),
                            )
                            break
                        except Exception as e:
                            download_errors.append(
                                f"{profile_desc} / {format_attempt['desc']}: {str(e)}"
                            )
                            if self._is_http_403_error(e) and retry_count < retry_limit:
                                retry_count += 1
                                delay = self._calculate_download_backoff(retry_count)
                                logger.warning(
                                    "下载遇到403，%ss后重试 (%s/%s): %s / %s",
                                    delay,
                                    retry_count,
                                    retry_limit,
                                    profile_desc,
                                    format_attempt["desc"],
                                )
                                time.sleep(delay)
                                continue
                            logger.warning(
                                "下载失败 (%s / %s): %s",
                                profile_desc,
                                format_attempt["desc"],
                                str(e),
                            )
                            break

                    if downloaded_file and os.path.exists(downloaded_file):
                        break

                if downloaded_file and os.path.exists(downloaded_file):
                    break

            if not downloaded_file:
                summarized_error = self._summarize_download_errors(download_errors)
                logger.error("所有下载尝试都失败了: %s", summarized_error)
                # 列出临时目录中的文件用于调试
                try:
                    files = os.listdir(temp_dir)
                    logger.error(
                        "临时目录文件数量: %s, 示例: %s",
                        len(files),
                        files[:30],
                    )
                    if files:
                        logger.error(
                            "目录中存在文件，但未匹配到当前任务可用文件 (expected_video_id=%s)",
                            expected_video_id,
                        )
                except Exception as e:
                    logger.error(f"无法列出临时目录文件: {str(e)}")
                return {
                    "audio_file": None,
                    "temp_dir": None,
                    "error": summarized_error,
                }

            # 转换为音频格式
            audio_file = self._convert_to_audio(downloaded_file, temp_dir)
            if not audio_file:
                return {
                    "audio_file": None,
                    "temp_dir": None,
                    "error": "音频格式转换失败，无法生成可转录的 WAV 文件",
                }

            should_cleanup_temp_dir = False
            return {
                "audio_file": audio_file,
                "temp_dir": temp_dir,
                "error": None,
            }

        except Exception as e:
            logger.error(f"下载视频时出错: {str(e)}")
            return None
        finally:
            if should_cleanup_temp_dir:
                self._cleanup_task_temp_dir(temp_dir)
            if semaphore:
                semaphore.release()

    def _find_downloaded_file(
        self,
        temp_dir: str,
        expected_video_id: Optional[str],
        baseline_files: Optional[set] = None,
    ) -> Optional[str]:
        """改进的下载文件查找逻辑"""
        try:
            if not os.path.exists(temp_dir):
                logger.error(f"临时目录不存在: {temp_dir}")
                return None

            files = os.listdir(temp_dir)
            logger.info("临时目录文件数量: %s", len(files))

            if not files:
                logger.warning("临时目录中没有文件")
                return None

            candidate_files = self._get_stable_download_candidates(temp_dir, files)
            if baseline_files is not None:
                candidate_files = [
                    candidate
                    for candidate in candidate_files
                    if os.path.basename(candidate[0]) not in baseline_files
                ]
                if not candidate_files:
                    logger.warning("本次下载未产出新的稳定文件")
                    return None

            if not candidate_files:
                logger.warning("未找到可用的下载文件（仅检测到临时/不完整文件）")
                return None

            # 策略1: 如果有预期的视频ID，优先匹配
            if expected_video_id:
                # 先查找精确匹配（不带_part_的文件）
                for file_path, file, _ in candidate_files:
                    base_name, _ = os.path.splitext(file)
                    if base_name == expected_video_id:
                        logger.info(f"通过精确匹配到文件: {file_path}")
                        return file_path

                for file_path, file, _ in candidate_files:
                    if file.startswith(expected_video_id):
                        logger.info(f"通过视频ID匹配到文件: {file_path}")
                        return file_path

                logger.warning(
                    "候选文件未匹配预期视频ID: expected_video_id=%s, candidates=%s",
                    expected_video_id,
                    [candidate[1] for candidate in candidate_files],
                )
                return None

            # 当无法确定视频ID时，只能在当前任务独立目录内回退到最新文件。
            candidate_files.sort(key=lambda item: item[2], reverse=True)
            newest_file = candidate_files[0][0]
            logger.info(f"在当前任务目录中选择最新文件: {newest_file}")
            return newest_file

        except Exception as e:
            logger.error(f"查找下载文件时发生错误: {str(e)}")
            return None

    @staticmethod
    def _is_incomplete_download_file(filename: str) -> bool:
        """判断是否为下载中间文件。"""
        lower_name = filename.lower()
        if lower_name.endswith((".part", ".ytdl", ".tmp", ".temp")):
            return True
        return ".part-" in lower_name

    def _get_stable_download_candidates(
        self, temp_dir: str, files: List[str]
    ) -> List[Tuple[str, str, float]]:
        """返回可用于后续转换的稳定文件列表。"""
        candidates = []
        for file in files:
            if self._is_incomplete_download_file(file):
                continue

            file_path = os.path.join(temp_dir, file)
            if not os.path.isfile(file_path):
                continue

            try:
                if os.path.getsize(file_path) <= 0:
                    continue
                mtime = os.path.getmtime(file_path)
            except OSError:
                continue

            candidates.append((file_path, file, mtime))

        return candidates

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
    ) -> Optional[Dict[str, Any]]:
        """下载字幕文件

        Args:
            url: 视频URL
            platform: 平台名称
            lang_priority: 语言优先级列表

        Returns:
            dict: 字幕内容及其元数据，失败返回None
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
    ) -> Optional[Dict[str, Any]]:
        """下载YouTube字幕"""
        try:
            # 添加率限制防止IP被封
            time.sleep(2)
            with yt_dlp.YoutubeDL(self.yt_dlp_opts) as ydl:
                info = ydl.extract_info(url, download=False)

                available_subtitles = info.get("subtitles", {})
                available_auto = info.get("automatic_captions", {})
                subtitle_keys = list(available_subtitles.keys())
                auto_keys = list(available_auto.keys())

                # 按优先级查找字幕
                for lang in lang_priority:
                    # 优先使用人工字幕
                    matched_lang = self._match_language_key(lang, subtitle_keys)
                    if matched_lang:
                        logger.info(f"找到{matched_lang}人工字幕")
                        subtitle_result = self._extract_subtitle_content(
                            available_subtitles[matched_lang]
                        )
                        return self._build_subtitle_result(
                            subtitle_result, matched_lang, "subtitle"
                        )

                    # 如果没有人工字幕，使用自动字幕
                    matched_lang = self._match_language_key(lang, auto_keys)
                    if matched_lang:
                        logger.info(f"找到{matched_lang}自动字幕")
                        subtitle_result = self._extract_subtitle_content(
                            available_auto[matched_lang]
                        )
                        return self._build_subtitle_result(
                            subtitle_result, matched_lang, "automatic_caption"
                        )

                logger.warning("未找到匹配语言的字幕")
                return None

        except Exception as e:
            logger.error(f"下载YouTube字幕失败: {str(e)}")
            return None

    def download_bilibili_subtitles(
        self, url: str, lang_priority: List[str]
    ) -> Optional[Dict[str, Any]]:
        """下载Bilibili字幕"""
        try:
            opts = self._get_yt_dlp_opts_for_platform("bilibili", url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

                available_subtitles = info.get("subtitles", {})
                subtitle_keys = list(available_subtitles.keys())

                # Bilibili通常只有中文字幕
                for lang in lang_priority:
                    matched_lang = self._match_language_key(lang, subtitle_keys)
                    if matched_lang:
                        logger.info(f"找到{matched_lang}字幕")
                        subtitle_result = self._extract_subtitle_content(
                            available_subtitles[matched_lang]
                        )
                        return self._build_subtitle_result(
                            subtitle_result, matched_lang, "subtitle"
                        )

                # 如果没有指定语言，尝试任何可用的字幕
                if available_subtitles:
                    first_lang = list(available_subtitles.keys())[0]
                    logger.info(f"使用第一个可用字幕: {first_lang}")
                    subtitle_result = self._extract_subtitle_content(
                        available_subtitles[first_lang]
                    )
                    return self._build_subtitle_result(
                        subtitle_result, first_lang, "subtitle"
                    )

                logger.warning("未找到Bilibili字幕")
                return None

        except Exception as e:
            logger.error(f"下载Bilibili字幕失败: {str(e)}")
            return None

    def download_acfun_subtitles(
        self, url: str, lang_priority: List[str]
    ) -> Optional[Dict[str, Any]]:
        """下载AcFun字幕"""
        try:
            opts = self._get_yt_dlp_opts_for_platform("acfun", url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

                available_subtitles = info.get("subtitles", {})
                subtitle_keys = list(available_subtitles.keys())

                # AcFun通常只有中文字幕
                for lang in lang_priority:
                    matched_lang = self._match_language_key(lang, subtitle_keys)
                    if matched_lang:
                        logger.info(f"找到{matched_lang}字幕")
                        subtitle_result = self._extract_subtitle_content(
                            available_subtitles[matched_lang]
                        )
                        return self._build_subtitle_result(
                            subtitle_result, matched_lang, "subtitle"
                        )

                # 如果没有指定语言，尝试任何可用的字幕
                if available_subtitles:
                    first_lang = list(available_subtitles.keys())[0]
                    logger.info(f"使用第一个可用字幕: {first_lang}")
                    subtitle_result = self._extract_subtitle_content(
                        available_subtitles[first_lang]
                    )
                    return self._build_subtitle_result(
                        subtitle_result, first_lang, "subtitle"
                    )

                logger.warning("未找到AcFun字幕")
                return None

        except Exception as e:
            logger.error(f"下载AcFun字幕失败: {str(e)}")
            return None

    def _extract_subtitle_content(
        self, subtitle_formats: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """从字幕格式列表中提取内容"""
        try:
            # 按优先级尝试不同格式
            format_priority = ["srt", "vtt", "ttml", "json3", "srv3", "srv2", "srv1"]

            for format_name in format_priority:
                for subtitle_format in subtitle_formats:
                    if subtitle_format.get("ext") == format_name:
                        subtitle_url = subtitle_format.get("url")
                        if subtitle_url:
                            logger.info(f"下载{format_name}格式字幕: {subtitle_url}")
                            response = requests.get(subtitle_url, timeout=30)
                            if response.status_code == 200:
                                return {
                                    "content": response.text,
                                    "format": format_name,
                                    "url": subtitle_url,
                                }

            # 如果没有找到优先格式，使用第一个可用的
            if subtitle_formats:
                first_format = subtitle_formats[0]
                subtitle_url = first_format.get("url")
                if subtitle_url:
                    logger.info(f"使用第一个可用格式: {first_format.get('ext')}")
                    response = requests.get(subtitle_url, timeout=30)
                    if response.status_code == 200:
                        return {
                            "content": response.text,
                            "format": first_format.get("ext"),
                            "url": subtitle_url,
                        }

            logger.warning("无法提取字幕内容")
            return None

        except Exception as e:
            logger.error(f"提取字幕内容失败: {str(e)}")
            return None

    @staticmethod
    def _build_subtitle_result(
        subtitle_payload: Optional[Dict[str, Any]],
        matched_lang: Optional[str],
        source_type: str,
    ) -> Optional[Dict[str, Any]]:
        if not subtitle_payload or not subtitle_payload.get("content"):
            return None
        return {
            "content": subtitle_payload.get("content"),
            "format": subtitle_payload.get("format"),
            "url": subtitle_payload.get("url"),
            "matched_lang": matched_lang,
            "source_type": source_type,
        }

    def _process_video_for_transcription_with_url(
        self, url: str, platform: str
    ) -> Optional[Dict[str, Any]]:
        """使用指定URL完成转录前置处理。"""
        logger.info(f"处理{platform}视频用于转录: {url}")

        # 1. 获取视频信息
        video_info = self.get_video_info(url, platform)
        if not video_info:
            logger.error("获取视频信息失败")
            return None

        # 2. 检测语言和字幕策略
        language_details = self.get_video_language_details(video_info)
        language = language_details.get("language")
        should_download_subs, lang_priority = self.get_subtitle_strategy(
            language, video_info, language_details.get("confidence", 0.0)
        )

        if self._should_clip_url_only(video_info):
            logger.info("检测到中文字幕且启用URL剪藏，跳过字幕下载与转录")
            return {
                "video_info": video_info,
                "language": language,
                "language_details": language_details,
                "subtitle_content": None,
                "subtitle_metadata": None,
                "audio_file": None,
                "temp_dir": None,
                "needs_transcription": False,
                "readwise_url_only": True,
            }

        # 3. 尝试下载字幕
        subtitle_content = None
        subtitle_metadata = None
        if should_download_subs:
            subtitle_metadata = self.download_subtitles(url, platform, lang_priority)
            if subtitle_metadata:
                subtitle_content = subtitle_metadata.get("content")
                refined_details = self.get_video_language_details(
                    video_info, subtitle_result=subtitle_metadata
                )
                if (
                    refined_details.get("confidence", 0.0)
                    >= language_details.get("confidence", 0.0)
                    or language in {None, "mixed"}
                ):
                    language_details = refined_details
                    language = language_details.get("language")

        # 4. 如果没有字幕，下载音频用于转录
        audio_file = None
        temp_dir = None
        download_error = None
        if not subtitle_content:
            logger.info("未找到字幕，开始下载音频用于转录")
            download_result = self.download_video(url, platform=platform)
            if isinstance(download_result, dict):
                audio_file = download_result.get("audio_file")
                temp_dir = download_result.get("temp_dir")
                download_error = download_result.get("error")
            else:
                audio_file = download_result
                temp_dir = os.path.dirname(audio_file) if audio_file else None

            if audio_file:
                audio_probe = self._probe_audio_language(audio_file)
                if audio_probe:
                    refined_details = self.get_video_language_details(
                        video_info, audio_result=audio_probe
                    )
                    if (
                        refined_details.get("confidence", 0.0)
                        >= language_details.get("confidence", 0.0)
                        or language in {None, "mixed"}
                    ):
                        language_details = refined_details
                        language = language_details.get("language")

        return {
            "video_info": video_info,
            "language": language,
            "language_details": language_details,
            "subtitle_content": subtitle_content,
            "subtitle_metadata": subtitle_metadata,
            "audio_file": audio_file,
            "temp_dir": temp_dir,
            "download_error": download_error,
            "needs_transcription": subtitle_content is None,
        }

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
            if platform == "youtube":
                normalized_url = self._normalize_youtube_watch_url(url)
                if normalized_url and normalized_url != url:
                    logger.info(
                        "检测到YouTube非标准链接，先尝试标准URL: %s", normalized_url
                    )
                    primary_result = self._process_video_for_transcription_with_url(
                        normalized_url, platform
                    )
                    needs_fallback = primary_result is None or (
                        not primary_result.get("subtitle_content")
                        and not primary_result.get("audio_file")
                        and not primary_result.get("readwise_url_only")
                    )
                    if needs_fallback:
                        logger.warning("标准URL处理失败，回退使用原始URL: %s", url)
                        fallback_result = (
                            self._process_video_for_transcription_with_url(
                                url, platform
                            )
                        )
                        if fallback_result is not None:
                            return fallback_result
                    return primary_result

            return self._process_video_for_transcription_with_url(url, platform)

        except Exception as e:
            logger.error(f"处理视频用于转录失败: {str(e)}")
            return None
