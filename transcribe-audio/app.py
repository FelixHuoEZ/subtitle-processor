from flask import Flask, request, jsonify
import os
import logging
import sys
import time
import soundfile as sf
import numpy as np
from funasr import AutoModel
import torch
import json
from pydub import AudioSegment
import tempfile
from modelscope import snapshot_download
from pathlib import Path
import shutil
from datetime import datetime
import errno

# FunASR 模型列表
FUNASR_MODELS = [
    "SenseVoiceSmall", "paraformer-zh", "paraformer-zh-streaming", "paraformer-en",
    "conformer-en", "ct-punc", "fsmn-vad", "fsmn-kws", "fa-zh", "cam++",
    "Qwen-Audio", "Qwen-Audio-Chat", "emotion2vec+large"
]

MODEL_MAPPINGS = {
    "main": {
        "paraformer-zh": "damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "paraformer-zh-streaming": "damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "paraformer-zh-vad-punc": "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "paraformer-en": "damo/speech_paraformer-large_asr_nat-en-16k-common-vocab10020",
        "conformer-en": "damo/speech_conformer_asr_nat-en-16k-common-vocab10020",
        "SenseVoiceSmall": "damo/speech_SenseVoiceSmall_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "fa-zh": "damo/speech_FastConformer_asr_nat-zh-cn-16k-common-vocab8404",
        "Qwen-Audio": "damo/speech_qwen_audio_asr_nat-zh-cn-16k-common-vocab8404",
        "Qwen-Audio-Chat": "damo/speech_qwen_audio_chat_asr_nat-zh-cn-16k-common-vocab8404",
        "emotion2vec+large": "damo/speech_emotion2vec_large_sv_zh-cn_16k-common",
    },
    "vad": {
        "fsmn-vad": "damo/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "fsmn-kws": "damo/speech_fsmn_kws_zh-cn-16k-common-pytorch",
    },
    "punc": {
        "ct-punc": "damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
    },
    "spk": {
        "cam++": "damo/speech_campplus_sv_zh-cn_16k-common",
    },
}

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# 创建logger
logger = logging.getLogger("transcribe-audio")
logger.setLevel(logging.DEBUG)

# 日志文件输出
log_dir = Path(os.getenv("LOG_DIR", "/app/logs"))
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / os.getenv("LOG_FILE", "transcribe-audio.log")
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logger.addHandler(file_handler)
logging.getLogger().addHandler(file_handler)
logger.info("日志文件输出到 %s", log_file)

# 确保其他库的日志级别不会太详细
logging.getLogger("modelscope").setLevel(logging.ERROR)
logging.getLogger("funasr").setLevel(logging.ERROR)
logging.getLogger("jieba").setLevel(logging.ERROR)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("filelock").setLevel(logging.WARNING)

# 处理 macOS 上偶发的 EDEADLK 死锁，重试加载 FunASR 分词字典
try:
    from funasr.tokenizer import char_tokenizer as _funasr_char_tokenizer
except ImportError:
    _funasr_char_tokenizer = None
else:
    if not getattr(_funasr_char_tokenizer.load_seg_dict, "__name__", "").startswith("_load_seg_dict_with_retry"):
        _original_load_seg_dict = _funasr_char_tokenizer.load_seg_dict

        def _load_seg_dict_with_retry(seg_dict, max_retries=5, base_delay=0.5):
            """Wrap funasr load_seg_dict to mitigate occasional deadlocks on shared volumes."""
            last_err = None
            for attempt in range(1, max_retries + 1):
                try:
                    return _original_load_seg_dict(seg_dict)
                except OSError as err:
                    last_err = err
                    if err.errno != errno.EDEADLK or attempt == max_retries:
                        raise
                    wait_time = base_delay * attempt
                    logger.warning(
                        "加载分词字典时遇到系统 EDEADLK 死锁，正在重试 (%s/%s)，%.1f 秒后继续: %s",
                        attempt,
                        max_retries,
                        wait_time,
                        err,
                    )
                    time.sleep(wait_time)
            raise last_err

        _load_seg_dict_with_retry.__wrapped__ = _original_load_seg_dict
        _funasr_char_tokenizer.load_seg_dict = _load_seg_dict_with_retry

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max-limit
app.config['UPLOAD_FOLDER'] = '/app/uploads'


# 全局模型变量
model = None

# 全局进度跟踪
current_progress = {
    "status": "idle",
    "progress": 0,
    "total_chunks": 0,
    "current_chunk": 0,
    "message": "等待处理...",
    "start_time": None,
    "estimated_time": None
}

# 设置请求超时时间（5分钟）
app.config['TIMEOUT'] = 300

def ensure_dir(dir_path):
    """确保目录存在，如果不存在则创建"""
    Path(dir_path).mkdir(parents=True, exist_ok=True)

