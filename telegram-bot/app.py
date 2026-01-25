import asyncio
import datetime
import json
import logging
import os
import re
import signal
import sys
import tempfile
import threading
import time
import traceback
from threading import Thread
from typing import Any, Awaitable, Dict, List, Optional, Tuple

import httpx
import pytz
import requests
import telegram
import urllib3
import yaml
from flask import Flask, jsonify, request
from telegram import Update
from telegram.error import Conflict, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# 配置日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# 减少HTTP相关的日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_config():
    """加载YAML配置文件"""
    config_path = os.getenv("CONFIG_PATH", "config/config.yml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"加载配置文件失败: {str(e)}")
        return None


# 加载配置
config: Dict[str, Any] = load_config() or {}


def _get_bool(value: Any, default: bool = False) -> bool:
    """将配置值转换为布尔值"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _get_int(value: Any, default: int) -> int:
    """将配置值转换为整数，无法解析时返回默认值"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_path(path: str) -> str:
    """确保 webhook 路径以单个斜杠开头"""
    if not path:
        return "/telegram/webhook"
    return "/" + path.strip("/")


# 获取配置
TELEGRAM_TOKEN = config.get("tokens", {}).get("telegram")
SUBTITLE_PROCESSOR_URL = os.getenv(
    "SUBTITLE_PROCESSOR_URL", "http://subtitle-processor:5000"
)
SERVER_DOMAIN = config.get("servers", {}).get("domain")
TELEGRAM_SETTINGS = (
    config.get("telegram", {}) if isinstance(config.get("telegram"), dict) else {}
)
WEBHOOK_CONFIG = (
    TELEGRAM_SETTINGS.get("webhook", {})
    if isinstance(TELEGRAM_SETTINGS.get("webhook"), dict)
    else {}
)

TELEGRAM_ENABLED = _get_bool(
    os.getenv("TELEGRAM_BOT_ENABLED", TELEGRAM_SETTINGS.get("enabled", True)),
    True,
)

SUBTITLE_CONNECT_TIMEOUT = int(os.getenv("SUBTITLE_CONNECT_TIMEOUT", "30"))
SUBTITLE_READ_TIMEOUT = int(os.getenv("SUBTITLE_READ_TIMEOUT", "1800"))

WEBHOOK_ENABLED = _get_bool(
    os.getenv("TELEGRAM_WEBHOOK_ENABLED", WEBHOOK_CONFIG.get("enabled", False))
)
WEBHOOK_LISTEN = os.getenv(
    "TELEGRAM_WEBHOOK_LISTEN", WEBHOOK_CONFIG.get("listen", "0.0.0.0")
)
WEBHOOK_PORT = int(os.getenv("TELEGRAM_WEBHOOK_PORT", WEBHOOK_CONFIG.get("port", 8082)))
WEBHOOK_PATH = _normalize_path(
    os.getenv("TELEGRAM_WEBHOOK_PATH", WEBHOOK_CONFIG.get("path", "/telegram/webhook"))
)
WEBHOOK_SECRET_TOKEN = os.getenv(
    "TELEGRAM_WEBHOOK_SECRET", WEBHOOK_CONFIG.get("secret_token")
)
WEBHOOK_DROP_PENDING = _get_bool(
    os.getenv(
        "TELEGRAM_WEBHOOK_DROP_PENDING",
        WEBHOOK_CONFIG.get("drop_pending_updates", True),
    ),
    True,
)

DEFAULT_PUBLIC_URL = None
if SERVER_DOMAIN:
    DEFAULT_PUBLIC_URL = f"{SERVER_DOMAIN.rstrip('/')}{WEBHOOK_PATH}"

WEBHOOK_PUBLIC_URL = os.getenv(
    "TELEGRAM_WEBHOOK_URL", WEBHOOK_CONFIG.get("public_url") or DEFAULT_PUBLIC_URL
)

PROMPT_FLOW_CONFIG = (
    TELEGRAM_SETTINGS.get("prompt_flow", {})
    if isinstance(TELEGRAM_SETTINGS.get("prompt_flow"), dict)
    else {}
)

REQUIRE_LOCATION_SELECTION = _get_bool(PROMPT_FLOW_CONFIG.get("require_location"), True)
REQUIRE_TAG_INPUT = _get_bool(PROMPT_FLOW_CONFIG.get("require_tags"), True)
REQUIRE_HOTWORD_INPUT = _get_bool(PROMPT_FLOW_CONFIG.get("require_hotwords"), True)
TAGS_TIMEOUT_SECONDS = _get_int(PROMPT_FLOW_CONFIG.get("tags_timeout_seconds"), 180)
HOTWORDS_TIMEOUT_SECONDS = _get_int(
    PROMPT_FLOW_CONFIG.get("hotwords_timeout_seconds"), 180
)


logger.info(f"使用的SUBTITLE_PROCESSOR_URL: {SUBTITLE_PROCESSOR_URL}")
logger.info(f"使用的SERVER_DOMAIN: {SERVER_DOMAIN}")
logger.info(
    "Webhook配置: enabled=%s, public_url=%s, listen=%s, port=%s, path=%s",
    WEBHOOK_ENABLED,
    WEBHOOK_PUBLIC_URL,
    WEBHOOK_LISTEN,
    WEBHOOK_PORT,
    WEBHOOK_PATH,
)
logger.info(
    "Telegram输入流程配置: require_location=%s, require_tags=%s, require_hotwords=%s, "
    "tags_timeout=%s, hotwords_timeout=%s",
    REQUIRE_LOCATION_SELECTION,
    REQUIRE_TAG_INPUT,
    REQUIRE_HOTWORD_INPUT,
    TAGS_TIMEOUT_SECONDS,
    HOTWORDS_TIMEOUT_SECONDS,
)

# 获取环境变量
PROXY = os.getenv("ALL_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
if PROXY:
    logger.info(f"Using proxy: {PROXY}")

_raw_admins = TELEGRAM_SETTINGS.get("admins", [])
if isinstance(_raw_admins, (str, int)):
    _raw_admins = [_raw_admins]

TELEGRAM_ADMIN_IDS = {
    str(admin).strip()
    for admin in _raw_admins
    if admin is not None and str(admin).strip()
}


def is_admin_user(user_id: int) -> bool:
    """Check whether the user is allowed to change runtime settings."""
    if not TELEGRAM_ADMIN_IDS:
        return True
    return str(user_id) in TELEGRAM_ADMIN_IDS


def fetch_hotword_settings_from_server() -> Dict[str, Any]:
    """Pull the latest hotword settings from subtitle-processor."""
    try:
        response = requests.get(
            f"{SUBTITLE_PROCESSOR_URL}/process/settings/hotword", timeout=10
        )
        response.raise_for_status()
        payload = response.json()
        settings = payload.get("settings")
        if isinstance(settings, dict):
            return settings
    except Exception as exc:
        logger.warning("获取热词配置失败: %s", exc)
    return {}


def update_hotword_settings_on_server(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Send a settings update to subtitle-processor and return the new state."""
    response = requests.post(
        f"{SUBTITLE_PROCESSOR_URL}/process/settings/hotword",
        json=payload,
        timeout=10,
    )
    response.raise_for_status()
    result = response.json()
    settings = result.get("settings")
    if isinstance(settings, dict):
        return settings
    raise ValueError("未从服务器返回有效的设置状态")


# 禁用不安全的HTTPS警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# 后台任务调度工具，确保异常被记录
def schedule_background_task(
    context: ContextTypes.DEFAULT_TYPE, coro: Awaitable[Any]
) -> asyncio.Task:
    task = context.application.create_task(coro)

    def _log_task_error(t: asyncio.Task) -> None:
        try:
            t.result()
        except Exception as exc:
            logger.error(f"后台任务失败: {exc}", exc_info=True)

    task.add_done_callback(_log_task_error)
    return task


def log_update_metadata(prefix: str, update: Update) -> None:
    """记录Telegram消息的原始发送时间和本地接收延迟."""
    try:
        message = getattr(update, "message", None) or getattr(
            update, "edited_message", None
        )
        if not message:
            return
        message_date = message.date
        if not message_date:
            logger.debug("%s message_date missing", prefix)
            return
        if message_date.tzinfo is None:
            message_ts = message_date.replace(tzinfo=datetime.timezone.utc).timestamp()
        else:
            message_ts = message_date.timestamp()
        now_ts = time.time()
        latency_ms = int((now_ts - message_ts) * 1000)
        logger.debug(
            "%s message_date=%s latency_ms=%s",
            prefix,
            message_date.isoformat(),
            latency_ms,
        )
    except Exception as exc:
        logger.debug("log_update_metadata error: %s", exc, exc_info=True)


# 全局变量
VALID_LOCATIONS = {"1": "new", "2": "later", "3": "archive", "4": "feed"}

DEFAULT_LOCATION = str(TELEGRAM_SETTINGS.get("default_location", "new")).lower()
if DEFAULT_LOCATION not in VALID_LOCATIONS.values():
    logger.warning(
        "配置的 default_location=%s 无效，将使用 new",
        TELEGRAM_SETTINGS.get("default_location"),
    )
    DEFAULT_LOCATION = "new"

TAGS_HELP_MESSAGE = "请输入标签，多个标签用逗号分隔（例如：'youtube字幕,学习笔记,英语学习'）。\n输入 /skip 跳过添加标签。"
HOTWORDS_HELP_MESSAGE = "请输入热词，多个热词用逗号分隔（例如：'人工智能,机器学习,AI语音'）。\n输入 /skip 跳过添加热词。"
LOCATION_HELP_MESSAGE = (
    "请选择保存位置：\n"
    "1. 新内容 (New)\n"
    "2. 稍后阅读 (Later)\n"
    "3. 已归档 (Archive)\n"
    "4. Feed"
)
VIDEO_URL_DOMAINS = ("youtube.com", "youtu.be", "bilibili.com", "b23.tv")

# 用户状态存储
user_states = {}
last_user_requests: Dict[Tuple[int, int], Dict[str, Any]] = {}
active_tasks: Dict[Tuple[int, int], Dict[str, Dict[str, Any]]] = {}


def _clean_url_token(token: str) -> str:
    """清理URL分隔符带来的多余字符."""
    if not token:
        return ""
    cleaned = token.strip().strip("()[]<>\"'")
    return cleaned.rstrip(".,;!?")


def extract_video_urls(text: str) -> List[str]:
    """从文本中提取可能的视频URL，支持空格/换行/逗号分隔."""
    if not text:
        return []
    tokens = re.split(r"[\s,，]+", text.strip())
    urls: List[str] = []
    seen = set()
    for token in tokens:
        cleaned = _clean_url_token(token)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if not any(domain in lowered for domain in VIDEO_URL_DOMAINS):
            continue
        if cleaned in seen:
            continue
        urls.append(cleaned)
        seen.add(cleaned)
    return urls


def _shorten_url(url: str, max_length: int = 64) -> str:
    """缩短URL显示，避免提示过长."""
    clean = (url or "").strip()
    if len(clean) <= max_length:
        return clean
    tail_len = 14
    head_len = max_length - tail_len - 3
    if head_len < 10:
        return clean[:max_length]
    return f"{clean[:head_len]}...{clean[-tail_len:]}"


def _shorten_text(text: str, max_length: int = 80) -> str:
    """缩短提示文本，避免消息过长."""
    clean = (text or "").strip()
    if len(clean) <= max_length:
        return clean
    return f"{clean[: max_length - 3]}..."


def _queue_total(state: Dict[str, Any]) -> int:
    """计算当前队列总数."""
    index = int(state.get("queue_index", 1))
    pending_count = len(state.get("pending_urls", []))
    return max(index + pending_count, 1)


def _queue_context_text(state: Dict[str, Any]) -> str:
    """生成当前队列上下文提示."""
    url = state.get("url")
    if not url:
        return ""
    index = int(state.get("queue_index", 1))
    total = _queue_total(state)
    url_display = _shorten_url(url)
    if total > 1:
        prefix = f"当前处理 #{index}/{total}: {url_display}"
    else:
        prefix = f"当前处理: {url_display}"
    return f"{prefix}\n本次输入仅作用于该链接。"


def _upsert_user_state(user_id: int, **updates: Any) -> Dict[str, Any]:
    """更新用户状态并保留队列信息."""
    state = user_states.get(user_id, {})
    state.update(updates)
    user_states[user_id] = state
    return state


def _append_pending_urls(state: Dict[str, Any], urls: List[str]) -> int:
    """追加待处理URL，返回新增数量."""
    pending = list(state.get("pending_urls", []))
    current = state.get("url")
    seen = set(pending)
    if current:
        seen.add(current)
    added = 0
    for url in urls:
        if url in seen:
            continue
        pending.append(url)
        seen.add(url)
        added += 1
    state["pending_urls"] = pending
    return added


def _build_prompt(base_message: str, state: Dict[str, Any]) -> str:
    """拼接带队列上下文的提示文案."""
    prefix = _queue_context_text(state)
    if prefix:
        return f"{prefix}\n{base_message}"
    return base_message


def _start_queue_state(user_id: int, urls: List[str]) -> Dict[str, Any]:
    """初始化队列状态并返回当前用户状态."""
    first_url = urls[0]
    return _upsert_user_state(
        user_id,
        url=first_url,
        queue_index=1,
        pending_urls=list(urls[1:]),
    )


def _pop_next_queue_url(state: Dict[str, Any]) -> Optional[str]:
    """弹出下一个待处理URL."""
    pending = list(state.get("pending_urls", []))
    if not pending:
        return None
    next_url = pending.pop(0)
    state["pending_urls"] = pending
    return next_url


def _request_key(user_id: int, chat_id: int) -> Tuple[int, int]:
    return (int(user_id), int(chat_id))


def _register_active_task(
    user_id: int,
    chat_id: int,
    process_id: str,
    url: str,
    status: str,
    location: Optional[str] = None,
    tags: Optional[List[str]] = None,
    hotwords: Optional[List[str]] = None,
    message_id: Optional[int] = None,
) -> Dict[str, Any]:
    """登记一个正在处理的任务."""
    key = _request_key(user_id, chat_id)
    entry = {
        "process_id": process_id,
        "url": url,
        "status": status,
        "location": location,
        "tags": list(tags or []),
        "hotwords": list(hotwords or []),
        "message_id": message_id,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    active_tasks.setdefault(key, {})[process_id] = entry
    return entry


def _update_active_task_status(
    user_id: int,
    chat_id: int,
    process_id: str,
    status: str,
    error: Optional[str] = None,
) -> None:
    """更新任务状态."""
    key = _request_key(user_id, chat_id)
    tasks = active_tasks.get(key)
    if not tasks or process_id not in tasks:
        return
    tasks[process_id]["status"] = status
    tasks[process_id]["updated_at"] = time.time()
    if error:
        tasks[process_id]["error"] = error


def _remove_active_task(user_id: int, chat_id: int, process_id: str) -> None:
    """移除已完成任务."""
    key = _request_key(user_id, chat_id)
    tasks = active_tasks.get(key)
    if not tasks:
        return
    tasks.pop(process_id, None)
    if not tasks:
        active_tasks.pop(key, None)


def _clear_failed_tasks(user_id: int, chat_id: int) -> int:
    """清理失败任务，返回清理数量."""
    key = _request_key(user_id, chat_id)
    tasks = active_tasks.get(key)
    if not tasks:
        return 0
    failed_ids = [
        process_id
        for process_id, task in tasks.items()
        if (task.get("status") or "").lower() == "failed"
    ]
    for process_id in failed_ids:
        tasks.pop(process_id, None)
    if not tasks:
        active_tasks.pop(key, None)
    return len(failed_ids)


def _list_active_tasks(user_id: int, chat_id: int) -> List[Dict[str, Any]]:
    """列出当前用户/聊天中的任务."""
    key = _request_key(user_id, chat_id)
    tasks = list(active_tasks.get(key, {}).values())
    return sorted(tasks, key=lambda item: item.get("created_at", 0))


def _status_label(status: str) -> str:
    """友好的状态展示."""
    mapping = {
        "queued": "排队中",
        "processing": "处理中",
        "pending": "准备中",
        "failed": "失败",
        "completed": "已完成",
        "unknown": "处理中",
    }
    return mapping.get(status, status)


def _start_processing_attempt(
    user_id: int,
    chat_id: int,
    url: str,
    location: str,
    tags: Optional[List[str]],
    hotwords: Optional[List[str]],
    origin: str,
) -> Dict[str, Any]:
    """初始化或更新用户的最后一次处理记录"""
    key = _request_key(user_id, chat_id)
    previous = last_user_requests.get(key, {})
    attempts = int(previous.get("attempts", 0)) + 1
    record = {
        "user_id": user_id,
        "chat_id": chat_id,
        "url": url,
        "location": location,
        "tags": list(tags or []),
        "hotwords": list(hotwords or []),
        "status": "pending",
        "error": None,
        "origin": origin,
        "attempts": attempts,
        "updated_at": time.time(),
    }
    # 保留上一次的标准化URL以支持重试
    if "normalized_url" in previous:
        record["normalized_url"] = previous["normalized_url"]
    if "platform" in previous:
        record["platform"] = previous["platform"]
    last_user_requests[key] = record
    logger.debug(
        "start_processing_attempt: user=%s chat=%s origin=%s attempts=%s",
        user_id,
        chat_id,
        origin,
        attempts,
    )
    return record


def _update_last_request(
    user_id: int,
    chat_id: int,
    **updates: Any,
) -> None:
    """更新最后一次请求状态"""
    key = _request_key(user_id, chat_id)
    if key not in last_user_requests:
        last_user_requests[key] = {
            "user_id": user_id,
            "chat_id": chat_id,
            "attempts": 0,
        }
    last_user_requests[key].update(updates)
    last_user_requests[key]["updated_at"] = time.time()
    logger.debug(
        "update_last_request: user=%s chat=%s updates=%s",
        user_id,
        chat_id,
        updates,
    )


def _get_last_request(user_id: int, chat_id: int) -> Optional[Dict[str, Any]]:
    return last_user_requests.get(_request_key(user_id, chat_id))


async def submit_request_via_context(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    url: str,
    location: str,
    tags: Optional[List[str]],
    hotwords: Optional[List[str]],
    confirmation_text: Optional[str],
    origin: str,
    reply_to_message_id: Optional[int] = None,
) -> None:
    """记录请求并调度后台任务"""
    safe_tags = list(tags or [])
    safe_hotwords = list(hotwords or [])
    _start_processing_attempt(
        user_id,
        chat_id,
        url,
        location,
        safe_tags,
        safe_hotwords,
        origin,
    )

    if confirmation_text:
        await context.bot.send_message(
            chat_id=chat_id,
            text=confirmation_text,
            reply_to_message_id=reply_to_message_id,
        )

    schedule_background_task(
        context,
        process_url_with_location(
            user_id,
            chat_id,
            url,
            location,
            context,
            safe_tags,
            safe_hotwords,
        ),
    )


last_user_requests: Dict[Tuple[int, int], Dict[str, Any]] = {}

# 全局变量追踪应用状态与心跳指标
last_activity = time.time()
is_bot_healthy = True
last_update_id = None
last_update_at = 0.0
last_heartbeat_ok = None  # None=未知, True/False=最近一次心跳结果
last_heartbeat_at = 0.0
last_ping_ms = None
consecutive_heartbeat_failures = 0

# 健康检查Flask应用
health_app = Flask(__name__)


@health_app.route("/health")
def health_check():
    """健康检查端点（支持 ?deep=1 返回详细JSON）"""
    global \
        last_activity, \
        is_bot_healthy, \
        last_update_at, \
        last_update_id, \
        last_heartbeat_ok, \
        last_ping_ms, \
        last_heartbeat_at

    current_time = time.time()
    time_since_activity = current_time - last_activity
    time_since_update = current_time - (last_update_at or 0)
    time_since_heartbeat = current_time - (last_heartbeat_at or 0)

    # 判定阈值（秒）
    idle_warn = 15 * 60
    idle_fail = 30 * 60
    hb_stale_warn = 5 * 60
    hb_stale_fail = 10 * 60

    unhealthy_reasons = []
    if time_since_activity > idle_fail:
        logger.warning("健康检查：超过30分钟无活动，仅记录告警，不判定为不健康")
    if last_heartbeat_ok is False and time_since_heartbeat > 60:
        unhealthy_reasons.append("heartbeat_failed")
    if time_since_heartbeat > hb_stale_fail:
        unhealthy_reasons.append("heartbeat_stale")
    if not is_bot_healthy:
        unhealthy_reasons.append("flag_unhealthy")

    status_code = 503 if unhealthy_reasons else 200

    if request.args.get("deep") == "1":
        return jsonify(
            {
                "status": "unhealthy" if unhealthy_reasons else "healthy",
                "reasons": unhealthy_reasons,
                "time_since_activity_sec": int(time_since_activity),
                "time_since_update_sec": int(time_since_update),
                "time_since_heartbeat_sec": int(time_since_heartbeat),
                "last_update_id": last_update_id,
                "last_heartbeat_ok": last_heartbeat_ok,
                "last_ping_ms": last_ping_ms,
            }
        ), status_code

    # 兼容旧探活：仅返回文本，但遵循状态码
    return ("OK" if status_code == 200 else "UNHEALTHY"), status_code


def start_health_server():
    """启动健康检查服务器"""
    health_app.run(host="0.0.0.0", port=8081, debug=False, use_reloader=False)


def update_activity():
    """更新最后活动时间"""
    global last_activity, is_bot_healthy
    last_activity = time.time()
    is_bot_healthy = True  # 有活动时重置健康状态


def record_update(update: object = None):
    """记录最近一次更新的元信息并刷新活动时间"""
    global last_update_at, last_update_id
    update_activity()
    last_update_at = time.time()
    try:
        if isinstance(update, Update):
            last_update_id = getattr(update, "update_id", None)
    except Exception:
        pass


def connection_monitor(application):
    """监控连接状态并在需要时重启"""

    def monitor_loop():
        global is_bot_healthy, last_activity
        while True:
            try:
                current_time = time.time()
                time_since_activity = current_time - last_activity

                # 若超过30分钟无活动，仅告警；是否重启交由心跳判定
                if time_since_activity > 1800:  # 30分钟
                    logger.warning("长时间无活动，执行连接测试")

                # 心跳失败与陈旧性综合判定
                if (
                    last_heartbeat_ok is False
                    and (current_time - last_heartbeat_at) > 60
                ) or ((current_time - last_heartbeat_at) > 600):
                    is_bot_healthy = False

                # 如果状态不健康超过5分钟，强制重启
                if not is_bot_healthy and time_since_activity > 300:
                    logger.critical("Bot状态不健康超过5分钟，触发容器重启")
                    os._exit(1)

                # 如果状态不健康超过5分钟，强制重启
                if not is_bot_healthy and time_since_activity > 300:
                    logger.critical("Bot状态不健康超过5分钟，触发容器重启")
                    os._exit(1)

                time.sleep(60)  # 每分钟检查一次

            except Exception as e:
                logger.error(f"连接监控器异常: {str(e)}")
                time.sleep(60)

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    logger.info("连接监控器已启动")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理错误的回调函数"""
    global is_bot_healthy
    logger.error("Exception while handling an update:", exc_info=context.error)

    # 更新活动时间
    record_update(update)

    try:
        # 如果是Conflict错误，尝试重置更新
        if isinstance(context.error, Conflict):
            logger.warning("检测到冲突错误，可能有多个bot实例在运行")
            is_bot_healthy = False
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "检测到系统异常，正在尝试恢复..."
                )
            # 等待一段时间后重试
            time.sleep(5)
            return

        # 如果是网络错误，标记为不健康状态
        if isinstance(context.error, NetworkError):
            logger.warning("网络错误，标记bot为不健康状态")
            is_bot_healthy = False
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "网络连接出现问题，请稍后重试。"
                )
            return

        # 其他Telegram相关错误
        if isinstance(context.error, TelegramError):
            logger.warning("Telegram错误，标记bot为不健康状态")
            is_bot_healthy = False
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "Telegram服务暂时不可用，请稍后重试。"
                )
            return

        # 发送通用错误消息给用户
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "抱歉，处理您的请求时出现了错误。请稍后再试。"
            )
    except Exception as e:
        logger.error(f"Error in error handler: {str(e)}")
        logger.error(traceback.format_exc())
        is_bot_healthy = False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """发送启动消息"""
    record_update(update)  # 更新活动时间与update信息
    await update.message.reply_text(
        "Hi! 发送视频链接给我，我会帮你处理字幕。\n"
        "支持的格式:\n"
        "1. 直接发送YouTube/Bilibili URL\n"
        "2. 使用命令 /process URL\n"
        "3. 一次发送多条URL（换行/空格/逗号分隔），将自动跳过标签/热词并并行处理\n"
        "4. 使用 /queue 查看当前任务列表，/queue_clear 清理失败任务\n"
        "5. 使用 /retry_failed 批量重试失败任务"
    )


