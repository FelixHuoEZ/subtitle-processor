"""Main Flask application factory for the subtitle processing service."""

import os
import logging
from flask import Flask, render_template, jsonify, request, redirect
from .config.config_manager import ConfigManager, get_config_value
from .services.logging_service import LoggingService
from .services.file_service import FileService
from .services.video_service import VideoService
from .services.transcription_service import TranscriptionService
from .services.subtitle_service import SubtitleService
from .services.translation_service import TranslationService
from .services.readwise_service import ReadwiseService
from .routes import upload_bp, view_bp, process_bp, settings_bp

logger = logging.getLogger(__name__)


def create_app(config_path=None):
    """创建Flask应用实例
    
    Args:
        config_path: 配置文件路径，可选
        
    Returns:
        Flask: 配置好的Flask应用实例
    """
    app = Flask(__name__)
    
    # 初始化配置管理器
    config_manager = ConfigManager()
    
    # 初始化日志服务
    logging_service = LoggingService()
    
    # 设置根日志级别为DEBUG，确保所有模块的日志都能输出
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # 创建控制台处理器确保所有日志都输出到控制台
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    logger.info("启动字幕处理服务应用")
    
    # 配置Flask应用
    _configure_app(app, config_manager)
    
    # 初始化服务
    _initialize_services(app)
    
    # 注册蓝图
    _register_blueprints(app)
    
    # 注册错误处理器
    _register_error_handlers(app)
    
    # 注册上下文处理器
    _register_context_processors(app)
    
    # 注册主要路由
    register_main_routes(app)
    
    logger.info("应用初始化完成")
    return app


def _configure_app(app, config_manager):
    """配置Flask应用"""
    try:
        # 基本配置
        app.config['SECRET_KEY'] = get_config_value('app.secret_key', 'dev-secret-key-change-in-production')
        app.config['MAX_CONTENT_LENGTH'] = get_config_value('app.max_file_size', 500 * 1024 * 1024)  # 500MB
        
        # 文件上传配置
        upload_folder = get_config_value('app.upload_folder', '/app/uploads')
        output_folder = get_config_value('app.output_folder', '/app/outputs')
        
        # 确保目录存在
        os.makedirs(upload_folder, exist_ok=True)
        os.makedirs(output_folder, exist_ok=True)
        
        app.config['UPLOAD_FOLDER'] = upload_folder
        app.config['OUTPUT_FOLDER'] = output_folder
        
        # 模板配置
        app.config['TEMPLATES_AUTO_RELOAD'] = get_config_value('app.debug', False)
        
        # 存储配置管理器实例
        app.config_manager = config_manager
        
        logger.info("Flask应用配置完成")
        
    except Exception as e:
        logger.error(f"配置Flask应用失败: {str(e)}")
        raise


def _initialize_services(app):
    """初始化所有服务"""
    try:
        # 初始化服务实例
        app.file_service = FileService()
        app.video_service = VideoService()
        app.transcription_service = TranscriptionService()
        app.subtitle_service = SubtitleService()
        app.translation_service = TranslationService()
        app.readwise_service = ReadwiseService()
        
        logger.info("所有服务初始化完成")
        
    except Exception as e:
        logger.error(f"初始化服务失败: {str(e)}")
        raise


def _register_blueprints(app):
    """注册Flask蓝图"""
    try:
        # 注册路由蓝图
        app.register_blueprint(upload_bp)
        app.register_blueprint(view_bp)
        app.register_blueprint(process_bp)
        app.register_blueprint(settings_bp)
        
        logger.info("所有蓝图注册完成")
        
    except Exception as e:
        logger.error(f"注册蓝图失败: {str(e)}")
        raise


def _register_error_handlers(app):
    """注册错误处理器"""
    
    @app.errorhandler(404)
    def not_found_error(error):
        """404错误处理"""
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Not found'}), 404
        return render_template('errors/404.html'), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        """500错误处理"""
        logger.error(f"Internal server error: {error}")
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Internal server error'}), 500
        return render_template('errors/500.html'), 500
    
    @app.errorhandler(413)
    def too_large(error):
        """文件过大错误处理"""
        logger.warning(f"File too large: {error}")
        if request.path.startswith('/api/'):
            return jsonify({'error': 'File too large'}), 413
        return render_template('errors/413.html'), 413
    
    logger.info("错误处理器注册完成")


def _register_context_processors(app):
    """注册上下文处理器"""
    
    @app.context_processor
    def inject_config():
        """注入配置到模板上下文"""
        return {
            'app_name': get_config_value('app.name', 'Subtitle Processor'),
            'app_version': get_config_value('app.version', '2.0.0'),
            'debug_mode': get_config_value('app.debug', False),
        }
    
    @app.context_processor
    def inject_services():
        """注入服务状态到模板上下文"""
        return {
            'readwise_enabled': app.readwise_service.enabled,
            'transcription_available': app.transcription_service._check_funasr_service(),
        }
    
    logger.info("上下文处理器注册完成")