def cleanup_model_locks(cache_dir):
    """移除遗留的模型锁文件，防止模型下载因文件锁卡死"""
    cache_path = Path(cache_dir)
    if not cache_path.exists():
        return
    for pattern in ("**/.mdl", "**/*.mdl.lock"):
        for lock_file in cache_path.glob(pattern):
            if lock_file.is_file():
                try:
                    lock_file.unlink()
                    logger.warning(f"移除了遗留的模型锁文件: {lock_file}")
                except OSError as err:
                    logger.warning(f"无法移除模型锁文件 {lock_file}: {err}")
    for lock_dir in cache_path.glob("**/.lock"):
        if lock_dir.is_dir():
            try:
                shutil.rmtree(lock_dir)
                logger.warning(f"移除了遗留的模型锁目录: {lock_dir}")
            except OSError as err:
                logger.warning(f"无法移除模型锁目录 {lock_dir}: {err}")
    for temp_dir in cache_path.glob("**/._____temp"):
        if temp_dir.is_dir():
            try:
                shutil.rmtree(temp_dir)
                logger.warning(f"移除了遗留的临时目录: {temp_dir}")
            except OSError as err:
                logger.warning(f"无法移除临时目录 {temp_dir}: {err}")

def download_model(model_id, revision, cache_dir):
    """下载指定的模型"""
    try:
        logger.info(f"开始下载模型 {model_id} 到 {cache_dir}")
        cleanup_model_locks(cache_dir)
        specific_lock_files = [
            Path(cache_dir) / model_id / ".mdl",
            Path(cache_dir) / "hub" / model_id / ".mdl",
        ]
        for lock_path in specific_lock_files:
            if lock_path.exists():
                try:
                    lock_path.unlink()
                    logger.warning(f"下载前移除锁文件: {lock_path}")
                except OSError as err:
                    logger.warning(f"移除锁文件 {lock_path} 失败: {err}")
        # 移除未完整下载的临时目录
        for stale_path in [
            Path(cache_dir) / "._____temp",
            Path(cache_dir) / "hub" / "._____temp",
            Path(cache_dir) / model_id / "._____temp",
            Path(cache_dir) / "hub" / model_id / "._____temp",
        ]:
            if stale_path.exists():
                try:
                    shutil.rmtree(stale_path)
                    logger.warning(f"移除了遗留的模型临时目录: {stale_path}")
                except OSError as err:
                    logger.warning(f"无法移除模型临时目录 {stale_path}: {err}")
        
        # 下载模型
        model_dir = snapshot_download(
            model_id=model_id,
            revision=revision,
            cache_dir=cache_dir
        )
        logger.info(f"模型下载完成: {model_dir}")
        
        # 确保目录有写权限
        for root, dirs, files in os.walk(cache_dir):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)
        
        return model_dir
    except Exception as e:
        logger.error(f"下载模型 {model_id} 时出错: {str(e)}")
        raise

def get_model_id(model_type, model_name):
    """获取完整的模型ID"""
    model_mappings = MODEL_MAPPINGS

    # 检查是否是完整的模型ID（包含仓库前缀）
    if "/" in model_name:
        return model_name
    
    # 检查是否在映射表中
    try:
        return model_mappings[model_type][model_name]
    except KeyError:
        # 如果找不到映射，尝试构建标准格式的模型ID
        if model_type == "main":
            # 主模型ID格式：damo/speech_[model_name]_asr_nat-[lang]-16k-common-[vocab]
            if "zh" in model_name.lower():
                return f"damo/speech_{model_name}_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
            elif "en" in model_name.lower():
                return f"damo/speech_{model_name}_asr_nat-en-16k-common-vocab10020"
            else:
                return f"damo/speech_{model_name}_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
        elif model_type == "vad":
            # VAD模型ID格式：damo/speech_[model_name]_zh-cn-16k-common-pytorch
            return f"damo/speech_{model_name}_zh-cn-16k-common-pytorch"
        elif model_type == "punc":
            # 标点模型ID格式：damo/punc_[model_name]_zh-cn-common-vocab272727-pytorch
            return f"damo/punc_{model_name}_zh-cn-common-vocab272727-pytorch"
        elif model_type == "spk":
            # 说话人分离模型ID格式：damo/speech_[model_name]_sv_zh-cn_16k-common
            return f"damo/speech_{model_name}_sv_zh-cn_16k-common"
        else:
            # 如果无法确定格式，直接添加damo/前缀
            logger.warning(f"未知的模型类型: {model_type}，将直接添加damo/前缀")
            return f"damo/{model_name}"


def resolve_model_config(model_type: str, model_name: str):
    """标准化模型配置，返回运行时所需名称与ID."""
    normalized = (model_name or "").strip()
    mapping = MODEL_MAPPINGS.get(model_type, {})
    requires_trust_remote_code = False

    if "/" in normalized:
        alias = next((alias for alias, full_id in mapping.items() if full_id == normalized), None)
        if alias:
            runtime_name = alias
        else:
            runtime_name = normalized
            requires_trust_remote_code = True
    else:
        runtime_name = normalized

    model_id = get_model_id(model_type, normalized)

    return {
        "name": normalized or runtime_name,
        "id": model_id,
        "runtime": runtime_name,
        "requires_trust_remote_code": requires_trust_remote_code,
    }

