"""Upload routes for file and URL processing."""

import logging
import os
import re
import threading
import uuid
from datetime import datetime

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from ..config.config_manager import get_config_value
from ..services.file_service import FileService
from ..services.readwise_service import ReadwiseService
from ..services.processing_service import ProcessingService
from ..services.subtitle_service import SubtitleService
from ..services.transcription_service import TranscriptionService
from ..services.translation_service import TranslationService
from ..services.video_service import VideoService
from ..services.runtime import service_proxy

logger = logging.getLogger(__name__)

# 创建蓝图
upload_bp = Blueprint("upload", __name__, url_prefix="/upload")

# 初始化服务
file_service = service_proxy(FileService)
video_service = service_proxy(VideoService)
transcription_service = service_proxy(TranscriptionService)
subtitle_service = service_proxy(SubtitleService)
translation_service = service_proxy(TranslationService)
readwise_service = service_proxy(ReadwiseService)
processing_service = service_proxy(
    lambda: ProcessingService(
        file_service=file_service,
        video_service=video_service,
        transcription_service=transcription_service,
        subtitle_service=subtitle_service,
        readwise_service=readwise_service,
        translation_service=translation_service,
    )
)

def configure_services(services):
    """Bind this module to the application service set."""
    global file_service, video_service, transcription_service
    global subtitle_service, translation_service, readwise_service, processing_service

    file_service = services.file_service
    video_service = services.video_service
    transcription_service = services.transcription_service
    subtitle_service = services.subtitle_service
    translation_service = services.translation_service
    readwise_service = services.readwise_service
    processing_service = services.processing_service


def _run_video_task_with_app_context(app, task_info, auto_transcribe):
    with app.app_context():
        _process_video_task(task_info, auto_transcribe)


