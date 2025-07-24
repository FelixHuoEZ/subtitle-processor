"""Transcription service for audio processing using FunASR."""

import os
import json
import logging
import requests
import time
import tempfile
from typing import Optional, Dict, List, Any
import wave

try:
    from ..config.config_manager import get_config_value
except ImportError:
    from config.config_manager import get_config_value

logger = logging.getLogger(__name__)


class TranscriptionService:
    """Service for audio transcription using FunASR servers."""
    
    def __init__(self):
        self.default_url = get_config_value('servers.transcribe.default_url', 'http://localhost:9000/asr')
        self.timeout = get_config_value('servers.transcribe.timeout', 30)
        self.servers = self.load_transcribe_servers()
    
    def load_transcribe_servers(self) -> List[Dict[str, Any]]:
        """
        Load transcription server configuration.
        
        Returns:
            List of server configurations
        """
        try:
            servers = get_config_value('servers.transcribe.servers', [])
            if not servers:
                # Fallback to default server
                servers = [{
                    'name': 'default',
                    'url': self.default_url,
                    'priority': 1,
                    'description': 'Default transcription server'
                }]
            
            # Sort by priority
            servers.sort(key=lambda x: x.get('priority', 999))
            logger.info(f"Loaded {len(servers)} transcription servers")
            
            return servers
            
        except Exception as e:
            logger.error(f"Error loading transcription servers: {str(e)}")
            return [{
                'name': 'default',
                'url': self.default_url,
                'priority': 1,
                'description': 'Default transcription server'
            }]
    
    def get_available_transcribe_server(self) -> Optional[Dict[str, Any]]:
        """
        Get an available transcription server.
        
        Returns:
            Server configuration or None if none available
        """
        for server in self.servers:
            try:
                url = server['url']
                # Try to ping the server with a health check
                health_url = url.replace('/asr', '/health') if '/asr' in url else f"{url}/health"
                
                response = requests.get(health_url, timeout=5)
                if response.status_code == 200:
                    logger.info(f"Found available transcription server: {server['name']}")
                    return server
                    
            except requests.RequestException:
                logger.debug(f"Server {server['name']} not available")
                continue
        
        logger.warning("No available transcription servers found")
        return None
    
    def transcribe_audio(self, audio_path: str, hotwords: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """
        Transcribe audio file using FunASR.
        
        Args:
            audio_path: Path to audio file
            hotwords: Optional hot words for better recognition
            
        Returns:
            Transcription result or None if failed
        """
        try:
            if not os.path.exists(audio_path):
                logger.error(f"Audio file not found: {audio_path}")
                return None
            
            # Check audio file
            file_size = os.path.getsize(audio_path)
            logger.info(f"Transcribing audio file: {audio_path} ({file_size / (1024*1024):.2f} MB)")
            
            # Check if file is too small (less than 1KB suggests empty or invalid file)
            if file_size < 1024:
                logger.error(f"Audio file too small ({file_size} bytes), likely invalid")
                return None
                
            # Validate audio file format
            try:
                with wave.open(audio_path, 'rb') as test_wav:
                    frames = test_wav.getnframes()
                    sample_rate = test_wav.getframerate()
                    duration = frames / float(sample_rate)
                    logger.info(f"Audio validation: {duration:.2f}s duration, {sample_rate}Hz sample rate, {frames} frames")
                    
                    if duration < 0.1:
                        logger.warning(f"Audio duration too short: {duration:.2f}s")
                        return None
            except Exception as e:
                logger.error(f"Audio file validation failed: {str(e)}")
                return None
            
            if hotwords:
                logger.info(f"Using hotwords: {hotwords}")
            
            # Get available server
            server = self.get_available_transcribe_server()
            if not server:
                logger.error("No available transcription servers")
                return None
            
            transcribe_url = server['url']
            if not transcribe_url.endswith('/recognize'):
                if '/asr' in transcribe_url:
                    transcribe_url = transcribe_url.replace('/asr', '/recognize')
                else:
                    transcribe_url = f"{transcribe_url}/recognize"
            
            logger.info(f"Using transcription server: {server['name']} ({transcribe_url})")
            
            # Prepare files
            files = {'audio': open(audio_path, 'rb')}
            
            # Add hotwords if provided
            data = {}
            if hotwords:
                data['hotwords'] = json.dumps(hotwords)
            
            try:
                # Make transcription request with better error handling
                logger.info(f"Sending transcription request to {transcribe_url} with timeout {self.timeout}s")
                response = requests.post(
                    transcribe_url,
                    files=files,
                    data=data,
                    timeout=self.timeout
                )
                
                files['audio'].close()
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info("Transcription completed successfully")
                    logger.debug(f"Transcription result: {result}")
                    
                    # Add audio info if not present
                    if 'audio_info' not in result:
                        try:
                            with wave.open(audio_path, 'rb') as wav_file:
                                frames = wav_file.getnframes()
                                sample_rate = wav_file.getframerate()
                                duration = frames / float(sample_rate)
                                
                                result['audio_info'] = {
                                    'duration_seconds': duration,
                                    'sample_rate': sample_rate,
                                    'frames': frames
                                }
                                logger.debug(f"Added audio info: duration={duration:.2f}s")
                        except Exception as e:
                            logger.warning(f"Could not get audio info: {str(e)}")
                    
                    return result
                elif response.status_code == 400:
                    error_text = response.text
                    logger.error(f"Transcription failed - Bad Request (400): {error_text}")
                    if "silent" in error_text.lower() or "empty" in error_text.lower():
                        logger.warning("Audio appears to be silent or empty, returning empty result")
                        return {"text": "", "status": "silent_audio", "audio_info": {"duration_seconds": 0}}
                    return None
                else:
                    logger.error(f"Transcription failed with status {response.status_code}: {response.text}")
                    return None
                    
            except requests.exceptions.Timeout:
                logger.error(f"Transcription request timed out after {self.timeout} seconds")
                return None
            except requests.exceptions.ConnectionError as e:
                logger.error(f"Connection error during transcription: {str(e)}")
                return None
            except requests.RequestException as e:
                logger.error(f"Transcription request failed: {str(e)}")
                return None
            finally:
                if not files['audio'].closed:
                    files['audio'].close()
            
        except Exception as e:
            logger.error(f"Error during transcription: {str(e)}")
            return None
    
    def transcribe_audio_segments(self, audio_segments: List[str], 
                                hotwords: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Transcribe multiple audio segments.
        
        Args:
            audio_segments: List of audio file paths
            hotwords: Optional hot words for better recognition
            
        Returns:
            List of transcription results
        """
        results = []
        
        for i, segment_path in enumerate(audio_segments, 1):
            logger.info(f"Transcribing segment {i}/{len(audio_segments)}: {segment_path}")
            
            result = self.transcribe_audio(segment_path, hotwords)
            if result:
                # Add segment information
                result['segment_index'] = i
                result['segment_path'] = segment_path
                results.append(result)
            else:
                logger.warning(f"Failed to transcribe segment {i}")
        
        logger.info(f"Transcribed {len(results)}/{len(audio_segments)} segments successfully")
        return results
    
    def merge_transcription_results(self, results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Merge multiple transcription results into one.
        
        Args:
            results: List of transcription results
            
        Returns:
            Merged transcription result
        """
        try:
            if not results:
                logger.error("No results to merge")
                return None
            
            if len(results) == 1:
                logger.info("Only one result, no merging needed")
                return results[0]
            
            logger.info(f"Merging {len(results)} transcription results")
            
            # Combine text content
            combined_text = []
            combined_timestamps = []
            total_duration = 0
            
            for result in results:
                text = result.get('text', '')
                if text:
                    combined_text.append(text)
                
                # Handle timestamps if present
                timestamps = result.get('timestamp', [])
                if timestamps and isinstance(timestamps, list):
                    # Adjust timestamps by adding offset
                    offset_ms = total_duration * 1000
                    adjusted_timestamps = []
                    for start, end in timestamps:
                        adjusted_timestamps.append([start + offset_ms, end + offset_ms])
                    combined_timestamps.extend(adjusted_timestamps)
                
                # Add duration
                audio_info = result.get('audio_info', {})
                segment_duration = audio_info.get('duration_seconds', 0)
                total_duration += segment_duration
            
            # Create merged result
            merged_result = {
                'text': ' '.join(combined_text),
                'status': 'success',
                'audio_info': {
                    'duration_seconds': total_duration,
                    'segments_count': len(results)
                }
            }
            
            if combined_timestamps:
                merged_result['timestamp'] = combined_timestamps
            
            logger.info(f"Merged transcription: {len(merged_result['text'])} characters, {total_duration:.2f}s")
            return merged_result
            
        except Exception as e:
            logger.error(f"Error merging transcription results: {str(e)}")
            return None
    
    def check_server_health(self, server_url: str) -> bool:
        """
        Check if a transcription server is healthy.
        
        Args:
            server_url: Server URL
            
        Returns:
            True if server is healthy
        """
        try:
            health_url = server_url.replace('/asr', '/health') if '/asr' in server_url else f"{server_url}/health"
            response = requests.get(health_url, timeout=5)
            return response.status_code == 200
        except:
            return False
    
    def get_server_status(self) -> Dict[str, bool]:
        """
        Get status of all configured transcription servers.
        
        Returns:
            Dictionary mapping server names to their health status
        """
        status = {}
        for server in self.servers:
            name = server['name']
            url = server['url']
            status[name] = self.check_server_health(url)
        
        return status
    
    def get_transcription_progress(self) -> Optional[Dict[str, Any]]:
        """
        Get current transcription progress from the active server.
        
        Returns:
            Progress information or None if not available
        """
        try:
            server = self.get_available_transcribe_server()
            if not server:
                return None
            
            progress_url = server['url'].replace('/asr', '/progress').replace('/recognize', '/progress')
            if not progress_url.endswith('/progress'):
                if '/asr' in progress_url or '/recognize' in progress_url:
                    progress_url = '/'.join(progress_url.split('/')[:-1]) + '/progress'
                else:
                    progress_url = f"{progress_url}/progress"
            
            response = requests.get(progress_url, timeout=5)
            if response.status_code == 200:
                return response.json()
            
            return None
        except Exception as e:
            logger.debug(f"Could not get transcription progress: {str(e)}")
            return None