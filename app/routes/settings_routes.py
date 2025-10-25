"""Settings routes for runtime feature toggles."""

import logging
from flask import Blueprint, jsonify, request

from ..services.hotword_settings import HotwordSettingsManager

logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings", __name__, url_prefix="/process/settings")
settings_manager = HotwordSettingsManager.get_instance()


def _apply_cors_headers(response):
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    return response


@settings_bp.route("/hotword", methods=["GET", "POST", "OPTIONS"])
def manage_hotword_settings():
    """Fetch or update hotword-related runtime settings."""
    if request.method == "OPTIONS":
        return _apply_cors_headers(jsonify({"status": "ok"}))

    if request.method == "GET":
        state = settings_manager.get_state()
        return _apply_cors_headers(jsonify({"success": True, "settings": state}))

    payload = request.get_json(silent=True) or {}
    updates = {}

    if "auto_hotwords" in payload:
        updates["auto_hotwords"] = payload["auto_hotwords"]
    if "enable_auto_hotwords" in payload:
        updates["auto_hotwords"] = payload["enable_auto_hotwords"]
    if "post_process" in payload:
        updates["post_process"] = payload["post_process"]
    if "enable_hotword_post_process" in payload:
        updates["post_process"] = payload["enable_hotword_post_process"]
    if "mode" in payload:
        updates["mode"] = payload["mode"]
    if "hotword_mode" in payload:
        updates["mode"] = payload["hotword_mode"]
    if "max_count" in payload:
        updates["max_count"] = payload["max_count"]
    if "hotword_max_count" in payload:
        updates["max_count"] = payload["hotword_max_count"]

    if not updates:
        return _apply_cors_headers(
            jsonify({"success": False, "error": "No supported fields provided"})
        ), 400

    new_state = settings_manager.update_state(**updates)
    logger.info("Hotword settings updated: %s", new_state)
    return _apply_cors_headers(jsonify({"success": True, "settings": new_state}))

