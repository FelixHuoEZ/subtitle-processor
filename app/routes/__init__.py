"""Route modules for Flask blueprints."""

from .upload_routes import upload_bp
from .view_routes import view_bp
from .process_routes import process_bp

__all__ = ['upload_bp', 'view_bp', 'process_bp']