def normalize_url(url):
    """标准化视频URL

    支持的格式：
    YouTube:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://youtube.com/shorts/VIDEO_ID
    - https://youtube.com/live/VIDEO_ID
    - https://m.youtube.com/watch?v=VIDEO_ID
    - https://youtube.com/v/VIDEO_ID
    - https://youtube.com/embed/VIDEO_ID

    Bilibili:
    - https://www.bilibili.com/video/BV1xx411c7mD
    - https://b23.tv/xxxxx
    - https://www.bilibili.com/video/av170001
    - https://m.bilibili.com/video/BV1xx411c7mD
    """
    import re

    # 清理URL
    url = url.strip()

    # YouTube URL处理
    youtube_patterns = [
        r"(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/watch\?v=([a-zA-Z0-9_-]+)",
        r"(?:https?:\/\/)?youtu\.be\/([a-zA-Z0-9_-]+)",
        r"(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/shorts\/([a-zA-Z0-9_-]+)",
        r"(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/live\/([a-zA-Z0-9_-]+)",
        r"(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/v\/([a-zA-Z0-9_-]+)",
        r"(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/embed\/([a-zA-Z0-9_-]+)",
    ]

    for pattern in youtube_patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            return f"https://www.youtube.com/watch?v={video_id}", "youtube"

    # Bilibili URL处理
    bilibili_patterns = [
        # BV号格式
        r"(?:https?:\/\/)?(?:www\.|m\.)?bilibili\.com\/video\/(BV[a-zA-Z0-9]+)",
        # av号格式
        r"(?:https?:\/\/)?(?:www\.|m\.)?bilibili\.com\/video\/av(\d+)",
        # 短链接格式
        r"(?:https?:\/\/)?b23\.tv\/([a-zA-Z0-9]+)",
    ]

    for pattern in bilibili_patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            # 如果是短链接，需要处理重定向
            if "b23.tv" in url:
                try:
                    import requests

                    response = requests.head(url, allow_redirects=True)
                    if response.status_code == 200:
                        final_url = response.url
                        # 递归处理重定向后的URL
                        return normalize_url(final_url)
                except:
                    pass

            # 如果是av号，转换为标准格式
            if video_id.startswith("av"):
                video_id = video_id[2:]
            if video_id.isdigit():
                return f"https://www.bilibili.com/video/av{video_id}", "bilibili"
            else:
                return f"https://www.bilibili.com/video/{video_id}", "bilibili"

    return None, None


