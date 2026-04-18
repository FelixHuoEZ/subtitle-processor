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


def test_get_video_language_details_preserves_significant_zh_evidence_in_final_srt():
    service = VideoService()
    info = {
        "language": None,
        "title": "再次改良英语：这能行吗？",
        "description": "",
        "channel": "英语兔",
        "uploader": "英语兔",
        "subtitles": [],
        "automatic_captions": [],
    }
    subtitle_result = {
        "track_type": "asr_original",
        "content": (
            "1\n"
            "00:00:00,000 --> 00:00:10,000\n"
            "上一期视频中的英语兔正字法推出后，许多小伙伴评论太复杂了。其实有更简单的。"
            "以下是英语兔，我之前看到的一则关于英语正字法的笑话，咱们一起通读一遍，"
            "看看这个简化版的正字法能否让你会心一笑。\n\n"
            "2\n"
            "00:00:10,100 --> 00:01:40,000\n"
            "The european commission has just announced and agreement我要把english "
            "will be the official language of the EU rather than german, which was "
            "the other possibility as part of the negotiations. A magistate. Jity's "
            "government conceded that english spelling had some room for improvement "
            "and has accepted a five year phasing plan that would be known as esual "
            "english in the first year s will replace the soft sea. Certainly, this "
            "will make the civil seventh jump with joy. The hearsey will be dropped "
            "in favor of the k. This should clear up, confusion and keyboards can "
            "have one less letter they will be. There will be growing public "
            "enthusiasm in the second year when that trouble, some PH will be "
            "replaced with f. This will make words like photograph twenty percent "
            "shorter in the thirdia public acceptance of the new spelling can be "
            "expected to reach the stage where more complicated changes are possible. "
            "Governments will encourage the removal of double letters, which have "
            "always been a deterrent to accurate spelling. Also, gal will agree that "
            "horrible miss of the silenties in the language is disgraceful, and they "
            "should go away. But the falth er people will be receptive to steps, "
            "such as replacing TH with z and w with v. During the fifth year. The "
            "unnecessary old can be dropped from words containing oil, and similar "
            "changes would, of course, be applied to other. Other combinations of "
            "letters after this fifth year ah we will have a heavly sensible "
            "hhiinside. There will be no more toouble or difficulties, and everyone "
            "feel finit easy to understand each other than the team is finally "
            "called true.\n\n"
            "3\n"
            "00:01:40,100 --> 00:01:47,000\n"
            "不知道你觉得以上的正字法如何英语图，我的兄弟德语图对此表示了高度赞扬。"
            "但英语图我还是觉得我的英语图正字法更好。如果你还没看过，可以去看一眼哟。\n"
        ),
    }
    audio_result = {
        "language": "en",
        "confidence": 0.9067,
    }

    details = service.get_video_language_details(
        info,
        subtitle_result=subtitle_result,
        audio_result=audio_result,
    )

    subtitle_signals = [
        signal
        for signal in details["signals"]
        if signal["source"] == "subtitle.text.asr_original"
    ]

    assert details["language"] == "en"
    assert details["confidence"] < 0.8
    assert details["scores"]["zh"] > 0.45
    assert any(signal["language"] == "zh" for signal in subtitle_signals)
    assert any(signal.get("secondary_zh_boost") for signal in subtitle_signals)
    assert subtitle_signals[0]["stats"]["chinese_chars"] >= 100


def test_get_subtitle_strategy_skips_exclusive_auto_caption_when_primary_language_is_uncertain():
    service = VideoService()
    info = {
        "subtitles": ["zh-CN"],
        "automatic_captions": ["en"],
    }

    should_download, lang_priority = service.get_subtitle_strategy("mixed", info)

    assert should_download is False
    assert lang_priority == []
