"""View routes for displaying content."""

import os
import json
import logging
from flask import Blueprint, request, jsonify, render_template, current_app

logger = logging.getLogger(__name__)

view_bp = Blueprint('view', __name__)


@view_bp.route('/')
def index():
    """Main page."""
    return "Subtitle Processing Service - Refactored Version"


@view_bp.route('/health')
def health():
    """Health check endpoint."""
    try:
        return jsonify({
            'status': 'healthy',
            'message': 'Subtitle processing service is running',
            'version': 'refactored'
        })
    except Exception as e:
        logger.error(f"Health check error: {str(e)}")
        return jsonify({'error': 'Health check failed'}), 500


@view_bp.route('/view/')
def list_files():
    """List all processed files."""
    try:
        logger.info("Listing processed files")
        
        # Load files info
        files_info_path = os.path.join(current_app.config['UPLOAD_FOLDER'], 'files_info.json')
        
        if os.path.exists(files_info_path):
            with open(files_info_path, 'r', encoding='utf-8') as f:
                files_info = json.load(f)
        else:
            files_info = {}
        
        return jsonify({
            'files': files_info,
            'count': len(files_info)
        })
    
    except Exception as e:
        logger.error(f"Error listing files: {str(e)}")
        return jsonify({'error': 'Failed to list files'}), 500


@view_bp.route('/view/<file_id>')
def view_file(file_id):
    """View specific file content."""
    try:
        logger.info(f"Viewing file: {file_id}")
        
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
            'file_info': file_info
        })
    
    except Exception as e:
        logger.error(f"Error viewing file {file_id}: {str(e)}")
        return jsonify({'error': 'Failed to view file'}), 500