def extract_video_id(url, platform):
    """从标准化的URL中提取视频ID"""
    if platform == "youtube":
        match = re.search(r"watch\?v=([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)
    elif platform == "bilibili":
        # BV号格式
        match = re.search(r"\/video\/(BV[a-zA-Z0-9]+)", url)
        if match:
            return match.group(1)
        # av号格式
        match = re.search(r"\/video\/av(\d+)", url)
        if match:
            return match.group(1)
    return None


async def send_subtitle_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict
) -> None:
    """发送字幕文件到Telegram

    Args:
        update: Telegram更新对象
        context: 回调上下文
        result: 字幕处理结果，包含字幕内容和视频信息
    """
    try:
        # 获取字幕内容和文件名
        subtitle_content = result.get("subtitle_content", "")
        video_info = result.get("video_info", {})

        # 使用视频标题作为文件名
        title = video_info.get("title", "") if video_info else ""
        if not title:
            if result.get("original_filename"):
                title = os.path.splitext(str(result["original_filename"]))[0]
            elif "filename" in result:
                title = os.path.splitext(result["filename"])[0]
        if not title:
            title = "subtitle"
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title).strip() or "subtitle"
        filename = f"{safe_title}.srt"
        logger.debug(
            "send_subtitle_file: resolved filename=%s title=%r original=%r fallback=%r",
            filename,
            video_info.get("title") if video_info else None,
            result.get("original_filename"),
            result.get("filename"),
        )

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".srt", delete=False
        ) as temp_file:
            temp_file.write(subtitle_content)
            temp_path = temp_file.name

        # 发送字幕文件
        with open(temp_path, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename=filename,
                caption=f"✅ 字幕已生成 ({result.get('source', 'unknown')})",
            )

        try:
            os.remove(temp_path)
        except Exception as e:
            logger.warning(f"删除临时文件失败: {str(e)}")

    except Exception as e:
        logger.error(f"发送字幕文件时出错: {str(e)}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text="❌ 发送字幕文件时出错，请稍后重试。"
        )


