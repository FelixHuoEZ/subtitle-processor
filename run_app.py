#!/usr/bin/env python3
"""
启动脚本 - 用于启动新的模块化字幕处理应用
"""

import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(__file__))

from app.main import create_app


def _env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


if __name__ == '__main__':
    print("正在启动模块化字幕处理应用...")
    
    try:
        # 创建应用
        app = create_app()
        print("✅ 应用创建成功")

        host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
        port = int(os.getenv("PORT") or os.getenv("FLASK_RUN_PORT") or "5000")
        debug = _env_bool("FLASK_DEBUG") or _env_bool("APP_DEBUG")
        
        # 启动应用
        print("🚀 启动Flask服务器...")
        print(f"   应用地址: http://localhost:{port}")
        print(f"   健康检查: http://localhost:{port}/health")
        print(f"   API信息: http://localhost:{port}/api/info")
        print(f"   调试模式: {'开启' if debug else '关闭'}")
        print("   按 Ctrl+C 停止服务器")
        print("-" * 50)
        
        app.run(
            host=host,
            port=port,
            debug=debug,
            use_reloader=debug,
            threaded=True
        )
        
    except KeyboardInterrupt:
        print("\n👋 应用已停止")
    except Exception as e:
        print(f"❌ 应用启动失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
