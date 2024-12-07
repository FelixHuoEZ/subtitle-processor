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
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 减少HTTP相关的日志
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('requests').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

# 获取环境变量
TELEGRAM_TOKEN_FILE = os.getenv('TELEGRAM_TOKEN_FILE')
TELEGRAM_TOKEN = None

if TELEGRAM_TOKEN_FILE and os.path.exists(TELEGRAM_TOKEN_FILE):
    try:
        with open(TELEGRAM_TOKEN_FILE, 'r') as f:
            TELEGRAM_TOKEN = f.read().strip()
        logger.info("成功从文件读取Telegram Token")
    except Exception as e:
        logger.error(f"读取Telegram Token文件失败: {str(e)}")
else:
    # 兼容旧的环境变量方式
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

SUBTITLE_PROCESSOR_URL = os.getenv('SUBTITLE_PROCESSOR_URL', 'http://subtitle-processor:5000')
logger.info(f"使用的SUBTITLE_PROCESSOR_URL: {SUBTITLE_PROCESSOR_URL}")

# 配置代理
PROXY = os.getenv('ALL_PROXY') or os.getenv('HTTPS_PROXY') or os.getenv('HTTP_PROXY')
if PROXY:
    logger.info(f"Using proxy: {PROXY}")

# 禁用不安全的HTTPS警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 全局变量
VALID_LOCATIONS = {
    '1': 'new',
    '2': 'later',
    '3': 'archive',
    '4': 'feed'
}

# 用户状态存储
user_states = {}

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
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
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
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ 发送字幕文件时出错，请稍后重试。"
        )

async def ask_location(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """询问用户选择location"""
    message = (
        "请选择保存位置 (10秒后默认选择'new'):\n"
        "1. new (新文章)\n"
        "2. later (稍后阅读)\n"
        "3. archive (存档)\n"
        "4. feed (订阅源)\n"
        "\n可以输入数字(1-4)或直接输入位置名称"
    )
    sent_message = await update.message.reply_text(message)
    
    # 保存用户状态
    user_states[update.effective_user.id] = {
        'url': url,
        'waiting_for_location': True,
        'start_time': time.time(),
        'message_id': sent_message.message_id
    }
    
    # 设置10秒后的默认选择
    context.job_queue.run_once(
        location_timeout,
        10,
        data={
            'user_id': update.effective_user.id,
            'chat_id': update.effective_chat.id,
            'message_id': sent_message.message_id,
            'url': url
        }
    )

async def location_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理location选择超时"""
    job = context.job
    data = job.data
    user_id = data['user_id']
    chat_id = data['chat_id']
    
    try:
        # 检查用户是否还在等待选择
        user_state = user_states.get(user_id)
        if user_state and user_state['waiting_for_location']:
            # 使用默认location处理URL
            try:
                await process_url_with_location(
                    user_id,
                    chat_id,
                    data['url'],
                    'new',
                    context
                )
            except Exception as e:
                logger.error(f"处理URL时出错: {str(e)}")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ 处理URL时出错，请稍后重试。"
                )
            
            try:
                # 尝试更新原消息
                await context.bot.edit_message_text(
                    "已使用默认位置(new)处理您的请求",
                    chat_id=chat_id,
                    message_id=data['message_id']
                )
            except telegram.error.BadRequest:
                # 如果编辑失败，发送新消息
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="已使用默认位置(new)处理您的请求"
                )
            
            # 清理用户状态
            if user_id in user_states:
                del user_states[user_id]
    except Exception as e:
        logger.error(f"处理location超时时出错: {str(e)}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ 处理请求时出错，请重新发送URL。"
            )
        except:
            pass

async def process_url_with_location(user_id: int, chat_id: int, url: str, location: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """使用指定的location处理URL"""
    try:
        # 标准化URL
        normalized_url, platform = normalize_url(url)
        if not normalized_url:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ 无效的URL格式"
            )
            return

        # 发送处理中的消息
        processing_message = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ 正在处理您的请求..."
        )

        # 准备请求数据
        data = {
            'url': normalized_url,
            'platform': platform,
            'location': location
        }

        # 发送请求到字幕处理服务
        try:
            response = requests.post(
                f"{SUBTITLE_PROCESSOR_URL}/process_youtube",
                json=data,
                timeout=300  # 5分钟超时
            )
            response.raise_for_status()
            result = response.json()

            # 更新处理中的消息
            await context.bot.edit_message_text(
                "✅ 视频处理完成，正在发送字幕文件...",
                chat_id=chat_id,
                message_id=processing_message.message_id
            )

            # 创建一个虚拟的Update对象来传递chat_id
            class DummyChat:
                def __init__(self, chat_id):
                    self.id = chat_id

            class DummyUpdate:
                def __init__(self, chat_id):
                    self.effective_chat = DummyChat(chat_id)

            dummy_update = DummyUpdate(chat_id)
            
            # 发送字幕文件
            await send_subtitle_file(dummy_update, context, result)

        except requests.exceptions.RequestException as e:
            error_message = f"处理请求时出错: {str(e)}"
            logger.error(error_message)
            await context.bot.edit_message_text(
                f"❌ {error_message}",
                chat_id=chat_id,
                message_id=processing_message.message_id
            )
            return

    except Exception as e:
        error_message = f"处理URL时出错: {str(e)}"
        logger.error(error_message)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ {error_message}"
            )
        except:
            pass

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户消息"""
    user_id = update.effective_user.id
    user_state = user_states.get(user_id)

    # 如果用户正在等待选择location
    if user_state and user_state.get('waiting_for_location'):
        location_input = update.message.text.lower().strip()
        
        # 检查输入是否有效
        if location_input in VALID_LOCATIONS.values():
            location = location_input
        elif location_input in VALID_LOCATIONS:
            location = VALID_LOCATIONS[location_input]
        else:
            await update.message.reply_text(
                "❌ 无效的选择，请输入数字(1-4)或有效的位置名称"
            )
            return

        # 处理URL
        await process_url_with_location(
            user_id,
            update.effective_chat.id,
            user_state['url'],
            location,
            context
        )
        
        # 删除选择提示消息
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=user_state['message_id']
            )
        except Exception:
            pass
        
        # 清理用户状态
        if user_id in user_states:
            del user_states[user_id]
        return

    # 检查是否是视频URL
    url = update.message.text
    if any(platform in url.lower() for platform in ['youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv']):
        await ask_location(update, context, url)
    else:
        await update.message.reply_text(
            "请发送YouTube或Bilibili视频链接"
        )

async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            await processing_message.edit_text(f"❌ 服务器错误: {response.status_code}")
            
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
        logger.error("请设置TELEGRAM_TOKEN环境变量或配置文件！")
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 启动Bot
    logger.info("启动Telegram Bot")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()