def ensure_models():
    """确保模型文件存在，如果不存在则下载"""
    # 运行时目录与共享目录解耦，避免共享卷上的文件锁问题
    runtime_dir = Path(os.getenv("MODEL_DIR", "/app/runtime-models"))
    ensure_dir(runtime_dir)
    model_dir = str(runtime_dir)

    cleanup_model_locks(model_dir)

    # 设置环境变量
    os.environ['MODELSCOPE_CACHE'] = model_dir
    os.environ['HF_HOME'] = model_dir
    os.environ['TORCH_HOME'] = model_dir

    # 获取所有模型名称 - 优先使用支持第三代热词的模型
    model_name = os.getenv("FUNASR_MODEL", "paraformer-zh").strip()
    vad_model = os.getenv("FUNASR_VAD_MODEL", "fsmn-vad").strip()
    punc_model = os.getenv("FUNASR_PUNC_MODEL", "ct-punc").strip()
    spk_model = os.getenv("FUNASR_SPK_MODEL", "").strip()
    
    # 获取完整的模型ID
    model_configs = {
        "main": resolve_model_config("main", model_name) if model_name else None,
        "vad": resolve_model_config("vad", vad_model) if vad_model else None,
        "punc": resolve_model_config("punc", punc_model) if punc_model else None,
        "spk": resolve_model_config("spk", spk_model) if spk_model else None,
    }
    
    logger.info("检查模型配置：")
    for model_type, config in model_configs.items():
        if not config:
            logger.info(f"{model_type}模型: 已禁用")
            continue
        logger.info(f"{model_type}模型: {config['runtime']} (ID: {config['id']})")
    
    # 检查所有模型文件是否存在
    def _model_exists(model_id: str) -> bool:
        candidates = [
            Path(model_dir) / "hub" / model_id,
            Path(model_dir) / model_id,
            Path(model_dir) / "models" / model_id,
        ]
        return any(path.exists() for path in candidates)
    
    model_paths = {
        model_type: [
            str(path) for path in [
                Path(model_dir) / "hub" / config["id"],
                Path(model_dir) / config["id"],
                Path(model_dir) / "models" / config["id"],
            ]
        ]
        for model_type, config in model_configs.items() if config
    }

    logger.info("检查模型文件：")
    for model_type, paths in model_paths.items():
        logger.info(f"{model_type}模型候选路径: {paths}")

    # 检查是否需要下载模型
    missing_models = [
        (model_type, config)
        for model_type, config in model_configs.items()
        if config and not _model_exists(config["id"])
    ]
    if missing_models:
        missing_desc = [f"{model_type}:{config['id']}" for model_type, config in missing_models]
        logger.info("缺失模型列表: %s", missing_desc)
        logger.info("部分模型文件不存在，开始下载...")
        ensure_dir(model_dir)

        # 下载所有缺失的模型
        success = True
        for model_type, config in missing_models:
            try:
                downloaded_path = download_model(
                    model_id=config["id"],
                    revision=None,  # 使用最新版本
                    cache_dir=model_dir
                )
                logger.info(f"模型 {config['id']} 下载成功: {downloaded_path}")
            except Exception as e:
                logger.error(f"下载模型 {config['id']} 失败: {str(e)}")
                success = False
                continue

        if not success:
            logger.error("部分模型下载失败，请检查错误信息并重试")
            sys.exit(1)
        else:
            logger.info(f"所有模型下载成功，模型目录: {model_dir}")
    else:
        logger.info("所有模型文件已存在，无需下载")
    
    timestamp_capable = {
        "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    }
    supports_timestamp = model_configs["main"]["id"] in timestamp_capable

    if not supports_timestamp:
        logger.warning("主模型不具备句级时间戳能力，将继续使用VAD但跳过句级时间戳")

    return model_dir, model_configs, supports_timestamp

# 初始化FunASR模型
MODEL_SUPPORTS_TIMESTAMP = False


def init_model():
    global model, MODEL_SUPPORTS_TIMESTAMP  # 声明使用全局变量
    print("="*50)
    print("开始初始化FunASR模型...")
    print("正在检测GPU状态...")
    
    try:
        # 检测GPU是否可用
        device = "cuda" if torch.cuda.is_available() else "cpu"
        print("\n" + "="*20 + " GPU检测信息 " + "="*20)
        print(f"CUDA是否可用: {torch.cuda.is_available()}")
        print(f"PyTorch版本: {torch.__version__}")
        
        if device == "cuda":
            gpu_count = torch.cuda.device_count()
            print(f"可用GPU数量: {gpu_count}")
            for i in range(gpu_count):
                print(f"GPU {i}: {torch.cuda.get_device_name(i)}")
                print(f"GPU {i} 总内存: {torch.cuda.get_device_properties(i).total_memory / 1024**3:.1f} GB")
                print(f"GPU {i} CUDA版本: {torch.version.cuda}")
                if hasattr(torch.backends.cudnn, 'version'):
                    print(f"GPU {i} cuDNN版本: {torch.backends.cudnn.version()}")
        else:
            print("警告: 未检测到可用的GPU，将使用CPU进行推理")
        
        print("\n" + "="*20 + " 模型加载开始 " + "="*20)
        
        # 确保模型存在
        model_dir, model_info, supports_timestamp = ensure_models()
        MODEL_SUPPORTS_TIMESTAMP = supports_timestamp
        if MODEL_SUPPORTS_TIMESTAMP:
            logger.info("当前主模型支持 sentence_timestamp 输出")
        else:
            logger.warning("当前主模型不支持 sentence_timestamp，将跳过时间戳和说话人信息")
        
        try:
            # 尝试使用指定的模型
            init_kwargs = {
                "model": model_info["main"]["runtime"],
                "device": device,
                "model_dir": model_dir,
                "batch_size": 1 if device == "cpu" else 4,
                "disable_update": True,
                "use_local": True,
            }
            trust_remote_code = model_info["main"].get("requires_trust_remote_code", False)
            vad_config = model_info.get("vad")
            if vad_config:
                init_kwargs.update({
                    "vad_model": vad_config["runtime"],
                    "vad_kwargs": {"max_single_segment_time": 60000},
                    "vad_model_dir": model_dir,
                })
                trust_remote_code = trust_remote_code or vad_config.get("requires_trust_remote_code", False)
            punc_config = model_info.get("punc")
            if punc_config:
                init_kwargs.update({
                    "punc_model": punc_config["runtime"],
                    "punc_model_dir": model_dir,
                })
                trust_remote_code = trust_remote_code or punc_config.get("requires_trust_remote_code", False)
            spk_config = model_info.get("spk")
            if spk_config:
                init_kwargs.update({
                    "spk_model": spk_config["runtime"],
                    "spk_model_dir": model_dir,
                })
                trust_remote_code = trust_remote_code or spk_config.get("requires_trust_remote_code", False)

            if trust_remote_code:
                init_kwargs["trust_remote_code"] = True

            logger.info("FunASR初始化参数: %s", {k: v if k != "hotword" else "***" for k, v in init_kwargs.items()})
            model = AutoModel(**init_kwargs)
            print(f"FunASR模型加载完成，使用设备: {device}")
            print(f"主模型: {model_info['main']['name']} ({model_info['main']['id']})")
            if vad_config:
                print(f"VAD模型: {vad_config['name']} ({vad_config['id']})")
            else:
                print("VAD模型: 已禁用")
            if punc_config:
                print(f"标点模型: {punc_config['name']} ({punc_config['id']})")
            else:
                print("标点模型: 已禁用")
            if spk_config:
                print(f"说话人模型: {spk_config['name']} ({spk_config['id']})")
            else:
                print("说话人模型: 已禁用")
            print(f"批处理大小: {1 if device == 'cpu' else 4}")
            
        except Exception as e:
            print(f"警告: 加载指定模型失败: {str(e)}")
            print("尝试使用默认模型配置")
            
            # 使用默认模型
            fallback_kwargs = {
                "model": "paraformer-zh",
                "device": device,
                "model_dir": model_dir,
                "batch_size": 1 if device == "cpu" else 4,
                "disable_update": True,
                "use_local": True,
            }
            fallback_kwargs.update({
                "vad_model": "fsmn-vad",
                "vad_kwargs": {"max_single_segment_time": 60000},
                "vad_model_dir": model_dir,
                "punc_model": "ct-punc",
                "punc_model_dir": model_dir,
                "spk_model": "cam++",
                "spk_model_dir": model_dir,
            })

            logger.warning("使用默认模型参数: %s", fallback_kwargs)
            model = AutoModel(**fallback_kwargs)
            MODEL_SUPPORTS_TIMESTAMP = False
            print(f"FunASR模型加载完成，使用设备: {device}")
            print(f"主模型: paraformer-zh")
            print(f"VAD模型: fsmn-vad")
            print(f"标点模型: ct-punc")
            print(f"说话人模型: cam++")
            print(f"批处理大小: {1 if device == 'cpu' else 4}")
        
        # 验证模型加载
        print("验证模型加载状态...")
        test_audio = np.zeros(16000, dtype=np.float32)  # 1秒的静音用于测试
        test_result = model.generate(input=test_audio, sample_rate=16000)
        print(f"模型验证结果: {test_result}")
        print("FunASR模型加载完成")
        
        return model
        
    except Exception as e:
        print(f"错误: 加载FunASR模型失败: {str(e)}")
        import traceback
        print(traceback.format_exc())  # 打印完整的错误堆栈
        sys.exit(1)

# 初始化模型
model = init_model()

@app.route('/health')
def health_check():
    """健康检查接口"""
    device_info = {
        "status": "healthy",
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "gpu_available": torch.cuda.is_available(),
        "gpu_count": torch.cuda.device_count() if torch.cuda.is_available() else 0,
        "gpu_names": [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())] if torch.cuda.is_available() else [],
        "timestamp": datetime.now().isoformat()
    }
    return jsonify(device_info)

