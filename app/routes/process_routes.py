"""Processing routes for video and subtitle handling."""

import os
import json
import logging
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app

logger = logging.getLogger(__name__)

process_bp = Blueprint('process', __name__)


@process_bp.route('/process_youtube', methods=['POST'])
def process_youtube():
    """Process YouTube video URL."""
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({'error': 'URL is required'}), 400
        
        url = data['url']
        logger.info(f"Processing YouTube URL: {url}")
        
        # Get services from app
        video_service = current_app.video_service
        subtitle_service = current_app.subtitle_service
        transcription_service = current_app.transcription_service
        
        # Get video information
        video_info = video_service.get_youtube_info(url)
        if not video_info:
            return jsonify({'error': 'Failed to get video information'}), 400
        
        # Determine processing strategy
        strategy = video_service.get_subtitle_strategy(video_info)
        
        result = {
            'file_id': str(uuid.uuid4()),
            'url': url,
            'video_info': video_info,
            'strategy': strategy,
            'timestamp': datetime.now().isoformat(),
            'status': 'processing'
        }
        
        if strategy == 'direct':
            # Try to download subtitles directly
            logger.info("Attempting direct subtitle extraction")
            # TODO: Implement direct subtitle download
            result['message'] = 'Direct subtitle extraction not yet implemented in refactored version'
            result['status'] = 'pending'
        else:
            # Use transcription
            logger.info("Using transcription approach")
            # TODO: Implement transcription workflow
            result['message'] = 'Transcription workflow not yet implemented in refactored version'
            result['status'] = 'pending'
        
        # Save file info
        files_info_path = os.path.join(current_app.config['UPLOAD_FOLDER'], 'files_info.json')
        
        if os.path.exists(files_info_path):
            with open(files_info_path, 'r', encoding='utf-8') as f:
                files_info = json.load(f)
        else:
            files_info = {}
        
        files_info[result['file_id']] = result
        
        with open(files_info_path, 'w', encoding='utf-8') as f:
            json.dump(files_info, f, ensure_ascii=False, indent=2)
        
        logger.info(f"YouTube processing initiated for: {video_info.get('title', 'Unknown')}")
        
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"Error processing YouTube URL: {str(e)}")
        return jsonify({'error': 'Processing failed'}), 500


