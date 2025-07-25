"""View routes for displaying files and content."""

import os
import json
import logging
from flask import Blueprint, render_template, jsonify, request, abort, send_file, flash, redirect, url_for
from ..services.file_service import FileService
from ..services.subtitle_service import SubtitleService
from ..config.config_manager import get_config_value

logger = logging.getLogger(__name__)

# 创建蓝图
view_bp = Blueprint('view', __name__, url_prefix='/view')

# 初始化服务
file_service = FileService()
subtitle_service = SubtitleService()


@view_bp.route('/')
def index():
    """文件列表页面"""
    try:
        # 获取分页参数
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        file_type = request.args.get('type', '')
        
        # 获取所有文件信息
        all_files = file_service.list_files()
        
        # 过滤文件类型
        if file_type:
            filtered_files = {k: v for k, v in all_files.items() 
                            if v.get('file_type') == file_type or v.get('status') == file_type}
        else:
            filtered_files = all_files
        
        # 转换为列表并排序
        files_list = list(filtered_files.values())
        files_list.sort(key=lambda x: x.get('upload_time', ''), reverse=True)
        
        # 分页处理
        total = len(files_list)
        start = (page - 1) * per_page
        end = start + per_page
        files_page = files_list[start:end]
        
        # 计算分页信息
        total_pages = (total + per_page - 1) // per_page
        has_prev = page > 1
        has_next = page < total_pages
        
        return render_template('file_list.html', 
                             files=files_page,
                             pagination={
                                 'page': page,
                                 'per_page': per_page,
                                 'total': total,
                                 'total_pages': total_pages,
                                 'has_prev': has_prev,
                                 'has_next': has_next,
                                 'prev_num': page - 1 if has_prev else None,
                                 'next_num': page + 1 if has_next else None
                             },
                             current_type=file_type)
        
    except Exception as e:
        logger.error(f"获取文件列表失败: {str(e)}")
        flash(f'获取文件列表失败: {str(e)}', 'error')
        return render_template('file_list.html', files=[], pagination={})


@view_bp.route('/<file_id>')
def file_detail(file_id):
    """文件详情页面"""
    try:
        file_info = file_service.get_file_info(file_id)
        if not file_info:
            abort(404)
        
        # 获取文件内容（如果是文本文件）
        file_content = None
        if file_info.get('file_type') == 'subtitle':
            try:
                file_path = file_info.get('file_path')
                if file_path and os.path.exists(file_path):
                    file_content = file_service.read_file(file_path)
            except Exception as e:
                logger.warning(f"读取文件内容失败: {str(e)}")
        
        return render_template('file_detail.html', 
                             file_info=file_info, 
                             file_content=file_content)
        
    except Exception as e:
        logger.error(f"获取文件详情失败: {str(e)}")
        abort(500)


