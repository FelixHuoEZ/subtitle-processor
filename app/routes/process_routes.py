"""Processing routes for video transcription, translation, and subtitle generation."""

import os
import json
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from ..services.file_service import FileService
from ..services.video_service import VideoService
from ..services.transcription_service import TranscriptionService
from ..services.subtitle_service import SubtitleService
from ..services.translation_service import TranslationService
from ..services.readwise_service import ReadwiseService
from ..config.config_manager import get_config_value

logger = logging.getLogger(__name__)

# 创建蓝图
process_bp = Blueprint('process', __name__, url_prefix='/process')

# 初始化服务
file_service = FileService()
video_service = VideoService()
transcription_service = TranscriptionService()
subtitle_service = SubtitleService()
translation_service = TranslationService()
readwise_service = ReadwiseService()


@process_bp.route('/', methods=['GET', 'OPTIONS'])
def process_index():
    """处理服务主页"""
    if request.method == 'OPTIONS':
        # 处理 CORS 预检请求
        response = jsonify({'status': 'ok'})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
        response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
        return response
    
    return jsonify({
        'service': 'Video and Audio Processing Service',
        'endpoints': {
            'video_processing': '/process/video/<process_id>',
            'audio_transcription': '/process/audio/<file_id>',
            'subtitle_translation': '/process/translate/<file_id>',
            'readwise_creation': '/process/readwise/<file_id>',
            'status_check': '/process/status/<task_id>'
        },
        'status': 'ready'
    })


@process_bp.route('/video/<process_id>')
def process_video(process_id):
    """视频处理页面"""
    try:
        task_info = file_service.get_file_info(process_id)
        if not task_info:
            flash('处理任务不存在', 'error')
            return redirect(url_for('upload.upload_url'))
        
        return render_template('process_video.html', task_info=task_info)
        
    except Exception as e:
        logger.error(f"获取视频处理页面失败: {str(e)}")
        flash(f'获取处理页面失败: {str(e)}', 'error')
        return redirect(url_for('upload.upload_url'))


