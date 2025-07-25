"""Readwise Reader integration service for article creation and management."""

import json
import logging
import requests
from datetime import datetime
from typing import Dict, Any, Optional, List
from ..config.config_manager import get_config_value

logger = logging.getLogger(__name__)


class ReadwiseService:
    """Readwise Reader集成服务 - 用于创建和管理文章"""
    
    def __init__(self):
        """初始化Readwise服务"""
        self.api_token = get_config_value('tokens.readwise.api_token', '')
        self.base_url = 'https://readwise.io/api/v3'
        self.enabled = bool(self.api_token)
        
        if not self.enabled:
            logger.info("Readwise API token未配置，服务将不可用")
    
    def create_article(self, title: str, content: str, url: str = None, 
                      summary: str = None, tags: List[str] = None) -> Optional[Dict[str, Any]]:
        """创建Readwise文章
        
        Args:
            title: 文章标题
            content: 文章内容
            url: 原始URL（可选）
            summary: 文章摘要（可选）
            tags: 标签列表（可选）
            
        Returns:
            dict: 创建结果，包含文章ID等信息
        """
        try:
            if not self.enabled:
                logger.warning("Readwise服务未启用")
                return None
            
            logger.info(f"创建Readwise文章: {title}")
            
            # 构造文章数据
            article_data = {
                'title': title,
                'content': content,
                'source': 'subtitle_processor',
                'created_at': datetime.now().isoformat(),
            }
            
            # 添加可选字段
            if url:
                article_data['url'] = url
                article_data['source_url'] = url
            
            if summary:
                article_data['summary'] = summary
            
            if tags:
                article_data['tags'] = tags
            
            # 发送创建请求
            response = self._make_request('POST', '/documents/', data=article_data)
            
            if response and response.get('id'):
                logger.info(f"Readwise文章创建成功，ID: {response['id']}")
                return response
            else:
                logger.error("Readwise文章创建失败")
                return None
                
        except Exception as e:
            logger.error(f"创建Readwise文章失败: {str(e)}")
            return None
    
    def create_article_from_subtitle(self, subtitle_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """从字幕数据创建Readwise文章
        
        Args:
            subtitle_data: 字幕数据，包含视频信息和字幕内容
            
        Returns:
            dict: 创建结果
        """
        try:
            if not self.enabled:
                return None
            
            video_info = subtitle_data.get('video_info', {})
            subtitle_content = subtitle_data.get('subtitle_content', '')
            
            if not video_info or not subtitle_content:
                logger.error("字幕数据不完整")
                return None
            
            # 构造文章标题
            title = video_info.get('title', '未知视频标题')
            
            # 构造文章内容
            content = self._format_subtitle_content(video_info, subtitle_content)
            
            # 构造URL
            url = video_info.get('webpage_url') or video_info.get('url')
            
            # 生成摘要
            summary = self._generate_summary(video_info, subtitle_content)
            
            # 生成标签
            tags = self._generate_tags(video_info)
            
            return self.create_article(
                title=title,
                content=content,
                url=url,
                summary=summary,
                tags=tags
            )
            
        except Exception as e:
            logger.error(f"从字幕创建Readwise文章失败: {str(e)}")
            return None
    
    def _format_subtitle_content(self, video_info: Dict[str, Any], subtitle_content: str) -> str:
        """格式化字幕内容为文章格式"""
        try:
            # 获取视频基本信息
            title = video_info.get('title', '未知视频')
            uploader = video_info.get('uploader', '未知作者')
            duration = video_info.get('duration', 0)
            upload_date = video_info.get('upload_date', '')
            description = video_info.get('description', '')
            url = video_info.get('webpage_url', '')
            
            # 格式化时长
            duration_str = self._format_duration(duration) if duration else '未知'
            
            # 格式化日期
            date_str = self._format_date(upload_date) if upload_date else '未知'
            
            # 构造文章内容
            content_parts = [
                f"# {title}",
                "",
                "## 视频信息",
                f"- **作者**: {uploader}",
                f"- **时长**: {duration_str}",
                f"- **发布日期**: {date_str}",
            ]
            
            if url:
                content_parts.append(f"- **原始链接**: {url}")
            
            content_parts.extend(["", "---", ""])
            
            # 添加视频描述（如果有且不太长）
            if description and len(description) < 500:
                content_parts.extend([
                    "## 视频描述",
                    description,
                    "",
                    "---",
                    ""
                ])
            
            # 添加字幕内容
            content_parts.extend([
                "## 字幕内容",
                "",
                self._clean_subtitle_for_readwise(subtitle_content)
            ])
            
            return "\\n".join(content_parts)
            
        except Exception as e:
            logger.error(f"格式化字幕内容失败: {str(e)}")
            return subtitle_content
    
    def _clean_subtitle_for_readwise(self, subtitle_content: str) -> str:
        """清理字幕内容，使其适合Readwise显示"""
        try:
            import re
            
            # 如果是SRT格式，提取纯文本
            if self._is_srt_format(subtitle_content):
                # 移除序号和时间戳，只保留文本
                lines = subtitle_content.split('\\n')
                text_lines = []
                
                for line in lines:
                    line = line.strip()
                    # 跳过序号行
                    if line.isdigit():
                        continue
                    # 跳过时间戳行
                    if re.match(r'\\d{2}:\\d{2}:\\d{2},\\d{3}\\s*-->\\s*\\d{2}:\\d{2}:\\d{2},\\d{3}', line):
                        continue
                    # 跳过空行
                    if not line:
                        continue
                    
                    text_lines.append(line)
                
                # 合并文本并添加段落分隔
                cleaned_text = ' '.join(text_lines)
                
                # 按句号分段，提高可读性
                sentences = re.split(r'[。！？.!?]', cleaned_text)
                paragraphs = []
                current_paragraph = []
                
                for sentence in sentences:
                    sentence = sentence.strip()
                    if sentence:
                        current_paragraph.append(sentence)
                        # 每3-5句为一段
                        if len(current_paragraph) >= 4:
                            paragraphs.append('。'.join(current_paragraph) + '。')
                            current_paragraph = []
                
                # 添加最后一段
                if current_paragraph:
                    paragraphs.append('。'.join(current_paragraph) + '。')
                
                return '\\n\\n'.join(paragraphs)
            else:
                # 纯文本，直接返回
                return subtitle_content
                
        except Exception as e:
            logger.error(f"清理字幕内容失败: {str(e)}")
            return subtitle_content
    
    def _is_srt_format(self, content: str) -> bool:
        """检测是否为SRT格式"""
        import re
        time_pattern = r'\\d{2}:\\d{2}:\\d{2},\\d{3}\\s*-->\\s*\\d{2}:\\d{2}:\\d{2},\\d{3}'
        return bool(re.search(time_pattern, content))
    
    def _generate_summary(self, video_info: Dict[str, Any], subtitle_content: str) -> str:
        """生成文章摘要"""
        try:
            title = video_info.get('title', '')
            uploader = video_info.get('uploader', '')
            duration = self._format_duration(video_info.get('duration', 0))
            
            # 提取字幕前200个字符作为内容预览
            if subtitle_content:
                # 如果是SRT格式，先提取纯文本
                if self._is_srt_format(subtitle_content):
                    import re
                    text_only = re.sub(r'\\d+\\n\\d{2}:\\d{2}:\\d{2},\\d{3} --> \\d{2}:\\d{2}:\\d{2},\\d{3}\\n', '', subtitle_content)
                    text_only = re.sub(r'\\n+', ' ', text_only).strip()
                else:
                    text_only = subtitle_content
                
                preview = text_only[:200] + '...' if len(text_only) > 200 else text_only
            else:
                preview = '无字幕内容'
            
            summary = f"视频: {title}"
            if uploader:
                summary += f" | 作者: {uploader}"
            if duration:
                summary += f" | 时长: {duration}"
            summary += f"\\n\\n内容预览: {preview}"
            
            return summary
            
        except Exception as e:
            logger.error(f"生成摘要失败: {str(e)}")
            return "自动生成的视频字幕文章"
    
    def _generate_tags(self, video_info: Dict[str, Any]) -> List[str]:
        """生成标签"""
        try:
            tags = ['video', 'subtitle']
            
            # 根据视频平台添加标签
            url = video_info.get('webpage_url', '') or video_info.get('url', '')
            if 'youtube.com' in url or 'youtu.be' in url:
                tags.append('youtube')
            elif 'bilibili.com' in url:
                tags.append('bilibili')
            elif 'acfun.cn' in url:
                tags.append('acfun')
            
            # 根据语言添加标签
            language = video_info.get('language', '')
            if language:
                if language.startswith('zh'):
                    tags.append('chinese')
                elif language.startswith('en'):
                    tags.append('english')
                elif language.startswith('ja'):
                    tags.append('japanese')
            
            # 根据作者添加标签
            uploader = video_info.get('uploader', '')
            if uploader and len(uploader) < 20:  # 避免过长的标签
                tags.append(f"author:{uploader}")
            
            return tags
            
        except Exception as e:
            logger.error(f"生成标签失败: {str(e)}")
            return ['video', 'subtitle']
    
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
    
    def _make_request(self, method: str, endpoint: str, data: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """发送API请求"""
        try:
            url = f"{self.base_url}{endpoint}"
            headers = {
                'Authorization': f'Token {self.api_token}',
                'Content-Type': 'application/json'
            }
            
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, timeout=30)
            elif method.upper() == 'POST':
                response = requests.post(url, headers=headers, json=data, timeout=30)
            elif method.upper() == 'PUT':
                response = requests.put(url, headers=headers, json=data, timeout=30)
            elif method.upper() == 'DELETE':
                response = requests.delete(url, headers=headers, timeout=30)
            else:
                logger.error(f"不支持的HTTP方法: {method}")
                return None
            
            if response.status_code in [200, 201, 202]:
                return response.json() if response.content else {}
            else:
                logger.error(f"Readwise API请求失败: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Readwise API请求出错: {str(e)}")
            return None
    
    def get_article(self, article_id: str) -> Optional[Dict[str, Any]]:
        """获取文章信息"""
        try:
            if not self.enabled:
                return None
            
            return self._make_request('GET', f'/documents/{article_id}/')
            
        except Exception as e:
            logger.error(f"获取Readwise文章失败: {str(e)}")
            return None
    
    def update_article(self, article_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """更新文章"""
        try:
            if not self.enabled:
                return None
            
            return self._make_request('PUT', f'/documents/{article_id}/', data=updates)
            
        except Exception as e:
            logger.error(f"更新Readwise文章失败: {str(e)}")
            return None
    
    def delete_article(self, article_id: str) -> bool:
        """删除文章"""
        try:
            if not self.enabled:
                return False
            
            result = self._make_request('DELETE', f'/documents/{article_id}/')
            return result is not None
            
        except Exception as e:
            logger.error(f"删除Readwise文章失败: {str(e)}")
            return False
    
    def list_articles(self, limit: int = 20, offset: int = 0) -> Optional[Dict[str, Any]]:
        """列出文章"""
        try:
            if not self.enabled:
                return None
            
            endpoint = f'/documents/?limit={limit}&offset={offset}'
            return self._make_request('GET', endpoint)
            
        except Exception as e:
            logger.error(f"列出Readwise文章失败: {str(e)}")
            return None
    
    def test_connection(self) -> bool:
        """测试Readwise连接"""
        try:
            if not self.enabled:
                logger.info("Readwise服务未启用")
                return False
            
            result = self._make_request('GET', '/documents/?limit=1')
            if result is not None:
                logger.info("Readwise连接测试成功")
                return True
            else:
                logger.error("Readwise连接测试失败")
                return False
                
        except Exception as e:
            logger.error(f"Readwise连接测试出错: {str(e)}")
            return False