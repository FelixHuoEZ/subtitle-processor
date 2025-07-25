"""Translation service for subtitle content using various translation APIs."""

import json
import logging
import time
import random
import requests
from typing import Dict, Any, Optional, List
from ..config.config_manager import get_config_value

logger = logging.getLogger(__name__)


class TranslationService:
    """翻译服务 - 支持DeepL、OpenAI等多种翻译API"""
    
    def __init__(self):
        """初始化翻译服务"""
        # DeepL配置
        self.deeplx_server = get_config_value('servers.deeplx', 'http://localhost:1188')
        self.deepl_api_key = get_config_value('tokens.deepl.api_key', '')
        self.deepl_base_url = get_config_value('tokens.deepl.base_url', 'https://api-free.deepl.com/v2')
        
        # OpenAI配置
        self.openai_api_key = get_config_value('tokens.openai.api_key', '')
        self.openai_base_url = get_config_value('tokens.openai.base_url', 'https://api.openai.com/v1')
        self.openai_model = get_config_value('tokens.openai.model', 'gpt-3.5-turbo')
        
        # 翻译重试配置
        self.max_retries = get_config_value('translation.max_retries', 3)
        self.base_delay = get_config_value('translation.base_delay', 3)
        self.request_interval = get_config_value('translation.request_interval', 1.0)
        
        # 分块翻译配置
        self.target_chunk_length = get_config_value('translation.chunk_size', 2000)
        self.min_chunk_length = get_config_value('translation.min_chunk_size', 1600) 
        self.max_chunk_length = get_config_value('translation.max_chunk_size', 2400)
        
        # 语言映射
        self.language_map = {
            'zh': 'ZH',
            'zh-CN': 'ZH', 
            'zh-TW': 'ZH',
            'en': 'EN',
            'en-US': 'EN',
            'en-GB': 'EN',
            'ja': 'JA',
            'ko': 'KO',
            'fr': 'FR',
            'de': 'DE',
            'es': 'ES',
            'it': 'IT',
            'pt': 'PT',
            'ru': 'RU'
        }
    
    def translate_text(self, text: str, target_lang: str, source_lang: str = 'auto') -> Optional[str]:
        """翻译文本
        
        Args:
            text: 待翻译文本
            target_lang: 目标语言代码
            source_lang: 源语言代码，默认自动检测
            
        Returns:
            str: 翻译结果，失败返回None
        """
        try:
            if not text or not text.strip():
                logger.warning("翻译文本为空")
                return None
            
            logger.info(f"翻译文本: {source_lang} -> {target_lang}")
            logger.debug(f"原文: {text[:100]}...")
            
            # 检查文本长度，决定是否分块翻译
            if len(text) > self.max_chunk_length:
                logger.info(f"文本过长({len(text)}字符)，使用分块翻译")
                return self._translate_in_chunks(text, target_lang, source_lang)
            else:
                # 使用重试机制翻译
                return self._translate_with_retry(text, target_lang, source_lang)
            
        except Exception as e:
            logger.error(f"翻译文本失败: {str(e)}")
            return None

    def _translate_with_retry(self, text: str, target_lang: str, source_lang: str = 'auto') -> Optional[str]:
        """带重试机制的翻译"""
        # 翻译服务优先级
        services = [
            ('DeepLX', self._translate_with_deeplx),
            ('DeepL API', self._translate_with_deepl_api),
            ('OpenAI', self._translate_with_openai)
        ]
        
        for retry in range(self.max_retries):
            for service_name, translate_func in services:
                try:
                    logger.debug(f"尝试使用 {service_name} 翻译 (重试 {retry + 1}/{self.max_retries})")
                    
                    result = translate_func(text, target_lang, source_lang)
                    if result:
                        logger.info(f"{service_name} 翻译成功")
                        return result
                    else:
                        logger.debug(f"{service_name} 翻译失败，尝试下一个服务")
                        # 在服务之间添加间隔
                        time.sleep(self.request_interval)
                        
                except Exception as e:
                    logger.debug(f"{service_name} 翻译出错: {str(e)}")
                    continue
            
            # 如果所有服务都失败，等待后重试
            if retry < self.max_retries - 1:
                delay = self.base_delay * (2 ** retry) + random.uniform(0, 1)
                logger.info(f"所有翻译服务都失败，等待 {delay:.1f} 秒后重试")
                time.sleep(delay)
        
        logger.error(f"翻译完全失败，已重试 {self.max_retries} 次")
        return None
    
    def _translate_in_chunks(self, text: str, target_lang: str, source_lang: str) -> Optional[str]:
        """分块翻译长文本"""
        try:
            # 分割文本为合适的块
            chunks = self._split_text_into_chunks(text)
            logger.info(f"文本分割为 {len(chunks)} 个块进行翻译")
            
            translated_chunks = []
            
            for i, chunk in enumerate(chunks, 1):
                logger.debug(f"翻译块 {i}/{len(chunks)} ({len(chunk)} 字符)")
                
                translated_chunk = self._translate_with_retry(chunk, target_lang, source_lang)
                if translated_chunk:
                    translated_chunks.append(translated_chunk)
                else:
                    logger.error(f"翻译块 {i} 失败")
                    return None  # 如果任何一块失败，整个翻译失败
                
                # 添加请求间隔
                if i < len(chunks):
                    time.sleep(self.request_interval)
            
            # 合并翻译结果
            result = ''.join(translated_chunks)
            logger.info(f"分块翻译完成，总长度: {len(result)} 字符")
            return result
            
        except Exception as e:
            logger.error(f"分块翻译失败: {str(e)}")
            return None
    
    def _split_text_into_chunks(self, text: str) -> List[str]:
        """将文本分割为合适大小的块"""
        try:
            if len(text) <= self.max_chunk_length:
                return [text]
            
            chunks = []
            current_pos = 0
            
            while current_pos < len(text):
                # 计算下一个块的结束位置
                end_pos = min(current_pos + self.target_chunk_length, len(text))
                
                # 如果不是最后一块，尝试在句子边界处分割
                if end_pos < len(text):
                    # 寻找合适的分割点（句号、问号、感叹号等）
                    sentence_breaks = ['。', '！', '？', '.', '!', '?', '\n\n']
                    
                    # 在目标长度附近寻找句子边界
                    search_start = max(current_pos + self.min_chunk_length, end_pos - 200)
                    search_end = min(end_pos + 200, len(text))
                    
                    best_break = end_pos
                    for break_char in sentence_breaks:
                        break_pos = text.rfind(break_char, search_start, search_end)
                        if break_pos != -1:
                            # 找到句子边界，在其后分割
                            best_break = break_pos + len(break_char)
                            break
                    
                    end_pos = best_break
                
                # 确保块不为空
                if end_pos > current_pos:
                    chunk = text[current_pos:end_pos].strip()
                    if chunk:
                        chunks.append(chunk)
                
                current_pos = end_pos
                
                # 防止无限循环
                if current_pos == end_pos and end_pos < len(text):
                    current_pos += 1
            
            logger.debug(f"文本分割完成: {len(chunks)} 个块，长度分别为 {[len(c) for c in chunks]}")
            return chunks
            
        except Exception as e:
            logger.error(f"分割文本失败: {str(e)}")
            return [text]  # 出错时返回原文本
    
    def _translate_with_deeplx(self, text: str, target_lang: str, source_lang: str) -> Optional[str]:
        """使用DeepLX翻译"""
        try:
            # 检查DeepLX服务是否可用
            if not self._check_deeplx_service():
                logger.debug("DeepLX服务不可用")
                return None
            
            # 构造请求数据
            data = {
                'text': text,
                'source_lang': source_lang if source_lang != 'auto' else 'AUTO',
                'target_lang': self.language_map.get(target_lang, target_lang.upper())
            }
            
            # 发送翻译请求
            url = f"{self.deeplx_server}/translate"
            response = requests.post(url, json=data, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                if 'data' in result and result['data']:
                    translated_text = result['data']
                    logger.debug(f"DeepLX翻译结果: {translated_text[:100]}...")
                    return translated_text
                else:
                    logger.warning("DeepLX返回结果为空")
                    return None
            else:
                logger.warning(f"DeepLX翻译失败，状态码: {response.status_code}")
                return None
                
        except Exception as e:
            logger.debug(f"DeepLX翻译出错: {str(e)}")
            return None
    
    def _translate_with_deepl_api(self, text: str, target_lang: str, source_lang: str) -> Optional[str]:
        """使用官方DeepL API翻译"""
        try:
            if not self.deepl_api_key:
                logger.debug("DeepL API密钥未配置")
                return None
            
            # 构造请求数据
            data = {
                'text': [text],
                'target_lang': self.language_map.get(target_lang, target_lang.upper()),
                'source_lang': source_lang if source_lang != 'auto' else None
            }
            
            # 移除None值
            data = {k: v for k, v in data.items() if v is not None}
            
            # 发送翻译请求
            url = f"{self.deepl_base_url}/translate"
            headers = {
                'Authorization': f'DeepL-Auth-Key {self.deepl_api_key}',
                'Content-Type': 'application/json'
            }
            
            response = requests.post(url, json=data, headers=headers, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                if 'translations' in result and result['translations']:
                    translated_text = result['translations'][0]['text']
                    logger.debug(f"DeepL API翻译结果: {translated_text[:100]}...")
                    return translated_text
                else:
                    logger.warning("DeepL API返回结果为空")
                    return None
            else:
                logger.warning(f"DeepL API翻译失败，状态码: {response.status_code}")
                return None
                
        except Exception as e:
            logger.debug(f"DeepL API翻译出错: {str(e)}")
            return None
    
    def _translate_with_openai(self, text: str, target_lang: str, source_lang: str) -> Optional[str]:
        """使用OpenAI翻译"""
        try:
            if not self.openai_api_key:
                logger.debug("OpenAI API密钥未配置")
                return None
            
            import openai
            
            # 配置OpenAI客户端
            client = openai.OpenAI(
                api_key=self.openai_api_key,
                base_url=self.openai_base_url
            )
            
            # 构造翻译提示
            lang_names = {
                'zh': '中文', 'zh-CN': '中文', 'zh-TW': '繁体中文',
                'en': 'English', 'en-US': 'English', 'en-GB': 'English',
                'ja': '日本语', 'ko': '한국어', 'fr': 'Français',
                'de': 'Deutsch', 'es': 'Español', 'it': 'Italiano',
                'pt': 'Português', 'ru': 'Русский'
            }
            
            target_lang_name = lang_names.get(target_lang, target_lang)
            prompt = f"请将以下文本翻译为{target_lang_name}，保持原意和语气，直接返回翻译结果：\\n\\n{text}"
            
            # 发送翻译请求
            response = client.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {"role": "system", "content": "你是一个专业的翻译助手，能够准确翻译各种语言的文本。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=2000
            )
            
            if response.choices and response.choices[0].message:
                translated_text = response.choices[0].message.content.strip()
                logger.debug(f"OpenAI翻译结果: {translated_text[:100]}...")
                return translated_text
            else:
                logger.warning("OpenAI返回结果为空")
                return None
                
        except Exception as e:
            logger.error(f"OpenAI翻译失败: {str(e)}")
            return None
    
    def _check_deeplx_service(self) -> bool:
        """检查DeepLX服务是否可用"""
        try:
            response = requests.get(f"{self.deeplx_server}/", timeout=5)
            return response.status_code == 200
        except Exception:
            return False
    
    def translate_subtitle_content(self, content: str, target_lang: str, source_lang: str = 'auto') -> Optional[str]:
        """翻译字幕内容
        
        Args:
            content: 字幕内容（SRT格式或纯文本）
            target_lang: 目标语言
            source_lang: 源语言
            
        Returns:
            str: 翻译后的字幕内容
        """
        try:
            # 检测是否为SRT格式
            if self._is_srt_format(content):
                return self._translate_srt_content(content, target_lang, source_lang)
            else:
                # 纯文本翻译
                return self.translate_text(content, target_lang, source_lang)
                
        except Exception as e:
            logger.error(f"翻译字幕内容失败: {str(e)}")
            return None
    
    def _is_srt_format(self, content: str) -> bool:
        """检测是否为SRT格式"""
        import re
        # 检查是否包含SRT时间戳格式
        time_pattern = r'\\d{2}:\\d{2}:\\d{2},\\d{3}\\s*-->\\s*\\d{2}:\\d{2}:\\d{2},\\d{3}'
        return bool(re.search(time_pattern, content))
    
    def _translate_srt_content(self, srt_content: str, target_lang: str, source_lang: str) -> Optional[str]:
        """翻译SRT格式字幕"""
        try:
            import re
            
            # 分割SRT内容
            blocks = re.split(r'\\n\\s*\\n', srt_content.strip())
            translated_blocks = []
            
            for block in blocks:
                if not block.strip():
                    continue
                
                lines = block.strip().split('\\n')
                if len(lines) < 3:
                    translated_blocks.append(block)
                    continue
                
                # 序号和时间戳保持不变
                subtitle_id = lines[0]
                timestamp = lines[1]
                
                # 翻译文本内容
                text_lines = lines[2:]
                text_content = '\\n'.join(text_lines)
                
                translated_text = self.translate_text(text_content, target_lang, source_lang)
                if translated_text:
                    # 重新组装字幕块
                    translated_block = f"{subtitle_id}\\n{timestamp}\\n{translated_text}"
                    translated_blocks.append(translated_block)
                else:
                    # 翻译失败，保持原文
                    translated_blocks.append(block)
            
            return '\\n\\n'.join(translated_blocks)
            
        except Exception as e:
            logger.error(f"翻译SRT内容失败: {str(e)}")
            return None
    
    def batch_translate(self, texts: List[str], target_lang: str, source_lang: str = 'auto') -> Dict[str, Any]:
        """批量翻译文本
        
        Args:
            texts: 文本列表
            target_lang: 目标语言
            source_lang: 源语言
            
        Returns:
            dict: 批量翻译结果
        """
        try:
            logger.info(f"开始批量翻译 {len(texts)} 条文本")
            
            results = []
            successful = 0
            failed = 0
            
            for i, text in enumerate(texts, 1):
                logger.debug(f"翻译进度: {i}/{len(texts)}")
                
                result = self.translate_text(text, target_lang, source_lang)
                if result:
                    results.append(result)
                    successful += 1
                else:
                    results.append(text)  # 翻译失败时保持原文
                    failed += 1
            
            summary = {
                'total': len(texts),
                'successful': successful,
                'failed': failed,
                'results': results
            }
            
            logger.info(f"批量翻译完成 - 成功: {successful}, 失败: {failed}")
            return summary
            
        except Exception as e:
            logger.error(f"批量翻译失败: {str(e)}")
            return {'total': 0, 'successful': 0, 'failed': 0, 'results': []}
    
    def detect_language(self, text: str) -> Optional[str]:
        """检测文本语言
        
        Args:
            text: 待检测文本
            
        Returns:
            str: 语言代码，失败返回None
        """
        try:
            if not text or not text.strip():
                return None
            
            # 使用简单的字符统计方法检测语言
            import re
            
            # 统计中文字符
            chinese_chars = len(re.findall(r'[\\u4e00-\\u9fff]', text))
            # 统计日文假名
            japanese_chars = len(re.findall(r'[\\u3040-\\u309f\\u30a0-\\u30ff]', text))
            # 统计韩文字符
            korean_chars = len(re.findall(r'[\\uac00-\\ud7af]', text))
            # 统计英文字符
            english_chars = len(re.findall(r'[a-zA-Z]', text))
            
            total_chars = len([c for c in text if c.isalnum()])
            
            if total_chars == 0:
                return None
            
            # 计算各语言字符占比
            chinese_ratio = chinese_chars / total_chars
            japanese_ratio = japanese_chars / total_chars
            korean_ratio = korean_chars / total_chars
            english_ratio = english_chars / total_chars
            
            # 根据占比判断语言
            if chinese_ratio > 0.3:
                return 'zh'
            elif japanese_ratio > 0.2:
                return 'ja'
            elif korean_ratio > 0.2:
                return 'ko'
            elif english_ratio > 0.5:
                return 'en'
            else:
                return 'auto'  # 无法确定
                
        except Exception as e:
            logger.error(f"语言检测失败: {str(e)}")
            return None
    
    def get_supported_languages(self) -> Dict[str, str]:
        """获取支持的语言列表"""
        return {
            'zh': '中文',
            'zh-CN': '简体中文',
            'zh-TW': '繁体中文',
            'en': 'English',
            'en-US': 'English (US)',
            'en-GB': 'English (UK)',
            'ja': '日本語',
            'ko': '한국어',
            'fr': 'Français',
            'de': 'Deutsch',
            'es': 'Español',
            'it': 'Italiano',
            'pt': 'Português',
            'ru': 'Русский'
        }