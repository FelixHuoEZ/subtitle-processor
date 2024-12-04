import os
import logging
import requests
import telegram
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackContext
from telegram.error import Conflict, NetworkError, TelegramError
import urllib3
import httpx
import json
import traceback
import sys
import signal
import time
import datetime
import pytz
import re

# 配置日志
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# 获取环境变量
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
SUBTITLE_PROCESSOR_URL = os.getenv('SUBTITLE_PROCESSOR_URL', 'http://subtitle-processor:5000')

logger.info(f"使用的SUBTITLE_PROCESSOR_URL: {SUBTITLE_PROCESSOR_URL}")

# 配置代理
PROXY = os.getenv('ALL_PROXY') or os.getenv('HTTPS_PROXY') or os.getenv('HTTP_PROXY')
if PROXY:
    logger.info(f"Using proxy: {PROXY}")

# 禁用不安全的HTTPS警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理错误的回调函数"""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    try:
        # 如果是Conflict错误，尝试重置更新
        if isinstance(context.error, Conflict):
            logger.warning("检测到冲突错误，可能有多个bot实例在运行")
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "检测到系统异常，正在尝试恢复..."
                )
            # 等待一段时间后重试
            time.sleep(5)
            return

        # 如果是网络错误，给出相应提示
        if isinstance(context.error, NetworkError):
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "网络连接出现问题，请稍后重试。"
                )
            return

        # 其他Telegram相关错误
        if isinstance(context.error, TelegramError):
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "Telegram服务暂时不可用，请稍后重试。"
                )
            return

        # 发送通用错误消息给用户
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "抱歉，处理您的请求时出现了错误。请稍后再试。"
            )
    except Exception as e:
        logger.error(f"Error in error handler: {str(e)}")
        logger.error(traceback.format_exc())

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """发送启动消息"""
    await update.message.reply_text(
        'Hi! 发送YouTube视频链接给我，我会帮你处理字幕。\n'
        '支持的格式:\n'
        '1. 直接发送YouTube URL\n'
        '2. 使用命令 /process <YouTube URL>'
    )

def normalize_url(url):
    """标准化视频URL
    
    支持的格式：
    YouTube:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://youtube.com/shorts/VIDEO_ID
    - https://m.youtube.com/watch?v=VIDEO_ID
    - https://youtube.com/v/VIDEO_ID
    - https://youtube.com/embed/VIDEO_ID
    
    Bilibili:
    - https://www.bilibili.com/video/BV1xx411c7mD
    - https://b23.tv/xxxxx
    - https://www.bilibili.com/video/av170001
    - https://m.bilibili.com/video/BV1xx411c7mD
    """
    import re
    
    # 清理URL
    url = url.strip()
    
    # YouTube URL处理
    youtube_patterns = [
        r'(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/watch\?v=([a-zA-Z0-9_-]+)',
        r'(?:https?:\/\/)?youtu\.be\/([a-zA-Z0-9_-]+)',
        r'(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/shorts\/([a-zA-Z0-9_-]+)',
        r'(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/v\/([a-zA-Z0-9_-]+)',
        r'(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/embed\/([a-zA-Z0-9_-]+)'
    ]
    
    for pattern in youtube_patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            return f'https://www.youtube.com/watch?v={video_id}', 'youtube'
    
    # Bilibili URL处理
    bilibili_patterns = [
        # BV号格式
        r'(?:https?:\/\/)?(?:www\.|m\.)?bilibili\.com\/video\/(BV[a-zA-Z0-9]+)',
        # av号格式
        r'(?:https?:\/\/)?(?:www\.|m\.)?bilibili\.com\/video\/av(\d+)',
        # 短链接格式
        r'(?:https?:\/\/)?b23\.tv\/([a-zA-Z0-9]+)'
    ]
    
    for pattern in bilibili_patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            # 如果是短链接，需要处理重定向
            if 'b23.tv' in url:
                try:
                    import requests
                    response = requests.head(url, allow_redirects=True)
                    if response.status_code == 200:
                        final_url = response.url
                        # 递归处理重定向后的URL
                        return normalize_url(final_url)
                except:
                    pass
            
            # 如果是av号，转换为标准格式
            if video_id.startswith('av'):
                video_id = video_id[2:]
            if video_id.isdigit():
                return f'https://www.bilibili.com/video/av{video_id}', 'bilibili'
            else:
                return f'https://www.bilibili.com/video/{video_id}', 'bilibili'
    
    return None, None

def extract_video_id(url, platform):
    """从标准化的URL中提取视频ID"""
    if platform == 'youtube':
        match = re.search(r'watch\?v=([a-zA-Z0-9_-]+)', url)
        if match:
            return match.group(1)
    elif platform == 'bilibili':
        # BV号格式
        match = re.search(r'\/video\/(BV[a-zA-Z0-9]+)', url)
        if match:
            return match.group(1)
        # av号格式
        match = re.search(r'\/video\/av(\d+)', url)
        if match:
            return match.group(1)
    return None

