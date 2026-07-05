from app.services.readwise_service import ReadwiseService


def test_url_only_clip_uses_original_youtube_url_without_subtitle(monkeypatch):
    service = ReadwiseService()
    service.enabled = True
    captured = {}

    def fake_create_article_from_url(**kwargs):
        captured.update(kwargs)
        return {"id": "reader-id", "url": "https://read.readwise.io/read/reader-id"}

    monkeypatch.setattr(service, "create_article_from_url", fake_create_article_from_url)

    result = service.create_article_from_subtitle(
        {
            "video_info": {
                "title": "NASA的400億美元計劃：拍清系外行星",
                "webpage_url": "https://www.youtube.com/watch?v=ywTGRxIOhI0",
                "uploader": "科學視界面",
            },
            "subtitle_content": None,
            "readwise_mode": "url_only",
            "readwise_reason": "original_zh_track_available",
            "tags": ["youtube"],
        }
    )

    assert result["url"] == "https://read.readwise.io/read/reader-id"
    assert captured["url"] == "https://www.youtube.com/watch?v=ywTGRxIOhI0"
    assert captured["title"] == "NASA的400億美元計劃：拍清系外行星"
    assert captured["author"] == "科學視界面"
    assert captured["tags"] == ["youtube"]


def test_readwise_save_response_gets_reader_url_fallback():
    response = ReadwiseService._ensure_reader_url({"id": "reader-id"})

    assert response["url"] == "https://read.readwise.io/read/reader-id"
