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


def test_reader_parse_check_detects_youtube_subtitle_failure(monkeypatch):
    service = ReadwiseService()
    service.enabled = True
    captured = {}

    def fake_make_request(method, endpoint, data=None, params=None):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["params"] = params
        return {
            "results": [
                {
                    "id": "reader-id",
                    "category": "video",
                    "word_count": None,
                    "html_content": (
                        "<p>Unfortunately, Youtube does not provide subtitles "
                        "for this video</p>"
                    ),
                }
            ]
        }

    monkeypatch.setattr(service, "_make_request", fake_make_request)

    result = service.check_reader_parse_result("reader-id")

    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/list/"
    assert captured["params"] == {"id": "reader-id", "withHtmlContent": "true"}
    assert result["status"] == "failed"
    assert result["reason"] == "youtube_subtitles_unavailable"


def test_reader_parse_check_accepts_reader_content(monkeypatch):
    service = ReadwiseService()
    service.enabled = True

    def fake_make_request(method, endpoint, data=None, params=None):
        return {
            "results": [
                {
                    "id": "reader-id",
                    "category": "video",
                    "word_count": 420,
                    "html_content": "<p>这是已经解析出的正文内容。</p>",
                }
            ]
        }

    monkeypatch.setattr(service, "_make_request", fake_make_request)

    result = service.check_reader_parse_result("reader-id")

    assert result["status"] == "ok"
    assert result["reason"] == "reader_content_available"


def test_reader_parse_check_treats_missing_document_as_pending(monkeypatch):
    service = ReadwiseService()
    service.enabled = True

    monkeypatch.setattr(
        service,
        "_make_request",
        lambda method, endpoint, data=None, params=None: {"results": []},
    )

    result = service.check_reader_parse_result("reader-id")

    assert result["status"] == "pending"
    assert result["reason"] == "document_not_ready"
