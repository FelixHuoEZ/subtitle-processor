"""Helpers for logging generated text without dumping full content by default."""

import json
import os
from typing import Any


def content_logging_enabled() -> bool:
    raw = os.getenv("DEBUG_CONTENT_LOGGING") or os.getenv("LOG_FULL_CONTENT")
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def preview_text(value: Any, limit: int = 200) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def summarize_text(value: Any, limit: int = 200) -> str:
    text = "" if value is None else str(value)
    if content_logging_enabled():
        return f"len={len(text)} preview={preview_text(text, limit)!r}"
    return f"len={len(text)}"


def summarize_payload(value: Any, limit: int = 200) -> str:
    if content_logging_enabled():
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)

    if isinstance(value, dict):
        keys = list(value.keys())
        return f"type=dict keys={keys} text={summarize_text(value.get('text'), limit)}"
    if isinstance(value, list):
        return f"type=list items={len(value)}"
    return summarize_text(value, limit)