@view_bp.route('/<file_id>/content')
def file_content(file_id):
    """获取文件内容（API接口）"""
    try:
        file_info = file_service.get_file_info(file_id)
        if not file_info:
            return jsonify({'error': 'File not found'}), 404
        
        file_path = file_info.get('file_path')
        if not file_path or not os.path.exists(file_path):
            return jsonify({'error': 'File not found on disk'}), 404
        
        # 读取文件内容
        content = file_service.read_file(file_path)
        
        return jsonify({
            'content': content,
            'file_info': file_info
        })
        
    except Exception as e:
        logger.error(f"获取文件内容失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


@view_bp.route('/<file_id>/download')
def download_file(file_id):
    """下载文件"""
    try:
        file_info = file_service.get_file_info(file_id)
        if not file_info:
            abort(404)
        
        file_path = file_info.get('file_path')
        if not file_path or not os.path.exists(file_path):
            abort(404)
        
        # 获取原始文件名
        download_name = file_info.get('original_filename', f"{file_id}.txt")
        
        return send_file(file_path, 
                        as_attachment=True, 
                        download_name=download_name)
        
    except Exception as e:
        logger.error(f"下载文件失败: {str(e)}")
        abort(500)


@view_bp.route('/<file_id>/subtitle')
def view_subtitle(file_id):
    """查看字幕内容（专门的字幕查看页面）"""
    try:
        file_info = file_service.get_file_info(file_id)
        if not file_info:
            abort(404)
        
        # 读取字幕内容
        subtitle_content = None
        parsed_subtitles = []
        
        if file_info.get('file_type') == 'subtitle' or file_info.get('status') == 'completed':
            try:
                file_path = file_info.get('file_path')
                if file_path and os.path.exists(file_path):
                    subtitle_content = file_service.read_file(file_path)
                    # 解析字幕内容
                    parsed_subtitles = subtitle_service.parse_srt_content(subtitle_content)
                elif file_info.get('subtitle_content'):
                    # 从文件信息中获取字幕内容
                    subtitle_content = file_info['subtitle_content']
                    parsed_subtitles = subtitle_service.parse_srt_content(subtitle_content)
                    
            except Exception as e:
                logger.warning(f"解析字幕内容失败: {str(e)}")
        
        return render_template('subtitle_viewer.html',
                             file_info=file_info,
                             subtitle_content=subtitle_content,
                             parsed_subtitles=parsed_subtitles)
        
    except Exception as e:
        logger.error(f"查看字幕失败: {str(e)}")
        abort(500)


@view_bp.route('/<video_id>/player')
def video_player(video_id):
    """视频播放器页面（用于YouTube等视频）"""
    try:
        # 检查是否是有效的YouTube视频ID
        if len(video_id) != 11:  # YouTube视频ID长度为11
            abort(404)
        
        # 构造视频信息
        video_info = {
            'id': video_id,
            'url': f'https://www.youtube.com/watch?v={video_id}',
            'embed_url': f'https://www.youtube.com/embed/{video_id}'
        }
        
        # 检查是否有对应的字幕文件
        files_info = file_service.list_files()
        subtitle_files = []
        
        for file_id, file_info in files_info.items():
            if video_id in file_info.get('url', '') or video_id in file_info.get('video_id', ''):
                subtitle_files.append(file_info)
        
        return render_template('video_player.html',
                             video_info=video_info,
                             subtitle_files=subtitle_files)
        
    except Exception as e:
        logger.error(f"视频播放器失败: {str(e)}")
        abort(500)


@view_bp.route('/search')
def search_files():
    """文件搜索"""
    try:
        query = request.args.get('q', '').strip()
        file_type = request.args.get('type', '')
        
        if not query:
            return render_template('search_results.html', 
                                 query=query, 
                                 results=[], 
                                 total=0)
        
        # 获取所有文件
        all_files = file_service.list_files()
        results = []
        
        # 搜索逻辑
        for file_id, file_info in all_files.items():
            # 搜索文件名
            if query.lower() in file_info.get('original_filename', '').lower():
                results.append(file_info)
                continue
            
            # 搜索描述或标题
            if query.lower() in file_info.get('title', '').lower():
                results.append(file_info)
                continue
            
            # 如果是字幕文件，搜索内容
            if file_info.get('file_type') == 'subtitle':
                try:
                    file_path = file_info.get('file_path')
                    if file_path and os.path.exists(file_path):
                        content = file_service.read_file(file_path)
                        if query.lower() in content.lower():
                            results.append(file_info)
                except Exception:
                    pass
        
        # 文件类型过滤
        if file_type:
            results = [f for f in results if f.get('file_type') == file_type]
        
        # 排序
        results.sort(key=lambda x: x.get('upload_time', ''), reverse=True)
        
        return render_template('search_results.html',
                             query=query,
                             results=results,
                             total=len(results),
                             file_type=file_type)
        
    except Exception as e:
        logger.error(f"搜索文件失败: {str(e)}")
        return render_template('search_results.html', 
                             query=query, 
                             results=[], 
                             total=0,
                             error=str(e))


@view_bp.route('/stats')
def file_stats():
    """文件统计页面"""
    try:
        all_files = file_service.list_files()
        
        # 统计数据
        stats = {
            'total_files': len(all_files),
            'audio_files': 0,
            'subtitle_files': 0,
            'completed_files': 0,
            'failed_files': 0,
            'total_size': 0,
            'platforms': {},
            'recent_files': []
        }
        
        # 收集统计信息
        for file_info in all_files.values():
            file_type = file_info.get('file_type', 'unknown')
            status = file_info.get('status', 'unknown')
            
            if file_type == 'audio':
                stats['audio_files'] += 1
            elif file_type == 'subtitle':
                stats['subtitle_files'] += 1
            
            if status == 'completed':
                stats['completed_files'] += 1
            elif status == 'failed':
                stats['failed_files'] += 1
            
            # 文件大小
            file_size = file_info.get('file_size', 0)
            if isinstance(file_size, (int, float)):
                stats['total_size'] += file_size
            
            # 平台统计
            platform = file_info.get('platform', 'unknown')
            if platform != 'unknown':
                stats['platforms'][platform] = stats['platforms'].get(platform, 0) + 1
        
        # 最近文件
        recent_files = list(all_files.values())
        recent_files.sort(key=lambda x: x.get('upload_time', ''), reverse=True)
        stats['recent_files'] = recent_files[:10]
        
        # 格式化文件大小
        stats['total_size_formatted'] = _format_file_size(stats['total_size'])
        
        return render_template('file_stats.html', stats=stats)
        
    except Exception as e:
        logger.error(f"获取文件统计失败: {str(e)}")
        return render_template('file_stats.html', stats={}, error=str(e))


@view_bp.route('/api/files')
def api_list_files():
    """文件列表API接口"""
    try:
        file_type = request.args.get('type')
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)
        
        all_files = file_service.list_files()
        
        # 过滤文件类型
        if file_type:
            filtered_files = {k: v for k, v in all_files.items() 
                            if v.get('file_type') == file_type}
        else:
            filtered_files = all_files
        
        # 转换为列表并排序
        files_list = list(filtered_files.values())
        files_list.sort(key=lambda x: x.get('upload_time', ''), reverse=True)
        
        # 分页
        total = len(files_list)
        files_page = files_list[offset:offset + limit]
        
        return jsonify({
            'files': files_page,
            'total': total,
            'limit': limit,
            'offset': offset,
            'has_more': offset + limit < total
        })
        
    except Exception as e:
        logger.error(f"API获取文件列表失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


def _format_file_size(size_bytes):
    """格式化文件大小"""
    try:
        if size_bytes == 0:
            return "0 B"
        
        size_names = ["B", "KB", "MB", "GB", "TB"]
        import math
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_names[i]}"
        
    except Exception:
        return str(size_bytes) + " B"