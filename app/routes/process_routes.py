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
        if not data:
            return jsonify({'error': 'Request data is required'}), 400
        
        logger.info("Processing general video request")
        
        # Determine if it's a URL or file
        if 'url' in data:
            url = data['url']
            
            # Determine platform
            platform = 'unknown'
            if 'youtube.com' in url or 'youtu.be' in url:
                platform = 'youtube'
            elif 'bilibili.com' in url:
                platform = 'bilibili'
            elif 'acfun.cn' in url:
                platform = 'acfun'
            
            logger.info(f"Processing {platform} URL: {url}")
            
            # Get services
            video_service = current_app.video_service
            
            # Get video information
            video_info = video_service.get_video_info(url, platform)
            if not video_info:
                return jsonify({'error': f'Failed to get {platform} video information'}), 400
            
            result = {
                'file_id': str(uuid.uuid4()),
                'url': url,
                'platform': platform,
                'video_info': video_info,
                'timestamp': datetime.now().isoformat(),
                'status': 'pending',
                'message': f'{platform.title()} processing not fully implemented in refactored version'
            }
            
        elif 'file_id' in data:
            # Process uploaded file
            file_id = data['file_id']
            logger.info(f"Processing uploaded file: {file_id}")
            
            result = {
                'file_id': file_id,
                'type': 'file',
                'timestamp': datetime.now().isoformat(),
                'status': 'pending',
                'message': 'File processing not fully implemented in refactored version'
            }
        
        else:
            return jsonify({'error': 'URL or file_id is required'}), 400
        
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
        
        return jsonify(result)
    
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