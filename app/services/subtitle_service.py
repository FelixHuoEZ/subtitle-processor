"""Subtitle processing service."""

import json
import logging
import re
from typing import List, Dict, Optional, Any

try:
    from ..utils.time_utils import format_time, parse_time
    from ..config.config_manager import get_config_value
except ImportError:
    from utils.time_utils import format_time, parse_time
    from config.config_manager import get_config_value

logger = logging.getLogger(__name__)


class SubtitleService:
    """Service for processing subtitles and transcription results."""
    
    def __init__(self):
        self.translate_max_retries = get_config_value('translation.max_retries', 3)
        self.translate_base_delay = get_config_value('translation.base_delay', 3)
        self.translate_request_interval = get_config_value('translation.request_interval', 1.0)
        self.translate_target_length = get_config_value('translation.chunk_size', 2000)
    
    def parse_srt(self, result: Any, hotwords: Optional[List[str]] = None) -> Optional[List[Dict]]:
        """
        Parse FunASR results to SRT format.
        
        Args:
            result: FunASR recognition result
            hotwords: Hot words list for logging and debugging
            
        Returns:
            List of subtitle dictionaries with start, duration, and text
        """
        try:
            logger.info("Starting subtitle content parsing")
            logger.debug(f"Input result type: {type(result)}")
            logger.debug(f"Input result content: {result}")
            
            text_content = None
            timestamps = None
            duration = None
            
            # If result is string, try to parse as dictionary
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                    logger.debug("Successfully parsed string to dictionary")
                except json.JSONDecodeError:
                    logger.debug("Input is plain text, using directly")
                    text_content = result
            
            # Extract information from dictionary
            if isinstance(result, dict):
                # Get audio duration
                if 'audio_info' in result and 'duration_seconds' in result['audio_info']:
                    duration = result['audio_info']['duration_seconds']
                    logger.debug(f"Got audio duration: {duration} seconds")
                
                # Get text content
                if 'text' in result:
                    if isinstance(result['text'], str):
                        text_content = result['text']
                        logger.debug(f"Got text content: {text_content[:200]}...")
                    else:
                        logger.error(f"text field is not string type: {type(result['text'])}")
                        return None
                
                # Get timestamps
                if 'timestamp' in result:
                    timestamps = result['timestamp']
                    if isinstance(timestamps, str):
                        try:
                            timestamps = json.loads(timestamps)
                            logger.debug("Successfully parsed timestamp string")
                        except json.JSONDecodeError:
                            logger.warning("Timestamp parsing failed, will not use timestamps")
                            timestamps = None
                    
                    if timestamps:
                        logger.debug(f"Timestamp count: {len(timestamps)}")
            
            if not text_content:
                logger.error("No valid text content found")
                return None
            
            # Split text into sentences
            sentences = self.split_into_sentences(text_content)
            
            # If we have timestamps, use them to generate subtitles
            if timestamps and isinstance(timestamps, list) and len(timestamps) > 0:
                logger.info("Using timestamps to generate subtitles")
                subtitles = []
                current_text = []
                current_start = timestamps[0][0]
                
                for i, (start, end) in enumerate(timestamps):
                    if i < len(text_content):
                        char = text_content[i]
                        current_text.append(char)
                        
                        # Determine if current subtitle should end
                        is_sentence_end = char in '.。!！?？;；'
                        is_too_long = len(''.join(current_text)) >= 25
                        is_long_pause = i < len(timestamps) - 1 and timestamps[i+1][0] - end > 800
                        is_natural_break = char in '，,、' and len(''.join(current_text)) >= 15
                        is_last_char = i == len(text_content) - 1
                        
                        if is_sentence_end or is_too_long or is_long_pause or is_natural_break or is_last_char:
                            if current_text:
                                subtitle = {
                                    'start': current_start / 1000.0,
                                    'duration': (end - current_start) / 1000.0,
                                    'text': ''.join(current_text).strip()
                                }
                                subtitles.append(subtitle)
                                logger.debug(f"Added subtitle: {subtitle}")
                                current_text = []
                                if i < len(timestamps) - 1:
                                    current_start = timestamps[i+1][0]
                
                # Handle remaining text
                if current_text:
                    subtitle = {
                        'start': current_start / 1000.0,
                        'duration': (timestamps[-1][1] - current_start) / 1000.0,
                        'text': ''.join(current_text).strip()
                    }
                    subtitles.append(subtitle)
                    logger.debug(f"Added final subtitle: {subtitle}")
                
                logger.info(f"Generated {len(subtitles)} subtitles using timestamps")
                return subtitles
            
            # If no timestamps, use estimated timestamps
            logger.info("Using estimated timestamps to generate subtitles")
            return self.generate_srt_timestamps(sentences, duration)
        
        except Exception as e:
            logger.error(f"Error parsing subtitles: {str(e)}")
            logger.error(f"Error input content: {result}")
            return None
    
    def parse_srt_content(self, srt_content: str) -> List[Dict]:
        """
        Parse SRT format subtitle content.
        
        Args:
            srt_content: SRT format subtitle content or transcription result
            
        Returns:
            List of parsed subtitles with id, start, end, duration and text fields
        """
        if not srt_content or not isinstance(srt_content, str):
            logger.error("Invalid subtitle content")
            return []
        
        logger.info(f"Starting subtitle content parsing, length: {len(srt_content)}")
        logger.debug(f"First 100 characters of subtitle content: {srt_content[:100]}")
        
        # Check if it's transcription result (no timestamps)
        if not re.search(r'\d+:\d+:\d+', srt_content):
            logger.info("Detected content is transcription result, need to generate timestamps")
            sentences = self.split_into_sentences(srt_content)
            if not sentences:
                logger.error("Cannot split sentences or sentence list is empty")
                return []
                
            logger.info(f"Split into {len(sentences)} sentences")
            
            # Generate timestamps (average time allocation per sentence)
            # Assume 0.3 seconds per character
            total_duration = sum(len(s) * 0.3 for s in sentences)
            logger.info(f"Estimated total duration: {total_duration} seconds")
            
            # Generate SRT format content
            srt_lines = []
            current_time = 0
            for i, sentence in enumerate(sentences, 1):
                if not sentence.strip():
                    continue
                duration = (len(sentence) / sum(len(s) for s in sentences)) * total_duration
                end_time = min(current_time + duration, total_duration)
                
                srt_lines.extend([
                    str(i),
                    f"{format_time(current_time)} --> {format_time(end_time)}",
                    sentence.strip(),
                    ""
                ])
                current_time = end_time
            
            srt_content = "\n".join(srt_lines)
            logger.info("Generated SRT format content with timestamps")
            
        subtitles_list = []
        current_subtitle = {}
        expected_id = 1
        
        try:
            lines = srt_content.strip().split('\n')
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                
                # Skip empty lines
                if not line:
                    i += 1
                    continue
                
                try:
                    # Subtitle ID
                    subtitle_id = int(line)
                    if subtitle_id != expected_id:
                        logger.warning(f"Non-consecutive subtitle ID: expected {expected_id}, got {subtitle_id}")
                    
                    current_subtitle = {'id': subtitle_id}
                    i += 1
                    
                    # Timeline
                    if i >= len(lines):
                        raise ValueError("Subtitle format error: missing timestamp line")
                        
                    time_line = lines[i].strip()
                    if '-->' not in time_line:
                        raise ValueError(f"Invalid timestamp format: {time_line}")
                        
                    try:
                        start_time, end_time = time_line.split(' --> ')
                        current_subtitle['start'] = parse_time(start_time.strip())
                        current_subtitle['end'] = parse_time(end_time.strip())
                        
                        if current_subtitle['start'] is None or current_subtitle['end'] is None:
                            raise ValueError("Invalid timestamp values")
                        if current_subtitle['start'] >= current_subtitle['end']:
                            raise ValueError("End time is earlier than start time")
                            
                        current_subtitle['duration'] = current_subtitle['end'] - current_subtitle['start']
                    except Exception as e:
                        logger.error(f"Error parsing timestamp: {str(e)}, line content: {time_line}")
                        raise ValueError(f"Timestamp parsing failed: {str(e)}")
                    
                    i += 1
                    
                    # Subtitle text
                    text_lines = []
                    while i < len(lines) and lines[i].strip():
                        text_lines.append(lines[i].strip())
                        i += 1
                    
                    if not text_lines:
                        logger.warning(f"Subtitle {subtitle_id} has no text content")
                        continue
                        
                    current_subtitle['text'] = ' '.join(text_lines)  # Join multiple lines with space
                    if len(current_subtitle['text']) > 0:
                        subtitles_list.append(current_subtitle)
                        expected_id += 1
                    
                except ValueError as e:
                    logger.error(f"Error parsing subtitle line: {str(e)}, line content: {line}")
                    # Try to skip to next subtitle block
                    while i < len(lines) and lines[i].strip():
                        i += 1
                    i += 1
                    expected_id += 1
                    continue
        
        except Exception as e:
            logger.error(f"Error parsing SRT content: {str(e)}")
            logger.error(f"First 100 characters of SRT content: {srt_content[:100]}")
            logger.exception("Detailed error information:")
        
        if not subtitles_list:
            logger.warning("No valid subtitles parsed")
        else:
            logger.info(f"Successfully parsed {len(subtitles_list)} subtitles")
        
        return subtitles_list
    
    def split_into_sentences(self, text: str) -> List[str]:
        """
        Split text into sentences.
        
        Args:
            text: Input text
            
        Returns:
            List of sentences
        """
        try:
            if not text:
                logger.error("Input text is empty")
                return []
                
            # Sentence ending punctuation
            sentence_endings = r'[。！？!?]+'
            
            # Split by punctuation
            sentences = []
            current_sentence = ""
            
            for char in text:
                current_sentence += char
                
                # If we encounter sentence ending punctuation and current sentence is not empty
                if re.search(sentence_endings, char) and current_sentence.strip():
                    sentences.append(current_sentence.strip())
                    current_sentence = ""
                    
            # Handle last sentence
            if current_sentence.strip():
                sentences.append(current_sentence.strip())
                
            # Filter out sentences that are too short
            sentences = [s for s in sentences if len(s) > 1]
            
            # Log processing results
            total_sentences = len(sentences)
            logger.info(f"Splitting completed, total {total_sentences} sentences")
            
            # Only show first 10 and last 10 sentences
            if total_sentences > 20:
                for i, sentence in enumerate(sentences[:10]):
                    logger.debug(f"Sentence[{i+1}]: {sentence[:50]}...")
                logger.debug("...")
                for i, sentence in enumerate(sentences[-10:]):
                    logger.debug(f"Sentence[{total_sentences-10+i+1}]: {sentence[:50]}...")
            else:
                for i, sentence in enumerate(sentences):
                    logger.debug(f"Sentence[{i+1}]: {sentence[:50]}...")
            
            return sentences
                
        except Exception as e:
            logger.error(f"Error splitting sentences: {str(e)}")
            return []
    
    def generate_srt_timestamps(self, sentences: List[str], total_duration: Optional[float] = None) -> List[Dict]:
        """
        Generate timestamps for sentences.
        
        Args:
            sentences: List of sentences
            total_duration: Total duration in seconds
            
        Returns:
            List of subtitle dictionaries with timestamps
        """
        try:
            if not sentences:
                logger.error("No sentences to generate timestamps for")
                return []
                
            logger.info("Starting timestamp generation")
            
            # If no total duration provided, use estimated value
            if not total_duration:
                # Assume 0.3 seconds per character
                total_duration = sum(len(s) * 0.3 for s in sentences)
                logger.info("Using estimated timestamps to generate subtitles")
                
            logger.debug(f"Total duration: {total_duration} seconds")
            logger.debug(f"Number of sentences: {len(sentences)}")
            
            # Calculate duration for each sentence
            total_chars = sum(len(s) for s in sentences)
            timestamps = []
            current_time = 0
            
            # Only show first 10 and last 10 timestamp generations
            total_sentences = len(sentences)
            for i, sentence in enumerate(sentences, 1):
                duration = (len(sentence) / total_chars) * total_duration
                end_time = min(current_time + duration, total_duration)
                
                if i < 10 or i >= total_sentences - 10:
                    logger.debug(f"Generate subtitle[{i}/{total_sentences}]: {current_time:.1f}s - {sentence[:50]}...")
                elif i == 10:
                    logger.debug("...")
                
                timestamps.append({
                    'start': current_time,
                    'end': end_time,
                    'duration': duration,
                    'text': sentence
                })
                
                current_time = end_time
                
            return timestamps
                
        except Exception as e:
            logger.error(f"Error generating timestamps: {str(e)}")
            return []
    
    def clean_subtitle_content(self, content: str, is_funasr: bool = False) -> Optional[str]:
        """
        Clean subtitle content.
        
        Args:
            content: Raw subtitle content
            is_funasr: Whether it's FunASR result
            
        Returns:
            Cleaned content
        """
        try:
            if not content:
                return None
                
            # If it's FunASR result, needs special handling
            if is_funasr:
                # Remove excess punctuation
                content = re.sub(r'[,.，。]+(?=[,.，。])', '', content)
                # Remove duplicate spaces
                content = re.sub(r'\s+', ' ', content)
                # Remove empty lines
                content = '\n'.join(line for line in content.split('\n') if line.strip())
                return content
                
            # Remove empty lines
            lines = content.split('\n')
            cleaned_lines = []
            for line in lines:
                line = line.strip()
                if line:
                    cleaned_lines.append(line)
            
            # Recombine
            return '\n'.join(cleaned_lines)
        except Exception as e:
            logger.error(f"Error cleaning subtitle content: {str(e)}")
            return content
    
    def process_subtitle_content(self, content: str, is_funasr: bool = False, 
                               translate: bool = False, language: Optional[str] = None, 
                               hotwords: Optional[List[str]] = None) -> str:
        """
        Process subtitle content, remove sequence numbers and timelines.
        
        Args:
            content: Subtitle content
            is_funasr: Whether it's FunASR converted subtitle
            translate: Whether translation is needed
            language: Video language
            hotwords: Hot words list for logging and debugging
            
        Returns:
            Processed subtitle content
        """
        try:
            if not content:
                logger.error("Input content is empty")
                return ""
                
            logger.info(f"Starting subtitle content processing [length: {len(content)} characters]")
            if is_funasr:
                logger.info("Using FunASR mode to process subtitles")
                if hotwords:
                    logger.info(f"Using hot words: {hotwords}")
            
            # Remove WEBVTT header
            content = re.sub(r'^WEBVTT\s*\n', '', content)
            
            # Split into lines
            lines = []
            current_text = []
            
            for line in content.split('\n'):
                line = line.strip()
                
                # Skip sequence number lines (pure numbers)
                if re.match(r'^\d+$', line):
                    continue
                    
                # Skip timeline lines
                if re.match(r'^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}', line):
                    continue
                    
                # Skip empty lines
                if not line:
                    if current_text:
                        if is_funasr:
                            # FunASR converted subtitles: merge all text, don't preserve line breaks
                            lines.append(' '.join(current_text))
                        else:
                            # Directly extracted subtitles: preserve original line breaks
                            lines.append('\n'.join(current_text))
                        current_text = []
                    continue
                    
                current_text.append(line)
                
            # Handle last text segment
            if current_text:
                if is_funasr:
                    lines.append(' '.join(current_text))
                else:
                    lines.append('\n'.join(current_text))
            
            # Merge processed text
            if is_funasr:
                # FunASR converted subtitles: connect all paragraphs with spaces
                result = ' '.join(lines)
            else:
                # Directly extracted subtitles: separate paragraphs with two line breaks
                result = '\n\n'.join(lines)
                
            logger.info(f"Subtitle processing completed:")
            logger.info(f"- Removed {len(lines)} sequence markers")
            logger.info(f"- Removed {len(lines)} timelines")
            logger.info(f"- Processed text length: {len(result)} characters")
            logger.debug(f"Processed content preview: {result[:200]}...")
            
            # Only translate when video is English and translation is needed
            if translate and language == 'en':
                logger.info("Detected English video, starting translation...")
                # Note: Translation functionality would need to be implemented
                # translated = self.translate_text(result)
                # if translated != result:
                #     result = f"{result}\n\n{translated}"
            
            return result
                
        except Exception as e:
            logger.error(f"Error processing subtitle content: {str(e)}")
            logger.error(f"Error input content: {content}")
            raise