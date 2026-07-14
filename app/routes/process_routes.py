"""Processing routes for video transcription, translation, and subtitle generation."""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, Response, current_app

from ..services.file_service import FileService
from ..services.processing_service import ProcessingService
from ..services.video_service import VideoService
from ..services.transcription_service import TranscriptionService
from ..services.subtitle_service import SubtitleService
from ..services.translation_service import TranslationService
from ..services.readwise_service import ReadwiseService
from ..config.config_manager import get_config_value
from ..services.runtime import service_proxy
from ..utils.file_utils import build_task_filename

logger = logging.getLogger(__name__)

RETRY_INTERRUPTED_OPERATION = 'retry_interrupted_task'
RETRY_TASK_COPY_FIELDS = (
    'auto_transcribe',
    'extract_audio',
    'filename',
    'hotwords',
    'location',
    'page_title',
    'tags',
    'video_id',
)
AUTO_RETRY_SAFE_STAGE_CODES = {
    'source_analysis',
    'wait_download_slot',
    'download_prepare',
    'language_confirmation',
    'transcribe_audio',
    'generate_subtitles',
    'normalize_subtitles',
    'translate_subtitles',
}
AUTO_RETRY_EXTERNAL_EFFECT_FIELDS = (
    'readwise_article_id',
    'readwise_url',
    'readwise_url_only_article_id',
    'readwise_url_only_url',
    'readwise_fallback_article_id',
    'readwise_fallback_url',
    'readwise_deleted_article_id',
)

# 创建蓝图
process_bp = Blueprint('process', __name__, url_prefix='/process')

# 初始化服务
file_service = service_proxy(FileService)
video_service = service_proxy(VideoService)
transcription_service = service_proxy(TranscriptionService)
subtitle_service = service_proxy(SubtitleService)
translation_service = service_proxy(TranslationService)
readwise_service = service_proxy(ReadwiseService)
processing_service = service_proxy(
    lambda: ProcessingService(
        file_service=file_service,
        video_service=video_service,
        transcription_service=transcription_service,
        subtitle_service=subtitle_service,
        readwise_service=readwise_service,
        translation_service=translation_service,
    )
)


def configure_services(services):
    """Bind this module to the application service set."""
    global file_service, video_service, transcription_service
    global subtitle_service, translation_service, readwise_service, processing_service

    file_service = services.file_service
    video_service = services.video_service
    transcription_service = services.transcription_service
    subtitle_service = services.subtitle_service
    translation_service = services.translation_service
    readwise_service = services.readwise_service
    processing_service = services.processing_service


def _run_force_local_readwise_with_app_context(app, task_id, claim_token):
    with app.app_context():
        processing_service.retry_readwise_with_local_content(
            task_id,
            claim_token=claim_token,
        )


def _run_retried_video_task_with_app_context(app, task_info):
    with app.app_context():
        processing_service.process_video_task(
            task_info,
            _task_bool(task_info.get('auto_transcribe')),
        )


def _record_auto_retry_metric(metrics_service, outcome, status_code):
    if not metrics_service:
        return
    try:
        metrics_service.record_auto_restart_retry(outcome, status_code)
    except Exception as exc:
        logger.warning('记录自动续跑运行指标失败: %s', exc)


def _wants_json_response():
    return request.is_json or request.accept_mimetypes.best == 'application/json'


def _task_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _last_progress_stage_code(task_info):
    runs = task_info.get('progress_runs') or []
    if not runs:
        return None
    stages = runs[-1].get('stages') or []
    if not stages:
        return None
    return stages[-1].get('code')


def _retry_attempt(task_info):
    try:
        return max(0, int(task_info.get('retry_attempt') or 0))
    except (TypeError, ValueError):
        return 0


