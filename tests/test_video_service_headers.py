import copy

from app.services.video_service import VideoService


def _make_service_with_opts(opts):
    service = VideoService.__new__(VideoService)
    service.yt_dlp_opts = copy.deepcopy(opts)
    return service


def test_get_platform_headers_bilibili():
    service = VideoService.__new__(VideoService)
    headers = service._get_platform_headers(
        "bilibili", "https://www.bilibili.com/video/BV1xx411c7mD"
    )
    assert headers["Origin"] == "https://www.bilibili.com"
    assert headers["Referer"] == "https://www.bilibili.com/"


def test_get_platform_headers_fallback_url():
    service = VideoService.__new__(VideoService)
    headers = service._get_platform_headers(None, "https://example.com/foo")
    assert headers == {
        "Origin": "https://example.com",
        "Referer": "https://example.com/",
    }


def test_get_yt_dlp_opts_for_platform_overrides_headers():
    service = _make_service_with_opts(
        {"http_headers": {"Referer": "https://www.youtube.com/"}}
    )
    opts = service._get_yt_dlp_opts_for_platform(
        "bilibili", "https://www.bilibili.com/video/BV1xx411c7mD"
    )
    assert opts["http_headers"]["Referer"] == "https://www.bilibili.com/"
    assert service.yt_dlp_opts["http_headers"]["Referer"] == "https://www.youtube.com/"


def test_build_download_base_opts_preserves_cookiefile():
    service = _make_service_with_opts(
        {
            "cookiefile": "/tmp/youtube.cookies.txt",
            "http_headers": {"Referer": "https://www.youtube.com/"},
            "extractor_args": {"youtube": {"player_client": ["tv"]}},
        }
    )

    opts = service._build_download_base_opts(
        "/tmp/download-task",
        "youtube",
        "https://www.youtube.com/watch?v=YNuzh0xWH44",
    )

    assert opts["cookiefile"] == "/tmp/youtube.cookies.txt"
    assert opts["outtmpl"] == "/tmp/download-task/%(id)s.%(ext)s"
    assert opts["http_headers"]["Accept"] == "*/*"
    assert opts["http_headers"]["Accept-Language"] == "en-US,en;q=0.5"
    assert opts["extractor_args"]["youtube"]["player_client"] == ["tv"]


def test_build_public_youtube_download_opts_strips_cookie_and_extractor_args():
    service = _make_service_with_opts(
        {
            "cookiefile": "/tmp/youtube.cookies.txt",
            "cookiesfrombrowser": ("firefox", "/tmp/profile"),
            "extractor_args": {"youtube": {"player_client": ["tv"]}},
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
    )

    opts = service._build_public_youtube_download_opts("/tmp/download-task")

    assert opts["outtmpl"] == "/tmp/download-task/%(id)s.%(ext)s"
    assert "cookiefile" not in opts
    assert "cookiesfrombrowser" not in opts
    assert "extractor_args" not in opts
    assert opts["quiet"] is True
    assert opts["noplaylist"] is True


def test_build_download_option_profiles_prioritizes_public_youtube_path():
    service = _make_service_with_opts(
        {
            "cookiefile": "/tmp/youtube.cookies.txt",
            "extractor_args": {"youtube": {"player_client": ["tv"]}},
            "quiet": True,
        }
    )

    profiles = service._build_download_option_profiles(
        "/tmp/download-task",
        "youtube",
        "https://www.youtube.com/watch?v=YNuzh0xWH44",
    )

    assert [profile["desc"] for profile in profiles] == [
        "默认公开视频参数",
        "登录态/兼容参数",
    ]
    assert "cookiefile" not in profiles[0]["opts"]
    assert "extractor_args" not in profiles[0]["opts"]
    assert profiles[1]["opts"]["cookiefile"] == "/tmp/youtube.cookies.txt"
