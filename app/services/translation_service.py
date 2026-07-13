"""Subtitle translation with configurable provider fallbacks."""

import logging
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..config.config_manager import get_config_value
from ..utils.logging_utils import summarize_text

logger = logging.getLogger(__name__)


class TranslationService:
    """Translate subtitle text through DeepL or OpenAI-compatible providers."""

    def __init__(self):
        self.deeplx_server = self._string_config(
            "servers.deeplx", "http://localhost:1188"
        ).rstrip("/")
        self.deepl_api_key = self._string_config("tokens.deepl.api_key", "")
        self.deepl_base_url = self._string_config(
            "tokens.deepl.base_url", "https://api-free.deepl.com/v2"
        ).rstrip("/")

        self.openai_providers = self._load_openai_providers(
            get_config_value("tokens.openai", [])
        )
        self.provider_specs = self._load_provider_specs(
            get_config_value("translation.services", None)
        )

        self.max_retries = self._positive_int_config("translation.max_retries", 3)
        self.base_delay = self._positive_float_config("translation.base_delay", 3.0)
        self.request_interval = max(
            0.0,
            self._float_config("translation.request_interval", 1.0),
        )
        self.request_timeout = self._positive_float_config(
            "translation.request_timeout", 60.0
        )
        self.target_chunk_length = self._positive_int_config(
            "translation.chunk_size", 2000
        )
        self.min_chunk_length = self._positive_int_config(
            "translation.min_chunk_size", 1600
        )
        self.max_chunk_length = max(
            self.target_chunk_length,
            self._positive_int_config("translation.max_chunk_size", 2400),
        )
        self.default_target_language = self._string_config(
            "translation.default_target_language", "zh"
        )

        self.deeplx_health_timeout = self._positive_float_config(
            "translation.deeplx_health_timeout", 2.0
        )
        self.deeplx_cooldown_seconds = self._positive_float_config(
            "translation.deeplx_cooldown_seconds", 300.0
        )
        self._deeplx_health_lock = threading.Lock()
        self._deeplx_health_checked_at = 0.0
        self._deeplx_available = False

        self.language_map = {
            "zh": "ZH",
            "zh-CN": "ZH",
            "zh-TW": "ZH",
            "en": "EN",
            "en-US": "EN",
            "en-GB": "EN",
            "ja": "JA",
            "ko": "KO",
            "fr": "FR",
            "de": "DE",
            "es": "ES",
            "it": "IT",
            "pt": "PT",
            "ru": "RU",
        }
        logger.info(
            "字幕翻译 provider 顺序: %s",
            " -> ".join(spec["name"] for spec in self.provider_specs) or "none",
        )

    @staticmethod
    def _string_config(key: str, default: str) -> str:
        value = get_config_value(key, default)
        return value.strip() if isinstance(value, str) else default

    @staticmethod
    def _positive_int_config(key: str, default: int) -> int:
        try:
            return max(1, int(get_config_value(key, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float_config(key: str, default: float) -> float:
        try:
            return float(get_config_value(key, default))
        except (TypeError, ValueError):
            return default

    @classmethod
    def _positive_float_config(cls, key: str, default: float) -> float:
        return max(0.1, cls._float_config(key, default))

    @staticmethod
    def _load_openai_providers(raw_config: Any) -> Dict[str, Dict[str, Any]]:
        providers: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw_config, dict):
            if raw_config.get("api_key"):
                providers[str(raw_config.get("name") or "default")] = dict(raw_config)
            else:
                for name, config in raw_config.items():
                    if isinstance(config, dict):
                        providers[str(name)] = dict(config)
        elif isinstance(raw_config, list):
            for index, config in enumerate(raw_config):
                if not isinstance(config, dict):
                    continue
                name = str(config.get("name") or index)
                providers[name] = dict(config)
        return providers

    def _load_provider_specs(self, raw_services: Any) -> List[Dict[str, str]]:
        specs: List[Tuple[int, int, Dict[str, str]]] = []
        if isinstance(raw_services, list):
            for index, service in enumerate(raw_services):
                if not isinstance(service, dict) or service.get("enabled") is False:
                    continue
                spec = self._provider_spec_from_config(service)
                if not spec:
                    continue
                try:
                    priority = int(service.get("priority", index + 1))
                except (TypeError, ValueError):
                    priority = index + 1
                specs.append((priority, index, spec))

        if not specs and raw_services is None:
            index = 0
            if self.deepl_api_key:
                specs.append(
                    (1, index, {"name": "deepl", "kind": "deepl", "config": ""})
                )
                index += 1
            for name in self.openai_providers:
                specs.append(
                    (
                        10 + index,
                        index,
                        {
                            "name": f"openai:{name}",
                            "kind": "openai",
                            "config": name,
                        },
                    )
                )
                index += 1
            if not specs:
                specs.append(
                    (99, index, {"name": "deeplx", "kind": "deeplx", "config": ""})
                )

        specs.sort(key=lambda item: (item[0], item[1]))
        return [spec for _, _, spec in specs]

    @staticmethod
    def _provider_spec_from_config(service: Dict[str, Any]) -> Optional[Dict[str, str]]:
        name = str(service.get("name") or "").strip()
        normalized = name.lower().replace("-", "_")
        if normalized in {"deepl", "deepl_api", "deepl_official"}:
            return {"name": name or "deepl", "kind": "deepl", "config": ""}
        if normalized in {"deeplx", "deeplx_v2"}:
            return {"name": name or "deeplx", "kind": "deeplx", "config": ""}
        if normalized == "openai" or normalized.startswith("openai_"):
            config_name = str(
                service.get("config_name")
                or (normalized.removeprefix("openai_") if normalized != "openai" else "default")
            )
            return {
                "name": name or f"openai:{config_name}",
                "kind": "openai",
                "config": config_name,
            }
        logger.warning("忽略未知字幕翻译 provider: %s", name or "<empty>")
        return None

    def translate_text(
        self, text: str, target_lang: str, source_lang: str = "auto"
    ) -> Optional[str]:
        """Return translated text only when every chunk succeeds."""
        result = self.translate_text_detailed(text, target_lang, source_lang)
        return result.get("content") if result.get("status") == "completed" else None

    def translate_text_detailed(
        self, text: str, target_lang: str, source_lang: str = "auto"
    ) -> Dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            return self._translation_result(
                "failed", target_lang, source_lang, error="empty_text"
            )

        chunks = self._split_text_into_chunks(text)
        translated_chunks: List[str] = []
        used_providers: List[str] = []
        for index, chunk in enumerate(chunks, 1):
            translated, provider = self._translate_with_retry(
                chunk, target_lang, source_lang
            )
            if not translated:
                status = "partial" if translated_chunks else "failed"
                return self._translation_result(
                    status,
                    target_lang,
                    source_lang,
                    providers=used_providers,
                    total_segments=len(chunks),
                    translated_segments=len(translated_chunks),
                    failed_segments=len(chunks) - len(translated_chunks),
                    error=f"chunk_{index}_failed",
                )
            translated_chunks.append(translated)
            if provider and provider not in used_providers:
                used_providers.append(provider)
            if index < len(chunks) and self.request_interval:
                time.sleep(self.request_interval)

        return self._translation_result(
            "completed",
            target_lang,
            source_lang,
            content="".join(translated_chunks),
            providers=used_providers,
            total_segments=len(chunks),
            translated_segments=len(chunks),
        )

    def _translate_with_retry(
        self, text: str, target_lang: str, source_lang: str
    ) -> Tuple[Optional[str], Optional[str]]:
        logger.info("翻译文本: %s -> %s", source_lang, target_lang)
        logger.debug("原文摘要: %s", summarize_text(text, 100))
        for retry_index in range(self.max_retries):
            for spec in self.provider_specs:
                translated = self._translate_with_provider(
                    spec, text, target_lang, source_lang
                )
                if translated:
                    logger.info("字幕翻译成功: provider=%s", spec["name"])
                    return translated, spec["name"]
                if self.request_interval:
                    time.sleep(self.request_interval)
            if retry_index < self.max_retries - 1:
                delay = self.base_delay * (2**retry_index) + random.uniform(0, 1)
                logger.info("所有字幕翻译 provider 失败，%.1f 秒后重试", delay)
                time.sleep(delay)
        logger.error("字幕翻译完全失败，已重试 %s 次", self.max_retries)
        return None, None

    def _translate_with_provider(
        self,
        spec: Dict[str, str],
        text: str,
        target_lang: str,
        source_lang: str,
    ) -> Optional[str]:
        if spec["kind"] == "deepl":
            return self._translate_with_deepl_api(text, target_lang, source_lang)
        if spec["kind"] == "deeplx":
            return self._translate_with_deeplx(text, target_lang, source_lang)
        if spec["kind"] == "openai":
            config = self.openai_providers.get(spec["config"])
            if not config and len(self.openai_providers) == 1:
                config = next(iter(self.openai_providers.values()))
            return self._translate_with_openai(
                text, target_lang, source_lang, config or {}
            )
        return None

    def _split_text_into_chunks(self, text: str) -> List[str]:
        if len(text) <= self.max_chunk_length:
            return [text]
        chunks = []
        current_pos = 0
        while current_pos < len(text):
            end_pos = min(current_pos + self.target_chunk_length, len(text))
            if end_pos < len(text):
                search_start = max(current_pos + self.min_chunk_length, end_pos - 200)
                search_end = min(end_pos + 200, len(text))
                for break_char in ("\n\n", "。", "！", "？", ".", "!", "?"):
                    break_pos = text.rfind(break_char, search_start, search_end)
                    if break_pos != -1:
                        end_pos = break_pos + len(break_char)
                        break
            if end_pos <= current_pos:
                end_pos = min(current_pos + self.target_chunk_length, len(text))
            chunk = text[current_pos:end_pos]
            if chunk:
                chunks.append(chunk)
            current_pos = end_pos
        return chunks or [text]

    def _translate_with_deeplx(
        self, text: str, target_lang: str, source_lang: str
    ) -> Optional[str]:
        if not self._check_deeplx_service():
            return None
        data = {
            "text": text,
            "source_lang": source_lang if source_lang != "auto" else "AUTO",
            "target_lang": self.language_map.get(target_lang, target_lang.upper()),
        }
        try:
            response = requests.post(
                f"{self.deeplx_server}/translate",
                json=data,
                timeout=self.request_timeout,
            )
            if response.status_code != 200:
                self._mark_deeplx_unavailable()
                logger.warning("DeepLX翻译失败，状态码: %s", response.status_code)
                return None
            translated = (response.json() or {}).get("data")
            return translated.strip() if isinstance(translated, str) else None
        except requests.RequestException as exc:
            self._mark_deeplx_unavailable()
            logger.debug("DeepLX翻译请求失败: %s", exc)
            return None

    def _translate_with_deepl_api(
        self, text: str, target_lang: str, source_lang: str
    ) -> Optional[str]:
        if not self.deepl_api_key:
            return None
        data = {
            "text": [text],
            "target_lang": self.language_map.get(target_lang, target_lang.upper()),
            "source_lang": source_lang if source_lang != "auto" else None,
        }
        data = {key: value for key, value in data.items() if value is not None}
        try:
            response = requests.post(
                f"{self.deepl_base_url}/translate",
                json=data,
                headers={
                    "Authorization": f"DeepL-Auth-Key {self.deepl_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.request_timeout,
            )
            if response.status_code != 200:
                logger.warning("DeepL API翻译失败，状态码: %s", response.status_code)
                return None
            translations = (response.json() or {}).get("translations") or []
            translated = translations[0].get("text") if translations else None
            return translated.strip() if isinstance(translated, str) else None
        except requests.RequestException as exc:
            logger.debug("DeepL API翻译请求失败: %s", exc)
            return None

    def _translate_with_openai(
        self,
        text: str,
        target_lang: str,
        source_lang: str,
        config: Dict[str, Any],
    ) -> Optional[str]:
        api_key = config.get("api_key")
        endpoint = config.get("api_endpoint") or config.get("base_url")
        model = config.get("model") or "gpt-4o-mini"
        if not isinstance(api_key, str) or not api_key.strip():
            return None
        if not isinstance(endpoint, str) or not endpoint.strip():
            endpoint = "https://api.openai.com/v1"
        endpoint = endpoint.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"

        target_name = self.get_supported_languages().get(target_lang, target_lang)
        prompt_template = config.get("prompt")
        if isinstance(prompt_template, str) and prompt_template.strip():
            try:
                instruction = prompt_template.format(target_lang=target_name)
            except (KeyError, ValueError):
                instruction = prompt_template
        else:
            instruction = (
                f"Translate the following subtitle text to {target_name}. "
                "Preserve meaning, line breaks, names, and formatting. "
                "Return only the translation."
            )
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a precise professional subtitle translator.",
                },
                {"role": "user", "content": f"{instruction}\n\n{text}"},
            ],
            "temperature": 0.2,
        }
        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key.strip()}",
                    "Content-Type": "application/json",
                },
                timeout=self.request_timeout,
            )
            if response.status_code != 200:
                logger.warning(
                    "OpenAI兼容翻译失败，状态码: %s", response.status_code
                )
                return None
            choices = (response.json() or {}).get("choices") or []
            message = choices[0].get("message") if choices else None
            translated = message.get("content") if isinstance(message, dict) else None
            return translated.strip() if isinstance(translated, str) else None
        except requests.RequestException as exc:
            logger.debug("OpenAI兼容翻译请求失败: %s", exc)
            return None

    def _check_deeplx_service(self) -> bool:
        now = time.monotonic()
        with self._deeplx_health_lock:
            if (
                self._deeplx_health_checked_at > 0
                and now - self._deeplx_health_checked_at
                < self.deeplx_cooldown_seconds
            ):
                return self._deeplx_available
            try:
                response = requests.get(
                    f"{self.deeplx_server}/",
                    timeout=self.deeplx_health_timeout,
                )
                self._deeplx_available = response.status_code == 200
            except requests.RequestException:
                self._deeplx_available = False
            self._deeplx_health_checked_at = now
            return self._deeplx_available

    def _mark_deeplx_unavailable(self) -> None:
        with self._deeplx_health_lock:
            self._deeplx_available = False
            self._deeplx_health_checked_at = time.monotonic()

    def translate_subtitle_content(
        self, content: str, target_lang: str, source_lang: str = "auto"
    ) -> Optional[str]:
        """Return a subtitle only when every translatable segment succeeds."""
        result = self.translate_subtitle_content_detailed(
            content, target_lang, source_lang
        )
        return result.get("content") if result.get("status") == "completed" else None

    def translate_subtitle_content_detailed(
        self, content: str, target_lang: str, source_lang: str = "auto"
    ) -> Dict[str, Any]:
        if not isinstance(content, str) or not content.strip():
            return self._translation_result(
                "failed", target_lang, source_lang, error="empty_subtitle"
            )
        if not self._is_srt_format(content):
            return self.translate_text_detailed(content, target_lang, source_lang)

        blocks = re.split(r"\n\s*\n", content.strip())
        total_translatable_blocks = sum(
            1
            for block in blocks
            if self._is_translatable_srt_block(block)
        )
        translated_blocks: List[str] = []
        used_providers: List[str] = []
        translated_count = 0
        translatable_count = 0
        for block_index, block in enumerate(blocks, 1):
            lines = block.strip().splitlines()
            if len(lines) < 3 or not self._is_srt_timing_line(lines[1]):
                translated_blocks.append(block)
                continue
            translatable_count += 1
            result = self.translate_text_detailed(
                "\n".join(lines[2:]), target_lang, source_lang
            )
            if result.get("status") != "completed" or not result.get("content"):
                status = "partial" if translated_count else "failed"
                return self._translation_result(
                    status,
                    target_lang,
                    source_lang,
                    providers=used_providers,
                    total_segments=total_translatable_blocks,
                    translated_segments=translated_count,
                    failed_segments=total_translatable_blocks - translated_count,
                    error=f"subtitle_block_{block_index}_failed",
                )
            translated_blocks.append(
                f"{lines[0]}\n{lines[1]}\n{result['content']}"
            )
            translated_count += 1
            for provider in result.get("providers") or []:
                if provider not in used_providers:
                    used_providers.append(provider)

        if not translatable_count:
            return self._translation_result(
                "failed", target_lang, source_lang, error="no_srt_blocks"
            )
        return self._translation_result(
            "completed",
            target_lang,
            source_lang,
            content="\n\n".join(translated_blocks),
            providers=used_providers,
            total_segments=translatable_count,
            translated_segments=translated_count,
        )

    @staticmethod
    def _is_srt_format(content: str) -> bool:
        return bool(
            re.search(
                r"\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}",
                content,
            )
        )

    @staticmethod
    def _is_srt_timing_line(line: str) -> bool:
        return bool(
            re.fullmatch(
                r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
                r"\d{2}:\d{2}:\d{2}[,.]\d{3}",
                line.strip(),
            )
        )

    @classmethod
    def _is_translatable_srt_block(cls, block: str) -> bool:
        lines = block.strip().splitlines()
        return len(lines) >= 3 and cls._is_srt_timing_line(lines[1])

    @staticmethod
    def _translation_result(
        status: str,
        target_language: str,
        source_language: str,
        *,
        content: Optional[str] = None,
        providers: Optional[List[str]] = None,
        total_segments: int = 0,
        translated_segments: int = 0,
        failed_segments: int = 0,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "status": status,
            "content": content if status == "completed" else None,
            "source_language": source_language,
            "target_language": target_language,
            "providers": list(providers or []),
            "total_segments": total_segments,
            "translated_segments": translated_segments,
            "failed_segments": failed_segments,
            "error": error,
        }

    def batch_translate(
        self, texts: List[str], target_lang: str, source_lang: str = "auto"
    ) -> Dict[str, Any]:
        results = []
        failed = 0
        for text in texts:
            translated = self.translate_text(text, target_lang, source_lang)
            if translated is None:
                failed += 1
            results.append(translated)
        return {
            "total": len(texts),
            "successful": len(texts) - failed,
            "failed": failed,
            "results": results,
        }

    def detect_language(self, text: str) -> Optional[str]:
        if not isinstance(text, str) or not text.strip():
            return None
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        japanese_chars = len(re.findall(r"[\u3040-\u30ff]", text))
        korean_chars = len(re.findall(r"[\uac00-\ud7af]", text))
        english_chars = len(re.findall(r"[a-zA-Z]", text))
        total_chars = len([char for char in text if char.isalnum()])
        if total_chars == 0:
            return None
        if chinese_chars / total_chars > 0.3:
            return "zh"
        if japanese_chars / total_chars > 0.2:
            return "ja"
        if korean_chars / total_chars > 0.2:
            return "ko"
        if english_chars / total_chars > 0.5:
            return "en"
        return "auto"

    @staticmethod
    def get_supported_languages() -> Dict[str, str]:
        return {
            "zh": "中文",
            "zh-CN": "简体中文",
            "zh-TW": "繁体中文",
            "en": "English",
            "en-US": "English (US)",
            "en-GB": "English (UK)",
            "ja": "日本語",
            "ko": "한국어",
            "fr": "Français",
            "de": "Deutsch",
            "es": "Español",
            "it": "Italiano",
            "pt": "Português",
            "ru": "Русский",
        }
