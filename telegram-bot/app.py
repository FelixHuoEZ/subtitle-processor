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
import yaml
import threading
import asyncio
from flask import Flask
from threading import Thread

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

def load_config():
    """加载YAML配置文件"""
    config_path = os.getenv('CONFIG_PATH', 'config/config.yml')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"加载配置文件失败: {str(e)}")
        return None

# 加载配置
config = load_config()
if not config:
    logger.error("配置加载失败，使用空配置")
    config = {}

# 获取配置
TELEGRAM_TOKEN = config.get('tokens', {}).get('telegram')
SUBTITLE_PROCESSOR_URL = os.getenv('SUBTITLE_PROCESSOR_URL', 'http://subtitle-processor:5000')
SERVER_DOMAIN = config.get('servers', {}).get('domain')

logger.info(f"使用的SUBTITLE_PROCESSOR_URL: {SUBTITLE_PROCESSOR_URL}")
logger.info(f"使用的SERVER_DOMAIN: {SERVER_DOMAIN}")

# 获取环境变量
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

TAGS_HELP_MESSAGE = "请输入标签，多个标签用逗号分隔（例如：'youtube字幕,学习笔记,英语学习'）。\n输入 /skip 跳过添加标签。"

# 用户状态存储
user_states = {}

# 全局变量追踪应用状态
last_activity = time.time()
is_bot_healthy = True

# 健康检查Flask应用
health_app = Flask(__name__)

@health_app.route('/health')
def health_check():
    """健康检查端点"""
    global last_activity, is_bot_healthy
    
    current_time = time.time()
    time_since_activity = current_time - last_activity
    
    # 如果超过10分钟没有活动且bot状态不健康，返回错误
    if time_since_activity > 600 and not is_bot_healthy:
        return "Bot unhealthy - no activity for too long", 500
        
    return "OK", 200

def start_health_server():
    """启动健康检查服务器"""
    health_app.run(host='0.0.0.0', port=8081, debug=False, use_reloader=False)

def update_activity():
    """更新最后活动时间"""
    global last_activity, is_bot_healthy
    last_activity = time.time()
    is_bot_healthy = True  # 有活动时重置健康状态

