"""Upload routes for file and URL processing."""

import os
import uuid
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from werkzeug.utils import secure_filename
from ..services.file_service import FileService
from ..services.video_service import VideoService
from ..services.transcription_service import TranscriptionService
from ..services.subtitle_service import SubtitleService
from ..services.translation_service import TranslationService
from ..services.readwise_service import ReadwiseService
from ..config.config_manager import get_config_value

logger = logging.getLogger(__name__)

# 创建蓝图
upload_bp = Blueprint('upload', __name__, url_prefix='/upload')

# 初始化服务
file_service = FileService()
video_service = VideoService()
transcription_service = TranscriptionService()
subtitle_service = SubtitleService()
translation_service = TranslationService()
readwise_service = ReadwiseService()


@upload_bp.route('/', methods=['GET', 'POST'])
def upload_file():
    """文件上传页面和处理"""
    if request.method == 'GET':
        return render_template('upload.html')
    
    try:
        # 检查是否有文件上传
        if 'file' not in request.files:
            flash('没有选择文件', 'error')
            return redirect(request.url)
        
        file = request.files['file']
        if file.filename == '':
            flash('没有选择文件', 'error')
            return redirect(request.url)
        
        # 检查文件类型
        allowed_extensions = get_config_value('app.allowed_extensions', ['.txt', '.srt', '.vtt', '.wav', '.mp3', '.m4a'])
        filename = secure_filename(file.filename)
        file_ext = os.path.splitext(filename)[1].lower()
        
        if file_ext not in allowed_extensions:
            flash(f'不支持的文件类型: {file_ext}', 'error')
            return redirect(request.url)
        
        # 生成文件ID和保存文件
        file_id = str(uuid.uuid4())
        file_path = os.path.join(file_service.upload_folder, f"{file_id}{file_ext}")
        file.save(file_path)
        
        # 创建文件信息
        file_info = {
            'id': file_id,
            'original_filename': file.filename,
            'filename': f"{file_id}{file_ext}",
            'file_path': file_path,
            'file_size': os.path.getsize(file_path),
            'upload_time': datetime.now().isoformat(),
            'status': 'uploaded',
            'file_type': _detect_file_type(file_ext)
        }
        
        # 保存文件信息
        file_service.add_file_info(file_id, file_info)
        
        logger.info(f"文件上传成功: {filename} -> {file_id}")
        flash(f'文件上传成功: {filename}', 'success')
        
        # 根据文件类型重定向到相应的处理页面
        if file_info['file_type'] == 'audio':
            return redirect(url_for('process.transcribe_audio', file_id=file_id))
        elif file_info['file_type'] == 'subtitle':
            return redirect(url_for('process.process_subtitle', file_id=file_id))
        else:
            return redirect(url_for('view.file_detail', file_id=file_id))
        
    except Exception as e:
        logger.error(f"文件上传失败: {str(e)}")
        flash(f'文件上传失败: {str(e)}', 'error')
        return redirect(request.url)


@upload_bp.route('/url', methods=['GET', 'POST'])
def upload_url():
    """URL处理页面和处理"""
    if request.method == 'GET':
        return render_template('upload_url.html')
    
    try:
        # 获取URL
        url = request.form.get('url', '').strip()
        if not url:
            flash('请输入视频URL', 'error')
            return redirect(request.url)
        
        # 检测平台
        platform = _detect_platform(url)
        if not platform:
            flash('不支持的视频平台', 'error')
            return redirect(request.url)
        
        # 生成处理ID
        process_id = str(uuid.uuid4())
        
        # 创建处理任务信息
        task_info = {
            'id': process_id,
            'url': url,
            'platform': platform,
            'status': 'pending',
            'created_time': datetime.now().isoformat(),
            'updated_time': datetime.now().isoformat()
        }
        
        # 保存任务信息
        file_service.add_file_info(process_id, task_info)
        
        logger.info(f"URL处理任务创建: {url} -> {process_id}")
        flash(f'视频处理任务已创建', 'success')
        
        # 重定向到处理页面
        return redirect(url_for('process.process_video', process_id=process_id))
        
    except Exception as e:
        logger.error(f"URL处理失败: {str(e)}")
        flash(f'URL处理失败: {str(e)}', 'error')
        return redirect(request.url)


