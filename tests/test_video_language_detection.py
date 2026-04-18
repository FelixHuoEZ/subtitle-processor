import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.video_service import VideoService


def test_get_video_language_details_prefers_metadata_language():
    service = VideoService()
    info = {
        "language": "en-US",
        "title": "这是一个中文标题",
        "description": "但视频实际元数据标记为英文",
        "subtitles": [],
        "automatic_captions": [],
    }

    details = service.get_video_language_details(info)

    assert details["language"] == "en"
    assert details["confidence"] >= 0.6


def test_get_video_language_details_ignores_subtitle_signals():
    service = VideoService()
    info = {
        "language": None,
        "title": "这是一个误导性的中文标题",
        "description": "",
        "subtitles": ["en"],
        "automatic_captions": ["en"],
    }
    subtitle_result = {
        "content": "Hello everyone and welcome back to the channel.",
        "format": "srt",
        "matched_lang": "en",
        "source_type": "automatic_caption",
    }

    details = service.get_video_language_details(info, subtitle_result=subtitle_result)

    assert details["language"] is None
    assert details["scores"]["zh"] > 0
    assert details["scores"]["en"] == 0.0


def test_get_video_language_details_uses_audio_probe():
    service = VideoService()
    info = {
        "language": None,
        "title": "这是一个中文标题",
        "description": "",
        "subtitles": [],
        "automatic_captions": [],
    }
    audio_result = {
        "language": "en",
        "confidence": 0.92,
    }

    details = service.get_video_language_details(info, audio_result=audio_result)

    assert details["language"] == "en"
    assert details["confidence"] >= 0.7


def test_get_subtitle_strategy_skips_exclusive_auto_caption_when_primary_language_is_uncertain():
    service = VideoService()
    info = {
        "subtitles": ["zh-CN"],
        "automatic_captions": ["en"],
    }

    should_download, lang_priority = service.get_subtitle_strategy("mixed", info)

    assert should_download is False
    assert lang_priority == []