def _auto_retry_eligibility(task_info):
    try:
        max_attempts = max(
            1,
            int(os.getenv('AUTO_RETRY_INTERRUPTED_MAX_ATTEMPTS', '3')),
        )
    except ValueError:
        max_attempts = 3

    retry_attempt = _retry_attempt(task_info)
    if retry_attempt >= max_attempts:
        return False, 'max_attempts_reached'
    if any(task_info.get(field) for field in AUTO_RETRY_EXTERNAL_EFFECT_FIELDS):
        return False, 'readwise_side_effect_may_exist'
    if task_info.get('readwise_url_only_delete_status') == 'deleted':
        return False, 'readwise_delete_side_effect_exists'

    interrupted_stage = _last_progress_stage_code(task_info)
    if interrupted_stage not in AUTO_RETRY_SAFE_STAGE_CODES:
        return False, f'unsafe_interrupted_stage:{interrupted_stage or "unknown"}'
    return True, interrupted_stage


def _run_startup_auto_retries(app, task_ids, delay_seconds):
    time.sleep(delay_seconds)
    metrics_service = getattr(app, 'runtime_metrics_service', None)
    for task_id in task_ids:
        try:
            with app.test_request_context(headers={'Accept': 'application/json'}):
                response = retry_interrupted_task(
                    task_id,
                    request_source='auto_restart_retry',
                    enforce_auto_safety=True,
                )
            status_code = (
                response[1]
                if isinstance(response, tuple)
                else response.status_code
            )
            logger.info(
                '服务重启自动续跑结果: task=%s status_code=%s',
                task_id,
                status_code,
            )
            if metrics_service:
                if status_code == 202:
                    outcome = 'scheduled'
                elif status_code == 409:
                    outcome = 'skipped'
                else:
                    outcome = 'failed'
                _record_auto_retry_metric(metrics_service, outcome, status_code)
        except Exception as exc:
            logger.error('服务重启自动续跑失败: task=%s error=%s', task_id, exc)
            _record_auto_retry_metric(metrics_service, 'failed', 500)


def schedule_auto_retry_interrupted_tasks(app, task_ids):
    """Schedule safe, lineage-preserving retries for tasks interrupted at startup."""
    task_ids = list(task_ids or [])
    if not task_ids or not _task_bool(os.getenv('AUTO_RETRY_INTERRUPTED_TASKS')):
        return False
    try:
        delay_seconds = max(
            0.0,
            float(os.getenv('AUTO_RETRY_INTERRUPTED_DELAY_SECONDS', '5')),
        )
    except ValueError:
        delay_seconds = 5.0

    thread = threading.Thread(
        target=_run_startup_auto_retries,
        args=(app, task_ids, delay_seconds),
        daemon=True,
        name='auto-retry-interrupted-tasks',
    )
    thread.start()
    logger.warning(
        '已安排 %s 个启动中断任务进行安全自动续跑，延迟 %.1f 秒',
        len(task_ids),
        delay_seconds,
    )
    return True


def _retry_task_response(task_id, reused=False):
    if _wants_json_response():
        return jsonify({
            'success': True,
            'status': 'processing',
            'process_id': task_id,
            'status_url': f'/process/status/{task_id}',
            'view_url': f'/view/{task_id}',
            'reused': reused,
        }), 202
    return redirect(url_for('view.file_detail', file_id=task_id), code=303)


def _normalize_language_confirmation_choice(language):
    normalized = video_service._normalize_language_code(language)
    if normalized in {"zh", "en"}:
        return normalized
    if isinstance(language, str) and language.strip().lower() == "auto":
        return "auto"
    return None


@process_bp.route('/', methods=['GET', 'OPTIONS'])
def process_index():
    """处理服务主页"""
    if request.method == 'OPTIONS':
        return '', 204
    
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
        
        return redirect(url_for('view.file_detail', file_id=process_id))
        
    except Exception as e:
        logger.error(f"获取视频处理页面失败: {str(e)}")
        flash(f'获取处理页面失败: {str(e)}', 'error')
        return redirect(url_for('view.index'))