@upload_bp.route('/batch', methods=['GET', 'POST'])
def batch_upload():
    """批量文件上传"""
    if request.method == 'GET':
        return render_template('batch_upload.html')
    
    try:
        files = request.files.getlist('files')
        if not files or len(files) == 0:
            flash('没有选择文件', 'error')
            return redirect(request.url)
        
        results = []
        successful = 0
        failed = 0
        
        for file in files:
            if file.filename == '':
                continue
            
            try:
                # 处理单个文件
                filename = secure_filename(file.filename)
                file_ext = os.path.splitext(filename)[1].lower()
                
                # 检查文件类型
                allowed_extensions = get_config_value('app.allowed_extensions', ['.txt', '.srt', '.vtt', '.wav', '.mp3', '.m4a'])
                if file_ext not in allowed_extensions:
                    results.append({'filename': filename, 'status': 'failed', 'error': f'不支持的文件类型: {file_ext}'})
                    failed += 1
                    continue
                
                # 保存文件
                file_id = str(uuid.uuid4())
                file_path = os.path.join(file_service.upload_folder, f"{file_id}{file_ext}")
                file.save(file_path)
                
                # 创建文件信息
                file_info = {
                    'id': file_id,
                    'original_filename': filename,
                    'filename': f"{file_id}{file_ext}",
                    'file_path': file_path,
                    'file_size': os.path.getsize(file_path),
                    'upload_time': datetime.now().isoformat(),
                    'status': 'uploaded',
                    'file_type': _detect_file_type(file_ext)
                }
                
                file_service.add_file_info(file_id, file_info)
                
                results.append({'filename': filename, 'status': 'success', 'file_id': file_id})
                successful += 1
                
            except Exception as e:
                logger.error(f"批量上传文件失败 {filename}: {str(e)}")
                results.append({'filename': filename, 'status': 'failed', 'error': str(e)})
                failed += 1
        
        flash(f'批量上传完成 - 成功: {successful}, 失败: {failed}', 'success')
        return render_template('batch_upload_result.html', results=results)
        
    except Exception as e:
        logger.error(f"批量上传失败: {str(e)}")
        flash(f'批量上传失败: {str(e)}', 'error')
        return redirect(request.url)


@upload_bp.route('/status/<file_id>')
def upload_status(file_id):
    """获取上传状态"""
    try:
        file_info = file_service.get_file_info(file_id)
        if not file_info:
            return jsonify({'error': 'File not found'}), 404
        
        return jsonify(file_info)
        
    except Exception as e:
        logger.error(f"获取上传状态失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


@upload_bp.route('/validate', methods=['POST'])
def validate_file():
    """验证文件（AJAX接口）"""
    try:
        if 'file' not in request.files:
            return jsonify({'valid': False, 'message': '没有选择文件'})
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'valid': False, 'message': '没有选择文件'})
        
        filename = secure_filename(file.filename)
        file_ext = os.path.splitext(filename)[1].lower()
        
        # 检查文件类型
        allowed_extensions = get_config_value('app.allowed_extensions', ['.txt', '.srt', '.vtt', '.wav', '.mp3', '.m4a'])
        if file_ext not in allowed_extensions:
            return jsonify({'valid': False, 'message': f'不支持的文件类型: {file_ext}'})
        
        # 检查文件大小（如果需要）
        max_size = get_config_value('app.max_file_size', 500 * 1024 * 1024)  # 500MB
        if hasattr(file, 'content_length') and file.content_length > max_size:
            return jsonify({'valid': False, 'message': '文件过大'})
        
        return jsonify({'valid': True, 'message': '文件验证通过'})
        
    except Exception as e:
        logger.error(f"文件验证失败: {str(e)}")
        return jsonify({'valid': False, 'message': str(e)})


def _detect_file_type(file_ext):
    """检测文件类型"""
    audio_extensions = ['.wav', '.mp3', '.m4a', '.flac', '.aac', '.ogg', '.wma']
    subtitle_extensions = ['.srt', '.vtt', '.txt', '.ass', '.ssa']
    
    if file_ext in audio_extensions:
        return 'audio'
    elif file_ext in subtitle_extensions:
        return 'subtitle'
    else:
        return 'unknown'


def _detect_platform(url):
    """检测视频平台"""
    if 'youtube.com' in url or 'youtu.be' in url:
        return 'youtube'
    elif 'bilibili.com' in url:
        return 'bilibili'
    elif 'acfun.cn' in url:
        return 'acfun'
    else:
        return None