def _to_bool(value, default=False):
    """Parse common JSON/form boolean values."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _normalize_tags(value):
    if not value:
        return []
    if isinstance(value, str):
        values = re.split(r"[,，]", value)
    else:
        values = value
    return [str(tag).strip() for tag in values if str(tag).strip()]


def _count_srt_entries(srt_content):
    return processing_service.count_srt_entries(srt_content)


@upload_bp.route("/", methods=["GET", "POST"])
def upload_file():
    """文件上传页面和处理"""
    if request.method == "GET":
        return render_template("upload.html")

    try:
        # 检查是否有文件上传
        if "file" not in request.files:
            flash("没有选择文件", "error")
            return redirect(request.url)

        file = request.files["file"]
        if file.filename == "":
            flash("没有选择文件", "error")
            return redirect(request.url)

        # 检查文件类型
        allowed_extensions = get_config_value(
            "app.allowed_extensions", [".txt", ".srt", ".vtt", ".wav", ".mp3", ".m4a"]
        )
        filename = secure_filename(file.filename)
        file_ext = os.path.splitext(filename)[1].lower()

        if file_ext not in allowed_extensions:
            flash(f"不支持的文件类型: {file_ext}", "error")
            return redirect(request.url)

        # 生成文件ID和保存文件
        file_id = str(uuid.uuid4())
        file_path = os.path.join(file_service.upload_folder, f"{file_id}{file_ext}")
        file.save(file_path)

        # 创建文件信息
        file_info = {
            "id": file_id,
            "original_filename": file.filename,
            "filename": f"{file_id}{file_ext}",
            "file_path": file_path,
            "file_size": os.path.getsize(file_path),
            "upload_time": datetime.now().isoformat(),
            "status": "uploaded",
            "file_type": _detect_file_type(file_ext),
        }

        # 保存文件信息
        file_service.add_file_info(file_id, file_info)

        logger.info(f"文件上传成功: {filename} -> {file_id}")
        flash(f"文件上传成功: {filename}", "success")

        # 根据文件类型重定向到相应的处理页面
        if file_info["file_type"] == "audio":
            return redirect(url_for("process.transcribe_audio", file_id=file_id))
        elif file_info["file_type"] == "subtitle":
            return redirect(url_for("process.process_subtitle", file_id=file_id))
        else:
            return redirect(url_for("view.file_detail", file_id=file_id))

    except Exception as e:
        logger.error(f"文件上传失败: {str(e)}")
        flash(f"文件上传失败: {str(e)}", "error")
        return redirect(request.url)


@upload_bp.route("/url", methods=["GET", "POST"])
def upload_url():
    """URL处理页面和处理"""
    if request.method == "GET":
        return render_template("upload_url.html")

    try:
        # 获取URL (支持JSON和表单数据)
        if request.is_json:
            data = request.get_json(silent=True) or {}
            url = data.get("url", "").strip()
            extract_audio = _to_bool(data.get("extract_audio"), True)
            auto_transcribe = _to_bool(data.get("auto_transcribe"), False)
            auto_start = _to_bool(data.get("auto_start"), True)  # 默认自动开始处理
            tags = _normalize_tags(data.get("tags", []))  # 获取用户指定的标签
            request_source = (data.get("request_source") or "").strip().lower()
            page_title = _clean_text(data.get("page_title") or data.get("title"))
            video_id = _clean_text(data.get("video_id"))
        else:
            url = request.form.get("url", "").strip()
            extract_audio = _to_bool(request.form.get("extract_audio"), False)
            auto_transcribe = _to_bool(request.form.get("auto_transcribe"), False)
            auto_start = _to_bool(request.form.get("auto_start"), False)
            tags = _normalize_tags(request.form.get("tags", ""))  # 表单数据中的标签
            request_source = (request.form.get("request_source") or "").strip().lower()
            page_title = _clean_text(
                request.form.get("page_title") or request.form.get("title")
            )
            video_id = _clean_text(request.form.get("video_id"))

        if not url:
            if request.is_json:
                return jsonify({"error": "请输入视频URL"}), 400
            flash("请输入视频URL", "error")
            return redirect(request.url)

        # 检测平台
        platform = _detect_platform(url)
        if not platform:
            if request.is_json:
                return jsonify({"error": "不支持的视频平台"}), 400
            flash("不支持的视频平台", "error")
            return redirect(request.url)

        # 生成处理ID
        process_id = str(uuid.uuid4())

        # 创建处理任务信息
        task_info = {
            "id": process_id,
            "url": url,
            "platform": platform,
            "tags": tags,  # 保存用户指定的标签
            "status": "pending",
            "created_time": datetime.now().isoformat(),
            "updated_time": datetime.now().isoformat(),
            "auto_transcribe": auto_transcribe,
            "extract_audio": extract_audio,
            "request_source": request_source or None,
        }
        if page_title:
            task_info["page_title"] = page_title
            task_info["filename"] = page_title
        if video_id:
            task_info["video_id"] = video_id

        # 保存任务信息
        file_service.add_file_info(process_id, task_info)

        logger.info(f"URL处理任务创建: {url} -> {process_id}")
        logger.info(f"自动启动设置: {auto_start}")
        logger.info(f"用户标签: {tags}")
        print(f"DEBUG: auto_start = {auto_start}, type = {type(auto_start)}")
        print(f"DEBUG: user_tags = {tags}")

        if auto_start:
            app = current_app._get_current_object()
            thread = threading.Thread(
                target=_run_video_task_with_app_context,
                args=(app, dict(task_info), auto_transcribe),
                daemon=True,
                name=f"video-task-{process_id}",
            )
            thread.start()

        # 根据请求类型返回不同响应
        if request.is_json:
            response_data = {
                "success": True,
                "process_id": process_id,
                "status_url": f"/process/status/{process_id}",
                "process_url": f"/process/video/{process_id}",
                "view_url": f"/view/{process_id}",
                "platform": platform,
                "status": task_info.get("status", "pending"),
                "readwise_mode": task_info.get("readwise_mode"),
                "readwise_reason": task_info.get("readwise_reason"),
                "readwise_url_only": task_info.get("readwise_url_only", False),
                "readwise_article_id": task_info.get("readwise_article_id"),
                "readwise_url": task_info.get("readwise_url"),
            }
            if auto_start:
                response_data.update(
                    {
                        "message": "视频处理任务已开始，结果请稍后通过 status_url 查询",
                        "auto_started": True,
                        "status": "processing",
                    }
                )
                return jsonify(response_data), 202
            response_data["message"] = "视频处理任务已创建"
            return jsonify(response_data)
        else:
            if auto_start:
                flash("视频处理任务已创建，正在后台处理", "success")
            else:
                flash("视频处理任务已创建", "success")
            return redirect(url_for("process.process_video", process_id=process_id))

    except Exception as e:
        logger.error(f"URL处理失败: {str(e)}")
        if request.is_json:
            return jsonify({"error": f"URL处理失败: {str(e)}"}), 500
        flash(f"URL处理失败: {str(e)}", "error")
        return redirect(request.url)


@upload_bp.route("/batch", methods=["GET", "POST"])
def batch_upload():
    """批量文件上传"""
    if request.method == "GET":
        return render_template("batch_upload.html")

    try:
        files = request.files.getlist("files")
        if not files or len(files) == 0:
            flash("没有选择文件", "error")
            return redirect(request.url)

        results = []
        successful = 0
        failed = 0

        for file in files:
            if file.filename == "":
                continue

            try:
                # 处理单个文件
                filename = secure_filename(file.filename)
                file_ext = os.path.splitext(filename)[1].lower()

                # 检查文件类型
                allowed_extensions = get_config_value(
                    "app.allowed_extensions",
                    [".txt", ".srt", ".vtt", ".wav", ".mp3", ".m4a"],
                )
                if file_ext not in allowed_extensions:
                    results.append(
                        {
                            "filename": filename,
                            "status": "failed",
                            "error": f"不支持的文件类型: {file_ext}",
                        }
                    )
                    failed += 1
                    continue

                # 保存文件
                file_id = str(uuid.uuid4())
                file_path = os.path.join(
                    file_service.upload_folder, f"{file_id}{file_ext}"
                )
                file.save(file_path)

                # 创建文件信息
                file_info = {
                    "id": file_id,
                    "original_filename": filename,
                    "filename": f"{file_id}{file_ext}",
                    "file_path": file_path,
                    "file_size": os.path.getsize(file_path),
                    "upload_time": datetime.now().isoformat(),
                    "status": "uploaded",
                    "file_type": _detect_file_type(file_ext),
                }

                file_service.add_file_info(file_id, file_info)

                results.append(
                    {"filename": filename, "status": "success", "file_id": file_id}
                )
                successful += 1

            except Exception as e:
                logger.error(f"批量上传文件失败 {filename}: {str(e)}")
                results.append(
                    {"filename": filename, "status": "failed", "error": str(e)}
                )
                failed += 1

        flash(f"批量上传完成 - 成功: {successful}, 失败: {failed}", "success")
        return render_template("batch_upload_result.html", results=results)

    except Exception as e:
        logger.error(f"批量上传失败: {str(e)}")
        flash(f"批量上传失败: {str(e)}", "error")
        return redirect(request.url)


@upload_bp.route("/status/<file_id>")
def upload_status(file_id):
    """获取上传状态"""
    try:
        file_info = file_service.get_file_info(file_id)
        if not file_info:
            return jsonify({"error": "File not found"}), 404

        return jsonify(file_info)

    except Exception as e:
        logger.error(f"获取上传状态失败: {str(e)}")
        return jsonify({"error": str(e)}), 500


@upload_bp.route("/validate", methods=["POST"])
def validate_file():
    """验证文件（AJAX接口）"""
    try:
        if "file" not in request.files:
            return jsonify({"valid": False, "message": "没有选择文件"})

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"valid": False, "message": "没有选择文件"})

        filename = secure_filename(file.filename)
        file_ext = os.path.splitext(filename)[1].lower()

        # 检查文件类型
        allowed_extensions = get_config_value(
            "app.allowed_extensions", [".txt", ".srt", ".vtt", ".wav", ".mp3", ".m4a"]
        )
        if file_ext not in allowed_extensions:
            return jsonify({"valid": False, "message": f"不支持的文件类型: {file_ext}"})

        # 检查文件大小（如果需要）
        max_size = get_config_value("app.max_file_size", 500 * 1024 * 1024)  # 500MB
        if hasattr(file, "content_length") and file.content_length > max_size:
            return jsonify({"valid": False, "message": "文件过大"})

        return jsonify({"valid": True, "message": "文件验证通过"})

    except Exception as e:
        logger.error(f"文件验证失败: {str(e)}")
        return jsonify({"valid": False, "message": str(e)})


def _process_video_task(task_info, auto_transcribe):
    return processing_service.process_video_task(task_info, auto_transcribe)


def _detect_file_type(file_ext):
    """检测文件类型"""
    audio_extensions = [".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".wma"]
    subtitle_extensions = [".srt", ".vtt", ".txt", ".ass", ".ssa"]

    if file_ext in audio_extensions:
        return "audio"
    elif file_ext in subtitle_extensions:
        return "subtitle"
    else:
        return "unknown"


def _detect_platform(url):
    """检测视频平台"""
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    elif "bilibili.com" in url:
        return "bilibili"
    elif "acfun.cn" in url:
        return "acfun"
    else:
        return None


def _normalize_language_choice(language):
    return processing_service.normalize_language_choice(language)


def _should_request_language_confirmation(task_info, result):
    return processing_service.should_request_language_confirmation(task_info, result)


def _language_confirmation_is_resolved(task_info):
    return processing_service.language_confirmation_is_resolved(task_info)


def _request_language_confirmation_if_needed(
    process_id, task_info, result, skip_if_resolved=False, stage="unknown"
):
    return processing_service.request_language_confirmation_if_needed(
        process_id,
        task_info,
        result,
        skip_if_resolved=skip_if_resolved,
        stage=stage,
    )


def _wait_for_language_confirmation(process_id):
    return processing_service.wait_for_language_confirmation(process_id)


def _refresh_language_state_from_final_subtitle(
    task_info,
    result,
    subtitle_content,
    subtitle_track_type="asr_original",
):
    return processing_service.refresh_language_state_from_final_subtitle(
        task_info,
        result,
        subtitle_content,
        subtitle_track_type=subtitle_track_type,
    )


def _apply_language_confirmation(result, task_info, confirmation):
    return processing_service.apply_language_confirmation(result, task_info, confirmation)
