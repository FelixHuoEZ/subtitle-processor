"""Upload routes for file handling."""

import os
import json
import logging
from flask import Blueprint, request, jsonify, current_app
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

upload_bp = Blueprint('upload', __name__)


@upload_bp.route('/upload', methods=['POST'])
def upload_file():
    """Handle file upload."""
    try:
        logger.info("Processing file upload request")
        
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        if file:
            # Secure the filename
            filename = secure_filename(file.filename)
            if not filename:
                filename = 'uploaded_file'
            
            # Save file
            file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            
            logger.info(f"File uploaded successfully: {filename}")
            
            return jsonify({
                'message': 'File uploaded successfully',
                'filename': filename,
                'file_path': file_path
            })
    
    except Exception as e:
        logger.error(f"Error uploading file: {str(e)}")
        return jsonify({'error': 'Upload failed'}), 500