async def _prompt_location(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    url: str,
    reply_to_message_id: Optional[int] = None,
) -> None:
    """发送location选择提示."""
    logger.info("ask_location: user=%s url=%s", user_id, url)
    state = _upsert_user_state(
        user_id,
        state="waiting_for_location",
        url=url,
        last_interaction=datetime.datetime.now(pytz.UTC),
    )

    message = await context.bot.send_message(
        chat_id=chat_id,
        text=_build_prompt(LOCATION_HELP_MESSAGE, state),
        reply_to_message_id=reply_to_message_id,
    )

    state["message_id"] = message.message_id

    context.job_queue.run_once(
        location_timeout,
        180,
        data={"user_id": user_id, "chat_id": chat_id},
        name="location_timeout",
    )


async def _prompt_tags(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    url: str,
    location: str,
    reply_to_message_id: Optional[int] = None,
) -> None:
    """发送标签输入提示."""
    logger.info("ask_tags: user=%s location=%s url=%s", user_id, location, url)
    state = _upsert_user_state(
        user_id,
        state="waiting_for_tags",
        url=url,
        location=location,
        last_interaction=datetime.datetime.now(pytz.UTC),
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=_build_prompt(TAGS_HELP_MESSAGE, state),
        reply_to_message_id=reply_to_message_id,
    )

    if TAGS_TIMEOUT_SECONDS > 0:
        context.job_queue.run_once(
            tags_timeout,
            TAGS_TIMEOUT_SECONDS,
            data={"user_id": user_id, "chat_id": chat_id},
            name=f"tags_timeout_{user_id}",
        )


async def _prompt_hotwords(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    url: str,
    location: str,
    tags: List[str],
    reply_to_message_id: Optional[int] = None,
) -> None:
    """发送热词输入提示."""
    logger.info(
        "ask_hotwords: user=%s location=%s url=%s tags=%s",
        user_id,
        location,
        url,
        tags,
    )
    state = _upsert_user_state(
        user_id,
        state="waiting_for_hotwords",
        url=url,
        location=location,
        tags=tags,
        last_interaction=datetime.datetime.now(pytz.UTC),
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=_build_prompt(HOTWORDS_HELP_MESSAGE, state),
        reply_to_message_id=reply_to_message_id,
    )

    if HOTWORDS_TIMEOUT_SECONDS > 0:
        context.job_queue.run_once(
            hotwords_timeout,
            HOTWORDS_TIMEOUT_SECONDS,
            data={"user_id": user_id, "chat_id": chat_id},
            name=f"hotwords_timeout_{user_id}",
        )


async def _start_input_flow(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    url: str,
    location: str,
    reply_to_message_id: Optional[int] = None,
) -> None:
    """根据配置启动标签/热词输入流程."""
    if REQUIRE_TAG_INPUT:
        await _prompt_tags(
            context,
            chat_id,
            user_id,
            url,
            location,
            reply_to_message_id=reply_to_message_id,
        )
        return

    if REQUIRE_HOTWORD_INPUT:
        await _prompt_hotwords(
            context,
            chat_id,
            user_id,
            url,
            location,
            [],
            reply_to_message_id=reply_to_message_id,
        )
        return

    await submit_request_via_context(
        context,
        user_id,
        chat_id,
        url,
        location,
        [],
        [],
        "✅ 已收到请求，正在后台处理...",
        "auto_skip_prompts",
        reply_to_message_id=reply_to_message_id,
    )
    await _advance_queue_or_cleanup(context, user_id, chat_id)


async def _process_queue_without_prompts(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    location: str,
) -> None:
    """批量模式下并行提交队列，跳过标签/热词."""
    state = user_states.get(user_id)
    if not state:
        return
    urls: List[str] = []
    if state.get("url"):
        urls.append(state["url"])
    urls.extend(state.get("pending_urls", []))
    if not urls:
        user_states.pop(user_id, None)
        return

    for idx, url in enumerate(urls, start=1):
        state["queue_index"] = idx
        state["url"] = url
        state["location"] = location
        schedule_background_task(
            context,
            process_url_with_location(
                user_id,
                chat_id,
                url,
                location,
                context,
                tags=[],
                hotwords=[],
                await_completion=False,
            ),
        )

    user_states.pop(user_id, None)


async def _advance_queue_or_cleanup(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int
) -> None:
    """进入下一条队列或清理状态."""
    state = user_states.get(user_id)
    if not state:
        return

    next_url = _pop_next_queue_url(state)
    if not next_url:
        user_states.pop(user_id, None)
        return

    state["queue_index"] = int(state.get("queue_index", 1)) + 1
    state.pop("tags", None)
    state.pop("hotwords", None)
    state["url"] = next_url
    state["location"] = None

    if REQUIRE_LOCATION_SELECTION:
        await _prompt_location(context, chat_id, user_id, next_url)
    else:
        await _start_input_flow(context, chat_id, user_id, next_url, DEFAULT_LOCATION)


async def ask_location(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str
) -> None:
    """询问用户选择location"""
    await _prompt_location(
        context,
        update.effective_chat.id,
        update.effective_user.id,
        url,
        reply_to_message_id=update.message.message_id if update.message else None,
    )


async def location_timeout(context: CallbackContext) -> None:
    """处理location选择超时"""
    try:
        user_id = context.job.data["user_id"]
        chat_id = context.job.data["chat_id"]
        logger.warning("location_timeout: user=%s chat=%s", user_id, chat_id)

        if (
            user_id in user_states
            and user_states[user_id].get("state") == "waiting_for_location"
        ):
            # 发送超时消息
            await context.bot.send_message(
                chat_id=chat_id, text="⌛ 选择超时，请重新发送视频链接"
            )

            # 清理用户状态
            if user_id in user_states:
                del user_states[user_id]
    except Exception as e:
        logger.error(f"处理location超时时出错: {str(e)}")


async def ask_tags(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, location: str
) -> None:
    """询问用户输入tags"""
    user_id = update.effective_user.id
    if not REQUIRE_TAG_INPUT:
        logger.info("ask_tags: user=%s 配置禁用标签输入，跳过该步骤", user_id)
        await ask_hotwords(update, context, url, location, [])
        return
    await _prompt_tags(
        context,
        update.effective_chat.id,
        user_id,
        url,
        location,
        reply_to_message_id=update.message.message_id if update.message else None,
    )


async def ask_hotwords(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    location: str,
    tags: Optional[List[str]],
) -> None:
    """询问用户输入热词"""
    user_id = update.effective_user.id
    if not REQUIRE_HOTWORD_INPUT:
        logger.info("ask_hotwords: user=%s 配置禁用热词输入，跳过该步骤", user_id)
        await finalize_user_request(update, context, url, location, tags or [], [])
        return
    await _prompt_hotwords(
        context,
        update.effective_chat.id,
        user_id,
        url,
        location,
        tags or [],
        reply_to_message_id=update.message.message_id if update.message else None,
    )


async def finalize_user_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    location: str,
    tags: Optional[List[str]],
    hotwords: Optional[List[str]],
    confirmation_text: str = "✅ 已收到请求，正在后台处理...",
    origin: str = "user",
) -> None:
    """发送确认消息并调度后台处理任务"""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        logger.error(
            "finalize_user_request: 缺少聊天或用户信息 url=%s location=%s",
            url,
            location,
        )
        return

    await submit_request_via_context(
        context,
        user.id,
        chat.id,
        url,
        location,
        tags,
        hotwords,
        confirmation_text,
        origin,
        reply_to_message_id=update.message.message_id if update.message else None,
    )
    await _advance_queue_or_cleanup(context, user.id, chat.id)


async def tags_timeout(context: CallbackContext) -> None:
    """处理tags输入超时"""
    user_id = context.job.data["user_id"]
    chat_id = context.job.data["chat_id"]
    logger.warning("tags_timeout: user=%s chat=%s", user_id, chat_id)

    if (
        user_id in user_states
        and user_states[user_id].get("state") == "waiting_for_tags"
    ):
        # 使用默认的空tags继续处理
        url = user_states[user_id]["url"]
        location = user_states[user_id]["location"]
        await submit_request_via_context(
            context,
            user_id,
            chat_id,
            url,
            location,
            [],
            [],
            "✅ 已收到请求，正在后台处理...",
            "tags_timeout",
        )
        await _advance_queue_or_cleanup(context, user_id, chat_id)


