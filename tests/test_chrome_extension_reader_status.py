import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_extension_exposes_short_reader_status_labels_and_reprocess_action():
    content_script = (
        ROOT / "chrome-extension" / "youtube-content.js"
    ).read_text(encoding="utf-8")

    assert "检查中…" in content_script
    assert "已剪藏 ↗" in content_script
    assert "button.textContent = '剪藏'" in content_script
    assert "状态未知" in content_script
    assert "重新处理" in content_script
    assert "CHECK_YOUTUBE_READER_STATUS" in content_script
    assert "reader_index_warming" in content_script
    assert "READER_STATUS_MAX_WARMING_RETRIES = 6" in content_script
    assert "response.readwise_fallback_url" in content_script
    assert "response.readwise_url_only_url" in content_script


def test_extension_background_uses_server_side_reader_status_endpoint():
    background = (ROOT / "chrome-extension" / "background.js").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(
        (ROOT / "chrome-extension" / "manifest.json").read_text(encoding="utf-8")
    )

    assert "/process/reader-status/youtube/" in background
    assert "buildAccessHeaders(settings)" in background
    assert "readwiseToken" not in background
    assert manifest["version"] == "1.5"
