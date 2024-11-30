import os
import logging
import requests
import telegram
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
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

async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理YouTube URL"""
    try:
        # 获取URL
        if context.args:
            # 从命令参数获取URL
            url = context.args[0]
        else:
            # 从消息文本获取URL
            url = update.message.text.strip()
        
        logger.debug(f"收到URL: {url}")
        
        if not ('youtube.com' in url or 'youtu.be' in url):
            await update.message.reply_text('请发送有效的YouTube视频链接！')
            return

        # 发送处理中的消息
        processing_message = await update.message.reply_text('正在处理视频，请稍候...')

        # 配置requests会话
        session = requests.Session()
        
        # 对内部服务不使用代理
        if 'subtitle-processor' in SUBTITLE_PROCESSOR_URL:
            logger.debug("内部服务请求，不使用代理")
            session.proxies = {}  # 清空代理设置
        else:
            if PROXY:
                logger.debug(f"外部服务请求，使用代理: {PROXY}")
                session.proxies = {
                    'http': PROXY,
                    'https': PROXY
                }
                session.verify = False

        request_url = f'{SUBTITLE_PROCESSOR_URL}/process_youtube'
        request_data = {'url': url}
        
        logger.debug(f"发送请求到: {request_url}")
        logger.debug(f"请求数据: {request_data}")
        logger.debug(f"代理配置: {session.proxies}")

        # 发送请求到字幕处理服务
        try:
            response = session.post(
                request_url,
                json=request_data,
                timeout=300  # 5分钟超时
            )
            
            logger.debug(f"收到响应: 状态码={response.status_code}")
            logger.debug(f"响应内容: {response.text[:500]}...")  # 只记录前500个字符
            
            if response.status_code == 200:
                result = response.json()
                logger.debug(f"解析的JSON响应: {result}")
                
                # 发送成功消息和字幕文件链接
                success_message = (
                    f'处理完成！\n'
                    f'视频标题: {result.get("title", "未知")}\n'
                )
                
                # 添加查看链接
                view_url = result.get('view_url')
                if view_url:
                    # 使用配置的URL
                    full_url = f'{SUBTITLE_PROCESSOR_URL}{view_url}'
                    success_message += f'查看字幕: {full_url}\n'
                
                await processing_message.edit_text(success_message)
            else:
                error_msg = f'处理失败 (HTTP {response.status_code})'
                try:
                    error_details = response.json()
                    if 'error' in error_details:
                        error_msg += f": {error_details['error']}"
                except:
                    error_msg += f": {response.text}"
                logger.error(f"处理失败: {error_msg}")
                await processing_message.edit_text(error_msg)

        except requests.exceptions.RequestException as e:
            error_msg = f"请求失败: {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            await processing_message.edit_text(error_msg)

    except Exception as e:
        error_msg = f"处理URL时发生错误: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        await update.message.reply_text(error_msg)

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