async def hotwords_timeout(context: CallbackContext) -> None:
    """处理热词输入超时"""
    user_id = context.job.data["user_id"]
    chat_id = context.job.data["chat_id"]
    logger.warning("hotwords_timeout: user=%s chat=%s", user_id, chat_id)

    state = user_states.get(user_id)
    if state and state.get("state") == "waiting_for_hotwords":
        url = state["url"]
        location = state["location"]
        tags = state.get("tags", [])
        await context.bot.send_message(
            chat_id=chat_id, text="⌛ 热词输入超时，将不添加热词继续处理。"
        )
        await submit_request_via_context(
            context,
            user_id,
            chat_id,
            url,
            location,
            tags,
            [],
            None,
            "hotwords_timeout",
        )
        await _advance_queue_or_cleanup(context, user_id, chat_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户消息"""
    record_update(update)  # 更新活动时间与update信息
    log_update_metadata("handle_message", update)
    user_id = update.effective_user.id
    user_state = user_states.get(user_id)
    text_preview = (update.message.text or "").strip() if update.message else None
    logger.info(
        "handle_message: user=%s state=%s text=%r",
        user_id,
        user_state.get("state") if isinstance(user_state, dict) else None,
        text_preview,
    )
    text = text_preview or ""
    incoming_urls = extract_video_urls(text)

    if user_state and user_state.get("state") in {
        "waiting_for_tags",
        "waiting_for_hotwords",
        "waiting_for_location",
    }:
        if incoming_urls:
            user_state.setdefault("queue_index", 1)
            user_state.setdefault("pending_urls", [])
            added = _append_pending_urls(user_state, incoming_urls)
            if added > 0:
                total = _queue_total(user_state)
                if user_state.get("state") == "waiting_for_tags":
                    step_hint = "输入标签或发送 /skip"
                elif user_state.get("state") == "waiting_for_hotwords":
                    step_hint = "输入热词或发送 /skip"
                else:
                    step_hint = "选择保存位置(1-4)"
                await update.message.reply_text(
                    f"✅ 已加入队列（+{added}），共 {total} 条。\n"
                    f"{_queue_context_text(user_state)}\n"
                    f"请继续{step_hint}。"
                )
            else:
                await update.message.reply_text("链接已在队列中，无需重复添加。")
            return

    # 如果用户正在等待输入tags
    if user_state and user_state.get("state") == "waiting_for_tags":
        if not text:
            await update.message.reply_text("❌ 标签不能为空，请输入标签或发送 /skip")
            logger.info("handle_message: user=%s 提供空标签", user_id)
            return

        normalized = text.replace("，", ",")
        if normalized.lower() in {"skip", "跳过"}:
            tags = []
        else:
            tags = [tag.strip() for tag in normalized.split(",") if tag.strip()]

        await update.message.reply_text("✅ 标签已记录。")
        logger.info("handle_message: user=%s 标签=%s", user_id, tags)
        await ask_hotwords(
            update, context, user_state["url"], user_state["location"], tags
        )
        return

    if user_state and user_state.get("state") == "waiting_for_hotwords":
        normalized = text.replace("，", ",").replace("\n", ",")

        if not text or normalized.lower() in {"skip", "跳过"}:
            hotwords = []
        else:
            hotwords = [word.strip() for word in normalized.split(",") if word.strip()]

        logger.info("handle_message: user=%s 热词=%s", user_id, hotwords)
        tags = user_state.get("tags", [])
        await finalize_user_request(
            update,
            context,
            user_state["url"],
            user_state["location"],
            tags,
            hotwords,
        )
        return

    # 如果用户正在等待选择location
    if user_state and user_state.get("state") == "waiting_for_location":
        location_input = update.message.text.lower().strip()
        logger.info(
            "handle_message: user=%s 选择location输入=%s", user_id, location_input
        )

        # 检查输入是否有效
        if location_input in VALID_LOCATIONS.values():
            location = location_input
        elif location_input in VALID_LOCATIONS:
            location = VALID_LOCATIONS[location_input]
        else:
            await update.message.reply_text(
                "❌ 无效的选择，请输入数字(1-4)或有效的位置名称"
            )
            logger.warning(
                "handle_message: user=%s location输入无效=%s", user_id, location_input
            )
            return

        # 删除选择提示消息
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id, message_id=user_state["message_id"]
            )
        except Exception:
            pass

        # 询问用户输入tags
        if user_state.get("skip_inputs"):
            schedule_background_task(
                context,
                _process_queue_without_prompts(
                    context, user_id, update.effective_chat.id, location
                ),
            )
            await update.message.reply_text("✅ 批量处理已开始，可用 /queue 查看进度。")
            return

        await ask_tags(update, context, user_state["url"], location)
        return

    # 检查是否是视频URL
    if incoming_urls:
        if len(incoming_urls) > 1:
            state = _start_queue_state(user_id, incoming_urls)
            state["skip_inputs"] = True
            await update.message.reply_text(
                f"✅ 已收到 {len(incoming_urls)} 条链接，批量模式将跳过标签/热词并顺序处理。"
            )
            if REQUIRE_LOCATION_SELECTION:
                await _prompt_location(
                    context,
                    update.effective_chat.id,
                    user_id,
                    state["url"],
                    reply_to_message_id=update.message.message_id
                    if update.message
                    else None,
                )
            else:
                logger.info(
                    "handle_message: user=%s 跳过location选择，使用默认位置=%s",
                    user_id,
                    DEFAULT_LOCATION,
                )
                schedule_background_task(
                    context,
                    _process_queue_without_prompts(
                        context,
                        user_id,
                        update.effective_chat.id,
                        DEFAULT_LOCATION,
                    ),
                )
                await update.message.reply_text(
                    "✅ 批量处理已开始，可用 /queue 查看进度。"
                )
        else:
            state = _start_queue_state(user_id, incoming_urls)
            if REQUIRE_LOCATION_SELECTION:
                await _prompt_location(
                    context,
                    update.effective_chat.id,
                    user_id,
                    state["url"],
                    reply_to_message_id=update.message.message_id
                    if update.message
                    else None,
                )
            else:
                logger.info(
                    "handle_message: user=%s 跳过location选择，使用默认位置=%s",
                    user_id,
                    DEFAULT_LOCATION,
                )
                await _start_input_flow(
                    context,
                    update.effective_chat.id,
                    user_id,
                    state["url"],
                    DEFAULT_LOCATION,
                    reply_to_message_id=update.message.message_id
                    if update.message
                    else None,
                )
    else:
        await update.message.reply_text("请发送YouTube或Bilibili视频链接")


async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /process 命令"""
    try:
        record_update(update)
        log_update_metadata("/process", update)
        logger.info(
            "/process command: user=%s text=%r",
            update.effective_user.id,
            update.message.text if update.message else None,
        )

        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await update.message.reply_text(
                "❌ 请在命令后提供视频URL，例如 /process URL"
            )
            return

        payload = parts[1].strip()
        urls = extract_video_urls(payload) or [payload]

        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        user_state = user_states.get(user_id)
        if user_state and user_state.get("state") in {
            "waiting_for_tags",
            "waiting_for_hotwords",
            "waiting_for_location",
        }:
            user_state.setdefault("queue_index", 1)
            user_state.setdefault("pending_urls", [])
            added = _append_pending_urls(user_state, urls)
            if added > 0:
                total = _queue_total(user_state)
                await update.message.reply_text(
                    f"✅ 已加入队列（+{added}），共 {total} 条。请先完成当前输入。"
                )
            else:
                await update.message.reply_text("链接已在队列中，无需重复添加。")
            return

        if len(urls) > 1:
            state = _start_queue_state(user_id, urls)
            state["skip_inputs"] = True
            await update.message.reply_text(
                f"✅ 已收到 {len(urls)} 条链接，批量模式将跳过标签/热词并顺序处理。"
            )
            schedule_background_task(
                context,
                _process_queue_without_prompts(
                    context, user_id, chat_id, DEFAULT_LOCATION
                ),
            )
            await update.message.reply_text("✅ 批量处理已开始，可用 /queue 查看进度。")
            return

        target_url = urls[0].strip()
        await update.message.reply_text("✅ 已收到请求，正在后台处理...")
        await submit_request_via_context(
            context,
            user_id,
            chat_id,
            target_url,
            "new",
            [],
            [],
            None,
            "process_command",
        )
    except Exception as e:
        logger.error(f"处理 /process 命令时出错: {str(e)}")
        await update.message.reply_text("❌ 处理视频时出错，请稍后重试。")


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /skip 命令"""
    record_update(update)
    log_update_metadata("/skip", update)
    user_id = update.effective_user.id
    user_state = user_states.get(user_id)
    logger.info("/skip command: user=%s state=%s", user_id, user_state)

    if not user_state:
        await update.message.reply_text("当前没有需要跳过的步骤。")
        return

    state = user_state.get("state")
    if state == "waiting_for_tags":
        await update.message.reply_text("✅ 标签已跳过。")
        await ask_hotwords(
            update, context, user_state["url"], user_state["location"], []
        )
    elif state == "waiting_for_hotwords":
        tags = user_state.get("tags", [])
        await finalize_user_request(
            update,
            context,
            user_state["url"],
            user_state["location"],
            tags,
            [],
            confirmation_text="✅ 热词已跳过，正在后台处理...",
            origin="skip_hotwords",
        )
    elif state == "waiting_for_location":
        await update.message.reply_text("❌ 请选择一个保存位置（1-4），暂不支持跳过。")
    else:
        await update.message.reply_text("当前没有可跳过的步骤。")


async def retry_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """重试上一次失败的处理请求"""
    record_update(update)
    log_update_metadata("/retry", update)
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        logger.error("/retry 缺少chat或user信息")
        return

    entry = _get_last_request(user.id, chat.id)
    if not entry or not entry.get("url"):
        await update.message.reply_text(
            "当前没有可以重试的请求，请先发送一个视频链接。"
        )
        return

    status = entry.get("status")
    if status in {"pending", "processing", "queued"}:
        await update.message.reply_text("上一条请求仍在处理中，请稍后再试。")
        return
    if status == "completed":
        await update.message.reply_text(
            "上一条请求已完成，如需再次处理请重新发送链接。"
        )
        return

    url_to_use = entry.get("normalized_url") or entry.get("url")
    location = entry.get("location") or DEFAULT_LOCATION
    tags = entry.get("tags") or []
    hotwords = entry.get("hotwords") or []

    logger.info(
        "/retry command: user=%s chat=%s status=%s url=%s location=%s",
        user.id,
        chat.id,
        status,
        url_to_use,
        location,
    )

    await submit_request_via_context(
        context,
        user.id,
        chat.id,
        url_to_use,
        location,
        tags,
        hotwords,
        "♻️ 已重新提交上一次请求，正在后台处理...",
        "retry_command",
        reply_to_message_id=update.message.message_id if update.message else None,
    )


async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """查询当前正在处理的任务列表"""
    record_update(update)
    log_update_metadata("/queue", update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    active_tasks_list = []
    failed_tasks = []
    for task in _list_active_tasks(user_id, chat_id):
        status = (task.get("status") or "").lower()
        if status == "completed":
            continue
        if status == "failed":
            failed_tasks.append(task)
        else:
            active_tasks_list.append(task)
    if not active_tasks_list and not failed_tasks:
        await update.message.reply_text("当前没有正在处理的任务。")
        return

    lines = []
    if active_tasks_list:
        lines.append(f"正在处理 {len(active_tasks_list)} 个任务：")
        for idx, task in enumerate(active_tasks_list, start=1):
            url_display = _shorten_url(task.get("url") or "")
            status_label = _status_label(task.get("status", "processing"))
            lines.append(f"{idx}. {url_display} ({status_label})")

    if failed_tasks:
        if lines:
            lines.append("")
        lines.append(f"失败 {len(failed_tasks)} 个任务：")
        for idx, task in enumerate(failed_tasks, start=1):
            url_display = _shorten_url(task.get("url") or "")
            error = _shorten_text(task.get("error") or "")
            if error:
                lines.append(f"{idx}. {url_display} (失败: {error})")
            else:
                lines.append(f"{idx}. {url_display} (失败)")
        lines.append("可使用 /retry_failed 批量重试，或 /queue_clear 清理失败任务。")

    await update.message.reply_text("\n".join(lines))


async def queue_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """清理失败任务"""
    record_update(update)
    log_update_metadata("/queue_clear", update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    cleared = _clear_failed_tasks(user_id, chat_id)
    if cleared == 0:
        await update.message.reply_text("当前没有可清理的失败任务。")
        return
    await update.message.reply_text(f"已清理 {cleared} 条失败任务。")


async def retry_failed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """批量重试失败任务"""
    record_update(update)
    log_update_metadata("/retry_failed", update)
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        logger.error("/retry_failed 缺少chat或user信息")
        return

    failed_tasks = [
        task
        for task in _list_active_tasks(user.id, chat.id)
        if (task.get("status") or "").lower() == "failed"
    ]
    if not failed_tasks:
        await update.message.reply_text("当前没有失败任务。")
        return

    retry_items = []
    for task in failed_tasks:
        url = task.get("url")
        if not url:
            continue
        location = task.get("location") or DEFAULT_LOCATION
        tags = task.get("tags") or []
        hotwords = task.get("hotwords") or []
        retry_items.append((url, location, tags, hotwords))

    if not retry_items:
        await update.message.reply_text("失败任务缺少URL，无法重试。")
        return

    for url, location, tags, hotwords in retry_items:
        schedule_background_task(
            context,
            process_url_with_location(
                user.id,
                chat.id,
                url,
                location,
                context,
                tags=tags,
                hotwords=hotwords,
            ),
        )

    await update.message.reply_text(
        f"♻️ 已重新提交 {len(retry_items)} 条失败任务，处理中。失败记录仍保留，可用 /queue_clear 清理。"
    )


async def hotword_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看热词配置状态"""
    record_update(update)
    log_update_metadata("/hotword_status", update)
    logger.info("/hotword_status command: user=%s", update.effective_user.id)
    settings = await asyncio.to_thread(fetch_hotword_settings_from_server)
    if settings:
        context.application.bot_data["hotword_settings"] = settings
    else:
        settings = context.application.bot_data.get("hotword_settings", {})

    auto_state = "开启" if settings.get("auto_hotwords") else "关闭"
    post_state = "开启" if settings.get("post_process") else "关闭"
    mode = settings.get("mode", "user_only")
    max_count = settings.get("max_count", 20)

    await update.message.reply_text(
        f"🔤 自动热词：{auto_state}\n"
        f"🛠 热词后处理：{post_state}\n"
        f"🧭 当前模式：{mode}\n"
        f"🔢 热词上限：{max_count}"
    )


async def hotword_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """切换热词开关"""
    record_update(update)
    log_update_metadata("/hotword_toggle", update)
    user_id = update.effective_user.id
    logger.info(
        "/hotword_toggle command: user=%s text=%r",
        user_id,
        update.message.text if update.message else None,
    )
    if not is_admin_user(user_id):
        await update.message.reply_text("❌ 仅管理员可以执行该操作。")
        return

    text = (update.message.text or "").strip()
    parts = text.split(maxsplit=1)
    desired_state = None

    if len(parts) > 1:
        arg = parts[1].lower()
        if arg in {"on", "true", "enable", "1", "开启", "open"}:
            desired_state = True
        elif arg in {"off", "false", "disable", "0", "关闭", "close"}:
            desired_state = False
        else:
            await update.message.reply_text(
                "❌ 参数无效，请使用 /hotword_toggle [on|off]"
            )
            return

    current_settings = context.application.bot_data.get("hotword_settings", {})
    if desired_state is None:
        desired_state = not current_settings.get("auto_hotwords", False)

    try:
        new_settings = await asyncio.to_thread(
            update_hotword_settings_on_server, {"auto_hotwords": desired_state}
        )
    except Exception as exc:
        logger.error("更新热词开关失败: %s", exc)
        await update.message.reply_text("❌ 切换热词开关失败，请稍后重试。")
        return

    context.application.bot_data["hotword_settings"] = new_settings
    auto_state = "开启" if new_settings.get("auto_hotwords") else "关闭"
    post_state = "开启" if new_settings.get("post_process") else "关闭"
    mode = new_settings.get("mode", "user_only")
    max_count = new_settings.get("max_count", 20)

    await update.message.reply_text(
        f"🔤 自动热词已{auto_state}\n"
        f"🛠 热词后处理：{post_state}\n"
        f"🧭 当前模式：{mode}\n"
        f"🔢 热词上限：{max_count}"
    )


async def monitor_process_completion(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    message_id: int,
    process_id: str,
    poll_interval: int = 8,
    max_attempts: int = 120,
) -> None:
    """轮询字幕处理任务状态，完成后自动发送字幕."""
    poll_url = f"{SUBTITLE_PROCESSOR_URL}/process/status/{process_id}"
    logger.debug(
        "monitor_process_completion: chat=%s message=%s process=%s url=%s",
        chat_id,
        message_id,
        process_id,
        poll_url,
    )

    class DummyChat:
        def __init__(self, chat_id: int):
            self.id = chat_id

    class DummyUpdate:
        def __init__(self, chat_id: int):
            self.effective_chat = DummyChat(chat_id)

    content_wait_attempts = 0
    max_content_attempts = 30

    for attempt in range(1, max_attempts + 1):
        try:
            response = await asyncio.to_thread(
                requests.get,
                poll_url,
                params={"include_content": "1"},
                timeout=30,
            )
        except Exception as exc:
            logger.debug(
                "轮询任务状态失败(%s) attempt=%s: %s", process_id, attempt, exc
            )
            await asyncio.sleep(poll_interval)
            continue

        if response.status_code == 404:
            logger.warning("任务不存在或已过期: %s", process_id)
            try:
                await context.bot.edit_message_text(
                    "⚠️ 未找到处理任务，请稍后重试。",
                    chat_id=chat_id,
                    message_id=message_id,
                )
            except Exception as edit_err:
                logger.debug("更新消息失败: %s", edit_err)
            _update_last_request(
                user_id,
                chat_id,
                status="failed",
                error="未找到处理任务",
            )
            _update_active_task_status(
                user_id, chat_id, process_id, "failed", error="未找到处理任务"
            )
            return

        if response.status_code >= 500:
            logger.warning(
                "任务状态查询失败(%s): %s %s",
                process_id,
                response.status_code,
                response.text,
            )
            await asyncio.sleep(poll_interval)
            continue

        try:
            payload = response.json()
        except ValueError:
            logger.error("任务状态返回非JSON(%s): %s", process_id, response.text)
            await asyncio.sleep(poll_interval)
            continue

        status = (payload.get("status") or "").lower()
        logger.debug(
            "任务状态(%s) attempt=%s status=%s progress=%s",
            process_id,
            attempt,
            status,
            payload.get("progress"),
        )
        if status:
            _update_active_task_status(user_id, chat_id, process_id, status)

        if status == "completed":
            subtitle_content = payload.get("subtitle_content") or ""
            logger.debug(
                "任务完成检测: process=%s subtitle_len=%s",
                process_id,
                len(subtitle_content),
            )
            filename = payload.get("filename") or f"{process_id}.srt"
            video_info = payload.get("video_info") or {}

            if not subtitle_content.strip():
                view_status_url = f"{poll_url}/subtitle"
                try:
                    subtitle_response = await asyncio.to_thread(
                        requests.get,
                        view_status_url,
                        timeout=30,
                    )
                    if subtitle_response.status_code == 200:
                        subtitle_candidate = subtitle_response.text or ""
                        logger.debug(
                            "通过字幕接口获取内容: process=%s len=%s",
                            process_id,
                            len(subtitle_candidate),
                        )
                        if subtitle_candidate.strip():
                            subtitle_content = subtitle_candidate
                    elif subtitle_response.status_code == 202:
                        logger.debug("字幕接口返回未就绪状态(202): %s", process_id)
                    else:
                        logger.debug(
                            "字幕接口返回状态码 %s: %s",
                            subtitle_response.status_code,
                            subtitle_response.text,
                        )
                except Exception as subtitle_fetch_error:
                    logger.debug(
                        "通过字幕接口获取失败(%s): %s",
                        process_id,
                        subtitle_fetch_error,
                    )

            if not subtitle_content.strip():
                content_wait_attempts += 1
                if content_wait_attempts <= max_content_attempts:
                    logger.debug(
                        "任务完成但字幕内容为空，等待重试(%s/%s): %s",
                        content_wait_attempts,
                        max_content_attempts,
                        process_id,
                    )
                    await asyncio.sleep(max(2, poll_interval // 2))
                    continue
                logger.warning(
                    "任务完成但仍未获取字幕内容，将提示用户在网页查看: %s", process_id
                )
                try:
                    await context.bot.edit_message_text(
                        "⚠️ 视频处理完成，但未能获取字幕内容，请稍后在网页查看。",
                        chat_id=chat_id,
                        message_id=message_id,
                    )
                except Exception as edit_err:
                    logger.debug("更新消息失败: %s", edit_err)
                _update_last_request(
                    user_id,
                    chat_id,
                    status="failed",
                    error="字幕内容为空",
                )
                _update_active_task_status(
                    user_id, chat_id, process_id, "failed", error="字幕内容为空"
                )
                return

            try:
                await context.bot.edit_message_text(
                    "✅ 视频处理完成，正在发送字幕文件...",
                    chat_id=chat_id,
                    message_id=message_id,
                )
            except Exception as edit_err:
                logger.debug("更新消息失败: %s", edit_err)

            original_name = (
                (video_info.get("title") if isinstance(video_info, dict) else None)
                or payload.get("original_filename")
                or process_id
            )

            result_payload = {
                "subtitle_content": subtitle_content,
                "filename": filename,
                "video_info": video_info,
                "source": payload.get("source", "auto"),
                "original_filename": original_name,
            }
            logger.debug(
                "字幕发送前检查: process=%s payload_keys=%s video_info_title=%r original_name=%r filename=%r",
                process_id,
                list(payload.keys()),
                video_info.get("title") if isinstance(video_info, dict) else None,
                original_name,
                filename,
            )

            logger.debug(
                "准备发送字幕: process=%s filename=%s content_length=%s",
                process_id,
                filename,
                len(subtitle_content),
            )
            try:
                await send_subtitle_file(DummyUpdate(chat_id), context, result_payload)
                _update_last_request(
                    user_id,
                    chat_id,
                    status="completed",
                    error=None,
                )
                _remove_active_task(user_id, chat_id, process_id)
            except Exception as send_error:
                logger.error("发送字幕失败(%s): %s", process_id, send_error)
                _update_last_request(
                    user_id,
                    chat_id,
                    status="failed",
                    error="发送字幕失败",
                )
                _update_active_task_status(
                    user_id, chat_id, process_id, "failed", error="发送字幕失败"
                )
            return

        if status == "failed":
            error_message = payload.get("error") or "处理失败"
            try:
                await context.bot.edit_message_text(
                    f"❌ 视频处理失败：{error_message}",
                    chat_id=chat_id,
                    message_id=message_id,
                )
            except Exception as edit_err:
                logger.debug("更新消息失败: %s", edit_err)
            _update_last_request(
                user_id,
                chat_id,
                status="failed",
                error=error_message,
            )
            _update_active_task_status(
                user_id, chat_id, process_id, "failed", error=error_message
            )
            return

        await asyncio.sleep(poll_interval)

    logger.warning("任务超时未完成: %s", process_id)
    try:
        await context.bot.edit_message_text(
            "⏳ 处理时间超过预期，但仍在后台处理中。请稍后使用 /queue 或网页查询结果。",
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception as edit_err:
        logger.debug("更新消息失败: %s", edit_err)
    _update_last_request(
        user_id,
        chat_id,
        status="processing",
        error=None,
    )
    _update_active_task_status(user_id, chat_id, process_id, "processing")


async def process_url_with_location(
    user_id: int,
    chat_id: int,
    url: str,
    location: str,
    context: ContextTypes.DEFAULT_TYPE,
    tags: Optional[List[str]] = None,
    hotwords: Optional[List[str]] = None,
    await_completion: bool = False,
) -> None:
    """使用指定的location处理URL"""
    try:
        _update_last_request(user_id, chat_id, status="processing", error=None)
        logger.info(
            "process_url_with_location: user=%s chat=%s url=%s location=%s tags=%s hotwords=%s",
            user_id,
            chat_id,
            url,
            location,
            tags,
            hotwords,
        )
        # 标准化URL
        normalized_url, platform = normalize_url(url)
        if not normalized_url:
            await context.bot.send_message(chat_id=chat_id, text="❌ 无效的URL格式")
            _update_last_request(
                user_id,
                chat_id,
                status="failed",
                error="无效的URL格式",
            )
            return

        _update_last_request(
            user_id,
            chat_id,
            normalized_url=normalized_url,
            platform=platform,
        )

        # 获取video_id
        video_id = extract_video_id(normalized_url, platform)
        if not video_id:
            await context.bot.send_message(chat_id=chat_id, text="❌ 无法提取视频ID")
            _update_last_request(
                user_id,
                chat_id,
                status="failed",
                error="无法提取视频ID",
            )
            return

        # 记录处理信息
        tags_info = f", tags: {tags}" if tags else ""
        hotwords_info = f", hotwords: {hotwords}" if hotwords else ""
        logger.info(
            f"处理{platform}URL: {normalized_url}, location: {location}{tags_info}{hotwords_info}"
        )

        # 发送处理中的消息
        processing_message = await context.bot.send_message(
            chat_id=chat_id, text="⏳ 正在处理您的请求..."
        )
        _update_last_request(
            user_id,
            chat_id,
            status="processing",
            message_id=processing_message.message_id,
            error=None,
        )

        # 准备请求数据
        data = {
            "url": normalized_url,
            "platform": platform,
            "location": location,
            "video_id": video_id,
        }

        data["tags"] = tags or []
        data["hotwords"] = hotwords or []

        # 发送请求到字幕处理服务
        try:
            response = await asyncio.to_thread(
                requests.post,
                f"{SUBTITLE_PROCESSOR_URL}/process",
                json=data,
                timeout=(SUBTITLE_CONNECT_TIMEOUT, SUBTITLE_READ_TIMEOUT),
            )
            status_code = response.status_code
            try:
                result = response.json()
                if isinstance(result, dict):
                    result.setdefault("original_filename", data.get("video_id"))
            except ValueError:
                result = {}

            if status_code == 202:
                process_id = result.get("process_id")
                message_text = (
                    result.get("message")
                    or "⏳ 视频已进入后台处理，完成后我会继续跟进。"
                )
                try:
                    await context.bot.edit_message_text(
                        message_text,
                        chat_id=chat_id,
                        message_id=processing_message.message_id,
                    )
                except Exception as edit_err:
                    logger.debug("更新排队消息失败: %s", edit_err)

                if process_id:
                    _register_active_task(
                        user_id,
                        chat_id,
                        process_id,
                        normalized_url,
                        "queued",
                        location=location,
                        tags=tags,
                        hotwords=hotwords,
                        message_id=processing_message.message_id,
                    )
                    if await_completion:
                        await monitor_process_completion(
                            context,
                            user_id,
                            chat_id,
                            processing_message.message_id,
                            process_id,
                        )
                    else:
                        schedule_background_task(
                            context,
                            monitor_process_completion(
                                context,
                                user_id,
                                chat_id,
                                processing_message.message_id,
                                process_id,
                            ),
                        )
                    _update_last_request(
                        user_id,
                        chat_id,
                        status="queued",
                        error=None,
                        process_id=process_id,
                    )
                else:
                    logger.warning("202 响应缺少 process_id，无法继续跟踪")
                    _update_last_request(
                        user_id,
                        chat_id,
                        status="failed",
                        error="处理队列返回缺少process_id",
                    )
                return

            response.raise_for_status()

            if not result:
                logger.warning("处理结果为空，无法发送字幕")
                await context.bot.edit_message_text(
                    "⚠️ 未收到字幕结果，请稍后在网页查询。",
                    chat_id=chat_id,
                    message_id=processing_message.message_id,
                )
                _update_last_request(
                    user_id,
                    chat_id,
                    status="failed",
                    error="未收到字幕结果",
                )
                return

            if not result.get("subtitle_content"):
                logger.warning("处理结果缺少 subtitle_content: %s", result.keys())
                await context.bot.edit_message_text(
                    "⚠️ 字幕生成结果暂不可用，请稍后在网页查询。",
                    chat_id=chat_id,
                    message_id=processing_message.message_id,
                )
                _update_last_request(
                    user_id,
                    chat_id,
                    status="failed",
                    error="字幕生成结果缺失",
                )
                return

            try:
                await context.bot.edit_message_text(
                    "✅ 视频处理完成，正在发送字幕文件...",
                    chat_id=chat_id,
                    message_id=processing_message.message_id,
                )
            except Exception as edit_err:
                logger.debug("更新完成消息失败: %s", edit_err)

            class DummyChat:
                def __init__(self, chat_id):
                    self.id = chat_id

            class DummyUpdate:
                def __init__(self, chat_id):
                    self.effective_chat = DummyChat(chat_id)

            await send_subtitle_file(DummyUpdate(chat_id), context, result)
            _update_last_request(
                user_id,
                chat_id,
                status="completed",
                error=None,
            )

        except requests.exceptions.RequestException as e:
            error_message = f"处理请求时出错: {str(e)}"
            logger.error(error_message)
            await context.bot.edit_message_text(
                f"❌ {error_message}",
                chat_id=chat_id,
                message_id=processing_message.message_id,
            )
            _update_last_request(
                user_id,
                chat_id,
                status="failed",
                error=error_message,
            )
            return

    except Exception as e:
        error_message = f"处理URL时出错: {str(e)}"
        logger.error(error_message)
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ {error_message}")
        except:
            pass
        _update_last_request(
            user_id,
            chat_id,
            status="failed",
            error=error_message,
        )


def signal_handler(signum, frame):
    """处理进程信号"""
    logger.info(f"收到信号 {signum}，正在关闭应用...")
    sys.exit(0)


def main():
    """启动bot"""
    if not TELEGRAM_ENABLED:
        logger.warning(
            "TELEGRAM_BOT_ENABLED 为 false，进程仅提供健康检查，不会注册 Webhook 或处理消息"
        )
        # 启动健康检查服务器，保持容器处于就绪状态
        health_thread = Thread(target=start_health_server, daemon=True)
        health_thread.start()
        logger.info("健康检查服务器已启动在端口8081 (bot 已禁用)")
        try:
            while True:
                time.sleep(300)
        except KeyboardInterrupt:
            logger.info("检测到退出信号，停止已禁用的 bot 实例")
        return

    if not TELEGRAM_TOKEN:
        logger.error("请设置TELEGRAM_TOKEN环境变量或配置文件！")
        return

    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("正在初始化Telegram Bot...")

    # 启动健康检查服务器
    health_thread = Thread(target=start_health_server, daemon=True)
    health_thread.start()
    logger.info("健康检查服务器已启动在端口8081")

    # 创建应用并配置代理
    proxy_url = PROXY.replace("http://", "http://") if PROXY else None

    # 创建默认配置 - 修复时区警告
    from zoneinfo import ZoneInfo

    try:
        timezone = ZoneInfo("Asia/Shanghai")
    except:
        # 回退到pytz
        import pytz

        timezone = pytz.timezone("Asia/Shanghai")

    defaults = telegram.ext.Defaults(
        tzinfo=timezone,  # 使用新的时区API
        parse_mode=telegram.constants.ParseMode.HTML,
        link_preview_options=telegram.LinkPreviewOptions(is_disabled=True),
        disable_notification=False,
        block=True,
    )

    # 创建应用
    application_builder = Application.builder().token(TELEGRAM_TOKEN).defaults(defaults)

    # 如果有代理，添加代理配置 - 修复新版本API
    if proxy_url:
        try:
            # 新版本API
            application_builder.proxy(proxy_url)
            application_builder.connect_timeout(60.0)  # 增加连接超时
            application_builder.read_timeout(60.0)  # 增加读取超时
            application_builder.write_timeout(60.0)  # 增加写入超时
            application_builder.pool_timeout(60.0)  # 连接池超时
            logger.info(f"使用代理: {proxy_url}")
        except AttributeError:
            # 如果新API不存在，尝试旧API
            try:
                application_builder.proxy_url(proxy_url)
                application_builder.connect_timeout(60.0)
                application_builder.read_timeout(60.0)
                application_builder.write_timeout(60.0)
                application_builder.pool_timeout(60.0)
                logger.info(f"使用代理 (旧API): {proxy_url}")
            except AttributeError:
                logger.warning(f"无法设置代理，当前版本不支持: {proxy_url}")
                # 不使用代理继续运行

    # 构建应用
    application = application_builder.build()
    application.bot_data["hotword_settings"] = fetch_hotword_settings_from_server()
    logger.info("初始化热词设置: %s", application.bot_data.get("hotword_settings", {}))

    # 启动连接监控器
    connection_monitor(application)

    # 周期性心跳检查与状态日志
    async def ping_telegram(context: ContextTypes.DEFAULT_TYPE):
        global \
            last_heartbeat_ok, \
            last_ping_ms, \
            last_heartbeat_at, \
            consecutive_heartbeat_failures, \
            is_bot_healthy
        start_ts = time.time()
        try:
            await context.bot.get_me()
            last_heartbeat_ok = True
            last_heartbeat_at = time.time()
            last_ping_ms = int((last_heartbeat_at - start_ts) * 1000)
            consecutive_heartbeat_failures = 0
            is_bot_healthy = True
        except Exception as e:
            last_heartbeat_ok = False
            last_heartbeat_at = time.time()
            last_ping_ms = None
            consecutive_heartbeat_failures += 1
            logger.warning(
                f"心跳失败 #{consecutive_heartbeat_failures}: {type(e).__name__}: {e}"
            )
            if consecutive_heartbeat_failures >= 3:
                is_bot_healthy = False

    async def log_status(context: ContextTypes.DEFAULT_TYPE):
        now = time.time()
        logger.info(
            "Bot状态: healthy=%s, idle=%ss, since_update=%ss, hb_ok=%s, hb_age=%ss, ping_ms=%s, last_update_id=%s",
            is_bot_healthy,
            int(now - last_activity),
            int(now - (last_update_at or 0)),
            last_heartbeat_ok,
            int(now - (last_heartbeat_at or 0)),
            last_ping_ms,
            last_update_id,
        )

    # 安排周期任务
    application.job_queue.run_repeating(ping_telegram, interval=120, first=10)
    application.job_queue.run_repeating(log_status, interval=300, first=60)

    # 添加错误处理器
    application.add_error_handler(error_handler)

    # 添加处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("process", process_url))
    application.add_handler(CommandHandler("skip", skip_command))
    application.add_handler(CommandHandler("retry", retry_command))
    application.add_handler(CommandHandler("queue", queue_status))
    application.add_handler(CommandHandler("queue_clear", queue_clear))
    application.add_handler(CommandHandler("retry_failed", retry_failed))
    application.add_handler(CommandHandler("hotword_status", hotword_status))
    application.add_handler(CommandHandler("hotword_toggle", hotword_toggle))
    # 处理普通消息
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # 启动Bot - 支持Webhook或Polling
    logger.info("启动Telegram Bot")

    use_webhook = WEBHOOK_ENABLED and WEBHOOK_PUBLIC_URL
    if WEBHOOK_ENABLED and not WEBHOOK_PUBLIC_URL:
        logger.error("已启用Webhook但未提供public_url，自动回退到Polling模式")
        use_webhook = False

    if use_webhook:
        logger.info(
            "以Webhook模式运行: url=%s listen=%s port=%s path=%s",
            WEBHOOK_PUBLIC_URL,
            WEBHOOK_LISTEN,
            WEBHOOK_PORT,
            WEBHOOK_PATH,
        )
        try:
            application.run_webhook(
                listen=WEBHOOK_LISTEN,
                port=WEBHOOK_PORT,
                url_path=WEBHOOK_PATH.strip("/"),
                webhook_url=WEBHOOK_PUBLIC_URL,
                bootstrap_retries=5,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=WEBHOOK_DROP_PENDING,
                secret_token=WEBHOOK_SECRET_TOKEN,
            )
        except Exception as e:
            logger.error(f"Webhook模式启动失败，尝试回退到Polling: {e}")
            use_webhook = False

    if not use_webhook:
        # 设置轮询参数以增强连接稳定性
        polling_kwargs = {
            "allowed_updates": Update.ALL_TYPES,
            "drop_pending_updates": True,
            "timeout": 30,  # 轮询超时30秒
            "bootstrap_retries": 5,  # 启动重试次数
        }

        try:
            application.run_polling(**polling_kwargs)
        except Exception as e:
            logger.error(f"Bot polling失败: {str(e)}")
            raise


if __name__ == "__main__":
    main()