def connection_monitor(application):
    """监控连接状态并在需要时重启"""
    def monitor_loop():
        global is_bot_healthy, last_activity
        while True:
            try:
                current_time = time.time()
                time_since_activity = current_time - last_activity
                
                # 如果超过30分钟没有活动，尝试发送测试请求
                if time_since_activity > 1800:  # 30分钟
                    logger.warning("长时间无活动，执行连接测试")
                    try:
                        # 简单的连接测试 - 检查bot对象是否正常
                        if application and application.bot:
                            update_activity()
                        else:
                            is_bot_healthy = False
                    except Exception as e:
                        logger.error(f"连接测试失败: {str(e)}")
                        is_bot_healthy = False
                        
                        # 如果连续失败，强制退出让Docker重启
                        if time_since_activity > 3600:  # 1小时
                            logger.critical("连接长时间失败，触发容器重启")
                            os._exit(1)
                
                # 如果状态不健康超过5分钟，强制重启
                if not is_bot_healthy and time_since_activity > 300:
                    logger.critical("Bot状态不健康超过5分钟，触发容器重启")
                    os._exit(1)
                    
                time.sleep(60)  # 每分钟检查一次
                
            except Exception as e:
                logger.error(f"连接监控器异常: {str(e)}")
                time.sleep(60)
    
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    logger.info("连接监控器已启动")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理错误的回调函数"""
    global is_bot_healthy
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    # 更新活动时间
    update_activity()
    
    try:
        # 如果是Conflict错误，尝试重置更新
        if isinstance(context.error, Conflict):
            logger.warning("检测到冲突错误，可能有多个bot实例在运行")
            is_bot_healthy = False
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "检测到系统异常，正在尝试恢复..."
                )
            # 等待一段时间后重试
            time.sleep(5)
            return

        # 如果是网络错误，标记为不健康状态
        if isinstance(context.error, NetworkError):
            logger.warning("网络错误，标记bot为不健康状态")
            is_bot_healthy = False
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "网络连接出现问题，请稍后重试。"
                )
            return

        # 其他Telegram相关错误
        if isinstance(context.error, TelegramError):
            logger.warning("Telegram错误，标记bot为不健康状态")
            is_bot_healthy = False
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
        is_bot_healthy = False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """发送启动消息"""
    update_activity()  # 更新活动时间
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
    user_id = update.effective_user.id
    
    # 保存用户状态
    user_states[user_id] = {
        'state': 'waiting_for_location',
        'url': url,
        'last_interaction': datetime.datetime.now(pytz.UTC)
    }
    
    # 发送选择提示
    message = await update.message.reply_text(
        "请选择保存位置：\n"
        "1. 新内容 (New)\n"
        "2. 稍后阅读 (Later)\n"
        "3. 已归档 (Archive)\n"
        "4. Feed"
    )
    
    # 保存提示消息ID以便后续删除
    user_states[user_id]['message_id'] = message.message_id
    
    # 设置超时
    context.job_queue.run_once(
        location_timeout,
        180,  # 3分钟超时
        data={'user_id': user_id, 'chat_id': update.effective_chat.id},
        name='location_timeout'
    )

async def location_timeout(context: CallbackContext) -> None:
    """处理location选择超时"""
    try:
        user_id = context.job.data['user_id']
        chat_id = context.job.data['chat_id']
        
        if user_id in user_states and user_states[user_id].get('state') == 'waiting_for_location':
            # 发送超时消息
            await context.bot.send_message(
                chat_id=chat_id,
                text="⌛ 选择超时，请重新发送视频链接"
            )
            
            # 清理用户状态
            if user_id in user_states:
                del user_states[user_id]
    except Exception as e:
        logger.error(f"处理location超时时出错: {str(e)}")

async def ask_tags(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, location: str) -> None:
    """询问用户输入tags"""
    user_id = update.effective_user.id
    user_states[user_id] = {
        'state': 'waiting_for_tags',
        'url': url,
        'location': location,
        'last_interaction': datetime.datetime.now(pytz.UTC)
    }
    
    await update.message.reply_text(TAGS_HELP_MESSAGE)
    
    # 设置超时
    context.job_queue.run_once(
        tags_timeout,
        180,  # 3分钟超时
        data={'user_id': user_id, 'chat_id': update.effective_chat.id},
        name=f'tags_timeout_{user_id}'
    )

async def tags_timeout(context: CallbackContext) -> None:
    """处理tags输入超时"""
    user_id = context.job.data['user_id']
    chat_id = context.job.data['chat_id']
    
    if user_id in user_states and user_states[user_id].get('state') == 'waiting_for_tags':
        # 使用默认的空tags继续处理
        url = user_states[user_id]['url']
        location = user_states[user_id]['location']
        await process_url_with_location(user_id, chat_id, url, location, context, [])
        
        # 清理状态
        if user_id in user_states:
            del user_states[user_id]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户消息"""
    update_activity()  # 更新活动时间
    user_id = update.effective_user.id
    user_state = user_states.get(user_id)

    # 如果用户正在等待输入tags
    if user_state and user_state.get('state') == 'waiting_for_tags':
        if update.message.text.lower() == '/skip':
            # 用户选择跳过添加tags
            await process_url_with_location(
                user_id,
                update.effective_chat.id,
                user_state['url'],
                user_state['location'],
                context,
                []  # 明确传递空列表
            )
        else:
            # 处理用户输入的tags，支持中英文逗号
            text = update.message.text.strip()
            # 先将中文逗号替换为英文逗号，然后分割
            tags = [tag.strip() for tag in text.replace('，', ',').split(',') if tag.strip()]
            await process_url_with_location(
                user_id,
                update.effective_chat.id,
                user_state['url'],
                user_state['location'],
                context,
                tags
            )
        
        # 清理用户状态
        if user_id in user_states:
            del user_states[user_id]
        return

    # 如果用户正在等待选择location
    if user_state and user_state.get('state') == 'waiting_for_location':
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

        # 删除选择提示消息
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=user_state['message_id']
            )
        except Exception:
            pass

        # 询问用户输入tags
        await ask_tags(update, context, user_state['url'], location)
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

