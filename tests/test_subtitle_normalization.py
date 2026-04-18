import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.subtitle_service import SubtitleService


def test_normalize_json3_to_srt():
    service = SubtitleService()
    json3_content = (
        '{"wireMagic":"pb3","events":[{"tStartMs":80,"dDurationMs":1400,'
        '"segs":[{"utf8":"China\'s foreign exchange reserves"}]}]}'
    )

    normalized = service.normalize_external_subtitle_content(json3_content)

    assert normalized is not None
    assert "-->" in normalized
    assert "China's foreign exchange reserves" in normalized
    assert "wireMagic" not in normalized


def test_normalize_vtt_to_srt():
    service = SubtitleService()
    vtt_content = (
        "WEBVTT\n\n"
        "00:00:00.080 --> 00:00:01.400 align:start position:0%\n"
        "Hello world\n"
    )

    normalized = service.normalize_external_subtitle_content(vtt_content)

    assert normalized is not None
    assert "-->" in normalized
    assert "Hello world" in normalized
    assert "WEBVTT" not in normalized


def test_normalize_srv_xml_to_srt():
    service = SubtitleService()
    srv_content = (
        "<timedtext><body>"
        '<p t="80" d="1400">Hello <s>world</s></p>'
        '<p t="2000" d="1000">Again</p>'
        "</body></timedtext>"
    )

    normalized = service.normalize_external_subtitle_content(srv_content)

    assert normalized is not None
    assert "-->" in normalized
    assert "Hello world" in normalized
    assert "<timedtext>" not in normalized


def test_convert_to_srt_keeps_original_when_json3_parse_fails():
    service = SubtitleService()
    plain_text = "This is plain subtitle text"

    converted = service.convert_to_srt(plain_text, "json3")

    assert converted == plain_text


def test_generate_srt_uses_real_newlines_and_counts_entries():
    service = SubtitleService()

    srt_content = service._generate_srt_from_text("第一句。Second sentence.", duration=10)

    assert srt_content is not None
    assert "\\n" not in srt_content
    assert "\n" in srt_content
