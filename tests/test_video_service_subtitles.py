import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.video_service import VideoService
from yt_dlp.utils import DownloadError


def test_get_subtitle_strategy_skips_subtitles_without_primary_language():
    service = VideoService()
    info = {
        "subtitles": ["zh-Hant"],
        "automatic_captions": [],
    }

    should_download, lang_priority = service.get_subtitle_strategy("mixed", info)

    assert should_download is False
    assert lang_priority == []


def test_get_subtitle_strategy_uses_metadata_text_language():
    service = VideoService()
    info = {
        "title": "这是一个中文标题",
        "description": "这段简介也说明视频内容主要是中文。",
        "subtitles": {
            "zh-Hant": [{"ext": "vtt", "url": "https://example.com/zh-Hant.vtt"}]
        },
        "automatic_captions": {},
    }

    should_download, lang_priority = service.get_subtitle_strategy("mixed", info)

    assert should_download is True
    assert lang_priority == service._get_zh_language_priority()


def test_get_subtitle_strategy_rejects_mismatched_subtitle_language():
    service = VideoService()
    info = {
        "title": "这是一个中文标题",
        "description": "这段简介也说明视频内容主要是中文。",
        "subtitles": {
            "en": [{"ext": "vtt", "url": "https://example.com/en.vtt"}]
        },
        "automatic_captions": {},
    }

    should_download, lang_priority = service.get_subtitle_strategy("mixed", info)

    assert should_download is False
    assert lang_priority == service._get_zh_language_priority()


def test_match_language_key_handles_suffix():
    matched = VideoService._match_language_key("zh-Hans", ["zh-Hans-zh", "en", "zh"])

    assert matched == "zh-Hans-zh"


def test_should_clip_url_only_when_enabled():
    service = VideoService()
    service.readwise_url_only_when_zh_subs = True
    info = {
        "subtitles": {},
        "automatic_captions": {
            "zh-Hans-zh": [
                {"ext": "json3", "url": "https://example.com/subtitle.json3"}
            ]
        },
    }

    assert service._should_clip_url_only(info) is True


def test_extract_subtitle_content_prefers_srt(monkeypatch):
    service = VideoService()
    requested_urls = []

    class DummyResponse:
        def __init__(self, text):
            self.status_code = 200
            self.text = text

    def fake_get(url, timeout=30):
        requested_urls.append(url)
        return DummyResponse(f"subtitle from {url}")

    monkeypatch.setattr("app.services.video_service.requests.get", fake_get)
    subtitle_formats = [
        {"ext": "json3", "url": "https://example.com/subtitle.json3"},
        {"ext": "srt", "url": "https://example.com/subtitle.srt"},
    ]

    content = service._extract_subtitle_content(subtitle_formats)

    assert requested_urls[0].endswith(".srt")
    assert content["content"] == "subtitle from https://example.com/subtitle.srt"
    assert content["format"] == "srt"


def test_download_youtube_subtitles_returns_track_metadata(monkeypatch):
    service = VideoService()

    class DummyYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            return {
                "subtitles": {
                    "zh-CN": [{"ext": "srt", "url": "https://example.com/subtitle.srt"}]
                },
                "automatic_captions": {},
            }

    monkeypatch.setattr("app.services.video_service.time.sleep", lambda *_: None)
    monkeypatch.setattr("app.services.video_service.yt_dlp.YoutubeDL", DummyYDL)
    monkeypatch.setattr(
        service,
        "_extract_subtitle_content",
        lambda formats: {
            "content": "字幕内容",
            "format": "srt",
            "url": formats[0]["url"],
        },
    )

    result = service.download_youtube_subtitles(
        "https://www.youtube.com/watch?v=test", ["zh-CN"]
    )

    assert result["content"] == "字幕内容"
    assert result["matched_lang"] == "zh-CN"
    assert result["source_type"] == "subtitles"
    assert result["track_type"] == "human"


