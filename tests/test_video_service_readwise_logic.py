import json
from pathlib import Path

from app.services.video_service import VideoService


FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "regression_cases.json"
)


def load_regression_cases():
    with FIXTURE_PATH.open("r", encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def build_service(monkeypatch, *, readwise_url_only_when_zh_subs=False):
    monkeypatch.setenv(
        "READWISE_URL_ONLY_WHEN_ZH_SUBS",
        "true" if readwise_url_only_when_zh_subs else "false",
    )
    return VideoService()


def test_translated_tracks_do_not_count_as_original_zh_subtitles(monkeypatch):
    service = build_service(monkeypatch, readwise_url_only_when_zh_subs=True)
    info = {
        "automatic_captions": {
            "en-orig": [
                {
                    "ext": "json3",
                    "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=json3",
                    "name": "English (Original)",
                }
            ],
            "zh-Hans": [
                {
                    "ext": "json3",
                    "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=json3&tlang=zh-Hans",
                    "name": "Chinese (Simplified)",
                }
            ],
        }
    }

    track_catalog = service._build_track_catalog(info)
    translated_track = next(
        track for track in track_catalog if track["provider_language"] == "zh-Hans"
    )

    assert translated_track["track_type"] == "translated"
    assert translated_track["is_original_candidate"] is False
    assert translated_track["is_chinese_original_candidate"] is False
    assert service._should_clip_url_only(info, track_catalog=track_catalog) is False


def test_original_zh_tracks_still_trigger_url_only_when_flag_enabled(monkeypatch):
    service = build_service(monkeypatch, readwise_url_only_when_zh_subs=True)
    info = {
        "automatic_captions": {
            "zh-Hans": [
                {
                    "ext": "json3",
                    "url": "https://www.youtube.com/api/timedtext?lang=zh-Hans&fmt=json3",
                    "name": "Chinese (Simplified)",
                }
            ]
        }
    }

    track_catalog = service._build_track_catalog(info)

    assert service._should_clip_url_only(info, track_catalog=track_catalog) is True


def test_subtitle_text_can_override_incorrect_metadata_language(monkeypatch):
    service = build_service(monkeypatch)
    info = {
        "language": "en",
        "title": "中文讲解视频",
        "description": "这里是中文说明",
    }
    subtitle_result = {
        "content": "这是一个中文字幕样本。我们正在验证字幕正文可以纠正错误的元数据语言。"
        * 2,
        "track_type": "asr_original",
    }

    details = service.get_video_language_details(info, subtitle_result=subtitle_result)

    assert details["language"] == "zh"


def test_zh_locale_foreign_spoken_prefers_url_only_for_readwise(monkeypatch):
    service = build_service(monkeypatch)
    info = {
        "title": "再次改良英语：这能行吗？",
        "channel": "英语兔",
        "uploader": "英语兔",
        "automatic_captions": {
            "en-orig": [
                {
                    "ext": "json3",
                    "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=json3",
                    "name": "English (Original)",
                }
            ],
            "zh-Hans": [
                {
                    "ext": "json3",
                    "url": "https://www.youtube.com/api/timedtext?lang=en&fmt=json3&tlang=zh-Hans",
                    "name": "Chinese (Simplified)",
                }
            ],
        },
    }
    track_catalog = service._build_track_catalog(info)
    language_details = service.get_video_language_details(
        info,
        subtitle_result={
            "content": (
                "The European Commission has just announced an agreement whereby "
                "English will be the official language of the EU. "
            )
            * 5,
            "track_type": "asr_original",
        },
    )
    content_locale_details = service.get_content_locale_details(
        info, language_details=language_details
    )

    decision = service._build_readwise_decision(
        track_catalog, language_details, content_locale_details
    )

    assert content_locale_details["language"] == "zh"
    assert language_details["language"] == "en"
    assert decision["mode"] == "url_only"
    assert decision["reason"] == "zh_locale_foreign_spoken"
    assert decision["skip_processing"] is False


def test_regression_case_manifest_contains_approved_youtube_url():
    regression_cases = load_regression_cases()
    case = next(
        regression_case
        for regression_case in regression_cases
        if regression_case["case_id"] == "youtube_7R9H_EX6cnI"
    )

    assert case["url"] == "https://www.youtube.com/watch?v=7R9H-EX6cnI"
    assert case["expected"]["content_locale"] == "zh"
    assert case["expected"]["readwise_mode"] == "url_only"
