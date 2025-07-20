"""Translation service for text translation using various providers."""

import logging
import requests
import time
import json
from typing import Optional, Dict, List, Any

try:
    from ..config.config_manager import get_config_value
except ImportError:
    from config.config_manager import get_config_value

logger = logging.getLogger(__name__)


class TranslationService:
    """Service for text translation using DeepL, OpenAI, and other providers."""
    
    def __init__(self):
        self.max_retries = get_config_value('translation.max_retries', 3)
        self.base_delay = get_config_value('translation.base_delay', 3)
        self.request_interval = get_config_value('translation.request_interval', 1.0)
        self.chunk_size = get_config_value('translation.chunk_size', 2000)
        
        # Service configurations
        self.services = get_config_value('translation.services', [])
        self.deeplx_url = get_config_value('deeplx.api_url', 'http://deeplx:1188/translate')
        self.deeplx_v2_url = get_config_value('deeplx.api_v2_url', 'http://deeplx:1188/v2/translate')
        
        # Sort services by priority
        self.services.sort(key=lambda x: x.get('priority', 999))
    
    def translate_text(self, text: str, source_lang: str = 'en', target_lang: str = 'zh') -> str:
        """
        Translate text using available translation services.
        
        Args:
            text: Text to translate
            source_lang: Source language code
            target_lang: Target language code
            
        Returns:
            Translated text
        """
        if not text or not text.strip():
            return text
        
        logger.info(f"Starting translation: {len(text)} characters")
        logger.debug(f"Source language: {source_lang}, Target: {target_lang}")
        
        # Try each enabled service in priority order
        for service in self.services:
            if not service.get('enabled', False):
                continue
            
            service_name = service.get('name', '')
            logger.info(f"Trying translation service: {service_name}")
            
            try:
                if service_name == 'deeplx_v2':
                    result = self._translate_with_deeplx_v2(text, source_lang, target_lang)
                elif service_name == 'deeplx':
                    result = self._translate_with_deeplx(text, source_lang, target_lang)
                elif service_name.startswith('openai_'):
                    config_name = service.get('config_name', '')
                    result = self._translate_with_openai(text, source_lang, target_lang, config_name)
                else:
                    logger.warning(f"Unknown translation service: {service_name}")
                    continue
                
                if result and result != text:
                    logger.info(f"Translation successful with {service_name}")
                    return result
                    
            except Exception as e:
                logger.warning(f"Translation failed with {service_name}: {str(e)}")
                continue
        
        logger.warning("All translation services failed, returning original text")
        return text
    
    def _translate_with_deeplx_v2(self, text: str, source_lang: str, target_lang: str) -> Optional[str]:
        """Translate using DeepLX v2 API."""
        try:
            # Convert language codes for DeepL
            lang_map = {
                'en': 'EN',
                'zh': 'ZH-HANS',
                'zh-hans': 'ZH-HANS',
                'zh-hant': 'ZH-HANT',
                'ja': 'JA',
                'ko': 'KO',
                'fr': 'FR',
                'de': 'DE',
                'es': 'ES'
            }
            
            source = lang_map.get(source_lang.lower(), 'EN')
            target = lang_map.get(target_lang.lower(), 'ZH-HANS')
            
            data = {
                'text': text,
                'source_lang': source,
                'target_lang': target
            }
            
            response = requests.post(
                self.deeplx_v2_url,
                json=data,
                timeout=30,
                headers={'Content-Type': 'application/json'}
            )
            
            if response.status_code == 200:
                result = response.json()
                translated = result.get('data', '')
                if translated:
                    logger.debug(f"DeepLX v2 translation successful")
                    return translated
            
            logger.warning(f"DeepLX v2 failed: {response.status_code} {response.text}")
            return None
            
        except Exception as e:
            logger.error(f"DeepLX v2 translation error: {str(e)}")
            return None
    
    def _translate_with_deeplx(self, text: str, source_lang: str, target_lang: str) -> Optional[str]:
        """Translate using original DeepLX API."""
        try:
            data = {
                'text': text,
                'source_lang': source_lang.upper(),
                'target_lang': target_lang.upper()
            }
            
            response = requests.post(
                self.deeplx_url,
                json=data,
                timeout=30,
                headers={'Content-Type': 'application/json'}
            )
            
            if response.status_code == 200:
                result = response.json()
                translated = result.get('data', '')
                if translated:
                    logger.debug(f"DeepLX translation successful")
                    return translated
            
            logger.warning(f"DeepLX failed: {response.status_code} {response.text}")
            return None
            
        except Exception as e:
            logger.error(f"DeepLX translation error: {str(e)}")
            return None
    
    def _translate_with_openai(self, text: str, source_lang: str, target_lang: str, config_name: str) -> Optional[str]:
        """Translate using OpenAI API."""
        try:
            # Get OpenAI configuration
            openai_configs = get_config_value('tokens.openai', [])
            config = None
            
            for cfg in openai_configs:
                if cfg.get('name') == config_name:
                    config = cfg
                    break
            
            if not config:
                logger.error(f"OpenAI config '{config_name}' not found")
                return None
            
            api_key = config.get('api_key')
            api_endpoint = config.get('api_endpoint')
            model = config.get('model', 'gpt-3.5-turbo')
            prompt_template = config.get('prompt', 'Translate the following text to {target_lang}: {text}')
            
            if not api_key or not api_endpoint:
                logger.error(f"Incomplete OpenAI config for '{config_name}'")
                return None
            
            # Language name mapping
            lang_names = {
                'en': 'English',
                'zh': 'Chinese',
                'zh-hans': 'Simplified Chinese',
                'zh-hant': 'Traditional Chinese',
                'ja': 'Japanese',
                'ko': 'Korean',
                'fr': 'French',
                'de': 'German',
                'es': 'Spanish'
            }
            
            target_lang_name = lang_names.get(target_lang.lower(), target_lang)
            
            # Prepare prompt
            prompt = prompt_template.format(target_lang=target_lang_name, text=text)
            
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            }
            
            data = {
                'model': model,
                'messages': [
                    {'role': 'user', 'content': prompt}
                ],
                'temperature': 0.3,
                'max_tokens': len(text) * 2  # Estimate max tokens needed
            }
            
            response = requests.post(
                api_endpoint,
                json=data,
                headers=headers,
                timeout=60
            )
            
            if response.status_code == 200:
                result = response.json()
                if 'choices' in result and len(result['choices']) > 0:
                    translated = result['choices'][0]['message']['content'].strip()
                    if translated:
                        logger.debug(f"OpenAI translation successful with {config_name}")
                        return translated
            
            logger.warning(f"OpenAI {config_name} failed: {response.status_code} {response.text}")
            return None
            
        except Exception as e:
            logger.error(f"OpenAI translation error: {str(e)}")
            return None
    
    def _translate_with_retry(self, translate_func, text: str, source_lang: str, target_lang: str) -> Optional[str]:
        """
        Execute translation with retry logic.
        
        Args:
            translate_func: Translation function to call
            text: Text to translate
            source_lang: Source language
            target_lang: Target language
            
        Returns:
            Translated text or None
        """
        for attempt in range(self.max_retries):
            try:
                if attempt > 0:
                    delay = self.base_delay * (2 ** (attempt - 1))
                    logger.info(f"Retrying translation in {delay} seconds... (attempt {attempt + 1})")
                    time.sleep(delay)
                
                result = translate_func(text, source_lang, target_lang)
                if result:
                    return result
                    
            except Exception as e:
                logger.warning(f"Translation attempt {attempt + 1} failed: {str(e)}")
                if attempt == self.max_retries - 1:
                    logger.error("All translation attempts failed")
                    break
        
        return None
    
    def translate_chunks(self, text: str, source_lang: str = 'en', target_lang: str = 'zh') -> str:
        """
        Translate long text by splitting into chunks.
        
        Args:
            text: Text to translate
            source_lang: Source language code
            target_lang: Target language code
            
        Returns:
            Translated text
        """
        if len(text) <= self.chunk_size:
            return self.translate_text(text, source_lang, target_lang)
        
        logger.info(f"Splitting text into chunks for translation (max {self.chunk_size} chars per chunk)")
        
        # Split text into sentences first
        sentences = self._split_into_sentences(text)
        chunks = self._group_sentences_into_chunks(sentences, self.chunk_size)
        
        logger.info(f"Split into {len(chunks)} chunks")
        
        translated_chunks = []
        for i, chunk in enumerate(chunks, 1):
            logger.info(f"Translating chunk {i}/{len(chunks)}")
            
            translated = self.translate_text(chunk, source_lang, target_lang)
            translated_chunks.append(translated)
            
            # Add delay between requests
            if i < len(chunks):
                time.sleep(self.request_interval)
        
        return ' '.join(translated_chunks)
    
    def _split_into_sentences(self, text: str) -> List[str]:
        """Split text into sentences."""
        import re
        
        # Simple sentence splitting
        sentences = re.split(r'[.!?]+\s+', text)
        return [s.strip() for s in sentences if s.strip()]
    
    def _group_sentences_into_chunks(self, sentences: List[str], max_size: int) -> List[str]:
        """Group sentences into chunks under max_size."""
        chunks = []
        current_chunk = []
        current_size = 0
        
        for sentence in sentences:
            sentence_size = len(sentence)
            
            if current_size + sentence_size > max_size and current_chunk:
                # Start new chunk
                chunks.append(' '.join(current_chunk))
                current_chunk = [sentence]
                current_size = sentence_size
            else:
                current_chunk.append(sentence)
                current_size += sentence_size
        
        # Add remaining chunk
        if current_chunk:
            chunks.append(' '.join(current_chunk))
        
        return chunks