@process_bp.route('/video/<process_id>/start', methods=['POST'])
def start_video_processing(process_id):
    """开始视频处理"""
    try:
        task_info = file_service.get_file_info(process_id)
        if not task_info:
            return jsonify({'error': 'Task not found'}), 404
        
        url = task_info.get('url')
        platform = task_info.get('platform')
        
        if not url or not platform:
            return jsonify({'error': 'Invalid task info'}), 400
        
        # 更新任务状态
        file_service.update_file_info(process_id, {
            'status': 'processing',
            'updated_time': datetime.now().isoformat(),
            'progress': 0
        })
        
        # 开始处理视频
        result = video_service.process_video_for_transcription(url, platform)
        
        if not result:
            file_service.update_file_info(process_id, {
                'status': 'failed',
                'error': 'Video processing failed',
                'updated_time': datetime.now().isoformat()
            })
            return jsonify({'error': 'Video processing failed'}), 500
        
        # 更新任务信息
        file_service.update_file_info(process_id, {
            'video_info': result['video_info'],
            'language': result['language'],
            'needs_transcription': result['needs_transcription'],
            'progress': 50,
            'updated_time': datetime.now().isoformat()
        })
        
        # 如果有字幕内容，直接完成
        if result['subtitle_content']:
            subtitle_content = result['subtitle_content']
            
            # 处理字幕内容
            if subtitle_service.convert_to_srt:
                subtitle_content = subtitle_service.convert_to_srt(subtitle_content, 'json3')
            
            # 保存字幕文件
            video_title = result['video_info'].get('title', 'subtitle')
            subtitle_filename = f"{video_title}.srt"
            subtitle_path = file_service.save_file(subtitle_content, subtitle_filename)
            
            file_service.update_file_info(process_id, {
                'status': 'completed',
                'subtitle_content': subtitle_content,
                'subtitle_path': subtitle_path,
                'progress': 100,
                'updated_time': datetime.now().isoformat()
            })
            
            return jsonify({'status': 'completed', 'subtitle_path': subtitle_path})
        
        # 如果需要转录，开始音频转录
        elif result['audio_file']:
            # 开始转录音频
            hotwords = request.json.get('hotwords', []) if request.is_json else []
            transcription_result = transcription_service.transcribe_audio(result['audio_file'], hotwords)
            
            if not transcription_result:
                file_service.update_file_info(process_id, {
                    'status': 'failed',
                    'error': 'Audio transcription failed',
                    'updated_time': datetime.now().isoformat()
                })
                return jsonify({'error': 'Audio transcription failed'}), 500
            
            # 解析转录结果为SRT格式
            srt_content = subtitle_service.parse_srt(transcription_result, hotwords)
            
            if not srt_content:
                file_service.update_file_info(process_id, {
                    'status': 'failed',
                    'error': 'SRT parsing failed',
                    'updated_time': datetime.now().isoformat()
                })
                return jsonify({'error': 'SRT parsing failed'}), 500
            
            # 保存字幕文件
            video_title = result['video_info'].get('title', 'subtitle')
            subtitle_filename = f"{video_title}.srt"
            subtitle_path = file_service.save_file(srt_content, subtitle_filename)
            
            # 清理音频文件
            if os.path.exists(result['audio_file']):
                os.remove(result['audio_file'])
            
            file_service.update_file_info(process_id, {
                'status': 'completed',
                'subtitle_content': srt_content,
                'subtitle_path': subtitle_path,
                'transcription_result': transcription_result,
                'progress': 100,
                'updated_time': datetime.now().isoformat()
            })
            
            return jsonify({'status': 'completed', 'subtitle_path': subtitle_path})
        
        else:
            file_service.update_file_info(process_id, {
                'status': 'failed',
                'error': 'No subtitle or audio available',
                'updated_time': datetime.now().isoformat()
            })
            return jsonify({'error': 'No subtitle or audio available'}), 500
        
    except Exception as e:
        logger.error(f"视频处理失败: {str(e)}")
        file_service.update_file_info(process_id, {
            'status': 'failed',
            'error': str(e),
            'updated_time': datetime.now().isoformat()
        })
        return jsonify({'error': str(e)}), 500


@process_bp.route('/audio/<file_id>')
def transcribe_audio(file_id):
    """音频转录页面"""
    try:
        file_info = file_service.get_file_info(file_id)
        if not file_info:
            flash('文件不存在', 'error')
            return redirect(url_for('view.index'))
        
        if file_info.get('file_type') != 'audio':
            flash('不是音频文件', 'error')
            return redirect(url_for('view.file_detail', file_id=file_id))
        
        return render_template('transcribe_audio.html', file_info=file_info)
        
    except Exception as e:
        logger.error(f"获取音频转录页面失败: {str(e)}")
        flash(f'获取转录页面失败: {str(e)}', 'error')
        return redirect(url_for('view.index'))


@process_bp.route('/audio/<file_id>/start', methods=['POST'])
def start_audio_transcription(file_id):
    """开始音频转录"""
    try:
        file_info = file_service.get_file_info(file_id)
        if not file_info:
            return jsonify({'error': 'File not found'}), 404
        
        audio_path = file_info.get('file_path')
        if not audio_path or not os.path.exists(audio_path):
            return jsonify({'error': 'Audio file not found'}), 404
        
        # 获取热词
        hotwords = request.json.get('hotwords', []) if request.is_json else []
        
        # 更新文件状态
        file_service.update_file_info(file_id, {
            'status': 'transcribing',
            'updated_time': datetime.now().isoformat()
        })
        
        # 开始转录
        transcription_result = transcription_service.transcribe_audio(audio_path, hotwords)
        
        if not transcription_result:
            file_service.update_file_info(file_id, {
                'status': 'failed',
                'error': 'Transcription failed',
                'updated_time': datetime.now().isoformat()
            })
            return jsonify({'error': 'Transcription failed'}), 500
        
        # 解析为SRT格式
        srt_content = subtitle_service.parse_srt(transcription_result, hotwords)
        
        if not srt_content:
            file_service.update_file_info(file_id, {
                'status': 'failed',
                'error': 'SRT parsing failed',
                'updated_time': datetime.now().isoformat()
            })
            return jsonify({'error': 'SRT parsing failed'}), 500
        
        # 保存字幕文件
        original_name = file_info.get('original_filename', 'audio')
        subtitle_filename = f"{os.path.splitext(original_name)[0]}.srt"
        subtitle_path = file_service.save_file(srt_content, subtitle_filename)
        
        # 更新文件信息
        file_service.update_file_info(file_id, {
            'status': 'completed',
            'subtitle_content': srt_content,
            'subtitle_path': subtitle_path,
            'transcription_result': transcription_result,
            'updated_time': datetime.now().isoformat()
        })
        
        return jsonify({
            'status': 'completed',
            'subtitle_path': subtitle_path,
            'subtitle_content': srt_content
        })
        
    except Exception as e:
        logger.error(f"音频转录失败: {str(e)}")
        file_service.update_file_info(file_id, {
            'status': 'failed',
            'error': str(e),
            'updated_time': datetime.now().isoformat()
        })
        return jsonify({'error': str(e)}), 500


