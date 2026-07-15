"""Authentication boundary for Cloudflare Access and trusted internal clients."""

import hmac
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import jwt
from flask import Flask, g, jsonify, request

logger = logging.getLogger(__name__)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_UNAUTHENTICATED_PATHS = {"/health"}
_API_STATUS_PATH = re.compile(r"^/process/status/[^/]+$")
_API_READER_STATUS_PATH = re.compile(
    r"^/process/reader-status/youtube/[A-Za-z0-9_-]{6,32}$"
)


def _as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: Optional[str]) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _bounded_int(value: Optional[str], default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalize_team_domain(value: str) -> str:
    domain = value.strip().rstrip("/")
    if domain and not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    return domain


@dataclass(frozen=True)
class AccessAuthConfig:
    enabled: bool
    team_domain: str
    web_audiences: tuple[str, ...]
    api_audiences: tuple[str, ...]
    internal_token: str
    allowed_origins: tuple[str, ...]
    jwt_leeway_seconds: int

    @classmethod
    def from_env(cls) -> "AccessAuthConfig":
        return cls(
            enabled=_as_bool(os.getenv("ACCESS_AUTH_ENABLED")),
            team_domain=_normalize_team_domain(os.getenv("ACCESS_TEAM_DOMAIN", "")),
            web_audiences=_split_csv(os.getenv("ACCESS_WEB_APPLICATION_AUD")),
            api_audiences=_split_csv(os.getenv("ACCESS_API_APPLICATION_AUD")),
            internal_token=os.getenv("INTERNAL_SERVICE_TOKEN", "").strip(),
            allowed_origins=_split_csv(os.getenv("ACCESS_ALLOWED_ORIGINS")),
            jwt_leeway_seconds=_bounded_int(
                os.getenv("ACCESS_JWT_LEEWAY_SECONDS"),
                default=60,
                minimum=0,
                maximum=300,
            ),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        missing = []
        if not self.team_domain:
            missing.append("ACCESS_TEAM_DOMAIN")
        if not self.web_audiences:
            missing.append("ACCESS_WEB_APPLICATION_AUD")
        if not self.api_audiences:
            missing.append("ACCESS_API_APPLICATION_AUD")
        if not self.internal_token:
            missing.append("INTERNAL_SERVICE_TOKEN")
        if missing:
            raise RuntimeError(
                "ACCESS_AUTH_ENABLED requires: " + ", ".join(missing)
            )


class AccessAuthService:
    """Verify Cloudflare Access assertions or a private internal token."""

    def __init__(self, config: Optional[AccessAuthConfig] = None):
        self.config = config or AccessAuthConfig.from_env()
        self.config.validate()
        self._jwk_client = None
        if self.config.enabled:
            certs_url = f"{self.config.team_domain}/cdn-cgi/access/certs"
            self._jwk_client = jwt.PyJWKClient(certs_url, cache_keys=True)

    def is_origin_allowed(self, origin: Optional[str]) -> bool:
        return bool(origin and origin in self.config.allowed_origins)

    @property
    def audiences(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                self.config.web_audiences + self.config.api_audiences
            )
        )

    def _classify_request_audience(self, claims) -> Optional[str]:
        raw_audience = claims.get("aud", ())
        token_audiences = {
            raw_audience
        } if isinstance(raw_audience, str) else set(raw_audience or ())
        if token_audiences.intersection(self.config.web_audiences):
            return "web"
        if not token_audiences.intersection(self.config.api_audiences):
            return None
        api_request_allowed = (
            request.method == "POST" and request.path == "/process"
        ) or (
            request.method == "GET"
            and (
                _API_STATUS_PATH.fullmatch(request.path)
                or _API_READER_STATUS_PATH.fullmatch(request.path)
            )
        )
        return "api" if api_request_allowed else None

    def authenticate_request(self):
        if request.path in _UNAUTHENTICATED_PATHS:
            g.access_identity = {"type": "healthcheck"}
            return None

        if request.method == "OPTIONS":
            origin = request.headers.get("Origin")
            if origin and not self.is_origin_allowed(origin):
                return jsonify({"error": "Origin is not allowed"}), 403
            return "", 204

        if not self.config.enabled:
            g.access_identity = {"type": "disabled"}
            return None

        bearer = request.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            candidate = bearer[7:].strip()
            if candidate and hmac.compare_digest(candidate, self.config.internal_token):
                g.access_identity = {"type": "internal"}
                return None

        assertion = request.headers.get("Cf-Access-Jwt-Assertion", "").strip()
        if assertion:
            try:
                signing_key = self._jwk_client.get_signing_key_from_jwt(assertion)
                claims = jwt.decode(
                    assertion,
                    signing_key.key,
                    algorithms=["RS256"],
                    audience=list(self.audiences),
                    issuer=self.config.team_domain,
                    leeway=self.config.jwt_leeway_seconds,
                )
                audience_type = self._classify_request_audience(claims)
                if audience_type is None:
                    return jsonify({"error": "Access token is not permitted here"}), 403
                origin = request.headers.get("Origin")
                if (
                    audience_type == "web"
                    and request.method not in _SAFE_METHODS
                    and origin
                    and not self.is_origin_allowed(origin)
                ):
                    return jsonify({"error": "Origin is not allowed"}), 403
                g.access_identity = {
                    "type": "cloudflare_access",
                    "subject": claims.get("sub"),
                    "email": claims.get("email"),
                }
                return None
            except jwt.PyJWTError as exc:
                logger.warning("Cloudflare Access JWT rejected: %s", type(exc).__name__)
            except Exception as exc:
                logger.warning("Cloudflare Access verification unavailable: %s", type(exc).__name__)

        return jsonify({"error": "Authentication required"}), 401

    def add_cors_headers(self, response):
        origin = request.headers.get("Origin")
        if not self.is_origin_allowed(origin):
            return response

        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, CF-Access-Client-Id, "
            "CF-Access-Client-Secret"
        )
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, HEAD, POST, PUT, DELETE, OPTIONS"
        )
        response.headers["Access-Control-Max-Age"] = "600"
        return response


def configure_access_auth(app: Flask) -> AccessAuthService:
    """Attach authentication and CORS hooks to a Flask application."""
    service = AccessAuthService()
    app.access_auth_service = service
    app.before_request(service.authenticate_request)
    app.after_request(service.add_cors_headers)
    logger.info("Access authentication enabled=%s", service.config.enabled)
    return service