def test_process_video_result_includes_download_error(monkeypatch):
    service = VideoService()

    monkeypatch.setattr(
        service,
        "get_video_info",
        lambda url, platform: {"id": "YNuzh0xWH44", "title": "demo"},
    )
    monkeypatch.setattr(
        service,
        "get_video_language_details",
        lambda *args, **kwargs: {
            "language": None,
            "confidence": 0.0,
            "scores": {},
            "signals": [],
        },
    )
    monkeypatch.setattr(
        service,
        "get_subtitle_strategy",
        lambda language, video_info, confidence, track_catalog=None: (False, []),
    )
    monkeypatch.setattr(
        service,
        "download_video",
        lambda url, platform=None: {
            "audio_file": None,
            "temp_dir": None,
            "error": "YouTube 音频下载失败：媒体流返回 HTTP 403",
        },
    )

    result = service._process_video_for_transcription_with_url(
        "https://www.youtube.com/watch?v=YNuzh0xWH44",
        "youtube",
    )

    assert result is not None
    assert result["audio_file"] is None
    assert result["download_error"] == "YouTube 音频下载失败：媒体流返回 HTTP 403"
    assert result["needs_transcription"] is True


def test_download_video_falls_back_to_dynamic_format_id(monkeypatch, tmp_path):
    service = VideoService()
    attempted_formats = []

    class DummyYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            return {
                "id": "test-video",
                "title": "demo",
                "formats": [
                    {
                        "format_id": "140",
                        "ext": "m4a",
                        "acodec": "aac",
                        "vcodec": "none",
                        "abr": 128,
                    }
                ],
            }

        def download(self, urls):
            attempted_formats.append(self.opts.get("format"))
            if self.opts.get("format") != "140":
                raise DownloadError("requested format is not available")

            output_path = self.opts["outtmpl"].replace("%(id)s", "test-video")
            output_path = output_path.replace("%(ext)s", "m4a")
            Path(output_path).write_bytes(b"dummy audio")

    monkeypatch.setattr("app.services.video_service.time.sleep", lambda *_: None)
    monkeypatch.setattr("app.services.video_service.yt_dlp.YoutubeDL", DummyYDL)
    monkeypatch.setattr(
        service,
        "_build_download_option_profiles",
        lambda temp_dir, platform, url: [
            {
                "desc": "测试 profile",
                "opts": {
                    "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
                    "quiet": True,
                    "no_warnings": True,
                },
            }
        ],
    )
    monkeypatch.setattr(service, "_convert_to_audio", lambda path, output_dir: path)

    result = service.download_video(
        "https://www.youtube.com/watch?v=test-video",
        output_folder=str(tmp_path),
        platform="youtube",
    )

    assert result is not None
    assert result["error"] is None
    assert result["audio_file"] is not None
    assert result["audio_file"].endswith("test-video.m4a")
    assert attempted_formats[:3] == [None, "bestaudio/best", "140"]


def test_download_video_skips_youtube_probe_by_default(monkeypatch, tmp_path):
    service = VideoService()
    attempted_formats = []
    extract_calls = []

    class DummyYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            extract_calls.append((url, download))
            raise AssertionError("YouTube 默认下载路径不应先额外探测信息")

        def download(self, urls):
            attempted_formats.append(self.opts.get("format"))
            if self.opts.get("format") != "140":
                raise DownloadError("requested format is not available")

            output_path = self.opts["outtmpl"].replace("%(id)s", "test-video")
            output_path = output_path.replace("%(ext)s", "m4a")
            Path(output_path).write_bytes(b"dummy audio")

    monkeypatch.setattr("app.services.video_service.time.sleep", lambda *_: None)
    monkeypatch.setattr("app.services.video_service.yt_dlp.YoutubeDL", DummyYDL)
    monkeypatch.setattr(
        service,
        "_build_download_option_profiles",
        lambda temp_dir, platform, url: [
            {
                "desc": "测试 profile",
                "opts": {
                    "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
                    "quiet": True,
                    "no_warnings": True,
                },
            }
        ],
    )
    monkeypatch.setattr(service, "_convert_to_audio", lambda path, output_dir: path)

    result = service.download_video(
        "https://www.youtube.com/watch?v=test-video",
        output_folder=str(tmp_path),
        platform="youtube",
    )

    assert result is not None
    assert result["error"] is None
    assert result["audio_file"] is not None
    assert result["audio_file"].endswith("test-video.m4a")
    assert extract_calls == []
    assert attempted_formats[:3] == [None, "bestaudio/best", "140"]


def test_summarize_download_errors_prefers_bot_and_challenge_signal():
    service = VideoService()

    message = service._summarize_download_errors(
        [
            "ERROR: [youtube] I966V5bxKQ0: Requested format is not available.",
            "ERROR: [youtube] I966V5bxKQ0: Sign in to confirm you're not a bot.",
            "[youtube] I966V5bxKQ0: n challenge solving failed: Some formats may be missing.",
        ]
    )

    assert "YouTube 要求登录验证或 bot 校验" in message
