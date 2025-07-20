"""Main Flask application factory and configuration."""

import os
import json
import logging
from flask import Flask
from flask_cors import CORS

from .config.config_manager import get_config_manager
from .utils.logging_utils import setup_logging
from .services import (
    SubtitleService, VideoService, TranscriptionService, 
    TranslationService, ReadwiseService
)

# Setup logging
logger = setup_logging()


def create_app():
    """Create and configure Flask application."""
    app = Flask(__name__)
    
    # Load configuration
    config_manager = get_config_manager()
    
    # Configure Flask app
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max-limit
    app.config['UPLOAD_FOLDER'] = get_config_value('app.upload_folder', '/app/uploads')
    app.config['OUTPUT_FOLDER'] = get_config_value('app.output_folder', '/app/outputs')
    
    # Ensure directories exist
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)
    
    # Configure CORS
    CORS(app, resources={
        r"/upload": {"origins": "*"},
        r"/view/*": {"origins": "*"},
        r"/process_youtube": {"origins": "*"},
        r"/process": {"origins": "*"}
    })
    
    # Initialize services
    app.subtitle_service = SubtitleService()
    app.video_service = VideoService()
    app.transcription_service = TranscriptionService()
    app.translation_service = TranslationService()
    app.readwise_service = ReadwiseService()
    
    # Register blueprints
    register_blueprints(app)
    
    logger.info("Flask application created and configured")
    return app


def register_blueprints(app):
    """Register Flask blueprints."""
    try:
        from .routes import upload_bp, view_bp, process_bp
        
        app.register_blueprint(upload_bp)
        app.register_blueprint(view_bp)
        app.register_blueprint(process_bp)
        
        logger.info("Blueprints registered successfully")
    except ImportError as e:
        logger.warning(f"Could not import blueprints: {str(e)}")
        # Fallback to simple routes
        register_simple_routes(app)


def register_simple_routes(app):
    """Register simple routes as fallback."""
    from flask import request, jsonify, render_template
    
    @app.route('/')
    def index():
        return "Subtitle Processing Service - Refactored Version"
    
    @app.route('/health')
    def health():
        return jsonify({
            'status': 'healthy',
            'message': 'Subtitle processing service is running',
            'services': {
                'subtitle': 'available',
                'video': 'available', 
                'transcription': 'available',
                'translation': 'available',
                'readwise': 'configured' if app.readwise_service.is_configured() else 'not configured'
            }
        })
    
    logger.info("Simple routes registered as fallback")


def get_config_value(key_path, default=None):
    """Get configuration value using dot-separated path."""
    from .config.config_manager import get_config_value as _get_config_value
    return _get_config_value(key_path, default)


# Create application instance
app = create_app()


if __name__ == '__main__':
    # Development server
    debug_mode = os.environ.get('FLASK_ENV') == 'development'
    app.run(host='0.0.0.0', port=5000, debug=debug_mode)