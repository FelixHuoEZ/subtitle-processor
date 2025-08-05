"""Audio transcription service using FunASR for subtitle generation."""

import os
import json
import logging
import math
import subprocess
import requests
from typing import Dict, Any, Optional, List
from ..config.config_manager import get_config_value
from .hotword_service import HotwordService

logger = logging.getLogger(__name__)


class TranscriptionService:
    """音频转录服务 - 使用FunASR进行音频转录"""
    
    def __init__(self):
        """初始化转录服务"""
        self.funasr_server = get_config_value('servers.transcribe.default_url', 'http://transcribe-audio:10095')
        self.funasr_servers = self._load_transcribe_servers()
        self.openai_api_key = get_config_value('tokens.openai.api_key', '')
        self.openai_base_url = get_config_value('tokens.openai.base_url', 'https://api.openai.com/v1')
        self.hotword_service = HotwordService()
        self.default_hotwords = self.hotword_service.get_default_hotwords()
    
    
    def _load_transcribe_servers(self) -> List[Dict[str, Any]]:
        """加载转录服务器列表"""
        try:
            servers_config = get_config_value('servers.transcribe.servers', [])
            if not servers_config:
                # 使用默认服务器
                return [{'url': self.funasr_server, 'status': 'unknown'}]
            
            servers = []
            for server_config in servers_config:
                if isinstance(server_config, str):
                    servers.append({'url': server_config, 'status': 'unknown'})
                elif isinstance(server_config, dict):
                    servers.append({
                        'url': server_config.get('url', ''),
                        'status': 'unknown',
                        'priority': server_config.get('priority', 999),
                        'weight': server_config.get('weight', 1),
                        'name': server_config.get('name', 'Unknown')
                    })
            
            logger.info(f"加载了 {len(servers)} 个转录服务器")
            return servers
            
        except Exception as e:
            logger.error(f"加载转录服务器列表失败: {str(e)}")
            return [{'url': self.funasr_server, 'status': 'unknown'}]
    
    def _get_available_transcribe_server(self) -> Optional[str]:
        """获取可用的转录服务器"""
        try:
            # 检查所有服务器状态
            available_servers = []
            
            for server in self.funasr_servers:
                url = server['url']
                try:
                    health_url = f"{url.rstrip('/')}/health"
                    response = requests.get(health_url, timeout=5)
                    if response.status_code == 200:
                        server['status'] = 'healthy'
                        available_servers.append(server)
                        logger.debug(f"转录服务器可用: {url}")
                    else:
                        server['status'] = 'unhealthy'
                        logger.warning(f"转录服务器不可用: {url}")
                except Exception as e:
                    server['status'] = 'error'
                    logger.debug(f"转录服务器检查失败 {url}: {str(e)}")
            
            if not available_servers:
                logger.error("没有可用的转录服务器")
                return None
            
            # 按优先级选择服务器（优先级数字越小越优先）
            if any('priority' in server for server in available_servers):
                # 按优先级排序，选择优先级最高的（数字最小的）
                available_servers.sort(key=lambda x: x.get('priority', 999))
                selected_server = available_servers[0]
                logger.debug(f"按优先级选择服务器，优先级: {selected_server.get('priority', 'N/A')}")
            elif any('weight' in server for server in available_servers):
                import random
                weights = [server.get('weight', 1) for server in available_servers]
                selected_server = random.choices(available_servers, weights=weights)[0]
                logger.debug("按权重随机选择服务器")
            else:
                # 选择第一个可用服务器
                selected_server = available_servers[0]
                logger.debug("选择第一个可用服务器")
            
            logger.info(f"选择转录服务器: {selected_server['url']}")
            return selected_server['url']
            
        except Exception as e:
            logger.error(f"获取可用转录服务器失败: {str(e)}")
            return self.funasr_server  # 返回默认服务器
    
    def transcribe_audio(self, 
                        audio_file: str, 
                        hotwords: Optional[List[str]] = None,
                        video_info: Optional[Dict[str, Any]] = None,
                        tags: Optional[List[str]] = None,
                        platform: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """转录音频文件
        
        Args:
            audio_file: 音频文件路径
            hotwords: 用户指定的热词列表，提高识别准确率
            video_info: 视频信息字典，包含标题、频道等
            tags: 用户标签列表
            platform: 视频平台名称
            
        Returns:
            dict: 转录结果，包含文本和时间戳信息
        """
        try:
            logger.info(f"开始转录音频文件: {audio_file}")
            
            if not os.path.exists(audio_file):
                logger.error(f"音频文件不存在: {audio_file}")
                return None
            
            # 智能生成热词
            if hotwords:
                # 如果用户指定了热词，优先使用
                final_hotwords = hotwords
                logger.info(f"使用用户指定热词: {final_hotwords}")
            else:
                # 基于视频信息智能生成热词
                title = video_info.get('title') if video_info else None
                channel_name = video_info.get('uploader') if video_info else None
                
                generated_hotwords = self.hotword_service.generate_hotwords(
                    title=title,
                    tags=tags,
                    channel_name=channel_name,
                    platform=platform
                )
                
                # 合并生成的热词和默认热词
                final_hotwords = generated_hotwords + self.default_hotwords
                # 去重并限制数量
                final_hotwords = list(dict.fromkeys(final_hotwords))[:20]
                
                logger.info(f"智能生成热词 ({len(generated_hotwords)} 个): {generated_hotwords}")
                logger.info(f"最终使用热词 ({len(final_hotwords)} 个): {final_hotwords}")
            
            # 【关键日志】记录最终使用的热词
            logger.warning(f"🔥 TranscriptionService最终使用热词 ({len(final_hotwords)}个): {final_hotwords}")
            
            # 首先尝试FunASR转录
            result = self._transcribe_with_funasr(audio_file, final_hotwords)
            if result:
                logger.info("FunASR转录成功")
                return result
            
            # 如果FunASR失败，尝试OpenAI Whisper
            logger.warning("FunASR转录失败，尝试OpenAI Whisper")
            return self._transcribe_with_openai(audio_file)
            
        except Exception as e:
            logger.error(f"转录音频失败: {str(e)}")
            return None
    
    def _transcribe_with_funasr(self, audio_file: str, hotwords: List[str]) -> Optional[Dict[str, Any]]:
        """使用FunASR转录音频"""
        try:
            # 获取可用的转录服务器
            server_url = self._get_available_transcribe_server()
            if not server_url:
                logger.warning("没有可用的FunASR服务器")
                return None
            
            # 检查音频文件是否需要分割
            audio_segments = self.split_audio(audio_file)
            
            if len(audio_segments) == 1:
                # 单个文件直接转录
                return self._transcribe_single_file(audio_segments[0], hotwords, server_url)
            else:
                # 多个片段分别转录并合并结果
                logger.info(f"音频已分割为 {len(audio_segments)} 个片段，开始逐个转录")
                return self._transcribe_multiple_segments(audio_segments, hotwords, server_url)
                
        except Exception as e:
            logger.error(f"FunASR转录出错: {str(e)}")
            return None
    
    def _transcribe_single_file(self, audio_file: str, hotwords: List[str], server_url: str) -> Optional[Dict[str, Any]]:
        """转录单个音频文件"""
        try:
            # 准备文件和参数
            with open(audio_file, 'rb') as f:
                files = {'audio': f}
                hotword_str = ','.join(hotwords) if hotwords else ''
                data = {
                    'hotwords': hotword_str,
                }
                
                # 【关键日志】记录发送给FunASR的热词
                if hotwords:
                    logger.warning(f"🔥 发送给FunASR的热词 ({len(hotwords)}个): {hotwords}")
                    logger.warning(f"🔥 热词字符串格式: '{hotword_str}'")
                else:
                    logger.warning("🔥 没有热词发送给FunASR")
                
                # 发送转录请求
                url = f"{server_url.rstrip('/')}/recognize"
                logger.warning(f"🔥 发送FunASR请求到: {url}")
                response = requests.post(url, files=files, data=data, timeout=300)
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"FunASR响应状态: 200")
                logger.info(f"FunASR响应类型: {type(result)}")
                logger.info(f"FunASR响应内容: {str(result)}")
                
                # 解析结果
                return self._parse_funasr_result(result, audio_file)
            else:
                logger.error(f"FunASR转录失败，状态码: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"FunASR单文件转录出错: {str(e)}")
            return None
    
    def _transcribe_multiple_segments(self, audio_segments: List[str], hotwords: List[str], server_url: str) -> Optional[Dict[str, Any]]:
        """转录多个音频片段并合并结果"""
        try:
            all_results = []
            total_duration = 0
            
            for i, segment_path in enumerate(audio_segments, 1):
                logger.info(f"转录音频片段 {i}/{len(audio_segments)}: {segment_path}")
                
                result = self._transcribe_single_file(segment_path, hotwords, server_url)
                if result:
                    all_results.append(result)
                    # 累计时长
                    if 'audio_info' in result and 'duration_seconds' in result['audio_info']:
                        total_duration += result['audio_info']['duration_seconds']
                else:
                    logger.warning(f"音频片段转录失败: {segment_path}")
            
            if not all_results:
                logger.error("所有音频片段转录都失败了")
                return None
            
            # 合并转录结果
            merged_text = ""
            all_timestamps = []
            
            current_offset = 0
            for result in all_results:
                text = result.get('text', '')
                timestamps = result.get('timestamp', [])
                
                # 添加文本
                if merged_text and not merged_text.endswith((' ', '\n')):
                    merged_text += " "
                merged_text += text
                
                # 调整时间戳偏移
                if timestamps:
                    adjusted_timestamps = []
                    for ts in timestamps:
                        if isinstance(ts, list) and len(ts) >= 3:
                            # [start_time, end_time, text]
                            adjusted_ts = [ts[0] + current_offset, ts[1] + current_offset, ts[2]]
                            adjusted_timestamps.append(adjusted_ts)
                    all_timestamps.extend(adjusted_timestamps)
                
                # 更新偏移量
                if 'audio_info' in result and 'duration_seconds' in result['audio_info']:
                    current_offset += result['audio_info']['duration_seconds']
            
            # 构造合并后的结果
            merged_result = {
                'text': merged_text,
                'audio_info': {
                    'duration_seconds': total_duration,
                    'file_size': sum(os.path.getsize(seg) for seg in audio_segments if os.path.exists(seg)),
                    'segments_count': len(audio_segments)
                },
                'timestamp': all_timestamps,
                'source': 'funasr_segments'
            }
            
            # 清理临时音频片段（除了原始文件）
            original_file = audio_segments[0] if len(audio_segments) == 1 else None
            for segment_path in audio_segments:
                if segment_path != original_file and os.path.exists(segment_path):
                    try:
                        os.remove(segment_path)
                        logger.debug(f"清理临时音频片段: {segment_path}")
                    except Exception as e:
                        logger.warning(f"清理临时文件失败 {segment_path}: {str(e)}")
            
            logger.info(f"音频片段转录完成，合并了 {len(all_results)} 个结果")
            return merged_result
            
        except Exception as e:
            logger.error(f"多片段转录失败: {str(e)}")
            return None
    
    def _transcribe_with_openai(self, audio_file: str) -> Optional[Dict[str, Any]]:
        """使用OpenAI Whisper转录音频"""
        try:
            if not self.openai_api_key:
                logger.warning("OpenAI API密钥未配置")
                return None
            
            import openai
            
            # 配置OpenAI客户端
            client = openai.OpenAI(
                api_key=self.openai_api_key,
                base_url=self.openai_base_url
            )
            
            # 转录音频
            with open(audio_file, 'rb') as audio:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio,
                    response_format="verbose_json",
                    timestamp_granularity=["word"]
                )
            
            # 构造返回结果
            result = {
                'text': transcript.text,
                'audio_info': {
                    'duration_seconds': transcript.duration if hasattr(transcript, 'duration') else None
                },
                'segments': getattr(transcript, 'words', []),
                'source': 'openai_whisper'
            }
            
            logger.info("OpenAI Whisper转录成功")
            return result
            
        except Exception as e:
            logger.error(f"OpenAI Whisper转录失败: {str(e)}")
            return None
    
    def _check_funasr_service(self) -> bool:
        """检查FunASR服务是否可用"""
        try:
            health_url = f"{self.funasr_server}/health"
            response = requests.get(health_url, timeout=10)
            return response.status_code == 200
        except Exception as e:
            logger.debug(f"FunASR服务检查失败: {str(e)}")
            return False
    
    def _parse_funasr_result(self, result: Dict[str, Any], audio_file: str) -> Dict[str, Any]:
        """解析FunASR转录结果"""
        try:
            # 获取音频信息
            audio_info = self._get_audio_info(audio_file)
            
            # 解析文本内容
            text_content = ""
            timestamp_info = None
            
            # 首先检查是否直接有text字段（新格式）
            if 'text' in result:
                text_content = result.get('text', '')
                timestamp_info = result.get('timestamp', [])
                logger.info(f"从直接text字段获取文本: {text_content[:100]}...")
            elif 'result' in result:
                # 标准FunASR结果格式（旧格式）
                asr_result = result['result']
                
                if isinstance(asr_result, dict):
                    text_content = asr_result.get('text', '')
                    timestamp_info = asr_result.get('timestamp', [])
                elif isinstance(asr_result, str):
                    text_content = asr_result
                elif isinstance(asr_result, list) and asr_result:
                    # 如果是列表，取第一个元素
                    first_result = asr_result[0]
                    if isinstance(first_result, dict):
                        text_content = first_result.get('text', '')
                        timestamp_info = first_result.get('timestamp', [])
                    else:
                        text_content = str(first_result)
                logger.info(f"从result字段获取文本: {text_content[:100]}...")
            else:
                logger.warning(f"未找到text或result字段，可用字段: {list(result.keys())}")
            
            # 构造标准化结果
            parsed_result = {
                'text': text_content,
                'audio_info': audio_info,
                'timestamp': timestamp_info,
                'source': 'funasr'
            }
            
            logger.debug(f"解析后的结果: {parsed_result}")
            return parsed_result
            
        except Exception as e:
            logger.error(f"解析FunASR结果失败: {str(e)}")
            return None
    
    def _get_audio_info(self, audio_file: str) -> Dict[str, Any]:
        """获取音频文件信息"""
        try:
            import subprocess
            
            # 使用ffprobe获取音频信息
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', '-show_streams', audio_file
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                info = json.loads(result.stdout)
                format_info = info.get('format', {})
                
                return {
                    'duration_seconds': float(format_info.get('duration', 0)),
                    'file_size': int(format_info.get('size', 0)),
                    'format_name': format_info.get('format_name', ''),
                    'bit_rate': int(format_info.get('bit_rate', 0))
                }
            else:
                logger.warning(f"ffprobe获取音频信息失败: {result.stderr}")
                
        except Exception as e:
            logger.warning(f"获取音频信息失败: {str(e)}")
        
        # 返回默认信息
        try:
            file_size = os.path.getsize(audio_file)
            return {
                'duration_seconds': None,
                'file_size': file_size,
                'format_name': 'unknown',
                'bit_rate': 0
            }
        except:
            return {
                'duration_seconds': None,
                'file_size': 0,
                'format_name': 'unknown',
                'bit_rate': 0
            }
    
    def batch_transcribe(self, audio_files: List[str], hotwords: Optional[List[str]] = None) -> Dict[str, Any]:
        """批量转录音频文件
        
        Args:
            audio_files: 音频文件路径列表
            hotwords: 热词列表
            
        Returns:
            dict: 批量转录结果
        """
        try:
            logger.info(f"开始批量转录 {len(audio_files)} 个音频文件")
            
            results = {}
            successful = 0
            failed = 0
            
            for i, audio_file in enumerate(audio_files, 1):
                logger.info(f"转录进度: {i}/{len(audio_files)} - {audio_file}")
                
                result = self.transcribe_audio(audio_file, hotwords)
                if result:
                    results[audio_file] = result
                    successful += 1
                    logger.info(f"转录成功: {audio_file}")
                else:
                    results[audio_file] = None
                    failed += 1
                    logger.error(f"转录失败: {audio_file}")
            
            summary = {
                'total': len(audio_files),
                'successful': successful,
                'failed': failed,
                'results': results
            }
            
            logger.info(f"批量转录完成 - 成功: {successful}, 失败: {failed}")
            return summary
            
        except Exception as e:
            logger.error(f"批量转录失败: {str(e)}")
            return {'total': 0, 'successful': 0, 'failed': 0, 'results': {}}
    
    def get_supported_formats(self) -> List[str]:
        """获取支持的音频格式列表"""
        return ['.wav', '.mp3', '.m4a', '.flac', '.aac', '.ogg', '.wma']
    
    def validate_audio_file(self, audio_file: str) -> bool:
        """验证音频文件是否有效"""
        try:
            if not os.path.exists(audio_file):
                logger.error(f"音频文件不存在: {audio_file}")
                return False
            
            # 检查文件扩展名
            _, ext = os.path.splitext(audio_file.lower())
            if ext not in self.get_supported_formats():
                logger.error(f"不支持的音频格式: {ext}")
                return False
            
            # 检查文件大小（限制为500MB）
            file_size = os.path.getsize(audio_file)
            max_size = 500 * 1024 * 1024  # 500MB
            if file_size > max_size:
                logger.error(f"音频文件过大: {file_size / 1024 / 1024:.2f}MB")
                return False
            
            logger.debug(f"音频文件验证通过: {audio_file}")
            return True
            
        except Exception as e:
            logger.error(f"验证音频文件失败: {str(e)}")
            return False
    
    def split_audio(self, audio_path: str, max_duration: int = 600, max_size: int = 100*1024*1024) -> List[str]:
        """分割大音频文件
        
        Args:
            audio_path: 音频文件路径
            max_duration: 最大时长（秒），默认600秒（10分钟）
            max_size: 最大文件大小（字节），默认100MB
            
        Returns:
            list: 分割后的音频片段路径列表
        """
        try:
            logger.info(f"开始检查音频文件是否需要分割: {audio_path}")
            
            # 获取音频信息
            audio_info = self._get_audio_info(audio_path)
            duration = audio_info.get('duration_seconds', 0)
            file_size = audio_info.get('file_size', 0)
            
            # 检查是否需要分割
            if duration <= max_duration and file_size <= max_size:
                logger.info("音频文件无需分割")
                return [audio_path]
            
            logger.info(f"音频文件需要分割 - 时长: {duration}s, 大小: {file_size / 1024 / 1024:.2f}MB")
            
            # 计算分割段数
            duration_segments = math.ceil(duration / max_duration) if duration > 0 else 1
            size_segments = math.ceil(file_size / max_size) if file_size > 0 else 1
            total_segments = max(duration_segments, size_segments)
            
            if total_segments <= 1:
                return [audio_path]
            
            # 计算每段时长
            segment_duration = duration / total_segments
            
            # 创建输出目录
            output_dir = os.path.dirname(audio_path)
            base_name = os.path.splitext(os.path.basename(audio_path))[0]
            
            # 分割音频
            segment_paths = []
            
            try:
                from pydub import AudioSegment
                
                # 加载音频文件
                logger.info("使用pydub分割音频文件")
                audio = AudioSegment.from_file(audio_path)
                
                for i in range(total_segments):
                    start_time = i * segment_duration * 1000  # pydub使用毫秒
                    end_time = min((i + 1) * segment_duration * 1000, len(audio))
                    
                    # 提取片段
                    segment = audio[start_time:end_time]
                    
                    # 保存片段
                    segment_path = os.path.join(output_dir, f"{base_name}_part_{i+1:03d}.wav")
                    segment.export(segment_path, format="wav")
                    segment_paths.append(segment_path)
                    
                    logger.info(f"创建音频片段 {i+1}/{total_segments}: {segment_path}")
                
            except ImportError:
                # 如果pydub不可用，使用ffmpeg
                logger.info("pydub不可用，使用ffmpeg分割音频文件")
                
                for i in range(total_segments):
                    start_time = i * segment_duration
                    
                    segment_path = os.path.join(output_dir, f"{base_name}_part_{i+1:03d}.wav")
                    
                    # 使用ffmpeg分割
                    cmd = [
                        'ffmpeg', '-i', audio_path,
                        '-ss', str(start_time),
                        '-t', str(segment_duration),
                        '-acodec', 'pcm_s16le',
                        '-ar', '16000', '-ac', '1',
                        segment_path, '-y'
                    ]
                    
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    if result.returncode == 0:
                        segment_paths.append(segment_path)
                        logger.info(f"创建音频片段 {i+1}/{total_segments}: {segment_path}")
                    else:
                        logger.error(f"ffmpeg分割失败: {result.stderr}")
                        # 清理已创建的片段
                        for path in segment_paths:
                            if os.path.exists(path):
                                os.remove(path)
                        return [audio_path]  # 返回原文件
            
            logger.info(f"音频分割完成，共创建 {len(segment_paths)} 个片段")
            return segment_paths
            
        except Exception as e:
            logger.error(f"分割音频文件失败: {str(e)}")
            return [audio_path]  # 出错时返回原文件