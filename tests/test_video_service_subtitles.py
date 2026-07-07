import os
import sys
import json
import time
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


def test_force_local_processing_bypasses_url_only_short_circuit(monkeypatch):
    service = VideoService()
    service.readwise_url_only_when_zh_subs = True
    video_info = {
        "id": "demo",
        "title": "中文视频",
        "webpage_url": "https://www.youtube.com/watch?v=demo",
        "subtitles": {},
        "automatic_captions": {
            "zh-Hans": [
                {
                    "ext": "json3",
                    "url": "https://example.com/subtitle.json3",
                    "name": "Chinese (Simplified)",
                }
            ]
        },
    }

    monkeypatch.setattr(service, "get_video_info", lambda url, platform: video_info)
    monkeypatch.setattr(
        service,
        "get_video_language_details",
        lambda *args, **kwargs: {"language": "zh", "confidence": 0.95},
    )
    monkeypatch.setattr(
        service,
        "get_content_locale_details",
        lambda *args, **kwargs: {"language": "zh", "confidence": 0.95},
    )
    monkeypatch.setattr(
        service,
        "get_subtitle_strategy",
        lambda *args, **kwargs: (True, ["zh-Hans"]),
    )
    monkeypatch.setattr(
        service,
        "download_subtitles",
        lambda *args, **kwargs: {"content": "本地字幕内容", "track_type": "asr_original"},
    )
    monkeypatch.setattr(
        service,
        "download_video",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("force-local should use subtitles instead of audio download")
        ),
    )

    result = service._process_video_for_transcription_with_url(
        "https://www.youtube.com/watch?v=demo",
        "youtube",
        force_local_processing=True,
    )

    assert result["subtitle_content"] == "本地字幕内容"
    assert result["needs_transcription"] is False
    assert result["readwise_url_only"] is True


def test_youtube_info_preserves_caption_maps_for_url_only(monkeypatch):
    service = VideoService()
    service.readwise_url_only_when_zh_subs = True

    monkeypatch.setattr("app.services.video_service.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        service,
        "_extract_youtube_info",
        lambda url: {
            "id": "ywTGRxIOhI0",
            "title": "NASA的400億美元計劃：拍清系外行星",
            "webpage_url": url,
            "availability": "public",
            "age_limit": 0,
            "subtitles": {},
            "automatic_captions": {
                "zh-Hans": [
                    {
                        "ext": "json3",
                        "url": "https://www.youtube.com/api/timedtext?lang=zh-Hans&fmt=json3",
                        "name": "Chinese (Simplified)",
                    }
                ]
            },
        },
    )

    info = service.get_youtube_info("https://www.youtube.com/watch?v=ywTGRxIOhI0")
    track_catalog = service._build_track_catalog(info)

    assert isinstance(info["automatic_captions"], dict)
    assert info["automatic_caption_languages"] == ["zh-Hans"]
    assert track_catalog[0]["track_type"] == "asr_original"
    assert track_catalog[0]["is_chinese_original_candidate"] is True
    assert service._should_clip_url_only(info, track_catalog=track_catalog) is True


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


def test_download_video_caches_audio_and_reuses(monkeypatch, tmp_path):
    monkeypatch.setenv("DOWNLOAD_ASSET_CACHE_ENABLED", "true")
    monkeypatch.setenv("DOWNLOAD_ASSET_CACHE_DIR", str(tmp_path / "asset-cache"))
    monkeypatch.setenv("DOWNLOAD_ASSET_CACHE_TTL_DAYS", "30")
    monkeypatch.setenv("DOWNLOAD_ASSET_CACHE_STORE_SOURCE", "true")

    service = VideoService()
    download_calls = []

    class DummyYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def download(self, urls):
            download_calls.append(list(urls))
            if self.opts.get("format") not in {None, "140"}:
                raise DownloadError("requested format is not available")
            output_path = self.opts["outtmpl"].replace("%(id)s", "test-video")
            output_path = output_path.replace("%(ext)s", "m4a")
            Path(output_path).write_bytes(b"source audio")

    def fake_convert(path, output_dir):
        audio_path = Path(output_dir) / "test-video.wav"
        audio_path.write_bytes(b"converted audio")
        return str(audio_path)

    monkeypatch.setattr("app.services.video_service.time.sleep", lambda *_: None)
    monkeypatch.setattr("app.services.video_service.yt_dlp.YoutubeDL", DummyYDL)
    monkeypatch.setattr(service, "_convert_to_audio", fake_convert)

    first = service.download_video(
        "https://youtu.be/test-video",
        output_folder=str(tmp_path / "tmp"),
        platform="youtube",
    )

    assert first is not None
    assert first["cache_hit"] is False
    assert first["audio_file"].startswith(str(tmp_path / "asset-cache"))
    assert Path(first["audio_file"]).read_bytes() == b"converted audio"
    assert len(download_calls) == 1

    index = json.loads((tmp_path / "asset-cache" / "index.json").read_text())
    entry = next(iter(index.values()))
    assert Path(entry["audio_path"]).exists()
    assert Path(entry["source_media_path"]).exists()

    second = service.download_video(
        "https://www.youtube.com/watch?v=test-video&feature=share",
        output_folder=str(tmp_path / "tmp2"),
        platform="youtube",
    )

    assert second is not None
    assert second["cache_hit"] is True
    assert second["audio_file"] == first["audio_file"]
    assert len(download_calls) == 1


def test_download_asset_cache_ignores_expired_entry(monkeypatch, tmp_path):
    monkeypatch.setenv("DOWNLOAD_ASSET_CACHE_ENABLED", "true")
    monkeypatch.setenv("DOWNLOAD_ASSET_CACHE_DIR", str(tmp_path / "asset-cache"))
    monkeypatch.setenv("DOWNLOAD_ASSET_CACHE_TTL_DAYS", "1")

    service = VideoService()
    identity = service._build_asset_cache_identity(
        "https://www.youtube.com/watch?v=test-video",
        "youtube",
    )
    cache_dir = tmp_path / "asset-cache" / identity["cache_key"]
    cache_dir.mkdir(parents=True)
    audio_path = cache_dir / "test-video.audio.wav"
    audio_path.write_bytes(b"cached")
    old_epoch = time.time() - (2 * 24 * 60 * 60)
    (tmp_path / "asset-cache" / "index.json").write_text(
        json.dumps(
            {
                identity["cache_key"]: {
                    **identity,
                    "audio_path": str(audio_path),
                    "created_at_epoch": old_epoch,
                }
            }
        ),
        encoding="utf-8",
    )

    cached = service._get_cached_download_asset(
        "https://www.youtube.com/watch?v=test-video",
        "youtube",
    )

    assert cached is None


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
