"""Upload routes for file and URL processing."""

import os
import uuid
import json
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
        # 获取URL (支持JSON和表单数据)
        if request.is_json:
            data = request.get_json()
            url = data.get('url', '').strip()
            extract_audio = data.get('extract_audio', True)
            auto_transcribe = data.get('auto_transcribe', False)
            auto_start = data.get('auto_start', True)  # 默认自动开始处理
            tags = data.get('tags', [])  # 获取用户指定的标签
        else:
            url = request.form.get('url', '').strip()
            extract_audio = request.form.get('extract_audio', 'false').lower() == 'true'
            auto_transcribe = request.form.get('auto_transcribe', 'false').lower() == 'true'
            auto_start = request.form.get('auto_start', 'false').lower() == 'true'
            tags = request.form.get('tags', '').split(',') if request.form.get('tags') else []  # 表单数据中的标签
        
        if not url:
            if request.is_json:
                return jsonify({'error': '请输入视频URL'}), 400
            flash('请输入视频URL', 'error')
            return redirect(request.url)
        
        # 检测平台
        platform = _detect_platform(url)
        if not platform:
            if request.is_json:
                return jsonify({'error': '不支持的视频平台'}), 400
            flash('不支持的视频平台', 'error')
            return redirect(request.url)
        
        # 生成处理ID
        process_id = str(uuid.uuid4())
        
        # 清理标签（移除空标签）
        tags = [tag.strip() for tag in tags if tag.strip()] if tags else []
        
        # 创建处理任务信息
        task_info = {
            'id': process_id,
            'url': url,
            'platform': platform,
            'tags': tags,  # 保存用户指定的标签
            'status': 'pending',
            'created_time': datetime.now().isoformat(),
            'updated_time': datetime.now().isoformat()
        }
        
        # 保存任务信息
        file_service.add_file_info(process_id, task_info)
        
        logger.info(f"URL处理任务创建: {url} -> {process_id}")
        logger.info(f"自动启动设置: {auto_start}")
        logger.info(f"用户标签: {tags}")
        print(f"DEBUG: auto_start = {auto_start}, type = {type(auto_start)}")
        print(f"DEBUG: user_tags = {tags}")
        
        # 如果设置了自动启动，立即开始处理
        if auto_start:
            print(f"=== 开始自动视频处理流程 ===")
            print(f"处理ID: {process_id}")
            print(f"视频URL: {url}")
            print(f"平台: {platform}")
            logger.info(f"=== 开始自动视频处理流程 === {process_id}")
            logger.info(f"处理ID: {process_id}")
            logger.info(f"视频URL: {url}")
            logger.info(f"平台: {platform}")
            print("DEBUG: 进入自动启动分支")
            try:
                logger.info(f"第1步：开始视频下载和预处理")
                # 调用视频处理服务
                result = video_service.process_video_for_transcription(
                    url=url,
                    platform=platform
                )
                logger.info(f"第1步完成：视频处理结果存在: {result is not None}")
                
                # 更新任务状态
                if result:
                    task_info['video_info'] = result.get('video_info', {})
                    task_info['language'] = result.get('language')
                    task_info['subtitle_content'] = result.get('subtitle_content')
                    task_info['audio_file'] = result.get('audio_file')
                    task_info['needs_transcription'] = result.get('needs_transcription', False)
                    task_info['updated_time'] = datetime.now().isoformat()
                    
                    # 调试输出
                    logger.info(f"视频处理结果 - subtitle_content存在: {bool(result.get('subtitle_content'))}")
                    logger.info(f"视频处理结果 - needs_transcription: {result.get('needs_transcription')}")
                    logger.info(f"视频处理结果 - audio_file: {result.get('audio_file')}")
                    
                    # 如果有字幕内容，直接完成
                    if result.get('subtitle_content'):
                        task_info['status'] = 'completed'
                        task_info['progress'] = 100
                        logger.info(f"第2步完成：视频已有字幕，无需转录: {process_id}")
                        
                        # 发送到Readwise Reader
                        logger.info(f"第3步：开始发送内容到Readwise Reader: {process_id}")
                        try:
                            readwise_result = readwise_service.create_article_from_subtitle(task_info)
                            if readwise_result:
                                task_info['readwise_article_id'] = readwise_result.get('id')
                                task_info['readwise_url'] = readwise_result.get('url')
                                logger.info(f"第3步完成：Readwise文章创建成功: {process_id} -> {readwise_result.get('id')}")
                            else:
                                logger.warning(f"第3步失败：Readwise文章创建失败: {process_id}")
                        except Exception as e:
                            logger.error(f"第3步错误：发送到Readwise失败: {process_id} - {str(e)}")
                        
                        logger.info(f"=== 视频处理流程完成 === {process_id}")
                    # 如果需要转录且有音频文件，进行转录
                    elif result.get('needs_transcription') and result.get('audio_file'):
                        logger.info(f"第2步：开始音频转录流程: {process_id}")
                        logger.info(f"needs_transcription: {result.get('needs_transcription')}")
                        logger.info(f"audio_file: {result.get('audio_file')}")
                        audio_file = result.get('audio_file')
                        try:
                            # 进行音频转录
                            logger.info(f"第2.1步：调用转录服务，音频文件: {audio_file}")
                            logger.info(f"音频文件是否存在: {os.path.exists(audio_file)}")
                            transcription_result = transcription_service.transcribe_audio(
                                audio_file=audio_file,
                                hotwords=None,
                                video_info=task_info.get('video_info', {}),
                                tags=task_info.get('tags', []),
                                platform=platform
                            )
                            logger.info(f"第2.1步完成：转录结果是否为None: {transcription_result is None}")
                            
                            if transcription_result is None:
                                logger.error("第2.1步失败：转录结果为None，转录失败")
                            else:
                                logger.info(f"转录数据类型: {type(transcription_result)}")
                                # 仅显示text字段内容和长度，避免过长的日志
                                if isinstance(transcription_result, dict) and 'text' in transcription_result:
                                    text_length = len(transcription_result['text']) if transcription_result['text'] else 0
                                    text_preview = transcription_result['text'][:100] + "..." if text_length > 100 else transcription_result['text']
                                    logger.info(f"转录文本长度: {text_length}")
                                    logger.info(f"转录文本预览: '{text_preview}'")
                                
                                # 转换为SRT格式前的调试
                                logger.info("第2.2步：开始转换为SRT格式")
                                srt_content = subtitle_service.parse_srt(transcription_result, [])
                                logger.info(f"第2.2步完成：SRT转换结果是否为None: {srt_content is None}")
                                if srt_content:
                                    srt_length = len(srt_content)
                                    logger.info(f"SRT内容长度: {srt_length}")
                                    # 计算字幕条数
                                    subtitle_count = srt_content.count('\n\n') + 1 if srt_content else 0
                                    logger.info(f"生成字幕条数: {subtitle_count}")
                                else:
                                    logger.error("第2.2步失败：SRT转换结果为None或空")
                                
                                if srt_content:
                                    task_info['status'] = 'completed'
                                    task_info['subtitle_content'] = srt_content
                                    task_info['transcription_result'] = transcription_result
                                    task_info['progress'] = 100
                                    logger.info(f"第2步完成：音频转录和SRT转换成功: {process_id}")
                                    
                                    # 发送到Readwise Reader
                                    logger.info(f"第3步：开始发送内容到Readwise Reader: {process_id}")
                                    try:
                                        readwise_result = readwise_service.create_article_from_subtitle(task_info)
                                        if readwise_result:
                                            task_info['readwise_article_id'] = readwise_result.get('id')
                                            task_info['readwise_url'] = readwise_result.get('url')
                                            logger.info(f"第3步完成：Readwise文章创建成功: {process_id} -> {readwise_result.get('id')}")
                                        else:
                                            logger.warning(f"第3步失败：Readwise文章创建失败: {process_id}")
                                    except Exception as e:
                                        logger.error(f"第3步错误：发送到Readwise失败: {process_id} - {str(e)}")
                                    
                                    logger.info(f"=== 视频处理流程完成 === {process_id}")
                                else:
                                    task_info['status'] = 'failed'
                                    task_info['error'] = 'SRT转换失败'
                                    logger.error(f"第2.2步失败：SRT转换失败: {process_id}")
                            
                            if transcription_result is None:
                                task_info['status'] = 'failed'
                                task_info['error'] = '音频转录失败'
                                logger.error(f"第2步失败：音频转录失败: {process_id}")
                        except Exception as e:
                            task_info['status'] = 'failed'
                            task_info['error'] = f'转录出错: {str(e)}'
                            logger.error(f"第2步错误：转录出错: {process_id} - {str(e)}")
                    else:
                        # 如果转录失败，至少提供基本的视频信息
                        task_info['status'] = 'completed'
                        task_info['progress'] = 100
                        task_info['subtitle_content'] = f"视频标题: {task_info.get('video_info', {}).get('title', '未知')}\n视频链接: {url}\n\n注意：音频转录暂时不可用，请手动处理字幕。"
                        logger.warning(f"第2步跳过：转录不可用，返回基本信息: {process_id}")
                        
                        # 尝试发送基本信息到Readwise
                        logger.info(f"第3步：发送基本视频信息到Readwise Reader: {process_id}")
                        try:
                            readwise_result = readwise_service.create_article_from_subtitle(task_info)
                            if readwise_result:
                                task_info['readwise_article_id'] = readwise_result.get('id')
                                task_info['readwise_url'] = readwise_result.get('url')
                                logger.info(f"第3步完成：Readwise文章(基本信息)创建成功: {process_id} -> {readwise_result.get('id')}")
                            else:
                                logger.warning(f"第3步失败：Readwise文章(基本信息)创建失败: {process_id}")
                        except Exception as e:
                            logger.error(f"第3步错误：发送基本信息到Readwise失败: {process_id} - {str(e)}")
                        
                        logger.info(f"=== 视频处理流程完成(仅基本信息) === {process_id}")
                else:
                    task_info['status'] = 'failed'
                    task_info['error'] = '视频处理失败'
                    task_info['updated_time'] = datetime.now().isoformat()
                    logger.error(f"第1步失败：视频处理失败: {process_id}")
                
                file_service.update_file_info(process_id, task_info)
                
            except Exception as e:
                logger.error(f"=== 视频处理流程出错 === {process_id} - {str(e)}")
                task_info['status'] = 'failed'
                task_info['error'] = str(e)
                file_service.update_file_info(process_id, task_info)
        
        # 根据请求类型返回不同响应
        if request.is_json:
            response_data = {
                'success': True,
                'process_id': process_id,
                'message': f'视频处理任务已创建，auto_start={auto_start}',
                'status_url': f'/process/video/{process_id}',
                'platform': platform
            }
            if auto_start:
                # 如果自动启动且处理完成，返回字幕内容
                if task_info.get('status') == 'completed' and task_info.get('subtitle_content'):
                    response_data['message'] = '视频处理已完成'
                    response_data['subtitle_content'] = task_info['subtitle_content']
                    response_data['video_info'] = task_info.get('video_info', {})
                elif task_info.get('status') == 'failed':
                    response_data['success'] = False
                    response_data['message'] = task_info.get('error', '视频处理失败')
                else:
                    response_data['message'] = '视频处理已开始'
                response_data['auto_started'] = True
            return jsonify(response_data)
        else:
            flash(f'视频处理任务已创建', 'success')
            return redirect(url_for('process.process_video', process_id=process_id))
        
    except Exception as e:
        logger.error(f"URL处理失败: {str(e)}")
        if request.is_json:
            return jsonify({'error': f'URL处理失败: {str(e)}'}), 500
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