@process_bp.route('/video/<process_id>/start', methods=['POST'])
def start_video_processing(process_id):
    """开始视频处理"""
    task_temp_dir = None
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
        if result:
            task_temp_dir = result.get('temp_dir')
        
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
            'language_details': result.get('language_details'),
            'content_locale': result.get('content_locale'),
            'content_locale_details': result.get('content_locale_details'),
            'subtitle_metadata': result.get('subtitle_metadata'),
            'needs_transcription': result['needs_transcription'],
            'download_asset_cache_hit': result.get('download_asset_cache_hit', False),
            'download_asset_cache_key': result.get('download_asset_cache_key'),
            'readwise_mode': result.get('readwise_mode'),
            'readwise_reason': result.get('readwise_reason'),
            'readwise_url_only': result.get('readwise_url_only', False),
            'spoken_pattern': result.get('spoken_pattern'),
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
            subtitle_filename = build_task_filename(video_title, process_id)
            subtitle_path = file_service.save_file(subtitle_content, subtitle_filename)
            
            file_service.update_file_info(process_id, {
                'status': 'completed',
                'filename': subtitle_filename,
                'subtitle_content': subtitle_content,
                'subtitle_path': subtitle_path,
                'readwise_mode': result.get('readwise_mode'),
                'readwise_reason': result.get('readwise_reason'),
                'readwise_url_only': result.get('readwise_url_only', False),
                'spoken_pattern': result.get('spoken_pattern'),
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
            subtitle_filename = build_task_filename(video_title, process_id)
            subtitle_path = file_service.save_file(srt_content, subtitle_filename)
            
            file_service.update_file_info(process_id, {
                'status': 'completed',
                'filename': subtitle_filename,
                'subtitle_content': srt_content,
                'subtitle_path': subtitle_path,
                'transcription_result': transcription_result,
                'readwise_mode': result.get('readwise_mode'),
                'readwise_reason': result.get('readwise_reason'),
                'readwise_url_only': result.get('readwise_url_only', False),
                'spoken_pattern': result.get('spoken_pattern'),
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
    finally:
        if task_temp_dir:
            video_service.cleanup_task_artifacts(task_temp_dir)


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
        subtitle_filename = build_task_filename(
            os.path.splitext(original_name)[0], file_id
        )
        subtitle_path = file_service.save_file(srt_content, subtitle_filename)
        
        # 更新文件信息
        file_service.update_file_info(file_id, {
            'status': 'completed',
            'filename': subtitle_filename,
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
        payload = request.get_json(silent=True) or {}
        target_lang = payload.get(
            'target_lang',
            translation_service.default_target_language,
        )
        source_lang = payload.get('source_lang', 'auto')
        
        # 获取字幕内容
        subtitle_content = None
        if file_info.get('subtitle_content'):
            subtitle_content = file_info['subtitle_content']
        elif file_info.get('file_path') and os.path.exists(file_info['file_path']):
            subtitle_content = file_service.read_file(file_info['file_path'])
        
        if not subtitle_content:
            return jsonify({'error': 'No subtitle content found'}), 404
        
        # 开始翻译
        translation_result = translation_service.translate_subtitle_content_detailed(
            subtitle_content, target_lang, source_lang
        )

        if translation_result.get('status') != 'completed':
            return jsonify({
                'error': 'Translation did not complete',
                'translation_status': translation_result.get('status'),
                'providers': translation_result.get('providers') or [],
                'total_segments': translation_result.get('total_segments', 0),
                'translated_segments': translation_result.get(
                    'translated_segments', 0
                ),
                'failed_segments': translation_result.get('failed_segments', 0),
                'reason': translation_result.get('error'),
            }), 502
        translated_content = translation_result['content']
        
        # 保存翻译后的字幕
        original_name = file_info.get('original_filename', 'subtitle')
        base_name = os.path.splitext(original_name)[0]
        translated_filename = build_task_filename(
            f"{base_name}_{target_lang}", file_id
        )
        translated_path = file_service.save_file(translated_content, translated_filename)
        
        return jsonify({
            'status': 'success',
            'translated_path': translated_path,
            'translated_content': translated_content,
            'source_language': translation_result.get('source_language'),
            'target_language': translation_result.get('target_language'),
            'providers': translation_result.get('providers') or [],
            'total_segments': translation_result.get('total_segments', 0),
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
            'subtitle_content': file_info.get('subtitle_content', ''),
            'readwise_mode': file_info.get('readwise_mode'),
            'readwise_reason': file_info.get('readwise_reason'),
            'readwise_url_only': file_info.get('readwise_url_only', False),
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
    """获取处理状态（支持 include_content=1 返回字幕文本）"""
    try:
        task_info = file_service.get_file_info(task_id)
        if not task_info:
            return jsonify({'success': False, 'status': 'not_found'}), 404

        status = task_info.get('status', 'unknown')
        progress_details = processing_service.get_progress_snapshot(task_info)
        current_stage = progress_details.get('current_stage') or {}
        response_data = {
            'success': True,
            'process_id': task_id,
            'status': status,
            'progress': progress_details.get('progress', task_info.get('progress')),
            'stage': current_stage.get('code') or task_info.get('stage'),
            'stage_label': current_stage.get('label') or task_info.get('stage_label'),
            'progress_details': progress_details,
            'error': task_info.get('error'),
            'video_info': task_info.get('video_info'),
            'language': task_info.get('language'),
            'language_details': task_info.get('language_details'),
            'content_locale': task_info.get('content_locale'),
            'content_locale_details': task_info.get('content_locale_details'),
            'readwise_mode': task_info.get('readwise_mode'),
            'readwise_reason': task_info.get('readwise_reason'),
            'readwise_url_only': task_info.get('readwise_url_only'),
            'skip_processing_for_url_only': task_info.get('skip_processing_for_url_only'),
            'readwise_article_id': task_info.get('readwise_article_id'),
            'readwise_url': task_info.get('readwise_url'),
            'readwise_delivery_status': task_info.get('readwise_delivery_status'),
            'readwise_url_only_article_id': task_info.get('readwise_url_only_article_id'),
            'readwise_url_only_url': task_info.get('readwise_url_only_url'),
            'readwise_parse_status': task_info.get('readwise_parse_status'),
            'readwise_parse_reason': task_info.get('readwise_parse_reason'),
            'readwise_parse_message': task_info.get('readwise_parse_message'),
            'readwise_parse_checked_at': task_info.get('readwise_parse_checked_at'),
            'readwise_parse_attempts': task_info.get('readwise_parse_attempts'),
            'readwise_auto_fallback_enabled': task_info.get('readwise_auto_fallback_enabled'),
            'readwise_auto_fallback_requested_at': task_info.get('readwise_auto_fallback_requested_at'),
            'force_local_readwise_available': task_info.get('force_local_readwise_available'),
            'force_local_readwise_requested_at': task_info.get('force_local_readwise_requested_at'),
            'download_asset_cache_hit': task_info.get('download_asset_cache_hit'),
            'download_asset_cache_key': task_info.get('download_asset_cache_key'),
            'readwise_url_only_delete_status': task_info.get('readwise_url_only_delete_status'),
            'readwise_url_only_deleted_at': task_info.get('readwise_url_only_deleted_at'),
            'readwise_deleted_article_id': task_info.get('readwise_deleted_article_id'),
            'readwise_fallback_from_article_id': task_info.get('readwise_fallback_from_article_id'),
            'readwise_fallback_article_id': task_info.get('readwise_fallback_article_id'),
            'readwise_fallback_url': task_info.get('readwise_fallback_url'),
            'spoken_pattern': task_info.get('spoken_pattern'),
            'language_confirmation': task_info.get('language_confirmation'),
            'language_override': task_info.get('language_override'),
            'filename': task_info.get('filename'),
            'page_title': task_info.get('page_title'),
            'subtitle_path': task_info.get('subtitle_path') or task_info.get('path'),
            'view_url': task_info.get('url'),
            'created_at': task_info.get('created_time') or task_info.get('upload_time'),
            'updated_at': task_info.get('status_updated_at') or task_info.get('updated_time'),
            'status_url': f"/process/status/{task_id}",
        }

        include_content = request.args.get('include_content') == '1'
        subtitle_path = response_data.get('subtitle_path')
        cached_subtitle = task_info.get('subtitle_content')

        if include_content and status == 'completed':
            if cached_subtitle:
                response_data['subtitle_content'] = cached_subtitle

            if not response_data.get('subtitle_content') and subtitle_path and os.path.exists(subtitle_path):
                try:
                    with open(subtitle_path, 'r', encoding='utf-8') as subtitle_file:
                        response_data['subtitle_content'] = subtitle_file.read()
                except Exception as subtitle_error:
                    logger.error("读取字幕文件失败(%s): %s", task_id, subtitle_error)
                    response_data['subtitle_content_error'] = str(subtitle_error)

            if not response_data.get('subtitle_content') and cached_subtitle:
                response_data['subtitle_content'] = cached_subtitle

        return jsonify(response_data)

    except Exception as e:
        logger.error(f"获取处理状态失败: {str(e)}")
        return jsonify({'success': False, 'status': 'error', 'error': str(e)}), 500


@process_bp.route('/status/<task_id>/force-local-readwise', methods=['POST'])
def force_local_readwise(task_id):
    """Force one URL-only task through local subtitles/transcription and full-text Readwise."""
    try:
        task_info = file_service.get_file_info(task_id)
        if not task_info:
            if _wants_json_response():
                return jsonify({'success': False, 'status': 'not_found'}), 404
            flash('处理任务不存在', 'error')
            return redirect(url_for('view.index'))

        if not task_info.get('url') or not task_info.get('platform'):
            message = '任务缺少原始链接或平台，无法本地重发 Readwise。'
            if _wants_json_response():
                return jsonify({'success': False, 'status': 'invalid_task', 'error': message}), 400
            flash(message, 'error')
            return redirect(url_for('view.file_detail', file_id=task_id))

        claim_token = processing_service.claim_force_local_readwise(task_id)
        if not claim_token:
            message = '该任务正在执行本地全文重发，请勿重复提交。'
            if _wants_json_response():
                return jsonify({
                    'success': False,
                    'status': 'already_processing',
                    'error': message,
                }), 409
            flash(message, 'error')
            return redirect(url_for('view.file_detail', file_id=task_id))

        previous_status = task_info.get('status')
        queued_at = datetime.now().isoformat()
        file_service.update_file_info(task_id, {
            'status': 'processing',
            'stage': 'pending',
            'stage_label': '准备本地全文重发',
            'stage_updated_at': queued_at,
            'status_updated_at': queued_at,
            'updated_time': queued_at,
        })

        app = current_app._get_current_object()
        try:
            thread = threading.Thread(
                target=_run_force_local_readwise_with_app_context,
                args=(app, task_id, claim_token),
                daemon=True,
                name=f"readwise-force-local-{task_id}",
            )
            thread.start()
        except Exception:
            processing_service.release_force_local_readwise(task_id, claim_token)
            restored_at = datetime.now().isoformat()
            file_service.update_file_info(task_id, {
                'status': previous_status,
                'status_updated_at': restored_at,
                'updated_time': restored_at,
            })
            raise

        if _wants_json_response():
            return jsonify({
                'success': True,
                'status': 'processing',
                'process_id': task_id,
                'status_url': f"/process/status/{task_id}",
            }), 202

        flash('已开始本地获取字幕/转录并重发 Readwise', 'success')
        return redirect(url_for('view.file_detail', file_id=task_id))

    except Exception as e:
        logger.error(f"启动强制本地Readwise重发失败: {str(e)}")
        if _wants_json_response():
            return jsonify({'success': False, 'status': 'error', 'error': str(e)}), 500
        flash(f'启动强制本地Readwise重发失败: {str(e)}', 'error')
        return redirect(url_for('view.file_detail', file_id=task_id))


@process_bp.route('/status/<task_id>/retry', methods=['POST'])
def retry_interrupted_task(
    task_id,
    request_source='web_retry',
    enforce_auto_safety=False,
):
    """Create one fresh task from an interrupted task and start it immediately."""
    claim_token = None
    new_task_id = None
    try:
        task_info = file_service.get_file_info(task_id)
        if not task_info:
            if _wants_json_response():
                return jsonify({'success': False, 'status': 'not_found'}), 404
            flash('处理任务不存在', 'error')
            return redirect(url_for('view.index'))

        existing_retry_id = task_info.get('retry_task_id')
        if existing_retry_id and file_service.get_file_info(existing_retry_id):
            return _retry_task_response(existing_retry_id, reused=True)

        if task_info.get('status') != 'interrupted':
            message = '只有因服务重启而中断的任务可以重新发起。'
            if _wants_json_response():
                return jsonify({
                    'success': False,
                    'status': 'invalid_task_status',
                    'error': message,
                }), 409
            flash(message, 'error')
            return redirect(url_for('view.file_detail', file_id=task_id))

        if enforce_auto_safety:
            eligible, reason = _auto_retry_eligibility(task_info)
            if not eligible:
                checked_at = datetime.now().isoformat()
                file_service.update_file_info(task_id, {
                    'auto_retry_status': 'skipped',
                    'auto_retry_reason': reason,
                    'auto_retry_checked_at': checked_at,
                    'updated_time': checked_at,
                })
                logger.warning(
                    '跳过服务重启自动续跑: task=%s reason=%s',
                    task_id,
                    reason,
                )
                return jsonify({
                    'success': False,
                    'status': 'auto_retry_skipped',
                    'reason': reason,
                }), 409

        if not task_info.get('url') or not task_info.get('platform'):
            message = '任务缺少原始链接或平台，无法重新发起。'
            if _wants_json_response():
                return jsonify({
                    'success': False,
                    'status': 'invalid_task',
                    'error': message,
                }), 400
            flash(message, 'error')
            return redirect(url_for('view.file_detail', file_id=task_id))

        claim_token = file_service.claim_task_operation(
            task_id,
            RETRY_INTERRUPTED_OPERATION,
            ttl_seconds=60,
        )
        if not claim_token:
            refreshed_task = file_service.get_file_info(task_id) or {}
            existing_retry_id = refreshed_task.get('retry_task_id')
            if existing_retry_id and file_service.get_file_info(existing_retry_id):
                return _retry_task_response(existing_retry_id, reused=True)

            message = '该任务正在重新发起，请勿重复提交。'
            if _wants_json_response():
                return jsonify({
                    'success': False,
                    'status': 'already_processing',
                    'error': message,
                }), 409
            flash(message, 'error')
            return redirect(url_for('view.file_detail', file_id=task_id))

        task_info = file_service.get_file_info(task_id) or task_info
        existing_retry_id = task_info.get('retry_task_id')
        if existing_retry_id and file_service.get_file_info(existing_retry_id):
            return _retry_task_response(existing_retry_id, reused=True)

        now = datetime.now().isoformat()
        new_task_id = str(uuid.uuid4())
        new_task = {
            'id': new_task_id,
            'url': task_info['url'],
            'platform': task_info['platform'],
            'status': 'pending',
            'created_time': now,
            'updated_time': now,
            'retry_of': task_id,
            'retry_root_id': task_info.get('retry_root_id') or task_id,
            'retry_attempt': _retry_attempt(task_info) + 1,
            'request_source': request_source,
            'original_request_source': task_info.get('request_source'),
        }
        for field in RETRY_TASK_COPY_FIELDS:
            if field in task_info:
                new_task[field] = task_info[field]
        new_task['auto_transcribe'] = _task_bool(new_task.get('auto_transcribe'))
        new_task['extract_audio'] = _task_bool(
            new_task.get('extract_audio', True)
        )

        file_service.add_file_info(new_task_id, new_task)
        if not file_service.get_file_info(new_task_id):
            raise RuntimeError('无法保存重新发起的任务')
        source_updates = {
            'retry_task_id': new_task_id,
            'retry_requested_at': now,
            'updated_time': now,
        }
        if request_source == 'auto_restart_retry':
            source_updates.update({
                'auto_retry_status': 'scheduled',
                'auto_retry_reason': _last_progress_stage_code(task_info),
                'auto_retry_checked_at': now,
            })
        file_service.update_file_info(task_id, source_updates)
        persisted_source = file_service.get_file_info(task_id) or {}
        if persisted_source.get('retry_task_id') != new_task_id:
            raise RuntimeError('无法保存原任务与新任务的关联')

        app = current_app._get_current_object()
        thread = threading.Thread(
            target=_run_retried_video_task_with_app_context,
            args=(app, dict(new_task)),
            daemon=True,
            name=f'video-task-retry-{new_task_id}',
        )
        thread.start()

        logger.info('重新发起中断任务: %s -> %s', task_id, new_task_id)
        if not _wants_json_response():
            flash('任务已重新发起', 'success')
        return _retry_task_response(new_task_id)

    except Exception as e:
        if new_task_id:
            file_service.delete_file_info(new_task_id)
            file_service.update_file_info(task_id, {
                'retry_task_id': None,
                'retry_requested_at': None,
                'updated_time': datetime.now().isoformat(),
            })
        logger.error('重新发起中断任务失败(%s): %s', task_id, str(e))
        if _wants_json_response():
            return jsonify({'success': False, 'status': 'error', 'error': str(e)}), 500
        flash(f'重新发起失败: {str(e)}', 'error')
        return redirect(url_for('view.file_detail', file_id=task_id))
    finally:
        if claim_token:
            file_service.release_task_operation(
                task_id,
                RETRY_INTERRUPTED_OPERATION,
                claim_token,
            )


@process_bp.route('/status/<task_id>/language', methods=['POST'])
def confirm_processing_language(task_id):
    """记录语言确认结果，供后台处理线程继续执行."""
    try:
        task_info = file_service.get_file_info(task_id)
        if not task_info:
            return jsonify({'success': False, 'error': 'task_not_found'}), 404

        data = request.get_json(silent=True) or {}
        selected_language = _normalize_language_confirmation_choice(
            data.get('language')
        )
        if selected_language not in {'zh', 'en', 'auto'}:
            return jsonify({'success': False, 'error': 'invalid_language'}), 400

        confirmation = dict(task_info.get('language_confirmation') or {})
        if not confirmation:
            return jsonify({'success': False, 'error': 'language_confirmation_not_required'}), 409

        if task_info.get('status') != 'waiting_for_language_confirmation':
            return jsonify(
                {
                    'success': False,
                    'error': 'task_not_waiting_for_language_confirmation',
                    'status': task_info.get('status'),
                }
            ), 409

        confirmation.update(
            {
                'status': 'confirmed',
                'selected_language': selected_language,
                'resolved_at': datetime.now().isoformat(),
                'resolved_by': data.get('source') or 'telegram',
            }
        )
        file_service.update_file_info(
            task_id,
            {
                'language_confirmation': confirmation,
                'updated_time': datetime.now().isoformat(),
            },
        )
        return jsonify(
            {
                'success': True,
                'process_id': task_id,
                'selected_language': selected_language,
                'language_confirmation': confirmation,
            }
        )
    except Exception as e:
        logger.error(f"记录语言确认失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


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
                subtitle_filename = build_task_filename(
                    os.path.splitext(original_name)[0], file_id
                )
                subtitle_path = file_service.save_file(srt_content, subtitle_filename)
                
                # 更新文件信息
                file_service.update_file_info(file_id, {
                    'status': 'completed',
                    'filename': subtitle_filename,
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
@process_bp.route('/status/<process_id>/subtitle', methods=['GET'])
def process_subtitle_content(process_id: str):
    """返回指定任务的字幕纯文本，用于轮询下载"""
    try:
        task_info = file_service.get_file_info(process_id)
        if not task_info:
            return Response("not found", status=404)

        subtitle_content = task_info.get('subtitle_content')
        if subtitle_content:
            return Response(subtitle_content, status=200, mimetype='text/plain; charset=utf-8')

        subtitle_path = task_info.get('subtitle_path')
        if subtitle_path and os.path.exists(subtitle_path):
            try:
                with open(subtitle_path, 'r', encoding='utf-8') as subtitle_file:
                    data = subtitle_file.read()
                return Response(data, status=200, mimetype='text/plain; charset=utf-8')
            except Exception as file_error:
                logger.error("读取字幕文件失败(%s): %s", process_id, file_error)
                return Response(str(file_error), status=500)

        return Response("pending", status=202)
    except Exception as exc:
        logger.error("获取字幕内容失败(%s): %s", process_id, exc)
        return Response(str(exc), status=500)