@app.route('/device')
def device_info():
    """设备信息接口"""
    device_info = {
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "gpu_available": torch.cuda.is_available(),
        "gpu_count": torch.cuda.device_count() if torch.cuda.is_available() else 0,
        "gpu_names": [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())] if torch.cuda.is_available() else [],
        "timestamp": datetime.now().isoformat()
    }
    return jsonify(device_info)

@app.route('/progress')
def get_progress():
    """获取当前转录进度"""
    global current_progress
    
    # 计算预估剩余时间
    if current_progress["start_time"] and current_progress["current_chunk"] > 0:
        elapsed_time = time.time() - current_progress["start_time"]
        avg_time_per_chunk = elapsed_time / current_progress["current_chunk"]
        remaining_chunks = current_progress["total_chunks"] - current_progress["current_chunk"]
        estimated_remaining = avg_time_per_chunk * remaining_chunks
        current_progress["estimated_time"] = estimated_remaining
    
    return jsonify(current_progress)

def update_progress(status, current_chunk=None, total_chunks=None, message=None):
    """更新进度信息"""
    global current_progress
    
    current_progress["status"] = status
    if current_chunk is not None:
        current_progress["current_chunk"] = current_chunk
    if total_chunks is not None:
        current_progress["total_chunks"] = total_chunks
    if message is not None:
        current_progress["message"] = message
    
    if total_chunks and total_chunks > 0:
        current_progress["progress"] = (current_progress["current_chunk"] / total_chunks) * 100
    
    if status == "processing" and current_progress["start_time"] is None:
        current_progress["start_time"] = time.time()
    elif status == "completed" or status == "error":
        current_progress["start_time"] = None
        current_progress["estimated_time"] = None

