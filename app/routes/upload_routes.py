"""Upload routes for file and URL processing."""

import json
import logging
import os
import re
import threading
import time
import traceback
import uuid
from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from ..config.config_manager import get_config_value
from ..services.file_service import FileService
from ..services.readwise_service import ReadwiseService
from ..services.subtitle_service import SubtitleService
from ..services.transcription_service import TranscriptionService
from ..services.translation_service import TranslationService
from ..services.video_service import VideoService
from ..utils.file_utils import build_task_filename

logger = logging.getLogger(__name__)

# 创建蓝图
upload_bp = Blueprint("upload", __name__, url_prefix="/upload")

# 初始化服务
file_service = FileService()
video_service = VideoService()
transcription_service = TranscriptionService()
subtitle_service = SubtitleService()
translation_service = TranslationService()
readwise_service = ReadwiseService()

LANGUAGE_CONFIRMATION_TIMEOUT_SECONDS = 180
LANGUAGE_CONFIRMATION_POLL_INTERVAL_SECONDS = 1.0
LANGUAGE_CONFIRMATION_CHOICES = {"zh", "en", "auto"}
LANGUAGE_CONFIRMATION_MISMATCH_MAX_CONFIDENCE = 0.9
SRT_TIMING_LINE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}$",
    re.MULTILINE,
)


