from types import SimpleNamespace

import pytest
from flask import Flask, jsonify

from app.services.access_auth_service import configure_access_auth


AUTH_ENV_KEYS = (
    "ACCESS_AUTH_ENABLED",
    "ACCESS_TEAM_DOMAIN",
    "ACCESS_WEB_APPLICATION_AUD",
    "ACCESS_API_APPLICATION_AUD",
    "ACCESS_ALLOWED_ORIGINS",
    "ACCESS_JWT_LEEWAY_SECONDS",
    "INTERNAL_SERVICE_TOKEN",
)


def _clear_auth_env(monkeypatch):
    for key in AUTH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _enable_auth(monkeypatch):
    monkeypatch.setenv("ACCESS_AUTH_ENABLED", "true")
    monkeypatch.setenv(
        "ACCESS_TEAM_DOMAIN", "https://example.cloudflareaccess.com/"
    )
    monkeypatch.setenv("ACCESS_WEB_APPLICATION_AUD", "web-aud")
    monkeypatch.setenv("ACCESS_API_APPLICATION_AUD", "api-aud")
    monkeypatch.setenv("ACCESS_ALLOWED_ORIGINS", "https://readwise.gauss.surf")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "internal-secret")


def _make_app():
    app = Flask(__name__)
    configure_access_auth(app)

    @app.get("/private")
    def private_route():
        return jsonify({"ok": True})

    @app.post("/private")
    def private_post():
        return jsonify({"ok": True})

    @app.get("/health")
    def health():
        return jsonify({"status": "healthy"})

    return app


def test_auth_is_backward_compatible_when_disabled(monkeypatch):
    _clear_auth_env(monkeypatch)
    client = _make_app().test_client()

    assert client.get("/private").status_code == 200
    assert client.get("/health").status_code == 200


