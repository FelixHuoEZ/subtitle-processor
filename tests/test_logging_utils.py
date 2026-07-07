from app.utils.logging_utils import (
    content_logging_enabled,
    summarize_payload,
    summarize_text,
)


def test_summarize_text_truncates_long_content(monkeypatch):
    monkeypatch.delenv("DEBUG_CONTENT_LOGGING", raising=False)
    text = "x" * 250

    summary = summarize_text(text, limit=20)

    assert summary == "len=250"

    monkeypatch.setenv("DEBUG_CONTENT_LOGGING", "true")
    debug_summary = summarize_text(text, limit=20)

    assert "len=250" in debug_summary
    assert "x" * 20 in debug_summary
    assert "x" * 40 not in debug_summary


def test_summarize_payload_uses_full_content_only_when_enabled(monkeypatch):
    payload = {"text": "abcdef" * 50, "other": "value"}
    monkeypatch.delenv("DEBUG_CONTENT_LOGGING", raising=False)

    summary = summarize_payload(payload, limit=12)

    assert "type=dict" in summary
    assert "text=len=300" in summary
    assert "abcdef" * 10 not in summary

    monkeypatch.setenv("DEBUG_CONTENT_LOGGING", "true")

    assert content_logging_enabled() is True
    assert "other" in summarize_payload(payload)
