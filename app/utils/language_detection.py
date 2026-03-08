"""Helpers for coarse primary-language detection."""

import html
import re
from typing import Any, Dict, Optional

SUPPORTED_PRIMARY_LANGUAGES = ("zh", "en")

_TIMESTAMP_RE = re.compile(
    r"\b\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?\b|\b\d+\b"
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_EN_LETTER_RE = re.compile(r"[A-Za-z]")
_EN_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def normalize_primary_language(language: Optional[str]) -> Optional[str]:
    """Normalize a language string to the primary code used by the app."""
    if language is None:
        return None

    normalized = str(language).strip().lower().replace("_", "-")
    if not normalized:
        return None
    if normalized.startswith("zh"):
        return "zh"
    if normalized.startswith("en"):
        return "en"
    if normalized in {"mixed", "unknown"}:
        return normalized
    if re.match(r"^[a-z]{2}(?:-[a-z0-9]+)?$", normalized):
        return normalized.split("-", 1)[0]
    return None


def blank_language_scores() -> Dict[str, float]:
    """Create a blank score bucket for supported primary languages."""
    return {language: 0.0 for language in SUPPORTED_PRIMARY_LANGUAGES}


def clean_text_for_language_detection(text: str) -> str:
    """Strip common subtitle markup and timestamps before text detection."""
    if not text:
        return ""

    cleaned_lines = []
    for raw_line in html.unescape(str(text)).replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper == "WEBVTT" or upper.startswith("NOTE"):
            continue
        if "-->" in line:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        line = _HTML_TAG_RE.sub(" ", line)
        line = re.sub(r"\{\\[^}]+\}", " ", line)
        line = re.sub(r"\[[^\]]+\]", " ", line)
        line = _TIMESTAMP_RE.sub(" ", line)
        line = re.sub(r"[^\w\u4e00-\u9fff'\s-]", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            cleaned_lines.append(line)

    return " ".join(cleaned_lines).strip()


def detect_text_primary_language(text: str, min_units: int = 12) -> Dict[str, Any]:
    """Detect whether a text sample is mainly Chinese or English."""
    cleaned = clean_text_for_language_detection(text)
    chinese_chars = len(_CJK_RE.findall(cleaned))
    english_letters = len(_EN_LETTER_RE.findall(cleaned))
    english_words = len(_EN_WORD_RE.findall(cleaned))

    # Balance English letter-heavy text against Chinese character-heavy text.
    zh_units = float(chinese_chars * 2)
    en_units = float(english_letters + english_words)
    total_units = zh_units + en_units

    result = {
        "language": None,
        "confidence": 0.0,
        "cleaned_text": cleaned,
        "scores": {"zh": zh_units, "en": en_units},
        "stats": {
            "chinese_chars": chinese_chars,
            "english_letters": english_letters,
            "english_words": english_words,
            "total_units": total_units,
        },
    }

    if total_units < float(min_units):
        return result

    zh_ratio = zh_units / total_units
    en_ratio = en_units / total_units
    confidence = max(zh_ratio, en_ratio)

    result["confidence"] = round(confidence, 4)
    result["stats"]["zh_ratio"] = round(zh_ratio, 4)
    result["stats"]["en_ratio"] = round(en_ratio, 4)

    if zh_ratio >= 0.62 and zh_ratio - en_ratio >= 0.18:
        result["language"] = "zh"
    elif en_ratio >= 0.62 and en_ratio - zh_ratio >= 0.18:
        result["language"] = "en"
    elif zh_ratio >= 0.3 and en_ratio >= 0.3:
        result["language"] = "mixed"

    return result


def add_language_score(
    scores: Dict[str, float], language: Optional[str], weight: float
) -> None:
    """Accumulate score for a supported primary language."""
    normalized = normalize_primary_language(language)
    if normalized not in SUPPORTED_PRIMARY_LANGUAGES:
        return
    if weight <= 0:
        return
    scores[normalized] = round(scores.get(normalized, 0.0) + float(weight), 6)


def decide_primary_language(
    scores: Dict[str, float],
    min_total: float = 0.25,
    min_margin: float = 0.18,
    min_confidence: float = 0.62,
) -> Dict[str, Any]:
    """Choose the primary language from accumulated scores."""
    normalized_scores = {
        language: round(float(scores.get(language, 0.0)), 6)
        for language in SUPPORTED_PRIMARY_LANGUAGES
    }
    total = round(sum(normalized_scores.values()), 6)
    result = {
        "language": None,
        "confidence": 0.0,
        "scores": normalized_scores,
        "total_score": total,
        "margin": 0.0,
    }

    if total < min_total:
        return result

    best_language, best_score = max(
        normalized_scores.items(), key=lambda item: item[1]
    )
    other_score = total - best_score
    confidence = best_score / total if total else 0.0
    margin = best_score - other_score

    result["confidence"] = round(confidence, 4)
    result["margin"] = round(margin, 4)

    if other_score >= min_total and margin < min_margin:
        result["language"] = "mixed"
        return result

    if confidence >= min_confidence and margin >= min_margin:
        result["language"] = best_language
    else:
        result["language"] = "mixed"

    return result
