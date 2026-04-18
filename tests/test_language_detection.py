from app.utils.language_detection import (
    clean_text_for_language_detection,
    detect_text_primary_language,
)


def test_clean_text_for_language_detection_removes_srt_indices_and_timestamps():
    srt_content = (
        "1\n"
        "00:00:00,000 --> 00:00:02,000\n"
        "这是第一句。\n\n"
        "2\n"
        "00:00:02,500 --> 00:00:04,500\n"
        "This is the second sentence.\n"
    )

    cleaned = clean_text_for_language_detection(srt_content)

    assert "-->" not in cleaned
    assert "00:00:00,000" not in cleaned
    assert "00:00:02,500" not in cleaned
    assert "1" not in cleaned
    assert "2" not in cleaned
    assert "这是第一句" in cleaned
    assert "This is the second sentence" in cleaned


def test_detect_text_primary_language_ignores_srt_timestamp_lines():
    srt_content = (
        "1\n"
        "00:00:00,000 --> 00:00:02,000\n"
        "这是第一句。\n\n"
        "2\n"
        "00:00:02,500 --> 00:00:06,500\n"
        "The european commission has just announced an agreement.\n"
    )

    detection = detect_text_primary_language(srt_content)

    assert detection["stats"]["chinese_chars"] > 0
    assert detection["stats"]["english_words"] > 0
    assert detection["cleaned_text"].count("00") == 0