def _count_srt_entries(srt_content):
    """统计SRT中的字幕条数，忽略字面量 \\n 造成的伪换行。"""
    if not srt_content or not isinstance(srt_content, str):
        return 0
    return len(SRT_TIMING_LINE_RE.findall(srt_content))


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
            data = request.get_json()
            url = data.get("url", "").strip()
            extract_audio = data.get("extract_audio", True)
            auto_transcribe = data.get("auto_transcribe", False)
            auto_start = data.get("auto_start", True)  # 默认自动开始处理
            tags = data.get("tags", [])  # 获取用户指定的标签
            request_source = (data.get("request_source") or "").strip().lower()
        else:
            url = request.form.get("url", "").strip()
            extract_audio = request.form.get("extract_audio", "false").lower() == "true"
            auto_transcribe = (
                request.form.get("auto_transcribe", "false").lower() == "true"
            )
            auto_start = request.form.get("auto_start", "false").lower() == "true"
            tags = (
                request.form.get("tags", "").split(",")
                if request.form.get("tags")
                else []
            )  # 表单数据中的标签
            request_source = (request.form.get("request_source") or "").strip().lower()

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

        # 清理标签（移除空标签）
        tags = [tag.strip() for tag in tags if tag.strip()] if tags else []

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

        # 保存任务信息
        file_service.add_file_info(process_id, task_info)

        logger.info(f"URL处理任务创建: {url} -> {process_id}")
        logger.info(f"自动启动设置: {auto_start}")
        logger.info(f"用户标签: {tags}")
        print(f"DEBUG: auto_start = {auto_start}, type = {type(auto_start)}")
        print(f"DEBUG: user_tags = {tags}")

        if auto_start:
            thread = threading.Thread(
                target=_process_video_task,
                args=(dict(task_info), auto_transcribe),
                daemon=True,
                name=f"video-task-{process_id}",
            )
            thread.start()

        # 根据请求类型返回不同响应
        if request.is_json:
            response_data = {
                "success": True,
                "process_id": process_id,
                "status_url": f"/process/video/{process_id}",
                "platform": platform,
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
    """后台执行视频下载、转录及推送流程"""
    process_id = task_info["id"]
    url = task_info["url"]
    platform = task_info["platform"]
    tags = task_info.get("tags", []) or []
    task_temp_dir = None

    print(f"=== 开始自动视频处理流程 ===")
    print(f"处理ID: {process_id}")
    print(f"视频URL: {url}")
    print(f"平台: {platform}")
    logger.info(f"=== 开始自动视频处理流程 === {process_id}")
    logger.info(f"处理ID: {process_id}")
    logger.info(f"视频URL: {url}")
    logger.info(f"平台: {platform}")
    logger.info(f"自动转录设置: {auto_transcribe}")
    print("DEBUG: 进入自动启动分支")

    task_info["status"] = "processing"
    task_info["progress"] = 0
    task_info["updated_time"] = datetime.now().isoformat()
    file_service.update_file_info(process_id, task_info)

    try:
        logger.info("第1步：开始视频下载和预处理")
        result = video_service.process_video_for_transcription(
            url=url, platform=platform
        )
        logger.info(f"第1步完成：视频处理结果存在: {result is not None}")

        if result:
            task_info["video_info"] = result.get("video_info", {})
            task_info["language"] = result.get("language")
            task_info["language_details"] = result.get("language_details")
            task_info["content_locale"] = result.get("content_locale")
            task_info["content_locale_details"] = result.get("content_locale_details")
            task_info["subtitle_content"] = result.get("subtitle_content")
            task_info["subtitle_metadata"] = result.get("subtitle_metadata")
            task_info["audio_file"] = result.get("audio_file")
            task_temp_dir = result.get("temp_dir")
            task_info["needs_transcription"] = result.get("needs_transcription", False)
            task_info["readwise_mode"] = result.get("readwise_mode")
            task_info["readwise_reason"] = result.get("readwise_reason")
            task_info["readwise_url_only"] = result.get("readwise_url_only", False)
            task_info["skip_processing_for_url_only"] = result.get(
                "skip_processing_for_url_only", False
            )
            task_info["spoken_pattern"] = result.get("spoken_pattern")
            task_info["updated_time"] = datetime.now().isoformat()
            file_service.update_file_info(process_id, task_info)

            _request_language_confirmation_if_needed(
                process_id,
                task_info,
                result,
                stage="pre_transcription",
            )

            logger.info(
                f"视频处理结果 - subtitle_content存在: {bool(result.get('subtitle_content'))}"
            )
            logger.info(
                f"视频处理结果 - needs_transcription: {result.get('needs_transcription')}"
            )
            logger.info(f"视频处理结果 - audio_file: {result.get('audio_file')}")

            if result.get("readwise_url_only") and result.get(
                "skip_processing_for_url_only"
            ):
                task_info["status"] = "completed"
                task_info["progress"] = 100
                logger.info(
                    "第2步完成：命中原始中文字幕 URL 剪藏规则，跳过字幕下载与转录: %s",
                    process_id,
                )
                logger.info(f"第3步：开始发送URL剪藏到Readwise Reader: {process_id}")

                try:
                    readwise_result = readwise_service.create_article_from_subtitle(
                        task_info
                    )
                    logger.info(f"Readwise调用返回结果(URL剪藏): {readwise_result}")

                    if readwise_result:
                        task_info["readwise_article_id"] = readwise_result.get("id")
                        task_info["readwise_url"] = readwise_result.get("url")
                        logger.info(
                            f"第3步完成：Readwise URL剪藏成功: {process_id} -> {readwise_result.get('id')}"
                        )
                    else:
                        logger.warning(f"第3步失败：Readwise URL剪藏失败: {process_id}")
                except Exception as e:
                    logger.error(
                        f"第3步错误：发送URL剪藏到Readwise失败: {process_id} - {str(e)}"
                    )
                    logger.error(f"异常堆栈(URL剪藏): {traceback.format_exc()}")

                task_info["updated_time"] = datetime.now().isoformat()
                logger.info(f"=== 视频处理流程完成 === {process_id}")

            elif result.get("subtitle_content"):
                raw_subtitle_content = result.get("subtitle_content")
                source_subtitle_format = subtitle_service.detect_subtitle_format(
                    raw_subtitle_content
                )
                normalized_subtitle_content = (
                    subtitle_service.normalize_external_subtitle_content(
                        raw_subtitle_content
                    )
                )

                if normalized_subtitle_content:
                    task_info["subtitle_content"] = normalized_subtitle_content
                else:
                    task_info["subtitle_content"] = raw_subtitle_content

                if isinstance(raw_subtitle_content, str):
                    converted_subtitle = (
                        task_info["subtitle_content"] != raw_subtitle_content
                    )
                    raw_length = len(raw_subtitle_content)
                else:
                    converted_subtitle = True
                    raw_length = 0
                normalized_length = (
                    len(task_info["subtitle_content"])
                    if isinstance(task_info["subtitle_content"], str)
                    else 0
                )
                logger.info(
                    "字幕规范化结果: source_format=%s, converted=%s, raw_len=%s, normalized_len=%s",
                    source_subtitle_format,
                    converted_subtitle,
                    raw_length,
                    normalized_length,
                )

                task_info["status"] = "completed"
                task_info["progress"] = 100
                if not task_info.get("subtitle_path"):
                    safe_title = task_info.get("video_info", {}).get("title") or process_id
                    subtitle_filename = build_task_filename(safe_title, process_id)
                    subtitle_path = file_service.save_file(
                        task_info.get("subtitle_content", ""), subtitle_filename
                    )
                    task_info["subtitle_path"] = subtitle_path
                    task_info["filename"] = subtitle_filename
                logger.info(f"第2步完成：视频已有字幕，无需转录: {process_id}")
                logger.info(f"第3步：开始发送内容到Readwise Reader: {process_id}")

                logger.debug("调试信息(有字幕) - task_info关键字段:")
                logger.debug(f"  - video_info存在: {bool(task_info.get('video_info'))}")
                logger.debug(
                    f"  - subtitle_content存在: {bool(task_info.get('subtitle_content'))}"
                )
                logger.debug(
                    f"  - subtitle_content长度: {len(task_info.get('subtitle_content', ''))}"
                )
                logger.debug(f"  - tags: {task_info.get('tags')}")

                try:
                    logger.info(
                        "调用readwise_service.create_article_from_subtitle(有字幕)..."
                    )
                    readwise_result = readwise_service.create_article_from_subtitle(
                        task_info
                    )
                    logger.info(f"Readwise调用返回结果(有字幕): {readwise_result}")

                    if readwise_result:
                        task_info["readwise_article_id"] = readwise_result.get("id")
                        task_info["readwise_url"] = readwise_result.get("url")
                        logger.info(
                            f"第3步完成：Readwise文章创建成功: {process_id} -> {readwise_result.get('id')}"
                        )
                    else:
                        logger.warning(f"第3步失败：Readwise文章创建失败: {process_id}")
                        logger.warning(
                            f"readwise_service返回了None或False(有字幕): {readwise_result}"
                        )
                except Exception as e:
                    logger.error(
                        f"第3步错误：发送到Readwise失败: {process_id} - {str(e)}"
                    )
                    logger.error(f"异常堆栈(有字幕): {traceback.format_exc()}")

                logger.info(f"=== 视频处理流程完成 === {process_id}")

            elif result.get("needs_transcription") and result.get("audio_file"):
                logger.info(f"第2步：开始音频转录流程: {process_id}")
                logger.info(f"needs_transcription: {result.get('needs_transcription')}")
                logger.info(f"audio_file: {result.get('audio_file')}")
                audio_file = result.get("audio_file")
                try:
                    logger.info(f"第2.1步：调用转录服务，音频文件: {audio_file}")
                    logger.info(f"音频文件是否存在: {os.path.exists(audio_file)}")
                    transcription_result = transcription_service.transcribe_audio(
                        audio_file=audio_file,
                        hotwords=None,
                        video_info=task_info.get("video_info", {}),
                        tags=tags,
                        platform=platform,
                    )
                    logger.info(
                        f"第2.1步完成：转录结果是否为None: {transcription_result is None}"
                    )

                    if transcription_result is None:
                        retry_limit = getattr(
                            transcription_service, "transcribe_max_retries", 5
                        )
                        failure_message = (
                            f"转录失败：已重试{retry_limit}次仍未成功，请稍后重试。"
                        )
                        task_info["status"] = "failed"
                        task_info["error"] = failure_message
                        logger.error(f"第2步失败：音频转录失败: {process_id}")

                        logger.info(
                            f"第3步：发送转录失败信息到Readwise Reader: {process_id}"
                        )
                        try:
                            failure_payload = {
                                "video_info": task_info.get("video_info", {}),
                                "tags": task_info.get("tags", []),
                                "failure_message": failure_message,
                            }
                            readwise_result = (
                                readwise_service.create_article_from_subtitle(
                                    failure_payload
                                )
                            )
                            logger.info(
                                f"Readwise调用返回结果(转录失败): {readwise_result}"
                            )
                            if readwise_result:
                                task_info["readwise_article_id"] = readwise_result.get(
                                    "id"
                                )
                                task_info["readwise_url"] = readwise_result.get("url")
                                logger.info(
                                    f"第3步完成：Readwise失败提示发送成功: {process_id} -> {readwise_result.get('id')}"
                                )
                            else:
                                logger.warning(
                                    f"第3步失败：Readwise失败提示发送失败: {process_id}"
                                )
                        except Exception as e:
                            logger.error(
                                f"第3步错误：发送失败提示到Readwise失败: {process_id} - {str(e)}"
                            )
                    else:
                        logger.info(f"转录数据类型: {type(transcription_result)}")
                        if (
                            isinstance(transcription_result, dict)
                            and "text" in transcription_result
                        ):
                            text_length = (
                                len(transcription_result["text"])
                                if transcription_result["text"]
                                else 0
                            )
                            text_preview = (
                                transcription_result["text"][:100] + "..."
                                if text_length > 100
                                else transcription_result["text"]
                            )
                            logger.info(f"转录文本长度: {text_length}")
                            logger.info(f"转录文本预览: '{text_preview}'")

                        logger.info("第2.2步：开始转换为SRT格式")
                        srt_content = subtitle_service.parse_srt(
                            transcription_result, []
                        )
                        logger.info(
                            f"第2.2步完成：SRT转换结果是否为None: {srt_content is None}"
                        )
                        if srt_content:
                            srt_length = len(srt_content)
                            logger.info(f"SRT内容长度: {srt_length}")
                            subtitle_count = _count_srt_entries(srt_content)
                            logger.info(f"生成字幕条数: {subtitle_count}")

                            task_info["status"] = "completed"
                            task_info["subtitle_content"] = srt_content
                            task_info["transcription_result"] = transcription_result
                            task_info["progress"] = 100
                            safe_title = task_info.get("video_info", {}).get("title") or process_id
                            subtitle_filename = build_task_filename(
                                safe_title, process_id
                            )
                            subtitle_path = file_service.save_file(
                                srt_content, subtitle_filename
                            )
                            task_info["subtitle_path"] = subtitle_path
                            task_info["filename"] = subtitle_filename
                            logger.info(
                                f"第2步完成：音频转录和SRT转换成功: {process_id}"
                            )

                            refreshed_language_details = (
                                _refresh_language_state_from_final_subtitle(
                                    task_info,
                                    result,
                                    subtitle_content=srt_content,
                                )
                            )
                            if refreshed_language_details:
                                logger.info(
                                    "转录后语言重算完成: language=%s confidence=%.4f readwise_mode=%s reason=%s",
                                    task_info.get("language"),
                                    float(
                                        (
                                            task_info.get("language_details") or {}
                                        ).get("confidence", 0.0)
                                    ),
                                    task_info.get("readwise_mode"),
                                    task_info.get("readwise_reason"),
                                )
                                task_info["updated_time"] = datetime.now().isoformat()
                                file_service.update_file_info(
                                    process_id,
                                    {
                                        "language": task_info.get("language"),
                                        "language_details": task_info.get(
                                            "language_details"
                                        ),
                                        "content_locale": task_info.get(
                                            "content_locale"
                                        ),
                                        "content_locale_details": task_info.get(
                                            "content_locale_details"
                                        ),
                                        "readwise_mode": task_info.get(
                                            "readwise_mode"
                                        ),
                                        "readwise_reason": task_info.get(
                                            "readwise_reason"
                                        ),
                                        "readwise_url_only": task_info.get(
                                            "readwise_url_only"
                                        ),
                                        "skip_processing_for_url_only": task_info.get(
                                            "skip_processing_for_url_only"
                                        ),
                                        "spoken_pattern": task_info.get(
                                            "spoken_pattern"
                                        ),
                                        "updated_time": task_info["updated_time"],
                                    },
                                )
                                _request_language_confirmation_if_needed(
                                    process_id,
                                    task_info,
                                    result,
                                    skip_if_resolved=True,
                                    stage="post_transcription",
                                )
                                logger.info(
                                    "转录后最终语言状态: process=%s language=%s confidence=%.4f readwise_mode=%s reason=%s override=%s",
                                    process_id,
                                    task_info.get("language"),
                                    float(
                                        (
                                            task_info.get("language_details") or {}
                                        ).get("confidence", 0.0)
                                    ),
                                    task_info.get("readwise_mode"),
                                    task_info.get("readwise_reason"),
                                    task_info.get("language_override") or "auto",
                                )

                            logger.info(
                                f"第3步：开始发送内容到Readwise Reader: {process_id}"
                            )
                            logger.debug("调试信息 - task_info关键字段:")
                            logger.debug(
                                f"  - video_info存在: {bool(task_info.get('video_info'))}"
                            )
                            logger.debug(
                                f"  - subtitle_content存在: {bool(task_info.get('subtitle_content'))}"
                            )
                            logger.debug(
                                f"  - subtitle_content长度: {len(task_info.get('subtitle_content', ''))}"
                            )
                            logger.debug(f"  - tags: {task_info.get('tags')}")
                            if task_info.get("video_info"):
                                vi = task_info["video_info"]
                                logger.debug(
                                    f"  - video_info.title: {vi.get('title', 'None')}"
                                )
                                logger.debug(
                                    f"  - video_info.uploader: {vi.get('uploader', 'None')}"
                                )

                            try:
                                logger.info(
                                    "调用readwise_service.create_article_from_subtitle..."
                                )
                                readwise_result = (
                                    readwise_service.create_article_from_subtitle(
                                        task_info
                                    )
                                )
                                logger.info(f"Readwise调用返回结果: {readwise_result}")

                                if readwise_result:
                                    task_info["readwise_article_id"] = (
                                        readwise_result.get("id")
                                    )
                                    task_info["readwise_url"] = readwise_result.get(
                                        "url"
                                    )
                                    logger.info(
                                        f"第3步完成：Readwise文章创建成功: {process_id} -> {readwise_result.get('id')}"
                                    )
                                else:
                                    logger.warning(
                                        f"第3步失败：Readwise文章创建失败: {process_id}"
                                    )
                                    logger.warning(
                                        f"readwise_service返回了None或False: {readwise_result}"
                                    )
                            except Exception as e:
                                logger.error(
                                    f"第3步错误：发送到Readwise失败: {process_id} - {str(e)}"
                                )
                                logger.error(f"异常堆栈: {traceback.format_exc()}")

                            logger.info(f"=== 视频处理流程完成 === {process_id}")
                        else:
                            task_info["status"] = "failed"
                            task_info["error"] = "SRT转换失败"
                            logger.error(f"第2.2步失败：SRT转换失败: {process_id}")
                except Exception as e:
                    task_info["status"] = "failed"
                    task_info["error"] = f"转录出错: {str(e)}"
                    logger.error(f"第2步错误：转录出错: {process_id} - {str(e)}")
            else:
                logger.error(
                    f"第2步失败：未获取到可用音频文件，终止后续流程: {process_id}"
                )
                task_info["status"] = "failed"
                download_error = None
                if isinstance(result, dict):
                    download_error = result.get("download_error")
                task_info["error"] = (
                    download_error or "音频下载失败，已终止后续流程"
                )
                task_info["progress"] = task_info.get("progress", 0)
                task_info["subtitle_content"] = None
                task_info["subtitle_path"] = None
                task_info["transcription_result"] = None
                task_info["readwise_article_id"] = None
                task_info["readwise_url"] = None
                task_info["updated_time"] = datetime.now().isoformat()
        else:
            task_info["status"] = "failed"
            task_info["error"] = "视频处理失败"
            task_info["updated_time"] = datetime.now().isoformat()
            logger.error(f"第1步失败：视频处理失败: {process_id}")

    except Exception as e:
        logger.error(f"=== 视频处理流程出错 === {process_id} - {str(e)}")
        task_info["status"] = "failed"
        task_info["error"] = str(e)
        task_info["updated_time"] = datetime.now().isoformat()
    finally:
        if task_temp_dir:
            video_service.cleanup_task_artifacts(task_temp_dir)
            task_info["audio_file"] = None
        file_service.update_file_info(process_id, task_info)


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
    normalized = video_service._normalize_language_code(language)
    if normalized in {"zh", "en", "mixed"}:
        return normalized
    raw_language = (language or "").strip().lower()
    if raw_language == "auto":
        return "auto"
    return None


def _should_request_language_confirmation(task_info, result):
    if (task_info.get("request_source") or "").strip().lower() != "telegram":
        return None

    if result.get("skip_processing_for_url_only"):
        return None

    language_details = result.get("language_details") or {}
    spoken_language = _normalize_language_choice(language_details.get("language"))
    spoken_confidence = float(language_details.get("confidence", 0.0) or 0.0)
    content_locale = _normalize_language_choice(
        result.get("content_locale")
        or (result.get("content_locale_details") or {}).get("language")
    )

    trigger_reason = None
    if spoken_language == "mixed":
        trigger_reason = "mixed_spoken_language"
    elif spoken_confidence < 0.75:
        trigger_reason = "low_spoken_confidence"
    elif (
        content_locale in {"zh", "en"}
        and spoken_language in {"zh", "en"}
        and content_locale != spoken_language
        and spoken_confidence < LANGUAGE_CONFIRMATION_MISMATCH_MAX_CONFIDENCE
    ):
        trigger_reason = "content_locale_spoken_mismatch"

    if not trigger_reason:
        return None

    video_info = result.get("video_info") or {}
    return {
        "status": "pending",
        "reason": trigger_reason,
        "suggested_language": spoken_language,
        "suggested_confidence": round(spoken_confidence, 4),
        "content_locale": content_locale,
        "url": task_info.get("url"),
        "video_title": video_info.get("title"),
        "video_uploader": video_info.get("uploader") or video_info.get("channel"),
        "requested_at": datetime.now().isoformat(),
        "timeout_seconds": LANGUAGE_CONFIRMATION_TIMEOUT_SECONDS,
        "choices": ["zh", "en", "auto"],
    }


def _language_confirmation_is_resolved(task_info):
    confirmation = (task_info or {}).get("language_confirmation") or {}
    selected_language = _normalize_language_choice(
        confirmation.get("selected_language")
    )
    return selected_language in LANGUAGE_CONFIRMATION_CHOICES or confirmation.get(
        "status"
    ) in {"confirmed", "timeout"}


def _request_language_confirmation_if_needed(
    process_id, task_info, result, skip_if_resolved=False, stage="unknown"
):
    if skip_if_resolved and _language_confirmation_is_resolved(task_info):
        confirmation = (task_info or {}).get("language_confirmation") or {}
        logger.info(
            "跳过重复语言确认: process=%s stage=%s existing_status=%s selected_language=%s",
            process_id,
            stage,
            confirmation.get("status"),
            confirmation.get("selected_language") or "auto",
        )
        return None

    confirmation_state = _should_request_language_confirmation(task_info, result)
    if not confirmation_state:
        return None

    logger.info(
        "语言确认触发: process=%s stage=%s reason=%s spoken_language=%s spoken_confidence=%.4f content_locale=%s readwise_mode=%s readwise_reason=%s",
        process_id,
        stage,
        confirmation_state.get("reason"),
        confirmation_state.get("suggested_language"),
        float(confirmation_state.get("suggested_confidence", 0.0) or 0.0),
        confirmation_state.get("content_locale"),
        result.get("readwise_mode"),
        result.get("readwise_reason"),
    )

    task_info["status"] = "waiting_for_language_confirmation"
    task_info["language_confirmation"] = confirmation_state
    task_info["updated_time"] = datetime.now().isoformat()
    file_service.update_file_info(process_id, task_info)

    resolved_confirmation = _wait_for_language_confirmation(process_id)
    logger.info(
        "语言确认已解决: process=%s stage=%s status=%s selected_language=%s",
        process_id,
        stage,
        resolved_confirmation.get("status"),
        resolved_confirmation.get("selected_language") or "auto",
    )
    task_info["language_confirmation"] = resolved_confirmation
    task_info["status"] = "processing"
    task_info["updated_time"] = datetime.now().isoformat()
    file_service.update_file_info(
        process_id,
        {
            "status": "processing",
            "language_confirmation": resolved_confirmation,
            "updated_time": task_info["updated_time"],
        },
    )
    _apply_language_confirmation(result, task_info, resolved_confirmation)
    task_info["updated_time"] = datetime.now().isoformat()
    file_service.update_file_info(process_id, task_info)
    return resolved_confirmation


def _wait_for_language_confirmation(process_id):
    deadline = time.time() + LANGUAGE_CONFIRMATION_TIMEOUT_SECONDS
    while time.time() < deadline:
        current_task_info = file_service.get_file_info(process_id) or {}
        confirmation = current_task_info.get("language_confirmation") or {}
        selected_language = _normalize_language_choice(
            confirmation.get("selected_language")
        )
        if selected_language in LANGUAGE_CONFIRMATION_CHOICES:
            resolved_confirmation = dict(confirmation)
            resolved_confirmation.setdefault("status", "confirmed")
            resolved_confirmation.setdefault("resolved_at", datetime.now().isoformat())
            return resolved_confirmation
        time.sleep(LANGUAGE_CONFIRMATION_POLL_INTERVAL_SECONDS)

    current_task_info = file_service.get_file_info(process_id) or {}
    confirmation = dict(current_task_info.get("language_confirmation") or {})
    confirmation.update(
        {
            "status": "timeout",
            "selected_language": "auto",
            "resolved_at": datetime.now().isoformat(),
        }
    )
    logger.info("语言确认超时，继续自动处理: process=%s", process_id)
    file_service.update_file_info(
        process_id,
        {
            "language_confirmation": confirmation,
            "status": "processing",
            "updated_time": datetime.now().isoformat(),
        },
    )
    return confirmation


def _refresh_language_state_from_final_subtitle(
    task_info,
    result,
    subtitle_content,
    subtitle_track_type="asr_original",
):
    if not isinstance(subtitle_content, str) or not subtitle_content.strip():
        return None

    video_info = result.get("video_info") or task_info.get("video_info") or {}
    refreshed_language_details = video_service.get_video_language_details(
        video_info,
        subtitle_result={
            "content": subtitle_content,
            "track_type": subtitle_track_type,
        },
        audio_result=result.get("audio_probe"),
    )
    refreshed_content_locale_details = video_service.get_content_locale_details(
        video_info,
        language_details=refreshed_language_details,
    )
    readwise_decision = video_service._build_readwise_decision(
        result.get("track_catalog") or [],
        refreshed_language_details,
        refreshed_content_locale_details,
    )
    process_id = (
        task_info.get("id")
        or task_info.get("process_id")
        or result.get("process_id")
        or "unknown"
    )
    logger.info(
        "转录后自动语言重算: process=%s auto_language=%s auto_confidence=%.4f content_locale=%s auto_readwise_mode=%s auto_readwise_reason=%s",
        process_id,
        refreshed_language_details.get("language"),
        float(refreshed_language_details.get("confidence", 0.0) or 0.0),
        refreshed_content_locale_details.get("language"),
        readwise_decision.get("mode"),
        readwise_decision.get("reason"),
    )

    result["language"] = refreshed_language_details.get("language")
    result["language_details"] = refreshed_language_details
    result["content_locale"] = refreshed_content_locale_details.get("language")
    result["content_locale_details"] = refreshed_content_locale_details
    result["readwise_mode"] = readwise_decision.get("mode")
    result["readwise_reason"] = readwise_decision.get("reason")
    result["readwise_url_only"] = readwise_decision.get("mode") == "url_only"
    result["skip_processing_for_url_only"] = readwise_decision.get(
        "skip_processing", False
    )
    result["spoken_pattern"] = readwise_decision.get("spoken_pattern")

    task_info["language"] = result["language"]
    task_info["language_details"] = refreshed_language_details
    task_info["content_locale"] = result["content_locale"]
    task_info["content_locale_details"] = refreshed_content_locale_details
    task_info["readwise_mode"] = result["readwise_mode"]
    task_info["readwise_reason"] = result["readwise_reason"]
    task_info["readwise_url_only"] = result["readwise_url_only"]
    task_info["skip_processing_for_url_only"] = result[
        "skip_processing_for_url_only"
    ]
    task_info["spoken_pattern"] = result["spoken_pattern"]

    if _normalize_language_choice(task_info.get("language_override")) in {"zh", "en"}:
        logger.info(
            "转录后自动语言重算将被人工选择覆盖: process=%s auto_language=%s auto_confidence=%.4f selected_language=%s",
            process_id,
            refreshed_language_details.get("language"),
            float(refreshed_language_details.get("confidence", 0.0) or 0.0),
            task_info.get("language_override"),
        )
        _apply_language_confirmation(result, task_info, task_info.get("language_confirmation"))

    return refreshed_language_details


def _apply_language_confirmation(result, task_info, confirmation):
    selected_language = _normalize_language_choice(
        (confirmation or {}).get("selected_language")
    )
    task_info["language_confirmation"] = confirmation
    task_info["language_override"] = (
        selected_language if selected_language in {"zh", "en"} else None
    )

    if selected_language not in {"zh", "en"}:
        logger.info(
            "语言确认保持自动: auto_language=%s auto_confidence=%.4f",
            (result.get("language_details") or {}).get("language"),
            float((result.get("language_details") or {}).get("confidence", 0.0) or 0.0),
        )
        return

    original_language_details = dict(result.get("language_details") or {})
    overridden_language_details = dict(original_language_details)
    overridden_language_details.update(
        {
            "language": selected_language,
            "confidence": 1.0,
            "source": "telegram_language_override",
            "manual_override": True,
            "auto_detected_language": original_language_details.get("language"),
            "auto_detected_confidence": original_language_details.get("confidence"),
        }
    )

    result["language"] = selected_language
    result["language_details"] = overridden_language_details
    task_info["language"] = selected_language
    task_info["language_details"] = overridden_language_details

    readwise_decision = video_service._build_readwise_decision(
        result.get("track_catalog") or [],
        overridden_language_details,
        result.get("content_locale_details") or {},
    )
    result["readwise_mode"] = readwise_decision.get("mode")
    result["readwise_reason"] = readwise_decision.get("reason")
    result["readwise_url_only"] = readwise_decision.get("mode") == "url_only"
    result["skip_processing_for_url_only"] = readwise_decision.get(
        "skip_processing", False
    )
    result["spoken_pattern"] = readwise_decision.get("spoken_pattern")

    task_info["readwise_mode"] = result["readwise_mode"]
    task_info["readwise_reason"] = result["readwise_reason"]
    task_info["readwise_url_only"] = result["readwise_url_only"]
    task_info["skip_processing_for_url_only"] = result["skip_processing_for_url_only"]
    task_info["spoken_pattern"] = result["spoken_pattern"]
    logger.info(
        "应用语言确认选择: selected_language=%s auto_language=%s auto_confidence=%s final_readwise_mode=%s final_readwise_reason=%s",
        selected_language,
        original_language_details.get("language"),
        original_language_details.get("confidence"),
        result["readwise_mode"],
        result["readwise_reason"],
    )