async def process_url_with_location(user_id: int, chat_id: int, url: str, location: str, context: ContextTypes.DEFAULT_TYPE, tags: list = None) -> None:
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

        # 获取video_id
        video_id = extract_video_id(normalized_url, platform)
        if not video_id:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ 无法提取视频ID"
            )
            return

        # 记录处理信息
        tags_info = f", tags: {tags}" if tags else ""
        logger.info(f"处理{platform}URL: {normalized_url}, location: {location}{tags_info}")

        # 发送处理中的消息
        processing_message = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ 正在处理您的请求..."
        )

        # 准备请求数据
        data = {
            'url': normalized_url,
            'platform': platform,
            'location': location,
            'video_id': video_id
        }
        
        # 如果有tags，添加到请求数据中
        if tags:
            data['tags'] = tags

        # 发送请求到字幕处理服务
        try:
            response = requests.post(
                f"{SUBTITLE_PROCESSOR_URL}/process",
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
    
    # 启动健康检查服务器
    health_thread = Thread(target=start_health_server, daemon=True)
    health_thread.start()
    logger.info("健康检查服务器已启动在端口8081")

    # 创建应用并配置代理
    proxy_url = PROXY.replace('http://', 'http://') if PROXY else None
    
    # 创建默认配置 - 修复时区警告
    from zoneinfo import ZoneInfo
    try:
        timezone = ZoneInfo('Asia/Shanghai')
    except:
        # 回退到pytz
        import pytz
        timezone = pytz.timezone('Asia/Shanghai')
    
    defaults = telegram.ext.Defaults(
        tzinfo=timezone,  # 使用新的时区API
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

    # 如果有代理，添加代理配置 - 修复新版本API
    if proxy_url:
        try:
            # 新版本API
            application_builder.proxy(proxy_url)
            application_builder.connect_timeout(60.0)  # 增加连接超时
            application_builder.read_timeout(60.0)     # 增加读取超时
            application_builder.write_timeout(60.0)    # 增加写入超时
            application_builder.pool_timeout(60.0)     # 连接池超时
            logger.info(f"使用代理: {proxy_url}")
        except AttributeError:
            # 如果新API不存在，尝试旧API
            try:
                application_builder.proxy_url(proxy_url)
                application_builder.connect_timeout(60.0)
                application_builder.read_timeout(60.0)
                application_builder.write_timeout(60.0)
                application_builder.pool_timeout(60.0)
                logger.info(f"使用代理 (旧API): {proxy_url}")
            except AttributeError:
                logger.warning(f"无法设置代理，当前版本不支持: {proxy_url}")
                # 不使用代理继续运行

    # 构建应用
    application = application_builder.build()
    
    # 启动连接监控器
    connection_monitor(application)

    # 添加错误处理器
    application.add_error_handler(error_handler)

    # 添加处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("process", process_url))
    # 处理普通消息
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 启动Bot - 添加重连机制
    logger.info("启动Telegram Bot")
    
    # 设置轮询参数以增强连接稳定性
    polling_kwargs = {
        'allowed_updates': Update.ALL_TYPES,
        'drop_pending_updates': True,
        'timeout': 30,          # 轮询超时30秒
        'bootstrap_retries': 5  # 启动重试次数
    }
    
    try:
        application.run_polling(**polling_kwargs)
    except Exception as e:
        logger.error(f"Bot polling失败: {str(e)}")
        # 可以在这里添加重启逻辑
        raise

if __name__ == '__main__':
    main()