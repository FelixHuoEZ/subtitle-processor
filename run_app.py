#!/usr/bin/env python3
"""
启动脚本 - 用于启动新的模块化字幕处理应用
"""

import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(__file__))

from app.main import create_app

if __name__ == '__main__':
    print("正在启动模块化字幕处理应用...")
    
    try:
        # 创建应用
        app = create_app()
        print("✅ 应用创建成功")
        
        # 启动应用
        print("🚀 启动Flask开发服务器...")
        print("   应用地址: http://localhost:5000")
        print("   健康检查: http://localhost:5000/health")
        print("   API信息: http://localhost:5000/api/info")
        print("   按 Ctrl+C 停止服务器")
        print("-" * 50)
        
        app.run(
            host='0.0.0.0',
            port=5000,
            debug=True,
            threaded=True
        )
        
    except KeyboardInterrupt:
        print("\n👋 应用已停止")
    except Exception as e:
        print(f"❌ 应用启动失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)