@process_bp.route('/process', methods=['POST'])
def process_general():
    """Process general video URL or file."""
    try:
        data = request.get_json()
        url = data.get('url')
        platform = data.get('platform', 'youtube')  # 默认为YouTube
        location = data.get('location', 'new')  # 默认为new
        tags = data.get('tags', [])  # 获取tags参数，默认为空列表
        hotwords = data.get('hotwords', [])  # 获取hotwords参数，默认为空列表
        
        # Log the request with location, tags and hotwords
        logger.info(f'处理{platform} URL: {url}, location: {location}, tags: {tags}, hotwords: {hotwords}')
        
        if not url or not platform:
            return jsonify({'error': '缺少必要参数'}), 400
        
        # Get services from app
        video_service = current_app.video_service
        subtitle_service = current_app.subtitle_service
        transcription_service = current_app.transcription_service
        translation_service = current_app.translation_service
        readwise_service = current_app.readwise_service
        
        # 获取视频信息
        video_info = video_service.get_video_info(url, platform)
        if not video_info:
            return jsonify({'error': f'Failed to get {platform} video information'}), 400
        
        # 检测视频语言
        logger.info("准备调用 get_video_language 函数...")
        try:
            language = video_service.get_video_language(video_info)
            logger.info(f"get_video_language 返回结果: {language}")
        except Exception as e:
            logger.error(f"get_video_language 函数出错: {str(e)}")
            language = None
            
        # 确定字幕策略
        should_download, lang_priority = video_service.get_subtitle_strategy(language, video_info)
        logger.info(f"字幕策略: should_download={should_download}, lang_priority={lang_priority}")
        
        # 下载字幕
        subtitle_result = video_service.download_subtitles(url, platform, video_info) if should_download else None
        subtitle_content, video_info = subtitle_result if isinstance(subtitle_result, tuple) else (subtitle_result, video_info)
        
        if not subtitle_content:
            # 如果没有字幕，尝试下载视频并转录
            logger.info("未找到字幕，尝试下载视频并转录...")
            audio_path = video_service.download_video(url)
            if not audio_path:
                return jsonify({"error": "下载视频失败", "success": False}), 500
                
            # 使用FunASR转录
            result = transcription_service.transcribe_audio(audio_path, hotwords)
            if not result:
                return jsonify({"error": "转录失败", "success": False}), 500
                
            # 解析转录结果生成字幕
            subtitles = subtitle_service.parse_srt(result, hotwords)
            if not subtitles:
                return jsonify({"error": "解析转录结果失败", "success": False}), 500
                
            # 生成SRT格式内容
            srt_content = ""
            for i, subtitle in enumerate(subtitles, 1):
                start_time = subtitle_service.format_time(subtitle['start'])
                end_time = subtitle_service.format_time(subtitle['start'] + subtitle['duration'])
                text = subtitle['text']
                # 如果是英文视频，添加翻译
                if language == 'en':
                    translation = translation_service.translate_text(text)
                    text = f"{text}\n{translation}"
                srt_content += f"{i}\n{start_time} --> {end_time}\n{text}\n\n"
            
            # 保存字幕文件
            title = video_info.get('title', '') if video_info else ''
            if not title:
                title = f"{os.path.splitext(os.path.basename(url))[0]}"
                
            output_filename = subtitle_service.sanitize_filename(f"{title}.srt")
            output_filepath = os.path.join(current_app.config['OUTPUT_FOLDER'], output_filename)
            with open(output_filepath, 'w', encoding='utf-8') as f:
                f.write(srt_content)
            
            logger.info(f"转录结果已保存到: {output_filepath}")
            
            # 删除临时音频文件
            try:
                os.remove(audio_path)
                logger.info(f"已删除临时文件: {audio_path}")
            except Exception as e:
                logger.warning(f"删除临时文件失败: {str(e)}")
                
            # 生成文件信息并保存
            file_id = str(uuid.uuid4())
            file_info = {
                'id': file_id,
                'filename': output_filename,
                'path': output_filepath,
                'url': f'/view/{file_id}',
                'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'show_timeline': True,
                'source': 'transcription',
                'video_info': video_info
            }
            
            # Save file info
            files_info_path = os.path.join(current_app.config['UPLOAD_FOLDER'], 'files_info.json')
            if os.path.exists(files_info_path):
                with open(files_info_path, 'r', encoding='utf-8') as f:
                    files_info = json.load(f)
            else:
                files_info = {}
            
            files_info[file_id] = file_info
            
            with open(files_info_path, 'w', encoding='utf-8') as f:
                json.dump(files_info, f, ensure_ascii=False, indent=2)
            
            # 发送到Readwise
            try:
                if video_info:
                    readwise_service.save_to_readwise(
                        title=video_info.get('title', 'Video Transcript'),
                        content=srt_content,
                        url=url,
                        published_date=video_info.get('published_date'),
                        author=video_info.get('uploader'),
                        tags=tags,
                        language=language,
                        location=location
                    )
                    logger.info("成功发送转录内容到Readwise")
            except Exception as e:
                logger.error(f"发送转录内容到Readwise失败: {str(e)}")
                
            return jsonify({
                'success': True,
                'video_info': video_info,
                'subtitle_content': srt_content,
                'filename': file_info['filename'],
                'view_url': file_info['url'],
                'source': 'transcription'
            })
            
        # 如果有字幕，根据平台确定字幕格式并转换
        if platform == 'youtube':
            # 检查内容是否已经是SRT格式
            if subtitle_content and subtitle_content.strip().split('\n')[0].isdigit():
                logger.info("字幕内容已经是SRT格式")
                srt_content = subtitle_content
            else:
                logger.info("尝试将字幕转换为SRT格式")
                srt_content = subtitle_service.convert_to_srt(subtitle_content, 'json3')
        elif platform == 'bilibili':
            # 检查内容是否已经是SRT格式
            if subtitle_content and subtitle_content.strip().split('\n')[0].isdigit():
                logger.info("字幕内容已经是SRT格式")
                srt_content = subtitle_content
            else:
                logger.info("尝试将字幕转换为SRT格式")
                srt_content = subtitle_service.convert_to_srt(subtitle_content, 'json3')
        elif platform == 'acfun':
            # 检查内容是否已经是SRT格式
            if subtitle_content and subtitle_content.strip().split('\n')[0].isdigit():
                logger.info("字幕内容已经是SRT格式")
                srt_content = subtitle_content
            else:
                logger.info("尝试将字幕转换为SRT格式")
                srt_content = subtitle_service.convert_to_srt(subtitle_content, 'json3')
        else:
            return jsonify({'error': '不支持的平台'}), 400
            
        if not srt_content:
            return jsonify({'error': '转换字幕失败'}), 500
            
        # 解析字幕内容为列表格式
        subtitles = subtitle_service.parse_srt_content(srt_content)
        if not subtitles:
            return jsonify({'error': '解析字幕失败'}), 500
            
        # 保存字幕文件
        title = video_info.get('title', '') if video_info else ''
        if not title:
            title = f"{os.path.splitext(os.path.basename(url))[0]}"
                
        output_filename = subtitle_service.sanitize_filename(f"{title}.srt")
        output_filepath = os.path.join(current_app.config['OUTPUT_FOLDER'], output_filename)
        with open(output_filepath, 'w', encoding='utf-8') as f:
            f.write(srt_content)
            
        logger.info(f"字幕文件已保存到: {output_filepath}")
        
        # 生成文件信息并保存
        file_id = str(uuid.uuid4())
        file_info = {
            'id': file_id,
            'filename': output_filename,
            'path': output_filepath,
            'url': f'/view/{file_id}',
            'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'show_timeline': True,
            'source': 'subtitle',
            'video_info': video_info
        }
        
        # Save file info
        files_info_path = os.path.join(current_app.config['UPLOAD_FOLDER'], 'files_info.json')
        if os.path.exists(files_info_path):
            with open(files_info_path, 'r', encoding='utf-8') as f:
                files_info = json.load(f)
        else:
            files_info = {}
        
        files_info[file_id] = file_info
        
        with open(files_info_path, 'w', encoding='utf-8') as f:
            json.dump(files_info, f, ensure_ascii=False, indent=2)
        
        # 发送到Readwise
        try:
            # 将字幕列表转换为纯文本
            subtitle_text = []
            for subtitle in subtitles:
                if 'text' in subtitle:
                    subtitle_text.append(subtitle['text'])
            content = '\n'.join(subtitle_text)
            
            # 记录原始内容长度
            logger.info(f"原始内容长度: {len(content)}")
            logger.info(f"作者信息: {video_info.get('uploader')}")
            
            # 将内容转换为HTML格式
            content_with_br = content.replace('\n', '<br>')
            html_content = f'<div class="content">{content_with_br}</div>'
            
            # 准备视频信息
            video_title = video_info.get('title', 'Video Transcript') if video_info else title
            video_url = url
            video_author = video_info.get('uploader') if video_info else None
            video_date = video_info.get('published_date') if video_info else None
            
            logger.info(f"正在发送内容到Readwise: {video_title}")
            
            # 发送到Readwise
            success = readwise_service.save_to_readwise(
                title=video_title,
                content=html_content,
                url=video_url,
                published_date=video_date,
                author=video_author,
                location=location,
                tags=tags,
                language=language
            )
            
            if success:
                logger.info("成功发送到Readwise")
            else:
                logger.error("发送到Readwise失败")
            
        except Exception as e:
            logger.error(f"发送到Readwise失败: {str(e)}")
        
        # 返回结果
        return jsonify({
            'success': True,
            'video_info': video_info,
            'subtitle_content': srt_content,
            'filename': file_info['filename'],
            'view_url': file_info['url'],
            'source': 'subtitle'
        })
        
    except Exception as e:
        logger.error(f"Error processing general request: {str(e)}")
        return jsonify({'error': 'Processing failed'}), 500


@process_bp.route('/status/<file_id>')
def get_status(file_id):
    """Get processing status for a file."""
    try:
        logger.info(f"Getting status for file: {file_id}")
        
        # Load files info
        files_info_path = os.path.join(current_app.config['UPLOAD_FOLDER'], 'files_info.json')
        
        if os.path.exists(files_info_path):
            with open(files_info_path, 'r', encoding='utf-8') as f:
                files_info = json.load(f)
        else:
            files_info = {}
        
        if file_id not in files_info:
            return jsonify({'error': 'File not found'}), 404
        
        file_info = files_info[file_id]
        
        return jsonify({
            'file_id': file_id,
            'status': file_info.get('status', 'unknown'),
            'message': file_info.get('message', ''),
            'last_updated': file_info.get('timestamp', '')
        })
    
    except Exception as e:
        logger.error(f"Error getting status for {file_id}: {str(e)}")
        return jsonify({'error': 'Failed to get status'}), 500