def convert_audio_to_wav(input_path, target_sample_rate=16000):
    """将音频转换为WAV格式并重采样"""
    try:
        # 使用pydub加载音频
        logger.info(f"开始转换音频文件: {input_path}")
        audio = AudioSegment.from_file(input_path)
        
        logger.info(f"原始音频信息: 通道数={audio.channels}, 采样率={audio.frame_rate}Hz, 时长={len(audio)/1000.0}秒")
        
        # 转换为单声道
        if audio.channels > 1:
            audio = audio.set_channels(1)
            logger.info("已转换为单声道")
        
        # 设置采样率
        if audio.frame_rate != target_sample_rate:
            audio = audio.set_frame_rate(target_sample_rate)
            logger.info(f"已调整采样率至 {target_sample_rate}Hz")
        
        # 调整音量
        if audio.dBFS < -30:  # 如果音量太小
            gain_needed = min(-30 - audio.dBFS, 30)  # 最多增益30dB
            audio = audio.apply_gain(gain_needed)
            logger.info(f"音量过小，已增加 {gain_needed}dB")
        
        # 创建临时文件
        temp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        temp_wav_path = temp_wav.name
        temp_wav.close()
        
        # 导出为WAV格式
        audio.export(temp_wav_path, format='wav', parameters=["-ac", "1", "-ar", str(target_sample_rate)])
        logger.info(f"音频已导出为WAV格式: {temp_wav_path}")
        
        return temp_wav_path
    except Exception as e:
        logger.error(f"音频转换失败: {str(e)}")
        raise

def normalize_audio(audio_data):
    """标准化音频数据"""
    try:
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)
        
        # 计算音频统计信息
        mean_val = np.mean(audio_data)
        std_val = np.std(audio_data)
        max_abs = np.max(np.abs(audio_data))
        
        logger.info(f"音频统计: 均值={mean_val:.6f}, 标准差={std_val:.6f}, 最大绝对值={max_abs:.6f}")
        
        # 如果音频太弱，进行放大
        if max_abs < 0.1:
            scale_factor = 0.5 / max_abs  # 放大到0.5的幅度
            audio_data = audio_data * scale_factor
            logger.info(f"音频信号较弱，已放大 {scale_factor:.2f} 倍")
        
        # 确保音频数据在 [-1, 1] 范围内
        if max_abs > 1.0:
            audio_data = audio_data / max_abs
            logger.info("音频数据已归一化到 [-1, 1] 范围")
        
        # 移除DC偏置
        audio_data = audio_data - np.mean(audio_data)
        
        return audio_data
    except Exception as e:
        logger.error(f"音频标准化失败: {str(e)}")
        raise

