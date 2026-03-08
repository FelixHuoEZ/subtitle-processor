"""Video URL helpers."""

from typing import Optional
from urllib.parse import parse_qs, urlparse


def extract_youtube_video_id(url: Optional[str]) -> Optional[str]:
    """Extract a YouTube video ID from common YouTube URL formats."""
    if not url:
        return None

    raw_url = str(url).strip()
    if not raw_url:
        return None

    candidate_url = raw_url if "://" in raw_url else f"https://{raw_url}"
    parsed = urlparse(candidate_url)
    host = (parsed.netloc or "").lower()
    path_parts = [part for part in (parsed.path or "").split("/") if part]

    if host == "youtu.be":
        return path_parts[0] if path_parts else None

    is_youtube_host = host == "youtube.com" or host.endswith(".youtube.com")
    if not is_youtube_host:
        return None

    query = parse_qs(parsed.query or "")
    watch_video_ids = query.get("v")
    if watch_video_ids:
        return watch_video_ids[0]

    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "live", "embed", "v"}:
        return path_parts[1]

    return None


def normalize_youtube_watch_url(url: Optional[str]) -> Optional[str]:
    """Normalize a YouTube URL to the canonical watch URL when possible."""
    video_id = extract_youtube_video_id(url)
    if not video_id:
        return None
    return f"https://www.youtube.com/watch?v={video_id}"
