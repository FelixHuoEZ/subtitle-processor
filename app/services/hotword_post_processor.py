"""Hotword post-processing utilities applied after transcription."""

import os
import logging
import difflib
import re
from typing import Dict, List, Any, Optional

import jieba

from .hotword_settings import HotwordSettingsManager


logger = logging.getLogger(__name__)


class HotwordPostProcessor:
    """Apply hotword-aware corrections to transcription results."""

    def __init__(self, settings_manager: Optional[HotwordSettingsManager] = None) -> None:
        self.settings_manager = settings_manager or HotwordSettingsManager.get_instance()
        self.similarity_threshold = float(os.getenv("HOTWORD_POST_SIMILARITY", "0.82"))
        self.enable_substring = os.getenv("ENABLE_HOTWORD_SUBSTRING", "false").lower() == "true"
        if self.settings_manager.get_state().get("post_process"):
            logger.info(
                "热词后处理已启用: similarity_threshold=%.2f, substring=%s",
                self.similarity_threshold,
                "on" if self.enable_substring else "off",
            )

    def process_result(self, result: Dict[str, Any], hotwords: List[str]) -> Dict[str, Any]:
        """Mutate result text according to hotword list."""
        state = self.settings_manager.get_state()
        if not state.get("post_process") or not hotwords or not result:
            return result

        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            return result

        processed = self._process_text_with_hotwords(text, hotwords)
        if processed["processed_text"] == text:
            return result

        logger.info(
            "热词后处理生效，修正 %s 处: %s",
            processed["corrections"],
            processed["matches"],
        )

        result["text"] = processed["processed_text"]
        result.setdefault("hotword_postprocess", {})
        result["hotword_postprocess"].update(
            {
                "matches": processed["matches"],
                "corrections": processed["corrections"],
                "hotwords_applied": processed["hotwords_applied"],
            }
        )
        return result

    def _process_text_with_hotwords(self, text: str, hotwords: List[str]) -> Dict[str, Any]:
        words = self._segment_text(text)
        processed_words: List[str] = []
        matches: List[Dict[str, Any]] = []
        corrections = 0

        for index, word in enumerate(words):
            best_match = self._find_best_hotword_match(word, hotwords)
            if best_match:
                hotword, similarity = best_match
                processed_words.append(hotword)
                matches.append(
                    {
                        "original": word,
                        "hotword": hotword,
                        "similarity": round(similarity, 4),
                        "position": index,
                    }
                )
                corrections += 1
            else:
                processed_words.append(word)

        processed_text = "".join(processed_words)
        processed_text = self._context_based_replacement(processed_text, hotwords)

        return {
            "original_text": text,
            "processed_text": processed_text,
            "matches": matches,
            "corrections": corrections,
            "hotwords_applied": len(hotwords),
        }

    def _segment_text(self, text: str) -> List[str]:
        try:
            return [token for token in jieba.cut(text) if token]
        except Exception:
            tokens = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+|\d+|[^\w\s]", text)
            return tokens or [text]

    def _find_best_hotword_match(self, word: str, hotwords: List[str]):
        candidate = word.strip()
        if not candidate:
            return None

        clean_word = re.sub(r"[^\w]", "", candidate)
        if not clean_word:
            return None

        best_match = None
        best_similarity = 0.0

        for hotword in hotwords:
            if clean_word == hotword:
                return hotword, 1.0

            if self.enable_substring and (hotword in clean_word or clean_word in hotword):
                ratio = (
                    len(hotword) / len(clean_word)
                    if len(hotword) <= len(clean_word)
                    else len(clean_word) / len(hotword)
                )
                substring_similarity = ratio * 0.9
                if substring_similarity > best_similarity:
                    best_similarity = substring_similarity
                    best_match = hotword

            similarity = difflib.SequenceMatcher(
                None, clean_word.lower(), hotword.lower()
            ).ratio()
            length_factor = min(len(clean_word), len(hotword)) / max(len(clean_word), len(hotword))
            adjusted_similarity = similarity * (0.7 + 0.3 * length_factor)

            if adjusted_similarity > best_similarity:
                best_similarity = adjusted_similarity
                best_match = hotword

        if best_match and best_similarity >= self.similarity_threshold:
            return best_match, best_similarity
        return None

    def _context_based_replacement(self, text: str, hotwords: List[str]) -> str:
        replacements = self._generate_common_replacements(hotwords)
        for pattern, replacement in replacements.items():
            if pattern in text:
                text = text.replace(pattern, replacement)
        return text

    def _generate_common_replacements(self, hotwords: List[str]) -> Dict[str, str]:
        replacements: Dict[str, str] = {}
        for hotword in hotwords:
            if hotword.lower() == "ultrathink":
                replacements.update(
                    {
                        "乌托": "ultrathink",
                        "阿尔特拉": "ultrathink",
                        "奥特拉": "ultrathink",
                        "ultra": "ultrathink",
                        "Ultra": "ultrathink",
                        "乌尔特拉": "ultrathink",
                        "奥拉": "ultrathink",
                    }
                )
            elif hotword == "Python":
                replacements.update(
                    {
                        "派森": "Python",
                        "派桑": "Python",
                        "皮桑": "Python",
                        "python": "Python",
                    }
                )
            elif hotword == "编程":
                replacements.update({"便程": "编程", "编成": "编程", "变成": "编程"})
            elif hotword == "机器学习":
                replacements.update({"机械学习": "机器学习", "机器雪洗": "机器学习", "机器血洗": "机器学习"})
            elif hotword == "教程":
                replacements.update({"叫程": "教程", "较程": "教程"})

            if re.fullmatch(r"[a-zA-Z]+", hotword):
                for variant in self._generate_phonetic_variants(hotword):
                    replacements[variant] = hotword

        return replacements

    def _generate_phonetic_variants(self, english_word: str) -> List[str]:
        word_lower = english_word.lower()
        phonetic_map = {
            "ultra": ["乌尔特拉", "奥特拉", "阿尔特拉", "乌托拉"],
            "think": ["辛克", "思克", "听克", "滕克"],
            "python": ["派森", "派桑", "皮桑"],
            "java": ["加瓦", "佳瓦", "嘉瓦"],
            "docker": ["道克", "多克", "都克"],
            "kubernetes": ["库伯内蒂斯", "库贝内蒂斯"],
            "react": ["瑞艾克特", "里艾克特"],
            "angular": ["安古拉", "安格拉"],
            "github": ["吉特哈布", "基特哈布", "吉哈布"],
        }

        variants: List[str] = []
        if word_lower in phonetic_map:
            variants.extend(phonetic_map[word_lower])

        for key, values in phonetic_map.items():
            if key in word_lower or word_lower in key:
                variants.extend(values)
        return variants