def test_enabled_auth_requires_all_source_verification_settings(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("ACCESS_AUTH_ENABLED", "true")

    with pytest.raises(RuntimeError, match="ACCESS_TEAM_DOMAIN"):
        _make_app()


def test_internal_token_authenticates_and_health_stays_public(monkeypatch):
    _clear_auth_env(monkeypatch)
    _enable_auth(monkeypatch)
    client = _make_app().test_client()

    assert client.get("/private").status_code == 401
    assert client.get("/health").status_code == 200
    response = client.get(
        "/private", headers={"Authorization": "Bearer internal-secret"}
    )
    assert response.status_code == 200


def test_cloudflare_assertion_validates_issuer_and_any_configured_audience(
    monkeypatch,
):
    _clear_auth_env(monkeypatch)
    _enable_auth(monkeypatch)
    decode_calls = []

    class FakeJWKClient:
        def __init__(self, url, cache_keys):
            assert url == (
                "https://example.cloudflareaccess.com/cdn-cgi/access/certs"
            )
            assert cache_keys is True

        def get_signing_key_from_jwt(self, assertion):
            assert assertion == "signed-access-token"
            return SimpleNamespace(key="public-key")

    def fake_decode(assertion, key, **kwargs):
        decode_calls.append((assertion, key, kwargs))
        return {
            "sub": "subject",
            "email": "user@example.com",
            "aud": ["web-aud"],
        }

    monkeypatch.setattr(
        "app.services.access_auth_service.jwt.PyJWKClient", FakeJWKClient
    )
    monkeypatch.setattr("app.services.access_auth_service.jwt.decode", fake_decode)
    client = _make_app().test_client()

    response = client.get(
        "/private", headers={"Cf-Access-Jwt-Assertion": "signed-access-token"}
    )

    assert response.status_code == 200
    assert decode_calls[0][2]["audience"] == ["web-aud", "api-aud"]
    assert decode_calls[0][2]["issuer"] == (
        "https://example.cloudflareaccess.com"
    )
    assert decode_calls[0][2]["leeway"] == 60


def test_cloudflare_assertion_leeway_is_configurable_and_bounded(monkeypatch):
    _clear_auth_env(monkeypatch)
    _enable_auth(monkeypatch)
    monkeypatch.setenv("ACCESS_JWT_LEEWAY_SECONDS", "999")

    class FakeJWKClient:
        def __init__(self, url, cache_keys):
            pass

        def get_signing_key_from_jwt(self, assertion):
            return SimpleNamespace(key="public-key")

    decode_calls = []
    monkeypatch.setattr(
        "app.services.access_auth_service.jwt.PyJWKClient", FakeJWKClient
    )
    monkeypatch.setattr(
        "app.services.access_auth_service.jwt.decode",
        lambda *args, **kwargs: decode_calls.append(kwargs) or {"aud": ["web-aud"]},
    )

    response = _make_app().test_client().get(
        "/private", headers={"Cf-Access-Jwt-Assertion": "signed-access-token"}
    )

    assert response.status_code == 200
    assert decode_calls[0]["leeway"] == 300


def test_api_audience_is_limited_to_extension_endpoints(monkeypatch):
    _clear_auth_env(monkeypatch)
    _enable_auth(monkeypatch)

    class FakeJWKClient:
        def __init__(self, url, cache_keys):
            pass

        def get_signing_key_from_jwt(self, assertion):
            return SimpleNamespace(key="public-key")

    monkeypatch.setattr(
        "app.services.access_auth_service.jwt.PyJWKClient", FakeJWKClient
    )
    monkeypatch.setattr(
        "app.services.access_auth_service.jwt.decode",
        lambda *args, **kwargs: {"aud": ["api-aud"]},
    )
    app = Flask(__name__)
    configure_access_auth(app)

    @app.post("/process")
    def submit():
        return jsonify({"ok": True})

    @app.get("/process/status/<task_id>")
    def status(task_id):
        return jsonify({"id": task_id})

    @app.get("/health/metrics")
    def metrics():
        return jsonify({"private": True})

    client = app.test_client()
    headers = {"Cf-Access-Jwt-Assertion": "signed-access-token"}

    assert client.post("/process", headers=headers).status_code == 200
    assert client.post(
        "/process",
        headers={**headers, "Origin": "chrome-extension://dynamic-id"},
    ).status_code == 200
    assert client.get("/process/status/task-1", headers=headers).status_code == 200
    assert client.get("/health/metrics", headers=headers).status_code == 403


def test_cors_preflight_only_allows_configured_origin(monkeypatch):
    _clear_auth_env(monkeypatch)
    _enable_auth(monkeypatch)
    client = _make_app().test_client()

    allowed = client.options(
        "/private",
        headers={
            "Origin": "https://readwise.gauss.surf",
            "Access-Control-Request-Method": "POST",
        },
    )
    blocked = client.options(
        "/private",
        headers={"Origin": "https://attacker.example"},
    )

    assert allowed.status_code == 204
    assert allowed.headers["Access-Control-Allow-Origin"] == (
        "https://readwise.gauss.surf"
    )
    assert "CF-Access-Client-Id" in allowed.headers[
        "Access-Control-Allow-Headers"
    ]
    assert blocked.status_code == 403
    assert "Access-Control-Allow-Origin" not in blocked.headers


def test_cloudflare_authenticated_write_rejects_untrusted_origin(monkeypatch):
    _clear_auth_env(monkeypatch)
    _enable_auth(monkeypatch)

    class FakeJWKClient:
        def __init__(self, url, cache_keys):
            pass

        def get_signing_key_from_jwt(self, assertion):
            return SimpleNamespace(key="public-key")

    monkeypatch.setattr(
        "app.services.access_auth_service.jwt.PyJWKClient", FakeJWKClient
    )
    monkeypatch.setattr(
        "app.services.access_auth_service.jwt.decode",
        lambda *args, **kwargs: {"aud": ["web-aud"]},
    )
    client = _make_app().test_client()

    response = client.post(
        "/private",
        headers={
            "Cf-Access-Jwt-Assertion": "signed-access-token",
            "Origin": "https://attacker.example",
        },
    )

    assert response.status_code == 403