def register_main_routes(app):
    """注册主要路由"""
    
    @app.route('/')
    def index():
        """首页"""
        return render_template('index.html')

    @app.route('/health')
    def health_check():
        """健康检查接口"""
        try:
            from datetime import datetime
            
            # 检查各服务状态
            services_status = {
                'file_service': True,  # 文件服务总是可用
                'video_service': True,  # 视频服务总是可用
                'transcription_service': app.transcription_service._check_funasr_service(),
                'translation_service': True,  # 翻译服务总是可用（至少有一个方法可用）
                'readwise_service': app.readwise_service.enabled
            }
            
            # 检查配置
            config_status = {
                'config_loaded': True,
                'upload_folder_exists': os.path.exists(get_config_value('app.upload_folder', '/app/uploads')),
                'output_folder_exists': os.path.exists(get_config_value('app.output_folder', '/app/outputs'))
            }
            
            # 总体状态
            overall_status = all(config_status.values())
            
            return jsonify({
                'status': 'healthy' if overall_status else 'degraded',
                'services': services_status,
                'config': config_status,
                'timestamp': str(datetime.now())
            }), 200 if overall_status else 503
            
        except Exception as e:
            logger.error(f"健康检查失败: {str(e)}")
            return jsonify({
                'status': 'unhealthy',
                'error': str(e),
                'timestamp': str(datetime.now())
            }), 503

    @app.route('/api/info')
    def api_info():
        """API信息接口"""
        try:
            return jsonify({
                'name': get_config_value('app.name', 'Subtitle Processor'),
                'version': get_config_value('app.version', '2.0.0'),
                'description': 'A comprehensive subtitle processing service',
                'supported_platforms': ['youtube', 'bilibili', 'acfun'],
                'supported_audio_formats': app.transcription_service.get_supported_formats(),
                'supported_languages': list(app.translation_service.get_supported_languages().keys()),
                'endpoints': {
                    'upload_file': '/upload/',
                    'upload_url': '/upload/url',
                    'batch_upload': '/upload/batch',
                    'file_list': '/view/',
                    'file_detail': '/view/<file_id>',
                    'process_video': '/process/video/<process_id>',
                    'transcribe_audio': '/process/audio/<file_id>',
                    'translate_subtitle': '/process/translate/<file_id>',
                    'create_readwise': '/process/readwise/<file_id>'
                }
            })
            
        except Exception as e:
            logger.error(f"获取API信息失败: {str(e)}")
            return jsonify({'error': str(e)}), 500

    @app.route('/process', methods=['GET', 'POST', 'OPTIONS'])
    def process_main():
        """处理主路由 - 重定向到 /process/"""
        if request.method == 'OPTIONS':
            # 处理 CORS 预检请求
            response = jsonify({'status': 'ok'})
            response.headers.add('Access-Control-Allow-Origin', '*')
            response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
            response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
            return response
        elif request.method == 'POST':
            # POST请求：检查是否包含视频URL，如果是则处理，否则返回端点信息
            if request.is_json:
                data = request.get_json()
                logger.info(f"收到POST请求数据: {data}")
                if data and 'url' in data:
                    logger.info(f"转发到upload/url端点: {data.get('url')}")
                    # 转发到upload/url端点处理
                    from flask import current_app
                    with current_app.test_request_context('/upload/url', method='POST', json=data, headers={'Content-Type': 'application/json'}):
                        from .routes.upload_routes import upload_url
                        return upload_url()
                        
            # 如果不包含URL，返回处理信息
            return jsonify({
                'service': 'Video and Audio Processing Service',
                'message': 'Use specific endpoints for processing',
                'endpoints': {
                    'video_processing': '/process/video/<process_id>',
                    'audio_transcription': '/process/audio/<file_id>',
                    'subtitle_translation': '/process/translate/<file_id>',
                    'readwise_creation': '/process/readwise/<file_id>',
                    'status_check': '/process/status/<task_id>',
                    'upload_url': '/upload/url'
                },
                'status': 'ready'
            })
        else:
            # GET请求重定向到带斜杠的版本
            return redirect('/process/', code=301)


def main():
    """主函数 - 用于直接运行应用"""
    import argparse
    from datetime import datetime
    
    parser = argparse.ArgumentParser(description='Subtitle Processing Service')
    parser.add_argument('--config', '-c', help='配置文件路径')
    parser.add_argument('--host', default='0.0.0.0', help='绑定主机地址')
    parser.add_argument('--port', type=int, default=5000, help='端口号')
    parser.add_argument('--debug', action='store_true', help='调试模式')
    
    args = parser.parse_args()
    
    # 创建应用
    app = create_app(args.config)
    
    # 运行应用
    logger.info(f"启动字幕处理服务 - 地址: {args.host}:{args.port}, 调试模式: {args.debug}")
    
    try:
        app.run(
            host=args.host,
            port=args.port,
            debug=args.debug,
            threaded=True
        )
    except KeyboardInterrupt:
        logger.info("应用被用户中断")
    except Exception as e:
        logger.error(f"应用运行失败: {str(e)}")
        raise


if __name__ == '__main__':
    main()