@process_bp.route('/subtitle/<file_id>')
def process_subtitle(file_id):
    """字幕处理页面"""
    try:
        file_info = file_service.get_file_info(file_id)
        if not file_info:
            flash('文件不存在', 'error')
            return redirect(url_for('view.index'))
        
        # 读取字幕内容
        subtitle_content = None
        if file_info.get('file_path') and os.path.exists(file_info['file_path']):
            subtitle_content = file_service.read_file(file_info['file_path'])
        
        return render_template('process_subtitle.html', 
                             file_info=file_info,
                             subtitle_content=subtitle_content)
        
    except Exception as e:
        logger.error(f"获取字幕处理页面失败: {str(e)}")
        flash(f'获取处理页面失败: {str(e)}', 'error')
        return redirect(url_for('view.index'))


@process_bp.route('/translate/<file_id>', methods=['POST'])
def translate_subtitle(file_id):
    """翻译字幕"""
    try:
        file_info = file_service.get_file_info(file_id)
        if not file_info:
            return jsonify({'error': 'File not found'}), 404
        
        # 获取翻译参数
        target_lang = request.json.get('target_lang', 'en')
        source_lang = request.json.get('source_lang', 'auto')
        
        # 获取字幕内容
        subtitle_content = None
        if file_info.get('subtitle_content'):
            subtitle_content = file_info['subtitle_content']
        elif file_info.get('file_path') and os.path.exists(file_info['file_path']):
            subtitle_content = file_service.read_file(file_info['file_path'])
        
        if not subtitle_content:
            return jsonify({'error': 'No subtitle content found'}), 404
        
        # 开始翻译
        translated_content = translation_service.translate_subtitle_content(
            subtitle_content, target_lang, source_lang
        )
        
        if not translated_content:
            return jsonify({'error': 'Translation failed'}), 500
        
        # 保存翻译后的字幕
        original_name = file_info.get('original_filename', 'subtitle')
        base_name = os.path.splitext(original_name)[0]
        translated_filename = f"{base_name}_{target_lang}.srt"
        translated_path = file_service.save_file(translated_content, translated_filename)
        
        return jsonify({
            'status': 'success',
            'translated_path': translated_path,
            'translated_content': translated_content
        })
        
    except Exception as e:
        logger.error(f"翻译字幕失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


@process_bp.route('/readwise/<file_id>', methods=['POST'])
def create_readwise_article(file_id):
    """创建Readwise文章"""
    try:
        if not readwise_service.enabled:
            return jsonify({'error': 'Readwise service not enabled'}), 400
        
        file_info = file_service.get_file_info(file_id)
        if not file_info:
            return jsonify({'error': 'File not found'}), 404
        
        # 构造字幕数据
        subtitle_data = {
            'video_info': file_info.get('video_info', {}),
            'subtitle_content': file_info.get('subtitle_content', '')
        }
        
        # 如果没有视频信息，从文件信息构造
        if not subtitle_data['video_info']:
            subtitle_data['video_info'] = {
                'title': file_info.get('original_filename', 'Unknown'),
                'uploader': 'Unknown',
                'url': file_info.get('url', '')
            }
        
        # 如果没有字幕内容，从文件读取
        if not subtitle_data['subtitle_content'] and file_info.get('file_path'):
            if os.path.exists(file_info['file_path']):
                subtitle_data['subtitle_content'] = file_service.read_file(file_info['file_path'])
        
        # 创建Readwise文章
        result = readwise_service.create_article_from_subtitle(subtitle_data)
        
        if not result:
            return jsonify({'error': 'Failed to create Readwise article'}), 500
        
        # 更新文件信息
        file_service.update_file_info(file_id, {
            'readwise_article_id': result.get('id'),
            'readwise_url': result.get('url'),
            'updated_time': datetime.now().isoformat()
        })
        
        return jsonify({
            'status': 'success',
            'article_id': result.get('id'),
            'article_url': result.get('url')
        })
        
    except Exception as e:
        logger.error(f"创建Readwise文章失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


@process_bp.route('/status/<task_id>')
def get_processing_status(task_id):
    """获取处理状态"""
    try:
        task_info = file_service.get_file_info(task_id)
        if not task_info:
            return jsonify({'error': 'Task not found'}), 404
        
        return jsonify({
            'id': task_id,
            'status': task_info.get('status', 'unknown'),
            'progress': task_info.get('progress', 0),
            'error': task_info.get('error'),
            'updated_time': task_info.get('updated_time')
        })
        
    except Exception as e:
        logger.error(f"获取处理状态失败: {str(e)}")
        return jsonify({'error': str(e)}), 500


@process_bp.route('/batch/transcribe', methods=['POST'])
def batch_transcribe():
    """批量转录"""
    try:
        file_ids = request.json.get('file_ids', [])
        hotwords = request.json.get('hotwords', [])
        
        if not file_ids:
            return jsonify({'error': 'No files specified'}), 400
        
        results = []
        successful = 0
        failed = 0
        
        for file_id in file_ids:
            try:
                file_info = file_service.get_file_info(file_id)
                if not file_info or file_info.get('file_type') != 'audio':
                    results.append({'file_id': file_id, 'status': 'failed', 'error': 'Invalid file'})
                    failed += 1
                    continue
                
                audio_path = file_info.get('file_path')
                if not audio_path or not os.path.exists(audio_path):
                    results.append({'file_id': file_id, 'status': 'failed', 'error': 'File not found'})
                    failed += 1
                    continue
                
                # 转录音频
                transcription_result = transcription_service.transcribe_audio(audio_path, hotwords)
                if not transcription_result:
                    results.append({'file_id': file_id, 'status': 'failed', 'error': 'Transcription failed'})
                    failed += 1
                    continue
                
                # 生成SRT
                srt_content = subtitle_service.parse_srt(transcription_result, hotwords)
                if not srt_content:
                    results.append({'file_id': file_id, 'status': 'failed', 'error': 'SRT parsing failed'})
                    failed += 1
                    continue
                
                # 保存文件
                original_name = file_info.get('original_filename', 'audio')
                subtitle_filename = f"{os.path.splitext(original_name)[0]}.srt"
                subtitle_path = file_service.save_file(srt_content, subtitle_filename)
                
                # 更新文件信息
                file_service.update_file_info(file_id, {
                    'status': 'completed',
                    'subtitle_content': srt_content,
                    'subtitle_path': subtitle_path,
                    'updated_time': datetime.now().isoformat()
                })
                
                results.append({'file_id': file_id, 'status': 'success', 'subtitle_path': subtitle_path})
                successful += 1
                
            except Exception as e:
                logger.error(f"批量转录文件失败 {file_id}: {str(e)}")
                results.append({'file_id': file_id, 'status': 'failed', 'error': str(e)})
                failed += 1
        
        return jsonify({
            'total': len(file_ids),
            'successful': successful,
            'failed': failed,
            'results': results
        })
        
    except Exception as e:
        logger.error(f"批量转录失败: {str(e)}")
        return jsonify({'error': str(e)}), 500