def _convert_timestamp_value(value):
    """FunASR时间戳转换为秒"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value > 1000:
            return value / 1000.0
        return float(value)
    return None


def process_recognition_result(result):
    """处理识别结果，返回统一结构"""
    try:
        logger.debug(f"处理识别结果: {type(result)} - {result}")

        combined_text_parts = []
        sentence_info = []
        raw_segments = []

        if isinstance(result, str):
            combined_text_parts.append(result.strip())
        elif isinstance(result, list):
            if len(result) == 0:
                logger.warning("识别结果列表为空")
            for i, item in enumerate(result):
                try:
                    if isinstance(item, dict):
                        text = (item.get('text') or item.get('result') or item.get('sentence') or '').strip()
                        if text:
                            combined_text_parts.append(text)

                        start = _convert_timestamp_value(item.get('start'))
                        end = _convert_timestamp_value(item.get('end'))

                        token_timestamps = item.get('timestamp', [])
                        if token_timestamps:
                            first_ts = token_timestamps[0]
                            last_ts = token_timestamps[-1]
                            if start is None and len(first_ts) >= 1:
                                start = _convert_timestamp_value(first_ts[0])
                            if end is None and len(last_ts) >= 2:
                                end = _convert_timestamp_value(last_ts[1])

                        if start is not None and end is not None and text:
                            sentence_info.append({
                                'text': text,
                                'start': start,
                                'end': end,
                                'word_timestamps': [
                                    [
                                        _convert_timestamp_value(ts[0]),
                                        _convert_timestamp_value(ts[1])
                                    ]
                                    for ts in token_timestamps
                                    if isinstance(ts, (list, tuple)) and len(ts) >= 2
                                ]
                            })
                        raw_segments.append(item)
                    elif isinstance(item, str):
                        combined_text_parts.append(item.strip())
                    else:
                        logger.debug(f"列表项 {i}: 未知格式 {type(item)} - {item}")
                except Exception as e:
                    logger.warning(f"处理列表项 {i} 时出错: {str(e)}")
                    continue
        elif isinstance(result, dict):
            text = (result.get('text') or result.get('result') or result.get('sentence') or '').strip()
            if text:
                combined_text_parts.append(text)

            sentence_list = result.get('sentence_info')
            if isinstance(sentence_list, list):
                for sentence in sentence_list:
                    try:
                        sent_text = (sentence.get('text') or '').strip()
                        start = _convert_timestamp_value(sentence.get('start'))
                        end = _convert_timestamp_value(sentence.get('end'))
                        if sent_text and start is not None and end is not None:
                            sentence_info.append({
                                'text': sent_text,
                                'start': start,
                                'end': end,
                                'word_timestamps': [
                                    [
                                        _convert_timestamp_value(ts[0]),
                                        _convert_timestamp_value(ts[1])
                                    ]
                                    for ts in sentence.get('timestamp', [])
                                    if isinstance(ts, (list, tuple)) and len(ts) >= 2
                                ]
                            })
                    except Exception as e:
                        logger.warning(f"处理sentence_info时出错: {str(e)}")
            raw_segments.append(result)
        elif result is None:
            combined_text_parts.append("")
        else:
            logger.warning(f"未知的识别结果格式: {type(result)} - {result}")
            if result:
                combined_text_parts.append(str(result))

        combined_text = " ".join(filter(None, combined_text_parts)).strip()
        return {
            'text': combined_text,
            'sentence_info': sentence_info,
            'raw_segments': raw_segments
        }
    except Exception as e:
        logger.error(f"处理识别结果时出错: {str(e)}")
        return {
            'text': "",
            'sentence_info': [],
            'raw_segments': []
        }

def process_audio_chunk(audio_data, sample_rate, chunk_size=30*16000, hotwords=None):
    """分块处理音频数据
    
    Args:
        audio_data: 音频数据
        sample_rate: 采样率
        chunk_size: 每个音频块的大小（默认30秒）
        hotwords: 热词列表，用于提高特定词汇的识别准确率
    """
    try:
        results = []
        total_len = len(audio_data)
        
        # 标准化音频数据
        audio_data = normalize_audio(audio_data)
        
        # 如果音频太短，直接处理整个音频
        if total_len < chunk_size:
            update_progress("processing", 0, 1, "处理短音频...")
            try:
                with torch.no_grad():
                    # print(f"\n处理短音频 (长度: {total_len/sample_rate:.2f}秒)")
                    # 修复热词参数传递
                    if hotwords:
                        # FunASR官方格式：直接使用hotword参数，空格分隔多个热词
                        hotword_string = ' '.join(hotwords)
                        logger.warning(f"🔥 短音频使用热词: '{hotword_string}'")
                        result = model.generate(
                            input=audio_data,
                            hotword=hotword_string
                        )
                    else:
                        logger.warning("🔥 短音频调用 model.generate，无热词")
                        result = model.generate(
                            input=audio_data
                        )
                    processed_result = process_recognition_result(result)
                    chunk_text = processed_result.get('text', '')

                    if chunk_text:
                        results.append(chunk_text)
                    update_progress("processing", 1, 1, "短音频处理完成")
            except Exception as e:
                print(f"处理短音频时出错: {str(e)}")
                update_progress("error", message=f"短音频处理错误: {str(e)}")
        else:
            # 分块处理长音频
            overlap = int(0.5 * sample_rate)  # 0.5秒重叠
            total_chunks = (total_len + chunk_size - 1)//chunk_size
            print(f"\n开始处理音频，总共 {total_chunks} 个块")
            update_progress("processing", 0, total_chunks, f"开始处理 {total_chunks} 个音频块...")
            
            for i in range(0, total_len, chunk_size - overlap):
                chunk = audio_data[i:min(i+chunk_size, total_len)]
                chunk_num = i//chunk_size + 1
                
                update_progress("processing", chunk_num, total_chunks, f"正在处理第 {chunk_num}/{total_chunks} 个音频块...")
                
                # 检查音频块的有效性
                chunk_max = float(np.max(np.abs(chunk))) if chunk.size else 0.0
                chunk_energy = float(np.mean(chunk**2)) if chunk.size else 0.0

                # 更保守地判断静音，避免将低音量语音当作噪声跳过
                silence_peak_threshold = 1e-4
                silence_energy_threshold = 1e-8
                if chunk_max < silence_peak_threshold and chunk_energy < silence_energy_threshold:
                    logger.debug(
                        "跳过静音块 %s/%s (峰值=%.6e, 能量=%.6e)",
                        chunk_num,
                        total_chunks,
                        chunk_max,
                        chunk_energy,
                    )
                    continue
                
                try:
                    with torch.no_grad():
                        # 修复热词参数传递
                        kwargs = {}
                        if hotwords:
                            hotword_string = ' '.join(hotwords)
                            logger.warning(f"🔥 长音频块{chunk_num}使用热词: '{hotword_string}'")
                            kwargs['hotword'] = hotword_string
                        if not MODEL_SUPPORTS_TIMESTAMP:
                            kwargs['sentence_timestamp'] = False
                        logger.debug(
                            "调用模型处理块 %s/%s，kwargs=%s",
                            chunk_num,
                            total_chunks,
                            {k: ("***" if k == "hotword" else v) for k, v in kwargs.items()},
                        )
                        result = model.generate(
                            input=chunk,
                            **kwargs
                        )
                        processed_result = process_recognition_result(result)
                        chunk_text = processed_result.get('text', '')

                        if chunk_text:
                            results.append(chunk_text)
                            # print(f"识别结果: {processed_result}")
                except Exception as e:
                    print(f"处理音频块 {chunk_num} 时出错: {str(e)}")
                    continue
        
        final_result = " ".join(results)
        print("\n音频处理完成！")
        update_progress("completed", message="音频处理完成！")
        return final_result
        
    except Exception as e:
        print(f"音频处理失败: {str(e)}")
        raise

@app.route('/recognize', methods=['POST'])
def recognize_audio():
    """处理音频文件并返回识别结果"""
    temp_files = []  # 用于跟踪需要清理的临时文件
    
    try:
        # 初始化进度
        update_progress("starting", 0, 0, "开始处理音频文件...")
        
        if 'audio' not in request.files:
            update_progress("error", message="没有找到音频文件")
            return jsonify({"error": "没有找到音频文件"}), 400
            
        audio_file = request.files['audio']
        if not audio_file:
            return jsonify({"error": "空的音频文件"}), 400
        
        # 获取原始文件信息
        original_filename = audio_file.filename
        file_size = len(audio_file.read())
        audio_file.seek(0)  # 重置文件指针
        logger.info(f"接收到音频文件: {original_filename}, 大小: {file_size/1024:.2f}KB")
        
        # 获取热词参数
        hotwords_raw = request.form.get('hotwords', '')
        logger.warning(f"🔥 FunASR接收到原始热词字符串: '{hotwords_raw}'")
        
        if hotwords_raw:
            hotwords = [word.strip() for word in hotwords_raw.split(',') if word.strip()]
            logger.warning(f"🔥 FunASR解析后的热词列表 ({len(hotwords)}个): {hotwords}")
        else:
            hotwords = []
            logger.warning("🔥 FunASR没有接收到热词参数")
        
        # 保存上传的音频文件
        orig_audio_path = os.path.join(app.config['UPLOAD_FOLDER'], 'temp_audio_orig')
        audio_file.save(orig_audio_path)
        temp_files.append(orig_audio_path)
        
        try:
            # 转换音频格式
            wav_path = convert_audio_to_wav(orig_audio_path)
            temp_files.append(wav_path)
            
            # 读取转换后的音频文件
            audio_data, sample_rate = sf.read(wav_path)
            
            # 检查音频数据是否为空或无效
            if len(audio_data) == 0:
                logger.error("音频数据为空")
                return jsonify({"error": "音频数据为空"}), 400
                
            if np.all(np.abs(audio_data) < 1e-6):
                logger.error("音频数据全为静音")
                return jsonify({"error": "音频数据全为静音"}), 400
            
            logger.info("开始音频识别...")

            hotword_string = ' '.join(hotwords) if hotwords else None
            generation_kwargs = {}
            if hotword_string:
                generation_kwargs['hotword'] = hotword_string
            if MODEL_SUPPORTS_TIMESTAMP:
                generation_kwargs['sentence_timestamp'] = True
            logger.debug(
                "整体识别调用参数: supports_timestamp=%s kwargs=%s",
                MODEL_SUPPORTS_TIMESTAMP,
                {k: ("***" if k == "hotword" else v) for k, v in generation_kwargs.items()},
            )

            parsed_result = None
            raw_result = None

            if MODEL_SUPPORTS_TIMESTAMP:
                try:
                    with torch.no_grad():
                        logger.info("调用FunASR整体识别（带VAD）")
                        raw_result = model.generate(
                            input=audio_data,
                            **generation_kwargs
                        )
                    parsed_result = process_recognition_result(raw_result)
                except Exception as e:
                    logger.error(f"整体识别（带时间戳）失败: {str(e)}")
                    logger.exception(e)
                    parsed_result = None
            else:
                logger.info("当前模型不支持时间戳，跳过整体识别，直接进入分块流程")

            if not parsed_result or not parsed_result.get('text'):
                logger.warning("整体识别结果为空，尝试分块处理")
                chunk_text = process_audio_chunk(
                    audio_data,
                    sample_rate,
                    hotwords=hotwords if hotwords else None
                )
                if chunk_text and chunk_text.strip():
                    parsed_result = {
                        'text': chunk_text.strip(),
                        'sentence_info': [],
                        'raw_segments': []
                    }
                elif MODEL_SUPPORTS_TIMESTAMP:
                    logger.warning("分块识别仍为空，尝试无时间戳的整体识别")
                    try:
                        fallback_kwargs = generation_kwargs.copy()
                        fallback_kwargs.pop('sentence_timestamp', None)
                        logger.debug(
                            "无时间戳整体识别参数: %s",
                            {k: ("***" if k == "hotword" else v) for k, v in fallback_kwargs.items()},
                        )
                        with torch.no_grad():
                            raw_result = model.generate(
                                input=audio_data,
                                **fallback_kwargs
                            )
                        parsed_result = process_recognition_result(raw_result)
                    except Exception as e:
                        logger.error(f"无时间戳整体识别失败: {str(e)}")
                        logger.exception(e)
                        parsed_result = {
                            'text': '',
                            'sentence_info': [],
                            'raw_segments': []
                        }
                else:
                    logger.warning("分块识别仍为空，模型不支持时间戳，将返回空结果")
                    parsed_result = {
                        'text': '',
                        'sentence_info': [],
                        'raw_segments': []
                    }

            text_output = parsed_result.get('text', '').strip()
            parsed_result['text'] = text_output

            logger.info("音频识别完成")

            sentence_info = parsed_result.get('sentence_info', [])

            response_data = {
                "success": True,
                "text": text_output,
                "audio_info": {
                    "original_filename": original_filename,
                    "file_size_kb": file_size/1024,
                    "duration_seconds": len(audio_data)/sample_rate,
                    "sample_rate": sample_rate
                },
                "sentence_info": sentence_info,
                "timestamp": sentence_info
            }
            return jsonify(response_data)
            
        except Exception as e:
            logger.error(f"处理音频文件时出错: {str(e)}")
            return jsonify({"error": f"处理音频文件时出错: {str(e)}"}), 500
            
    except Exception as e:
        logger.error(f"请求处理出错: {str(e)}")
        return jsonify({"error": f"请求处理出错: {str(e)}"}), 500
        
    finally:
        # 清理所有临时文件
        for temp_file in temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception as e:
                logger.error(f"清理临时文件失败: {str(e)}")

if __name__ == '__main__':
    try:
        # 确保上传目录存在
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        
        print("\n" + "="*20 + " FunASR服务启动 " + "="*20)
        print(f"服务启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"监听地址: 0.0.0.0:{10095}")
        print(f"上传目录: {app.config['UPLOAD_FOLDER']}")
        print(f"模型目录: {os.getenv('MODEL_DIR', '/app/models')}")
        print("="*56 + "\n")
        
        # 启动Flask应用
        app.run(host='0.0.0.0', port=10095, threaded=True)
    except Exception as e:
        print(f"错误: 启动FunASR服务失败: {str(e)}")
        import traceback
        print(traceback.format_exc())
        sys.exit(1)