async def send_subtitle_file(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict) -> None:
    """发送字幕文件到Telegram
    
    Args:
        update: Telegram更新对象
        context: 回调上下文
        result: 字幕处理结果，包含字幕内容和视频信息
    """
    try:
        # 获取字幕内容和文件名
        subtitle_content = result.get('subtitle_content', '')
        video_info = result.get('video_info', {})
        
        # 使用视频标题作为文件名
        title = video_info.get('title', '') if video_info else ''
        if not title and 'filename' in result:
            title = os.path.splitext(result['filename'])[0]
        if not title:
            title = 'subtitle'
        filename = f"{title}.srt"
        
        # 创建临时文件
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.srt', delete=False) as temp_file:
            temp_file.write(subtitle_content)
            temp_path = temp_file.name
        
        # 发送字幕文件
        with open(temp_path, 'rb') as f:
            await update.message.reply_document(
                document=f,
                filename=filename,
                caption=f"✅ 字幕已生成 ({result.get('source', 'unknown')})"
            )
        
        # 删除临时文件
        import os
        try:
            os.remove(temp_path)
        except Exception as e:
            logger.warning(f"删除临时文件失败: {str(e)}")
            
    except Exception as e:
        logger.error(f"发送字幕文件时出错: {str(e)}")
        await update.message.reply_text("❌ 发送字幕文件时出错，请稍后重试。")

async def process_url(update: Update, context: CallbackContext) -> None:
    """处理用户发送的视频URL"""
    try:
        # 获取消息文本
        message_text = update.message.text.strip()
        
        # 标准化URL
        normalized_url, platform = normalize_url(message_text)
        if not normalized_url:
            await update.message.reply_text("❌ 无效的视频URL。请发送YouTube或Bilibili视频链接。")
            return
        
        # 提取视频ID
        video_id = extract_video_id(normalized_url, platform)
        if not video_id:
            await update.message.reply_text("❌ 无法解析视频ID。请检查URL格式。")
            return
        
        # 发送处理中的消息
        processing_message = await update.message.reply_text("⏳ 正在处理视频，请稍候...")
        
        # 准备请求数据
        data = {
            'url': normalized_url,
            'platform': platform,
            'video_id': video_id
        }
        
        # 发送请求到字幕处理服务
        subtitle_processor_url = os.getenv('SUBTITLE_PROCESSOR_URL', 'http://subtitle-processor:5000')
        response = requests.post(f"{subtitle_processor_url}/process", json=data)
        
        if response.status_code == 200:
            result = response.json()
            
            # 检查是否有字幕内容
            if result.get('subtitle_content'):
                # 发送字幕文件
                await send_subtitle_file(update, context, result)
                await processing_message.edit_text("✅ 字幕处理完成！")
            else:
                await processing_message.edit_text("❌ 未找到可用的字幕。")
        else:
            error_message = response.json().get('error', '未知错误')
            await processing_message.edit_text(f"❌ 处理失败：{error_message}")
            
    except Exception as e:
        logger.error(f"处理URL时出错: {str(e)}")
        await update.message.reply_text("❌ 处理视频时出错，请稍后重试。")

def signal_handler(signum, frame):
    """处理进程信号"""
    logger.info(f"收到信号 {signum}，正在关闭应用...")
    sys.exit(0)

def main():
    """启动bot"""
    if not TELEGRAM_TOKEN:
        logger.error("请设置TELEGRAM_TOKEN环境变量！")
        return

    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("正在初始化Telegram Bot...")

    # 创建应用并配置代理
    proxy_url = PROXY.replace('http://', 'http://') if PROXY else None
    
    # 创建默认配置
    defaults = telegram.ext.Defaults(
        tzinfo=pytz.timezone('Asia/Shanghai'),  # 使用中国时区
        parse_mode=telegram.constants.ParseMode.HTML,
        link_preview_options=telegram.LinkPreviewOptions(is_disabled=True),
        disable_notification=False,
        block=True
    )

    # 创建应用
    application_builder = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .defaults(defaults)
    )

    # 如果有代理，添加代理配置
    if proxy_url:
        application_builder.proxy_url(proxy_url)
        application_builder.connect_timeout(30.0)
        application_builder.read_timeout(30.0)
        application_builder.write_timeout(30.0)
        logger.info(f"使用代理: {proxy_url}")

    # 构建应用
    application = application_builder.build()

    # 添加错误处理器
    application.add_error_handler(error_handler)

    # 添加处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("process", process_url))
    # 处理普通消息
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_url))

    # 启动Bot
    logger.info("启动Telegram Bot")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()