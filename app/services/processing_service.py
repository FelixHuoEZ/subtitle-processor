"""Background video processing orchestration."""

import logging
import os
import re
import time
import traceback
from datetime import datetime

from ..utils.logging_utils import summarize_text
from ..utils.file_utils import build_task_filename

logger = logging.getLogger(__name__)

LANGUAGE_CONFIRMATION_TIMEOUT_SECONDS = 180
LANGUAGE_CONFIRMATION_POLL_INTERVAL_SECONDS = 1.0
LANGUAGE_CONFIRMATION_CHOICES = {"zh", "en", "auto"}
LANGUAGE_CONFIRMATION_MISMATCH_MAX_CONFIDENCE = 0.9
READWISE_PARSE_CHECK_ATTEMPTS = 6
READWISE_PARSE_CHECK_INTERVAL_SECONDS = 5.0
SRT_TIMING_LINE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}$",
    re.MULTILINE,
)


class ProcessingService:
    """Coordinates video download, transcription, subtitle storage, and Readwise."""

    def __init__(
        self,
        file_service,
        video_service,
        transcription_service,
        subtitle_service,
        readwise_service,
    ):
        self.file_service = file_service
        self.video_service = video_service
        self.transcription_service = transcription_service
        self.subtitle_service = subtitle_service
        self.readwise_service = readwise_service

    def process_video_task(self, task_info, auto_transcribe):
        """后台执行视频下载、转录及推送流程"""
        process_id = task_info["id"]
        url = task_info["url"]
        platform = task_info["platform"]
        tags = task_info.get("tags", []) or []
        task_temp_dir = None

        print("=== 开始自动视频处理流程 ===")
        print(f"处理ID: {process_id}")
        print(f"视频URL: {url}")
        print(f"平台: {platform}")
        logger.info("=== 开始自动视频处理流程 === %s", process_id)
        logger.info("处理ID: %s", process_id)
        logger.info("视频URL: %s", url)
        logger.info("平台: %s", platform)
        logger.info("自动转录设置: %s", auto_transcribe)
        print("DEBUG: 进入自动启动分支")

        task_info["status"] = "processing"
        task_info["progress"] = 0
        task_info["updated_time"] = datetime.now().isoformat()
        self.file_service.update_file_info(process_id, task_info)

        try:
            logger.info("第1步：开始视频下载和预处理")
            result = self.video_service.process_video_for_transcription(
                url=url, platform=platform
            )
            logger.info("第1步完成：视频处理结果存在: %s", result is not None)

            if result:
                task_info["video_info"] = result.get("video_info", {})
                task_info["language"] = result.get("language")
                task_info["language_details"] = result.get("language_details")
                task_info["content_locale"] = result.get("content_locale")
                task_info["content_locale_details"] = result.get(
                    "content_locale_details"
                )
                task_info["subtitle_content"] = result.get("subtitle_content")
                task_info["subtitle_metadata"] = result.get("subtitle_metadata")
                task_info["audio_file"] = result.get("audio_file")
                task_temp_dir = result.get("temp_dir")
                task_info["needs_transcription"] = result.get(
                    "needs_transcription", False
                )
                task_info["readwise_mode"] = result.get("readwise_mode")
                task_info["readwise_reason"] = result.get("readwise_reason")
                task_info["readwise_url_only"] = result.get("readwise_url_only", False)
                task_info["skip_processing_for_url_only"] = result.get(
                    "skip_processing_for_url_only", False
                )
                task_info["spoken_pattern"] = result.get("spoken_pattern")
                task_info["updated_time"] = datetime.now().isoformat()
                self.file_service.update_file_info(process_id, task_info)

                self.request_language_confirmation_if_needed(
                    process_id,
                    task_info,
                    result,
                    stage="pre_transcription",
                )

                logger.info(
                    "视频处理结果 - subtitle_content存在: %s",
                    bool(result.get("subtitle_content")),
                )
                logger.info(
                    "视频处理结果 - needs_transcription: %s",
                    result.get("needs_transcription"),
                )
                logger.info("视频处理结果 - audio_file: %s", result.get("audio_file"))

                if result.get("readwise_url_only") and result.get(
                    "skip_processing_for_url_only"
                ):
                    self._handle_url_only_readwise(process_id, task_info)
                elif result.get("subtitle_content"):
                    self._handle_existing_subtitle(process_id, task_info, result)
                elif result.get("needs_transcription") and result.get("audio_file"):
                    self._handle_audio_transcription(
                        process_id,
                        task_info,
                        result,
                        tags=tags,
                        platform=platform,
                    )
                else:
                    logger.error(
                        "第2步失败：未获取到可用音频文件，终止后续流程: %s",
                        process_id,
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
                logger.error("第1步失败：视频处理失败: %s", process_id)

        except Exception as e:
            logger.error("=== 视频处理流程出错 === %s - %s", process_id, str(e))
            task_info["status"] = "failed"
            task_info["error"] = str(e)
            task_info["updated_time"] = datetime.now().isoformat()
        finally:
            if task_temp_dir:
                self.video_service.cleanup_task_artifacts(task_temp_dir)
                task_info["audio_file"] = None
            self.file_service.update_file_info(process_id, task_info)

    def _handle_url_only_readwise(self, process_id, task_info):
        task_info["status"] = "processing"
        task_info["progress"] = 90
        logger.info(
            "第2步完成：命中原始中文字幕 URL 剪藏规则，跳过字幕下载与转录: %s",
            process_id,
        )
        logger.info("第3步：开始发送URL剪藏到Readwise Reader: %s", process_id)

        try:
            readwise_result = self.readwise_service.create_article_from_subtitle(
                task_info
            )
            logger.info("Readwise调用返回结果(URL剪藏): %s", readwise_result)

            if readwise_result:
                task_info["readwise_article_id"] = readwise_result.get("id")
                task_info["readwise_url"] = readwise_result.get("url")
                task_info["readwise_url_only_article_id"] = readwise_result.get("id")
                task_info["readwise_url_only_url"] = readwise_result.get("url")
                task_info["readwise_parse_status"] = "checking"
                task_info["readwise_parse_reason"] = None
                task_info["readwise_parse_message"] = "正在确认 Readwise Reader 是否解析到字幕。"
                task_info["force_local_readwise_available"] = False
                task_info["updated_time"] = datetime.now().isoformat()
                task_info.pop("error", None)
                task_info.pop("readwise_error", None)
                self.file_service.update_file_info(process_id, task_info)

                parse_result = self._wait_for_readwise_parse_result(
                    readwise_result.get("id")
                )
                self._apply_url_only_parse_result(task_info, parse_result)
                logger.info(
                    "第3步完成：Readwise URL剪藏成功: %s -> %s",
                    process_id,
                    readwise_result.get("id"),
                )
            else:
                task_info["status"] = "failed"
                task_info["progress"] = 100
                task_info["error"] = "Readwise URL剪藏失败"
                task_info["readwise_error"] = "readwise_url_clip_failed"
                logger.warning("第3步失败：Readwise URL剪藏失败: %s", process_id)
        except Exception as e:
            task_info["status"] = "failed"
            task_info["progress"] = 100
            task_info["error"] = f"Readwise URL剪藏失败: {str(e)}"
            task_info["readwise_error"] = str(e)
            logger.error(
                "第3步错误：发送URL剪藏到Readwise失败: %s - %s",
                process_id,
                str(e),
            )
            logger.error("异常堆栈(URL剪藏): %s", traceback.format_exc())

        task_info["updated_time"] = datetime.now().isoformat()
        logger.info("=== 视频处理流程完成 === %s", process_id)

    def _wait_for_readwise_parse_result(self, article_id):
        last_result = None
        for attempt in range(1, READWISE_PARSE_CHECK_ATTEMPTS + 1):
            try:
                parse_result = self.readwise_service.check_reader_parse_result(article_id)
            except Exception as e:
                logger.warning("Readwise解析确认失败: article=%s error=%s", article_id, e)
                parse_result = {
                    "status": "unknown",
                    "reason": "parse_check_error",
                    "message": "确认 Readwise Reader 解析结果时出错。",
                    "document_id": article_id,
                    "checked_at": datetime.now().isoformat(),
                }

            parse_result["attempts"] = attempt
            last_result = parse_result
            logger.info(
                "Readwise解析确认: article=%s attempt=%s status=%s reason=%s",
                article_id,
                attempt,
                parse_result.get("status"),
                parse_result.get("reason"),
            )

            if parse_result.get("status") in {"ok", "failed", "unknown"}:
                return parse_result

            if attempt < READWISE_PARSE_CHECK_ATTEMPTS:
                time.sleep(READWISE_PARSE_CHECK_INTERVAL_SECONDS)

        if last_result:
            last_result = dict(last_result)
            last_result["status"] = "pending_timeout"
            last_result["reason"] = last_result.get("reason") or "parse_check_timeout"
            last_result["message"] = "Readwise Reader 解析仍在等待中，保留URL剪藏结果。"
            return last_result

        return {
            "status": "unknown",
            "reason": "parse_check_unavailable",
            "message": "未能确认 Readwise Reader 解析结果。",
            "document_id": article_id,
            "checked_at": datetime.now().isoformat(),
            "attempts": 0,
        }

    def _apply_url_only_parse_result(self, task_info, parse_result):
        parse_status = (parse_result or {}).get("status") or "unknown"
        task_info["readwise_parse_status"] = parse_status
        task_info["readwise_parse_reason"] = (parse_result or {}).get("reason")
        task_info["readwise_parse_message"] = (parse_result or {}).get("message")
        task_info["readwise_parse_checked_at"] = (parse_result or {}).get("checked_at")
        task_info["readwise_parse_attempts"] = (parse_result or {}).get("attempts")

        if parse_status == "failed":
            task_info["status"] = "readwise_parse_failed"
            task_info["progress"] = 100
            task_info["error"] = (
                task_info.get("readwise_parse_message")
                or "Readwise Reader 未能解析该视频字幕，可强制本地字幕/全文重发。"
            )
            task_info["readwise_error"] = task_info.get("readwise_parse_reason")
            task_info["force_local_readwise_available"] = True
            logger.warning(
                "Readwise URL剪藏解析失败: article=%s reason=%s",
                task_info.get("readwise_article_id"),
                task_info.get("readwise_parse_reason"),
            )
            return

        task_info["status"] = "completed"
        task_info["progress"] = 100
        task_info["force_local_readwise_available"] = False
        task_info.pop("error", None)
        task_info.pop("readwise_error", None)

    def retry_readwise_with_local_content(self, process_id):
        """Re-run one URL-only task locally and send a full-text Reader item."""
        task_info = self.file_service.get_file_info(process_id)
        if not task_info:
            return {
                "success": False,
                "status": "not_found",
                "error": "处理任务不存在",
            }

        task_info = dict(task_info)
        url = task_info.get("url")
        platform = task_info.get("platform")
        if not url or not platform:
            self._fail_force_local_readwise(
                task_info,
                "invalid_task",
                "任务缺少原始链接或平台，无法本地重发 Readwise。",
            )
            self.file_service.update_file_info(process_id, task_info)
            return {
                "success": False,
                "status": task_info.get("status"),
                "error": task_info.get("error"),
            }

        original_article_id = (
            task_info.get("readwise_url_only_article_id")
            or task_info.get("readwise_article_id")
        )
        original_reader_url = (
            task_info.get("readwise_url_only_url") or task_info.get("readwise_url")
        )
        if original_article_id:
            task_info["readwise_url_only_article_id"] = original_article_id
        if original_reader_url:
            task_info["readwise_url_only_url"] = original_reader_url

        task_temp_dir = None
        task_info.update(
            {
                "status": "processing",
                "progress": 3,
                "readwise_parse_status": (
                    "deleting_url_only" if original_article_id else "retrying_local"
                ),
                "readwise_parse_reason": "force_local_requested",
                "readwise_parse_message": (
                    "正在删除原 URL-only Reader 文档。"
                    if original_article_id
                    else "正在本地获取字幕/转录并重发 Readwise。"
                ),
                "force_local_readwise_available": False,
                "force_local_readwise_requested_at": datetime.now().isoformat(),
                "updated_time": datetime.now().isoformat(),
            }
        )
        task_info.pop("error", None)
        task_info.pop("readwise_error", None)
        self.file_service.update_file_info(process_id, task_info)

        try:
            if original_article_id:
                if not self.readwise_service.delete_article(original_article_id):
                    task_info["readwise_url_only_delete_status"] = "failed"
                    self._fail_force_local_readwise(
                        task_info,
                        "readwise_url_only_delete_failed",
                        "删除原 URL-only Reader 文档失败，未开始本地全文重发。",
                    )
                    return {
                        "success": False,
                        "status": task_info.get("status"),
                        "error": task_info.get("error"),
                    }

                task_info["readwise_url_only_delete_status"] = "deleted"
                task_info["readwise_url_only_deleted_at"] = datetime.now().isoformat()
                task_info["readwise_deleted_article_id"] = original_article_id
                task_info["readwise_article_id"] = None
                task_info["readwise_url"] = None
            else:
                task_info["readwise_url_only_delete_status"] = "skipped"

            task_info.update(
                {
                    "progress": 5,
                    "readwise_parse_status": "retrying_local",
                    "readwise_parse_message": "正在本地获取字幕/转录并重发 Readwise。",
                    "updated_time": datetime.now().isoformat(),
                }
            )
            self.file_service.update_file_info(process_id, task_info)

            result = self.video_service.process_video_for_transcription(
                url=url,
                platform=platform,
                force_local_processing=True,
            )
            if result:
                task_temp_dir = result.get("temp_dir")

            if not result:
                self._fail_force_local_readwise(
                    task_info,
                    "local_processing_failed",
                    "本地获取字幕/音频失败，未能重发 Readwise。",
                )
                return {
                    "success": False,
                    "status": task_info.get("status"),
                    "error": task_info.get("error"),
                }

            self._apply_video_result_fields(task_info, result)
            self._force_full_text_readwise_fields(task_info)
            task_info["readwise_article_id"] = None
            task_info["readwise_url"] = None
            task_info["updated_time"] = datetime.now().isoformat()
            self.file_service.update_file_info(process_id, task_info)

            if result.get("subtitle_content"):
                self._handle_existing_subtitle(
                    process_id,
                    task_info,
                    result,
                    force_full_text=True,
                )
            elif result.get("needs_transcription") and result.get("audio_file"):
                self._handle_audio_transcription(
                    process_id,
                    task_info,
                    result,
                    tags=task_info.get("tags", []) or [],
                    platform=platform,
                    force_full_text=True,
                )
            else:
                download_error = result.get("download_error")
                self._fail_force_local_readwise(
                    task_info,
                    "local_content_unavailable",
                    download_error or "本地处理没有得到可用字幕或音频。",
                )
                return {
                    "success": False,
                    "status": task_info.get("status"),
                    "error": task_info.get("error"),
                }

            fallback_article_id = task_info.get("readwise_article_id")
            if task_info.get("status") == "completed" and fallback_article_id:
                task_info["readwise_fallback_from_article_id"] = original_article_id
                task_info["readwise_fallback_article_id"] = fallback_article_id
                task_info["readwise_fallback_url"] = task_info.get("readwise_url")
                task_info["readwise_parse_status"] = "recovered"
                task_info["readwise_parse_reason"] = "force_local_full_text_sent"
                task_info["readwise_parse_message"] = "已使用本地字幕/全文重发到 Readwise。"
                task_info["force_local_readwise_available"] = False
                task_info.pop("error", None)
                task_info.pop("readwise_error", None)
            else:
                self._fail_force_local_readwise(
                    task_info,
                    "readwise_full_text_failed",
                    "本地全文已生成，但重发 Readwise 失败。",
                )

            return {
                "success": task_info.get("status") == "completed",
                "status": task_info.get("status"),
                "readwise_article_id": task_info.get("readwise_article_id"),
                "readwise_url": task_info.get("readwise_url"),
                "error": task_info.get("error"),
            }

        except Exception as e:
            logger.error("强制本地重发Readwise失败: %s - %s", process_id, str(e))
            logger.error("异常堆栈(强制本地重发): %s", traceback.format_exc())
            self._fail_force_local_readwise(
                task_info,
                "force_local_exception",
                f"强制本地重发 Readwise 出错: {str(e)}",
            )
            return {
                "success": False,
                "status": task_info.get("status"),
                "error": task_info.get("error"),
            }
        finally:
            if task_temp_dir:
                self.video_service.cleanup_task_artifacts(task_temp_dir)
                task_info["audio_file"] = None
            task_info["updated_time"] = datetime.now().isoformat()
            self.file_service.update_file_info(process_id, task_info)

    def _apply_video_result_fields(self, task_info, result):
        task_info["video_info"] = result.get("video_info", {})
        task_info["language"] = result.get("language")
        task_info["language_details"] = result.get("language_details")
        task_info["content_locale"] = result.get("content_locale")
        task_info["content_locale_details"] = result.get("content_locale_details")
        task_info["subtitle_content"] = result.get("subtitle_content")
        task_info["subtitle_metadata"] = result.get("subtitle_metadata")
        task_info["audio_file"] = result.get("audio_file")
        task_info["needs_transcription"] = result.get("needs_transcription", False)
        task_info["readwise_mode"] = result.get("readwise_mode")
        task_info["readwise_reason"] = result.get("readwise_reason")
        task_info["readwise_url_only"] = result.get("readwise_url_only", False)
        task_info["skip_processing_for_url_only"] = result.get(
            "skip_processing_for_url_only", False
        )
        task_info["spoken_pattern"] = result.get("spoken_pattern")

    @staticmethod
    def _force_full_text_readwise_fields(task_info):
        task_info["readwise_mode"] = "full_text"
        task_info["readwise_reason"] = "forced_local_after_reader_parse_failed"
        task_info["readwise_url_only"] = False
        task_info["skip_processing_for_url_only"] = False
        task_info["readwise_force_local"] = True

    @staticmethod
    def _fail_force_local_readwise(task_info, reason, message):
        task_info["status"] = "failed"
        task_info["progress"] = 100
        task_info["error"] = message
        task_info["readwise_error"] = reason
        task_info["readwise_parse_status"] = "force_local_failed"
        task_info["readwise_parse_reason"] = reason
        task_info["readwise_parse_message"] = message
        task_info["force_local_readwise_available"] = True

    def _handle_existing_subtitle(
        self, process_id, task_info, result, force_full_text=False
    ):
        if force_full_text:
            self._force_full_text_readwise_fields(task_info)

        raw_subtitle_content = result.get("subtitle_content")
        source_subtitle_format = self.subtitle_service.detect_subtitle_format(
            raw_subtitle_content
        )
        normalized_subtitle_content = (
            self.subtitle_service.normalize_external_subtitle_content(
                raw_subtitle_content
            )
        )

        if normalized_subtitle_content:
            task_info["subtitle_content"] = normalized_subtitle_content
        else:
            task_info["subtitle_content"] = raw_subtitle_content

        if isinstance(raw_subtitle_content, str):
            converted_subtitle = task_info["subtitle_content"] != raw_subtitle_content
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
            subtitle_path = self.file_service.save_file(
                task_info.get("subtitle_content", ""), subtitle_filename
            )
            task_info["subtitle_path"] = subtitle_path
            task_info["filename"] = subtitle_filename
        logger.info("第2步完成：视频已有字幕，无需转录: %s", process_id)
        logger.info("第3步：开始发送内容到Readwise Reader: %s", process_id)

        logger.debug("调试信息(有字幕) - task_info关键字段:")
        logger.debug("  - video_info存在: %s", bool(task_info.get("video_info")))
        logger.debug(
            "  - subtitle_content存在: %s",
            bool(task_info.get("subtitle_content")),
        )
        logger.debug(
            "  - subtitle_content长度: %s",
            len(task_info.get("subtitle_content", "")),
        )
        logger.debug("  - tags: %s", task_info.get("tags"))

        try:
            logger.info("调用readwise_service.create_article_from_subtitle(有字幕)...")
            readwise_result = self.readwise_service.create_article_from_subtitle(
                task_info
            )
            logger.info("Readwise调用返回结果(有字幕): %s", readwise_result)

            if readwise_result:
                task_info["readwise_article_id"] = readwise_result.get("id")
                task_info["readwise_url"] = readwise_result.get("url")
                logger.info(
                    "第3步完成：Readwise文章创建成功: %s -> %s",
                    process_id,
                    readwise_result.get("id"),
                )
            else:
                logger.warning("第3步失败：Readwise文章创建失败: %s", process_id)
                logger.warning(
                    "readwise_service返回了None或False(有字幕): %s",
                    readwise_result,
                )
        except Exception as e:
            logger.error("第3步错误：发送到Readwise失败: %s - %s", process_id, str(e))
            logger.error("异常堆栈(有字幕): %s", traceback.format_exc())

        logger.info("=== 视频处理流程完成 === %s", process_id)

    def _handle_audio_transcription(
        self,
        process_id,
        task_info,
        result,
        tags,
        platform,
        force_full_text=False,
    ):
        logger.info("第2步：开始音频转录流程: %s", process_id)
        logger.info("needs_transcription: %s", result.get("needs_transcription"))
        logger.info("audio_file: %s", result.get("audio_file"))
        audio_file = result.get("audio_file")
        try:
            logger.info("第2.1步：调用转录服务，音频文件: %s", audio_file)
            logger.info("音频文件是否存在: %s", os.path.exists(audio_file))
            transcription_result = self.transcription_service.transcribe_audio(
                audio_file=audio_file,
                hotwords=None,
                video_info=task_info.get("video_info", {}),
                tags=tags,
                platform=platform,
            )
            logger.info(
                "第2.1步完成：转录结果是否为None: %s",
                transcription_result is None,
            )

            if transcription_result is None:
                self._handle_transcription_failure(process_id, task_info)
                return

            logger.info("转录数据类型: %s", type(transcription_result))
            if isinstance(transcription_result, dict) and "text" in transcription_result:
                text_length = (
                    len(transcription_result["text"])
                    if transcription_result["text"]
                    else 0
                )
                logger.info("转录文本长度: %s", text_length)
                logger.info(
                    "转录文本摘要: %s",
                    summarize_text(transcription_result["text"], 100),
                )

            logger.info("第2.2步：开始转换为SRT格式")
            srt_content = self.subtitle_service.parse_srt(transcription_result, [])
            logger.info("第2.2步完成：SRT转换结果是否为None: %s", srt_content is None)
            if srt_content:
                self._handle_transcription_success(
                    process_id,
                    task_info,
                    result,
                    transcription_result,
                    srt_content,
                    force_full_text=force_full_text,
                )
            else:
                task_info["status"] = "failed"
                task_info["error"] = "SRT转换失败"
                logger.error("第2.2步失败：SRT转换失败: %s", process_id)
        except Exception as e:
            task_info["status"] = "failed"
            task_info["error"] = f"转录出错: {str(e)}"
            logger.error("第2步错误：转录出错: %s - %s", process_id, str(e))

    def _handle_transcription_failure(self, process_id, task_info):
        retry_limit = getattr(self.transcription_service, "transcribe_max_retries", 5)
        failure_message = f"转录失败：已重试{retry_limit}次仍未成功，请稍后重试。"
        task_info["status"] = "failed"
        task_info["error"] = failure_message
        logger.error("第2步失败：音频转录失败: %s", process_id)

        logger.info("第3步：发送转录失败信息到Readwise Reader: %s", process_id)
        try:
            failure_payload = {
                "video_info": task_info.get("video_info", {}),
                "tags": task_info.get("tags", []),
                "failure_message": failure_message,
            }
            readwise_result = self.readwise_service.create_article_from_subtitle(
                failure_payload
            )
            logger.info("Readwise调用返回结果(转录失败): %s", readwise_result)
            if readwise_result:
                task_info["readwise_article_id"] = readwise_result.get("id")
                task_info["readwise_url"] = readwise_result.get("url")
                logger.info(
                    "第3步完成：Readwise失败提示发送成功: %s -> %s",
                    process_id,
                    readwise_result.get("id"),
                )
            else:
                logger.warning("第3步失败：Readwise失败提示发送失败: %s", process_id)
        except Exception as e:
            logger.error(
                "第3步错误：发送失败提示到Readwise失败: %s - %s",
                process_id,
                str(e),
            )

    def _handle_transcription_success(
        self,
        process_id,
        task_info,
        result,
        transcription_result,
        srt_content,
        force_full_text=False,
    ):
        srt_length = len(srt_content)
        logger.info("SRT内容长度: %s", srt_length)
        subtitle_count = self.count_srt_entries(srt_content)
        logger.info("生成字幕条数: %s", subtitle_count)

        task_info["status"] = "completed"
        task_info["subtitle_content"] = srt_content
        task_info["transcription_result"] = transcription_result
        task_info["progress"] = 100
        safe_title = task_info.get("video_info", {}).get("title") or process_id
        subtitle_filename = build_task_filename(safe_title, process_id)
        subtitle_path = self.file_service.save_file(srt_content, subtitle_filename)
        task_info["subtitle_path"] = subtitle_path
        task_info["filename"] = subtitle_filename
        logger.info("第2步完成：音频转录和SRT转换成功: %s", process_id)

        refreshed_language_details = self.refresh_language_state_from_final_subtitle(
            task_info,
            result,
            subtitle_content=srt_content,
        )
        if refreshed_language_details:
            logger.info(
                "转录后语言重算完成: language=%s confidence=%.4f readwise_mode=%s reason=%s",
                task_info.get("language"),
                float((task_info.get("language_details") or {}).get("confidence", 0.0)),
                task_info.get("readwise_mode"),
                task_info.get("readwise_reason"),
            )
            task_info["updated_time"] = datetime.now().isoformat()
            self.file_service.update_file_info(
                process_id,
                {
                    "language": task_info.get("language"),
                    "language_details": task_info.get("language_details"),
                    "content_locale": task_info.get("content_locale"),
                    "content_locale_details": task_info.get(
                        "content_locale_details"
                    ),
                    "readwise_mode": task_info.get("readwise_mode"),
                    "readwise_reason": task_info.get("readwise_reason"),
                    "readwise_url_only": task_info.get("readwise_url_only"),
                    "skip_processing_for_url_only": task_info.get(
                        "skip_processing_for_url_only"
                    ),
                    "spoken_pattern": task_info.get("spoken_pattern"),
                    "updated_time": task_info["updated_time"],
                },
            )
            self.request_language_confirmation_if_needed(
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
                float((task_info.get("language_details") or {}).get("confidence", 0.0)),
                task_info.get("readwise_mode"),
                task_info.get("readwise_reason"),
                task_info.get("language_override") or "auto",
            )

        if force_full_text:
            self._force_full_text_readwise_fields(task_info)

        logger.info("第3步：开始发送内容到Readwise Reader: %s", process_id)
        logger.debug("调试信息 - task_info关键字段:")
        logger.debug("  - video_info存在: %s", bool(task_info.get("video_info")))
        logger.debug(
            "  - subtitle_content存在: %s",
            bool(task_info.get("subtitle_content")),
        )
        logger.debug(
            "  - subtitle_content长度: %s",
            len(task_info.get("subtitle_content", "")),
        )
        logger.debug("  - tags: %s", task_info.get("tags"))
        if task_info.get("video_info"):
            vi = task_info["video_info"]
            logger.debug("  - video_info.title: %s", vi.get("title", "None"))
            logger.debug("  - video_info.uploader: %s", vi.get("uploader", "None"))

        try:
            logger.info("调用readwise_service.create_article_from_subtitle...")
            readwise_result = self.readwise_service.create_article_from_subtitle(
                task_info
            )
            logger.info("Readwise调用返回结果: %s", readwise_result)

            if readwise_result:
                task_info["readwise_article_id"] = readwise_result.get("id")
                task_info["readwise_url"] = readwise_result.get("url")
                logger.info(
                    "第3步完成：Readwise文章创建成功: %s -> %s",
                    process_id,
                    readwise_result.get("id"),
                )
            else:
                logger.warning("第3步失败：Readwise文章创建失败: %s", process_id)
                logger.warning(
                    "readwise_service返回了None或False: %s",
                    readwise_result,
                )
        except Exception as e:
            logger.error("第3步错误：发送到Readwise失败: %s - %s", process_id, str(e))
            logger.error("异常堆栈: %s", traceback.format_exc())

        logger.info("=== 视频处理流程完成 === %s", process_id)

    @staticmethod
    def count_srt_entries(srt_content):
        """统计SRT中的字幕条数，忽略字面量 \\n 造成的伪换行。"""
        if not srt_content or not isinstance(srt_content, str):
            return 0
        return len(SRT_TIMING_LINE_RE.findall(srt_content))

    def normalize_language_choice(self, language):
        normalized = self.video_service._normalize_language_code(language)
        if normalized in {"zh", "en", "mixed"}:
            return normalized
        raw_language = (language or "").strip().lower()
        if raw_language == "auto":
            return "auto"
        return None

    def should_request_language_confirmation(self, task_info, result):
        if (task_info.get("request_source") or "").strip().lower() != "telegram":
            return None

        if result.get("skip_processing_for_url_only"):
            return None

        language_details = result.get("language_details") or {}
        spoken_language = self.normalize_language_choice(language_details.get("language"))
        spoken_confidence = float(language_details.get("confidence", 0.0) or 0.0)
        content_locale = self.normalize_language_choice(
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

    def language_confirmation_is_resolved(self, task_info):
        confirmation = (task_info or {}).get("language_confirmation") or {}
        selected_language = self.normalize_language_choice(
            confirmation.get("selected_language")
        )
        return selected_language in LANGUAGE_CONFIRMATION_CHOICES or confirmation.get(
            "status"
        ) in {"confirmed", "timeout"}

    def request_language_confirmation_if_needed(
        self, process_id, task_info, result, skip_if_resolved=False, stage="unknown"
    ):
        if skip_if_resolved and self.language_confirmation_is_resolved(task_info):
            confirmation = (task_info or {}).get("language_confirmation") or {}
            logger.info(
                "跳过重复语言确认: process=%s stage=%s existing_status=%s selected_language=%s",
                process_id,
                stage,
                confirmation.get("status"),
                confirmation.get("selected_language") or "auto",
            )
            return None

        confirmation_state = self.should_request_language_confirmation(task_info, result)
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
        self.file_service.update_file_info(process_id, task_info)

        resolved_confirmation = self.wait_for_language_confirmation(process_id)
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
        self.file_service.update_file_info(
            process_id,
            {
                "status": "processing",
                "language_confirmation": resolved_confirmation,
                "updated_time": task_info["updated_time"],
            },
        )
        self.apply_language_confirmation(result, task_info, resolved_confirmation)
        task_info["updated_time"] = datetime.now().isoformat()
        self.file_service.update_file_info(process_id, task_info)
        return resolved_confirmation

    def wait_for_language_confirmation(self, process_id):
        deadline = time.time() + LANGUAGE_CONFIRMATION_TIMEOUT_SECONDS
        while time.time() < deadline:
            current_task_info = self.file_service.get_file_info(process_id) or {}
            confirmation = current_task_info.get("language_confirmation") or {}
            selected_language = self.normalize_language_choice(
                confirmation.get("selected_language")
            )
            if selected_language in LANGUAGE_CONFIRMATION_CHOICES:
                resolved_confirmation = dict(confirmation)
                resolved_confirmation.setdefault("status", "confirmed")
                resolved_confirmation.setdefault("resolved_at", datetime.now().isoformat())
                return resolved_confirmation
            time.sleep(LANGUAGE_CONFIRMATION_POLL_INTERVAL_SECONDS)

        current_task_info = self.file_service.get_file_info(process_id) or {}
        confirmation = dict(current_task_info.get("language_confirmation") or {})
        confirmation.update(
            {
                "status": "timeout",
                "selected_language": "auto",
                "resolved_at": datetime.now().isoformat(),
            }
        )
        logger.info("语言确认超时，继续自动处理: process=%s", process_id)
        self.file_service.update_file_info(
            process_id,
            {
                "language_confirmation": confirmation,
                "status": "processing",
                "updated_time": datetime.now().isoformat(),
            },
        )
        return confirmation

    def refresh_language_state_from_final_subtitle(
        self,
        task_info,
        result,
        subtitle_content,
        subtitle_track_type="asr_original",
    ):
        if not isinstance(subtitle_content, str) or not subtitle_content.strip():
            return None

        video_info = result.get("video_info") or task_info.get("video_info") or {}
        refreshed_language_details = self.video_service.get_video_language_details(
            video_info,
            subtitle_result={
                "content": subtitle_content,
                "track_type": subtitle_track_type,
            },
            audio_result=result.get("audio_probe"),
        )
        refreshed_content_locale_details = self.video_service.get_content_locale_details(
            video_info,
            language_details=refreshed_language_details,
        )
        readwise_decision = self.video_service._build_readwise_decision(
            result.get("track_catalog") or [],
            refreshed_language_details,
            refreshed_content_locale_details,
            video_info,
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

        if self.normalize_language_choice(task_info.get("language_override")) in {
            "zh",
            "en",
        }:
            logger.info(
                "转录后自动语言重算将被人工选择覆盖: process=%s auto_language=%s auto_confidence=%.4f selected_language=%s",
                process_id,
                refreshed_language_details.get("language"),
                float(refreshed_language_details.get("confidence", 0.0) or 0.0),
                task_info.get("language_override"),
            )
            self.apply_language_confirmation(
                result, task_info, task_info.get("language_confirmation")
            )

        return refreshed_language_details

    def apply_language_confirmation(self, result, task_info, confirmation):
        selected_language = self.normalize_language_choice(
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
                float(
                    (result.get("language_details") or {}).get("confidence", 0.0)
                    or 0.0
                ),
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

        readwise_decision = self.video_service._build_readwise_decision(
            result.get("track_catalog") or [],
            overridden_language_details,
            result.get("content_locale_details") or {},
            result.get("video_info") or task_info.get("video_info") or {},
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
        task_info["skip_processing_for_url_only"] = result[
            "skip_processing_for_url_only"
        ]
        task_info["spoken_pattern"] = result["spoken_pattern"]
        logger.info(
            "应用语言确认选择: selected_language=%s auto_language=%s auto_confidence=%s final_readwise_mode=%s final_readwise_reason=%s",
            selected_language,
            original_language_details.get("language"),
            original_language_details.get("confidence"),
            result["readwise_mode"],
            result["readwise_reason"],
        )
