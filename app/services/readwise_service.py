"""Readwise Reader integration service for article creation and management."""

import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from ..config.config_manager import get_config_value
from ..utils.logging_utils import content_logging_enabled, summarize_text
from ..utils.video_utils import extract_youtube_video_id
from .subtitle_service import SubtitleService

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # 确保DEBUG级别日志可以输出

READER_PARSE_FAILURE_MARKERS = (
    "youtube does not provide subtitles for this video",
    "does not provide subtitles for this video",
)


class ReadwiseService:
    """Readwise Reader集成服务 - 用于创建和管理文章"""

    def __init__(self):
        """初始化Readwise服务"""
        self.api_token = get_config_value("tokens.readwise.api_token", "")
        self.base_url = "https://readwise.io/api/v3"
        self.enabled = bool(self.api_token)
        self.subtitle_service = SubtitleService()

        if not self.enabled:
            logger.info("Readwise API token未配置，服务将不可用")

    def create_article(
        self,
        title: str,
        content: str,
        url: str = None,
        tags: List[str] = None,
        author: str = None,
        summary: str = None,
    ) -> Optional[Dict[str, Any]]:
        """创建Readwise文章

        Args:
            title: 文章标题
            content: 文章内容
            url: 原始URL（可选）
            tags: 标签列表（可选）
            author: 文章作者（可选）
            summary: 文章摘要（可选）

        Returns:
            dict: 创建结果，包含文章ID等信息
        """
        try:
            if not self.enabled:
                logger.warning("Readwise服务未启用")
                return None

            logger.info(f"创建Readwise文章: {title}")

            # 构造文章数据 - 使用Readwise Reader API格式
            # 转换换行符为HTML格式，确保在Readwise中正确显示
            html_content = content.replace("\n", "<br>")
            html_content = f"<div>{html_content}</div>"

            article_data = {
                "html": html_content,
            }

            logger.info(
                "Readwise payload prepared: html_len=%s text_len=%s",
                len(html_content),
                len(content),
            )
            if content_logging_enabled():
                logger.debug("Readwise plain content: %s", content)
                logger.debug("Readwise HTML content: %s", html_content)

            # 最后检查：确保内容不包含时间戳
            if "-->" in content:
                logger.error("🚨 纯文本内容仍包含时间戳！")
                logger.error("包含时间戳的内容摘要: %s", summarize_text(content))
            else:
                logger.info("✅ 纯文本内容不含时间戳")

            if "-->" in html_content:
                logger.error("🚨 HTML内容仍包含时间戳！")
            else:
                logger.info("✅ HTML内容不含时间戳")

            # 添加可选字段
            if url:
                article_data["url"] = url
            else:
                # 如果没有URL，使用一个占位符URL
                article_data["url"] = "https://subtitle-processor.local/generated"

            if title:
                article_data["title"] = title

            if author:
                article_data["author"] = author

            if tags:
                article_data["tags"] = tags

            article_data["summary"] = self._normalize_summary(summary)

            # 发送创建请求到正确的端点
            response = self._make_request("POST", "/save/", data=article_data)

            if response and response.get("id"):
                response = self._ensure_reader_url(response)
                logger.info(f"Readwise文章创建成功，ID: {response['id']}")
                return response
            else:
                logger.error("Readwise文章创建失败")
                return None

        except Exception as e:
            logger.error(f"创建Readwise文章失败: {str(e)}")
            return None

    def create_article_from_url(
        self,
        title: str,
        url: str = None,
        tags: List[str] = None,
        author: str = None,
        summary: str = None,
    ) -> Optional[Dict[str, Any]]:
        """仅通过URL创建Readwise文章"""
        try:
            if not self.enabled:
                logger.warning("Readwise服务未启用")
                return None

            logger.info(f"创建Readwise URL剪藏: {title}")

            article_data: Dict[str, Any] = {}
            if url:
                article_data["url"] = url
            else:
                article_data["url"] = "https://subtitle-processor.local/generated"

            if title:
                article_data["title"] = title

            if author:
                article_data["author"] = author

            if tags:
                article_data["tags"] = tags

            article_data["summary"] = self._normalize_summary(summary)

            response = self._make_request("POST", "/save/", data=article_data)

            if response and response.get("id"):
                response = self._ensure_reader_url(response)
                logger.info(f"Readwise URL剪藏成功，ID: {response['id']}")
                return response

            logger.error("Readwise URL剪藏失败")
            return None

        except Exception as e:
            logger.error(f"创建Readwise URL剪藏失败: {str(e)}")
            return None

    def create_article_from_subtitle(
        self, subtitle_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """从字幕数据创建Readwise文章

        Args:
            subtitle_data: 字幕数据，包含视频信息和字幕内容

        Returns:
            dict: 创建结果
        """
        try:
            if not self.enabled:
                logger.warning("Readwise服务未启用，跳过文章创建")
                return None

            video_info = subtitle_data.get("video_info", {})
            subtitle_content = subtitle_data.get("subtitle_content") or ""
            failure_message = subtitle_data.get("failure_message")
            user_tags = subtitle_data.get("tags", [])
            readwise_mode = subtitle_data.get("readwise_mode")
            readwise_url_only = (
                readwise_mode == "url_only"
                or (
                    readwise_mode is None
                    and bool(subtitle_data.get("readwise_url_only"))
                )
            )
            readwise_reason = subtitle_data.get("readwise_reason")

            # 添加详细的调试信息
            logger.info("=== 开始创建Readwise文章 ===")
            logger.info(f"Readwise服务启用状态: {self.enabled}")
            logger.info(f"视频信息存在: {bool(video_info)}")
            logger.info("字幕内容存在: %s", bool(subtitle_content))
            logger.info("字幕内容摘要: %s", summarize_text(subtitle_content))

            # 构造URL - 支持自定义域名替换
            original_url = (
                video_info.get("webpage_url")
                or video_info.get("url")
                or subtitle_data.get("url")
            )

            # 获取作者信息
            author = video_info.get("uploader") or video_info.get("channel")

            if readwise_url_only:
                title = video_info.get("title") or subtitle_data.get("page_title") or "未知视频标题"
                logger.info(
                    "Readwise URL剪藏模式启用，直接发送原始URL: reason=%s url=%s",
                    readwise_reason or "unspecified",
                    original_url,
                )
                return self.create_article_from_url(
                    title=title,
                    url=original_url,
                    tags=user_tags,
                    author=author,
                    summary=subtitle_data.get("summary"),
                )

            # 检查是否配置了自定义域名，如果是YouTube链接则进行转换。URL-only
            # 必须保留原始URL，让 Readwise Reader 自己抓取网页。
            video_domain = get_config_value("servers.video_domain")

            if video_domain and original_url:
                video_id = extract_youtube_video_id(original_url)
                if video_id:
                    url = f"{video_domain}/view/{video_id}"
                    logger.info(f"URL转换: {original_url} -> {url}")
                else:
                    url = original_url
            else:
                url = original_url

            if failure_message:
                failure_text = str(failure_message).strip()
                if not failure_text:
                    failure_text = "转录失败，请稍后重试。"

                title = video_info.get("title", "转录失败")
                if not title.startswith("转录失败"):
                    title = f"转录失败: {title}"

                logger.warning("检测到转录失败标记，发送失败信息到Readwise")
                return self.create_article(
                    title=title,
                    content=failure_text,
                    url=url,
                    tags=user_tags,
                    author=author,
                    summary=subtitle_data.get("summary"),
                )

            # 详细检查数据完整性
            if not video_info:
                logger.error("❌ 数据验证失败：video_info为空或None")
                logger.error(f"subtitle_data.keys(): {list(subtitle_data.keys())}")
                return None

            if not subtitle_content:
                logger.error("❌ 数据验证失败：subtitle_content为空或None")
                logger.error("subtitle_content摘要: %s", summarize_text(subtitle_content))
                logger.error(f"subtitle_data.keys(): {list(subtitle_data.keys())}")
                return None

            logger.info("✅ 数据验证通过，继续处理")

            # 构造文章标题
            title = video_info.get("title", "未知视频标题")

            # 构造文章内容
            logger.info("开始格式化文章内容")
            content = self._format_subtitle_content(video_info, subtitle_content)
            logger.info("格式化完成: %s", summarize_text(content))

            # 检查格式化后的内容是否还包含时间戳
            if "-->" in content:
                logger.warning("⚠️ 格式化后的内容仍包含时间戳！")
            else:
                logger.info("✅ 格式化后的内容不含时间戳")

            # 获取用户指定的标签（从subtitle_data中获取，比如Telegram传递的）
            logger.info(f"用户标签: {user_tags}")

            return self.create_article(
                title=title,
                content=content,
                url=url,
                tags=user_tags,  # 只使用用户指定的标签
                author=author,
                summary=subtitle_data.get("summary"),
            )

        except Exception as e:
            logger.error(f"从字幕创建Readwise文章失败: {str(e)}")
            return None

    def get_reader_document(
        self, document_id: str, with_html_content: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Fetch a Reader document through the list endpoint."""
        try:
            if not self.enabled or not document_id:
                return None

            params: Dict[str, Any] = {"id": document_id}
            if with_html_content:
                params["withHtmlContent"] = "true"

            response = self._make_request("GET", "/list/", params=params)
            if not isinstance(response, dict):
                return None

            results = response.get("results") or []
            if not results:
                return None

            document = dict(results[0])
            if document.get("id"):
                document = self._ensure_reader_url(document)
            return document

        except Exception as e:
            logger.error("获取Readwise Reader文档失败: %s", str(e))
            return None

    def lookup_reader_document(self, document_id: str) -> Dict[str, Any]:
        """Check a Reader document ID without conflating missing and API failure."""
        if not self.enabled:
            return {
                "status": "unavailable",
                "reason": "reader_not_configured",
                "document": None,
            }
        if not document_id:
            return {
                "status": "missing",
                "reason": "missing_document_id",
                "document": None,
            }

        response = self._make_request(
            "GET",
            "/list/",
            params={"id": str(document_id), "limit": 1},
        )
        if not isinstance(response, dict):
            return {
                "status": "unavailable",
                "reason": "reader_list_request_failed",
                "document": None,
            }

        results = response.get("results") or []
        document = next(
            (
                dict(candidate)
                for candidate in results
                if isinstance(candidate, dict)
                and str(candidate.get("id") or "") == str(document_id)
            ),
            None,
        )
        if document is None:
            return {
                "status": "missing",
                "reason": "document_not_found",
                "document": None,
            }
        return {
            "status": "found",
            "reason": "document_found",
            "document": self._ensure_reader_url(document),
        }

    def list_reader_documents(
        self,
        *,
        category: Optional[str] = None,
        updated_after: Optional[str] = None,
        limit: int = 100,
        max_pages: int = 20,
        page_interval_seconds: float = 0.0,
        request_retry_delay_seconds: float = 0.0,
        max_request_retries: int = 0,
    ) -> Dict[str, Any]:
        """Fetch a bounded Reader document index with explicit completeness."""
        if not self.enabled:
            return {
                "status": "unavailable",
                "reason": "reader_not_configured",
                "documents": [],
            }

        bounded_limit = max(1, min(int(limit), 100))
        bounded_max_pages = max(1, int(max_pages))
        bounded_page_interval = max(0.0, float(page_interval_seconds))
        bounded_retry_delay = max(0.0, float(request_retry_delay_seconds))
        bounded_max_retries = max(0, int(max_request_retries))
        documents: List[Dict[str, Any]] = []
        page_cursor = None

        for page_number in range(bounded_max_pages):
            if page_number > 0 and bounded_page_interval > 0:
                time.sleep(bounded_page_interval)

            params: Dict[str, Any] = {"limit": bounded_limit}
            if category:
                params["category"] = category
            if updated_after:
                params["updatedAfter"] = str(updated_after)
            if page_cursor:
                params["pageCursor"] = page_cursor

            response = None
            for request_attempt in range(bounded_max_retries + 1):
                response = self._make_request("GET", "/list/", params=params)
                if isinstance(response, dict):
                    break
                if request_attempt < bounded_max_retries:
                    logger.warning(
                        "Reader list request failed; retrying page %s in %.1fs",
                        page_number + 1,
                        bounded_retry_delay,
                    )
                    if bounded_retry_delay > 0:
                        time.sleep(bounded_retry_delay)
            if not isinstance(response, dict):
                return {
                    "status": "unavailable",
                    "reason": "reader_list_request_failed",
                    "documents": documents,
                    "pages_read": page_number,
                }

            results = response.get("results") or []
            documents.extend(
                dict(document)
                for document in results
                if isinstance(document, dict)
            )
            page_cursor = response.get("nextPageCursor") or response.get(
                "next_page_cursor"
            )
            if not page_cursor:
                return {
                    "status": "complete",
                    "reason": None,
                    "documents": documents,
                    "pages_read": page_number + 1,
                }

        return {
            "status": "partial",
            "reason": "reader_list_page_limit_reached",
            "documents": documents,
            "pages_read": bounded_max_pages,
        }

    def check_reader_parse_result(self, document_id: str) -> Dict[str, Any]:
        """Classify whether Reader successfully parsed a saved URL."""
        checked_at = datetime.now().isoformat()
        result = {
            "status": "unknown",
            "reason": "not_checked",
            "document_id": document_id,
            "checked_at": checked_at,
        }

        if not document_id:
            result.update(
                {
                    "status": "unknown",
                    "reason": "missing_document_id",
                    "message": "缺少 Readwise Reader 文档ID，无法确认解析结果。",
                }
            )
            return result

        document = self.get_reader_document(document_id, with_html_content=True)
        if not document:
            result.update(
                {
                    "status": "pending",
                    "reason": "document_not_ready",
                    "message": "Readwise Reader 文档尚未出现在列表接口中。",
                }
            )
            return result

        html_content = document.get("html_content") or document.get("html") or ""
        text_content = self._html_to_text(html_content)
        normalized_text = text_content.lower()
        word_count = document.get("word_count")
        category = document.get("category")
        result.update(
            {
                "reason": "checked",
                "word_count": word_count,
                "category": category,
                "html_text_length": len(text_content),
                "reader_url": document.get("url"),
            }
        )

        if any(marker in normalized_text for marker in READER_PARSE_FAILURE_MARKERS):
            result.update(
                {
                    "status": "failed",
                    "reason": "youtube_subtitles_unavailable",
                    "message": "Readwise Reader 未能从 YouTube 抓到字幕。",
                }
            )
            return result

        if (
            category == "video"
            and not word_count
            and len(normalized_text) <= 240
            and "subtitle" in normalized_text
            and ("youtube" in normalized_text or "unfortunately" in normalized_text)
        ):
            result.update(
                {
                    "status": "failed",
                    "reason": "video_subtitles_unavailable",
                    "message": "Readwise Reader 返回了视频字幕不可用提示。",
                }
            )
            return result

        if word_count or len(text_content) >= 80:
            result.update(
                {
                    "status": "ok",
                    "reason": "reader_content_available",
                    "message": "Readwise Reader 已返回正文内容。",
                }
            )
            return result

        result.update(
            {
                "status": "pending",
                "reason": "reader_content_pending",
                "message": "Readwise Reader 文档存在，但正文仍在等待解析。",
            }
        )
        return result

    @staticmethod
    def _html_to_text(html_content: str) -> str:
        """Convert a small HTML fragment into normalized text for status checks."""
        if not html_content:
            return ""
        text_content = re.sub(r"<[^>]+>", " ", str(html_content))
        return re.sub(r"\s+", " ", text_content).strip()

    @staticmethod
    def _normalize_summary(summary: Optional[str]) -> str:
        if summary is None:
            return "**********"
        summary_value = str(summary).strip()
        return summary_value if summary_value else "**********"

    def _format_subtitle_content(
        self, video_info: Dict[str, Any], subtitle_content: str
    ) -> str:
        """格式化字幕内容为文章格式"""
        try:
            # 获取视频基本信息
            title = video_info.get("title", "未知视频")
            uploader = video_info.get("uploader", "未知作者")
            duration = video_info.get("duration", 0)
            upload_date = video_info.get("upload_date", "")
            description = video_info.get("description", "")
            url = video_info.get("webpage_url", "")

            # 格式化时长
            duration_str = self._format_duration(duration) if duration else "未知"

            # 格式化日期
            date_str = self._format_date(upload_date) if upload_date else "未知"

            # 构造文章内容 - 使用简洁的纯文本格式，信息之间有换行
            content_parts = [
                title,
                "",
                f"作者: {uploader}",
                "",
                f"时长: {duration_str}",
                "",
                f"发布日期: {date_str}",
                "",
            ]

            if url:
                content_parts.extend([f"链接: {url}", ""])

            # 添加视频描述（如果有且不太长）
            if description and len(description) < 500:
                content_parts.extend([description, ""])

            # 添加字幕内容
            logger.info(
                "开始字幕清理过程: before=%s",
                summarize_text(subtitle_content, limit=300),
            )

            cleaned_subtitle = self._clean_subtitle_for_readwise(subtitle_content)

            logger.info(
                "字幕清理完成: after=%s",
                summarize_text(cleaned_subtitle, limit=300),
            )
            if content_logging_enabled():
                logger.debug("清理前字幕完整内容: %s", subtitle_content)
                logger.debug("清理后字幕完整内容: %s", cleaned_subtitle)

            # 检查清理结果
            if "-->" in cleaned_subtitle:
                logger.error("🚨 字幕清理函数返回的内容仍包含时间戳！")
                logger.error(
                    "包含时间戳的内容摘要: %s",
                    summarize_text(cleaned_subtitle),
                )
            else:
                logger.info("✅ 字幕清理函数返回的内容不含时间戳")

            # 直接添加字幕内容，不需要标题
            content_parts.append(cleaned_subtitle)

            final_content = "\n".join(content_parts)

            # 最终检查整个格式化内容
            if "-->" in final_content:
                logger.error("🚨 最终格式化内容包含时间戳！")
                # 找出哪一部分包含时间戳
                for i, part in enumerate(content_parts):
                    if "-->" in part:
                        logger.error(
                            "时间戳来源于content_parts[%s]: %s",
                            i,
                            summarize_text(part, 100),
                        )
            else:
                logger.info("✅ 最终格式化内容不含时间戳")

            return final_content

        except Exception as e:
            logger.error(f"格式化字幕内容失败: {str(e)}")
            # 即使格式化失败，也要返回清理后的内容而不是原始内容
            try:
                cleaned_content = self._clean_subtitle_for_readwise(subtitle_content)
                logger.info("使用清理后的内容作为备用方案")
                return f"# 字幕内容\n\n{cleaned_content}"
            except Exception as clean_error:
                logger.error(f"字幕清理也失败: {str(clean_error)}")
                return "字幕处理失败"

    def _clean_subtitle_for_readwise(self, subtitle_content: str) -> str:
        """清理字幕内容，使其适合Readwise显示

        提取纯文本内容，移除时间戳、序号，并智能分段以提高可读性
        """
        try:
            import re

            if subtitle_content is None:
                return ""
            if not isinstance(subtitle_content, str):
                subtitle_content = str(subtitle_content)

            detected_format = self.subtitle_service.detect_subtitle_format(
                subtitle_content
            )
            if detected_format in {"json3", "json", "vtt", "srv", "ttml", "xml"}:
                converted_content = (
                    self.subtitle_service.normalize_external_subtitle_content(
                        subtitle_content
                    )
                )
                if converted_content:
                    logger.info(
                        "Readwise清理前先做字幕规范化: format=%s, before=%s, after=%s",
                        detected_format,
                        len(subtitle_content),
                        len(converted_content),
                    )
                    subtitle_content = converted_content

            logger.info("开始清理字幕内容用于Readwise")
            logger.info("原始内容摘要: %s", summarize_text(subtitle_content))

            if not subtitle_content or not subtitle_content.strip():
                logger.warning("字幕内容为空")
                return ""

            # 检测是否包含SRT格式的时间戳
            has_timestamps = "-->" in subtitle_content
            logger.info(f"内容包含时间戳标记: {has_timestamps}")

            if has_timestamps:
                # 采用简单直接的SRT解析方法
                text_parts = []

                # 处理转义的换行符和不同格式的换行符
                content_normalized = (
                    subtitle_content.replace("\\n", "\n")
                    .replace("\r\n", "\n")
                    .replace("\r", "\n")
                )
                lines = content_normalized.split("\n")

                logger.info("原始内容摘要: %s", summarize_text(subtitle_content, 100))
                logger.info("转义处理后摘要: %s", summarize_text(content_normalized, 100))
                logger.info("标准化后总行数: %s", len(lines))
                logger.info("前5行数量: %s", len(lines[:5]))

                i = 0
                while i < len(lines):
                    line = lines[i].strip()

                    # 跳过空行
                    if not line:
                        i += 1
                        continue

                    # 检查是否是序号行（纯数字）
                    if re.match(r"^\d+$", line):
                        logger.info(f"发现序号行: {line}")
                        i += 1

                        # 下一行应该是时间戳
                        if i < len(lines) and "-->" in lines[i]:
                            logger.info(f"跳过时间戳行: {lines[i].strip()}")
                            i += 1

                            # 接下来的行直到空行都是文本内容
                            text_lines = []
                            while i < len(lines) and lines[i].strip():
                                text_content = lines[i].strip()
                                if text_content:
                                    text_lines.append(text_content)
                                    logger.debug(
                                        "收集文本片段: %s",
                                        summarize_text(text_content, 30),
                                    )
                                i += 1

                            # 合并这个字幕块的文本
                            if text_lines:
                                combined_text = " ".join(text_lines)
                                text_parts.append(combined_text)

                        continue

                    # 如果不是序号行，但包含时间戳，也跳过
                    if "-->" in line:
                        logger.info(f"跳过独立时间戳行: {line}")
                        i += 1
                        continue

                    # 其他情况视为文本内容
                    text_parts.append(line)
                    logger.info(f"直接收集文本: {line[:30]}...")
                    i += 1

                # 合并所有文本 - 使用句号连接，让内容更自然
                processed_parts = []
                for i, part in enumerate(text_parts):
                    part = part.strip()
                    if not part:
                        continue

                    # 如果句子没有结尾标点符号，添加句号
                    if not part.endswith(("。", "！", "？", ".", "!", "?", "，", ",")):
                        part += "。"

                    processed_parts.append(part)

                raw_text = " ".join(processed_parts)
                logger.info(
                    f"SRT解析完成，提取文本段数: {len(text_parts)} -> 处理后: {len(processed_parts)}"
                )
                logger.info(f"提取的原始文本长度: {len(raw_text)}")
                logger.info("提取的原始文本摘要: %s", summarize_text(raw_text))
            else:
                # 不包含时间戳，直接使用原始内容
                raw_text = subtitle_content
                logger.info("非SRT格式，直接使用原始文本")

            # 基本清理
            # 移除多余的空格和换行符
            cleaned_text = re.sub(r"\s+", " ", raw_text).strip()

            # 检查原始文本中的标点符号
            punctuation_count = sum(1 for char in raw_text if char in "。！？.!?，,")
            logger.info(f"原始文本中的标点符号数量: {punctuation_count}")
            logger.info(
                f"原始文本包含的标点: {[char for char in raw_text if char in '。！？.!?，,'][:20]}"
            )

            # 移除重复的标点符号
            cleaned_text = re.sub(r"[,.，。]+(?=[,.，。])", "", cleaned_text)

            # 再次检查清理后的标点符号
            cleaned_punctuation_count = sum(
                1 for char in cleaned_text if char in "。！？.!?，,"
            )
            logger.info(f"清理后文本中的标点符号数量: {cleaned_punctuation_count}")
            logger.info(f"基本清理完成，长度: {len(cleaned_text)}")

            # 如果文本太短，直接返回
            if len(cleaned_text) < 50:
                logger.info("文本较短，直接返回")
                return cleaned_text

            # 智能分段：按句号和感叹号、问号分段
            sentences = re.split(r"([。！？.!?]+)", cleaned_text)

            # 重新组合句子，保留标点符号
            formatted_sentences = []
            i = 0
            while i < len(sentences):
                sentence = sentences[i].strip()
                if not sentence:
                    i += 1
                    continue

                # 如果下一个元素是标点符号，合并
                if i + 1 < len(sentences) and re.match(
                    r"^[。！？.!?]+$", sentences[i + 1].strip()
                ):
                    sentence = sentence + sentences[i + 1].strip()
                    i += 2
                else:
                    i += 1

                if sentence:
                    formatted_sentences.append(sentence)

            # 将句子组织成段落（每3-5句为一段）
            paragraphs = []
            current_paragraph = []

            for sentence in formatted_sentences:
                current_paragraph.append(sentence)

                # 每3-5句组成一段，或者遇到明显的结束标点
                if len(current_paragraph) >= 3 and sentence.endswith(
                    ("。", ".", "！", "!", "？", "?")
                ):
                    paragraphs.append(" ".join(current_paragraph))
                    current_paragraph = []
                elif len(current_paragraph) >= 5:  # 强制分段
                    paragraphs.append(" ".join(current_paragraph))
                    current_paragraph = []

            # 添加最后一段
            if current_paragraph:
                paragraphs.append(" ".join(current_paragraph))

            # 如果分段失败，使用原始清理后的文本
            if not paragraphs:
                final_result = cleaned_text
            else:
                # 用双换行连接段落
                final_result = "\n\n".join(paragraphs)

            # 最终清理
            final_result = re.sub(r"\n{3,}", "\n\n", final_result)
            final_result = re.sub(r" {2,}", " ", final_result)
            final_result = final_result.strip()

            # 记录处理结果
            logger.info("字幕清理完成")
            logger.info(
                f"原始长度: {len(subtitle_content)} -> 清理后长度: {len(final_result)}"
            )
            if paragraphs:
                logger.info(f"段落数量: {len(paragraphs)}")
            logger.info("清理后内容摘要: %s", summarize_text(final_result))

            # 最后检查：确保结果中不包含时间戳
            if "-->" in final_result:
                logger.error("🚨 清理后的内容仍包含时间戳，使用备用清理方法")
                # 备用方法：暴力删除所有包含-->的行
                lines = final_result.split("\n")
                clean_lines = []
                for line in lines:
                    if "-->" not in line and not re.match(r"^\d+$", line.strip()):
                        clean_lines.append(line)
                final_result = "\n".join(clean_lines)
                final_result = re.sub(r"\n{3,}", "\n\n", final_result).strip()
                logger.info(f"备用清理完成，最终长度: {len(final_result)}")

            return final_result

        except Exception as e:
            logger.error(f"清理字幕内容失败: {str(e)}")
            # 即使出错，也要尝试基本清理
            try:
                # 最基本的清理：删除明显的时间戳行
                lines = subtitle_content.split("\n")
                clean_lines = []
                for line in lines:
                    line = line.strip()
                    if line and "-->" not in line and not re.match(r"^\d+$", line):
                        clean_lines.append(line)
                return " ".join(clean_lines)
            except:
                return subtitle_content

    def _is_srt_format(self, content: str) -> bool:
        """检测是否为SRT格式"""
        import re

        # 支持多种时间戳格式：逗号分隔毫秒或空格分隔毫秒
        time_patterns = [
            r"\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}",  # 标准SRT：00:00:00,000 --> 00:00:16,391
            r"\d{2}:\d{2}:\d{2}\s+\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\s+\d{3}",  # 空格分隔毫秒：00:00:00 000 --> 00:00:16 391
            r"\d{2}:\d{2}:\d{2}\s+\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}\s+\d{3}",  # 更多空格的版本
        ]

        for pattern in time_patterns:
            if re.search(pattern, content):
                logger.debug(f"检测到SRT格式，匹配模式: {pattern}")
                return True

        logger.debug("未检测到SRT格式")
        return False

    def _format_duration(self, seconds: int) -> str:
        """格式化时长"""
        try:
            if not seconds:
                return "未知"

            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            seconds = seconds % 60

            if hours > 0:
                return f"{hours}:{minutes:02d}:{seconds:02d}"
            else:
                return f"{minutes}:{seconds:02d}"

        except Exception:
            return "未知"

    def _format_date(self, date_str: str) -> str:
        """格式化日期"""
        try:
            if not date_str:
                return "未知"

            # 假设格式为YYYYMMDD
            if len(date_str) == 8 and date_str.isdigit():
                year = date_str[:4]
                month = date_str[4:6]
                day = date_str[6:8]
                return f"{year}-{month}-{day}"

            return date_str

        except Exception:
            return date_str or "未知"

    def _make_request(
        self,
        method: str,
        endpoint: str,
        data: Dict[str, Any] = None,
        params: Dict[str, Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """发送API请求"""
        try:
            url = f"{self.base_url}{endpoint}"
            headers = {
                "Authorization": f"Token {self.api_token}",
                "Content-Type": "application/json",
            }

            if method.upper() == "GET":
                response = requests.get(url, headers=headers, params=params, timeout=30)
            elif method.upper() == "POST":
                response = requests.post(url, headers=headers, json=data, timeout=30)
            elif method.upper() == "PUT":
                response = requests.put(url, headers=headers, json=data, timeout=30)
            elif method.upper() == "DELETE":
                response = requests.delete(url, headers=headers, timeout=30)
            else:
                logger.error(f"不支持的HTTP方法: {method}")
                return None

            if response.status_code in [200, 201, 202, 204]:
                return response.json() if response.content else {}
            else:
                logger.error(
                    f"Readwise API请求失败: {response.status_code} - {response.text}"
                )
                return None

        except Exception as e:
            logger.error(f"Readwise API请求出错: {str(e)}")
            return None

    @staticmethod
    def _ensure_reader_url(response: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure callers can link directly to the Reader item."""
        if response and response.get("id") and not response.get("url"):
            response["url"] = f"https://read.readwise.io/read/{response['id']}"
        return response

    def get_article(self, article_id: str) -> Optional[Dict[str, Any]]:
        """获取文章信息"""
        try:
            if not self.enabled:
                return None

            return self._make_request("GET", f"/documents/{article_id}/")

        except Exception as e:
            logger.error(f"获取Readwise文章失败: {str(e)}")
            return None

    def update_article(
        self, article_id: str, updates: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """更新文章"""
        try:
            if not self.enabled:
                return None

            return self._make_request("PUT", f"/documents/{article_id}/", data=updates)

        except Exception as e:
            logger.error(f"更新Readwise文章失败: {str(e)}")
            return None

    def delete_article(self, article_id: str) -> bool:
        """删除文章"""
        try:
            if not self.enabled:
                return False

            result = self._make_request("DELETE", f"/delete/{article_id}/")
            return result is not None

        except Exception as e:
            logger.error(f"删除Readwise文章失败: {str(e)}")
            return False

    def list_articles(
        self, limit: int = 20, offset: int = 0
    ) -> Optional[Dict[str, Any]]:
        """列出文章"""
        try:
            if not self.enabled:
                return None

            endpoint = f"/documents/?limit={limit}&offset={offset}"
            return self._make_request("GET", endpoint)

        except Exception as e:
            logger.error(f"列出Readwise文章失败: {str(e)}")
            return None

    def test_connection(self) -> bool:
        """测试Readwise连接"""
        try:
            if not self.enabled:
                logger.info("Readwise服务未启用")
                return False

            # 使用save端点测试连接，但不提供数据（应该返回400但证明连接正常）
            url = f"{self.base_url}/save/"
            headers = {
                "Authorization": f"Token {self.api_token}",
                "Content-Type": "application/json",
            }

            response = requests.get(url, headers=headers, timeout=10)
            # 如果返回405（方法不允许），说明端点存在，连接正常
            if response.status_code in [200, 400, 405]:
                logger.info("Readwise连接测试成功")
                return True
            else:
                logger.error(
                    f"Readwise连接测试失败: {response.status_code} - {response.text}"
                )
                return False

        except Exception as e:
            logger.error(f"Readwise连接测试出错: {str(e)}")
            return False
