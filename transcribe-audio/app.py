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
import difflib
import re

class HotwordPostProcessor:
    """热词后处理器 - 在转录完成后进行热词匹配和替换"""
    
    def __init__(self):
        self.similarity_threshold = 0.5  # 降低相似度阈值，更容易匹配
        self.weight_boost = 1.5  # 热词权重提升
        
    def process_text_with_hotwords(self, text, hotwords, confidence_boost=0.1):
        """
        使用热词对转录文本进行后处理
        
        Args:
            text: 原始转录文本
            hotwords: 热词列表
            confidence_boost: 置信度提升值
            
        Returns:
            dict: 处理后的结果，包含修正文本和匹配信息
        """
        if not text or not hotwords:
            return {
                'original_text': text,
                'processed_text': text,
                'matches': [],
                'corrections': 0
            }
        
        logger.warning(f"🔥 开始热词后处理，原文本: '{text}'")
        logger.warning(f"🔥 使用热词 ({len(hotwords)}个): {hotwords}")
        
        # 将文本分词
        words = self._segment_text(text)
        processed_words = []
        matches = []
        corrections = 0
        
        for i, word in enumerate(words):
            # 寻找最匹配的热词
            best_match = self._find_best_hotword_match(word, hotwords)
            
            if best_match:
                hotword, similarity = best_match
                if similarity >= self.similarity_threshold:
                    logger.warning(f"🔥 热词匹配: '{word}' -> '{hotword}' (相似度: {similarity:.3f})")
                    processed_words.append(hotword)
                    matches.append({
                        'original': word,
                        'hotword': hotword,
                        'similarity': similarity,
                        'position': i
                    })
                    corrections += 1
                else:
                    processed_words.append(word)
            else:
                processed_words.append(word)
        
        processed_text = ''.join(processed_words)
        
        # 执行基于上下文的热词替换
        processed_text = self._context_based_replacement(processed_text, hotwords)
        
        result = {
            'original_text': text,
            'processed_text': processed_text,
            'matches': matches,
            'corrections': corrections,
            'hotwords_applied': len(hotwords)
        }
        
        logger.warning(f"🔥 热词后处理完成，修正 {corrections} 处，最终文本: '{processed_text}'")
        
        return result
    
    def _segment_text(self, text):
        """分词处理，保持原有格式"""
        try:
            import jieba
            # 使用jieba进行中文分词
            words = list(jieba.cut(text))
            logger.warning(f"🔥 分词结果: {words}")
            return words
        except ImportError:
            # 如果jieba不可用，使用改进的正则表达式分词
            # 按标点符号分割，同时保留中文字符序列
            import re
            # 匹配中文字符、英文单词、数字、标点符号
            tokens = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+|\d+|[^\w\s]|\s+', text)
            # 过滤空白tokens
            tokens = [token for token in tokens if token.strip()]
            logger.warning(f"🔥 正则分词结果: {tokens}")
            return tokens
    
    def _find_best_hotword_match(self, word, hotwords):
        """找到最佳匹配的热词"""
        if not word.strip():
            return None
            
        clean_word = re.sub(r'[^\w]', '', word)  # 移除标点符号
        if not clean_word:
            return None
        
        best_match = None
        best_similarity = 0
        
        for hotword in hotwords:
            # 精确匹配
            if clean_word == hotword:
                return (hotword, 1.0)
            
            # 子串匹配 - 特别适用于中文复合词
            if hotword in clean_word or clean_word in hotword:
                # 计算子串匹配的相似度
                if len(hotword) <= len(clean_word):
                    substring_similarity = len(hotword) / len(clean_word) * 0.9  # 给子串匹配稍低权重
                else:
                    substring_similarity = len(clean_word) / len(hotword) * 0.9
                
                if substring_similarity > best_similarity:
                    best_similarity = substring_similarity
                    best_match = hotword
                    logger.warning(f"🔥 子串匹配: '{clean_word}' <-> '{hotword}' (相似度: {substring_similarity:.3f})")
            
            # 模糊匹配
            similarity = difflib.SequenceMatcher(None, clean_word.lower(), hotword.lower()).ratio()
            
            # 考虑长度因素
            length_factor = min(len(clean_word), len(hotword)) / max(len(clean_word), len(hotword))
            adjusted_similarity = similarity * (0.7 + 0.3 * length_factor)
            
            if adjusted_similarity > best_similarity:
                best_similarity = adjusted_similarity
                best_match = hotword
        
        return (best_match, best_similarity) if best_match else None
    
    def _context_based_replacement(self, text, hotwords):
        """基于上下文的热词替换"""
        # 对于常见的转录错误模式进行替换
        replacements = self._generate_common_replacements(hotwords)
        
        for pattern, replacement in replacements.items():
            if pattern in text:
                text = text.replace(pattern, replacement)
                logger.warning(f"🔥 上下文替换: '{pattern}' -> '{replacement}'")
        
        return text
    
    def _generate_common_replacements(self, hotwords):
        """生成常见的替换模式"""
        replacements = {}
        
        # 为每个热词生成可能的错误识别模式
        for hotword in hotwords:
            # 转换为小写进行模糊匹配
            hotword_lower = hotword.lower()
            
            # 通用模式：处理英文单词的音近误识别
            if hotword == "ultrathink" or hotword == "Ultrathink":
                replacements.update({
                    "乌托": "ultrathink",
                    "阿尔特拉": "ultrathink", 
                    "奥特拉": "ultrathink",
                    "ultra": "ultrathink",
                    "Ultra": "ultrathink",
                    "乌尔特拉": "ultrathink",
                    "奥拉": "ultrathink"
                })
            elif hotword == "Python":
                replacements.update({
                    "派森": "Python",
                    "派桑": "Python", 
                    "皮桑": "Python",
                    "python": "Python"
                })
            elif hotword == "编程":
                replacements.update({
                    "便程": "编程",
                    "编成": "编程",
                    "变成": "编程"
                })
            elif hotword == "机器学习":
                replacements.update({
                    "机械学习": "机器学习",
                    "机器雪洗": "机器学习",
                    "机器血洗": "机器学习"
                })
            elif hotword == "教程":
                replacements.update({
                    "叫程": "教程",
                    "较程": "教程"
                })
            
            # 自动生成音近字替换模式
            # 对于英文词汇，查找可能的中文音译错误
            if re.match(r'^[a-zA-Z]+$', hotword):
                # 为英文热词生成常见的中文音译错误模式
                phonetic_variants = self._generate_phonetic_variants(hotword)
                for variant in phonetic_variants:
                    replacements[variant] = hotword
        
        return replacements
    
    def _generate_phonetic_variants(self, english_word):
        """为英文单词生成可能的中文音译变体"""
        variants = []
        word_lower = english_word.lower()
        
        # 基于英文单词的音节生成中文音译变体
        phonetic_map = {
            'ultra': ['乌尔特拉', '奥特拉', '阿尔特拉', '乌托拉'],
            'think': ['辛克', '思克', '听克', '滕克'],
            'python': ['派森', '派桑', '皮桑'],
            'java': ['加瓦', '佳瓦', '嘉瓦'],
            'docker': ['道克', '多克', '都克'],
            'kubernetes': ['库伯内蒂斯', '库贝内蒂斯'],
            'react': ['瑞艾克特', '里艾克特'],
            'angular': ['安古拉', '安格拉'],
            'github': ['吉特哈布', '基特哈布', '吉哈布'],
        }
        
        # 查找完全匹配
        if word_lower in phonetic_map:
            variants.extend(phonetic_map[word_lower])
        
        # 查找部分匹配
        for key, values in phonetic_map.items():
            if key in word_lower or word_lower in key:
                variants.extend(values)
        
        return variants

# FunASR 模型列表
FUNASR_MODELS = [
    "SenseVoiceSmall", "paraformer-zh", "paraformer-zh-streaming", "paraformer-en",
    "conformer-en", "ct-punc", "fsmn-vad", "fsmn-kws", "fa-zh", "cam++",
    "Qwen-Audio", "Qwen-Audio-Chat", "emotion2vec+large"
]

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
logger.setLevel(logging.WARNING)  # 将日志级别从INFO改为WARNING

# 确保其他库的日志级别不会太详细
logging.getLogger("modelscope").setLevel(logging.ERROR)
logging.getLogger("funasr").setLevel(logging.ERROR)
logging.getLogger("jieba").setLevel(logging.ERROR)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("filelock").setLevel(logging.WARNING)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max-limit
app.config['UPLOAD_FOLDER'] = '/app/uploads'


# 全局模型变量
model = None
hotword_processor = None

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

def download_model(model_id, revision, cache_dir):
    """下载指定的模型"""
    try:
        logger.info(f"开始下载模型 {model_id} 到 {cache_dir}")
        
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
    model_mappings = {
        "main": {
            "paraformer-zh": "damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            "paraformer-zh-streaming": "damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            "paraformer-en": "damo/speech_paraformer-large_asr_nat-en-16k-common-vocab10020",
            "conformer-en": "damo/speech_conformer_asr_nat-en-16k-common-vocab10020",
            "SenseVoiceSmall": "damo/speech_SenseVoiceSmall_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            "fa-zh": "damo/speech_FastConformer_asr_nat-zh-cn-16k-common-vocab8404",
            "Qwen-Audio": "damo/speech_qwen_audio_asr_nat-zh-cn-16k-common-vocab8404",
            "Qwen-Audio-Chat": "damo/speech_qwen_audio_chat_asr_nat-zh-cn-16k-common-vocab8404",
            "emotion2vec+large": "damo/speech_emotion2vec_large_sv_zh-cn_16k-common"
        },
        "vad": {
            "fsmn-vad": "damo/speech_fsmn_vad_zh-cn-16k-common-pytorch",
            "fsmn-kws": "damo/speech_fsmn_kws_zh-cn-16k-common-pytorch"
        },
        "punc": {
            "ct-punc": "damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch"
        },
        "spk": {
            "cam++": "damo/speech_campplus_sv_zh-cn_16k-common"
        }
    }
    
    # 检查是否是完整的模型ID（包含damo/前缀）
    if model_name.startswith("damo/"):
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

def ensure_models():
    """确保模型文件存在，如果不存在则下载"""
    # 设置模型目录
    model_dir = os.getenv("MODEL_DIR", "/app/models")
    
    # 设置环境变量
    os.environ['MODELSCOPE_CACHE'] = model_dir
    os.environ['HF_HOME'] = model_dir
    os.environ['TORCH_HOME'] = model_dir
    
    # 获取所有模型名称 - 优先使用支持第三代热词的模型
    model_name = os.getenv("FUNASR_MODEL", "SenseVoiceSmall")  # 使用更新的模型
    vad_model = os.getenv("FUNASR_VAD_MODEL", "fsmn-vad")
    punc_model = os.getenv("FUNASR_PUNC_MODEL", "ct-punc")
    spk_model = os.getenv("FUNASR_SPK_MODEL", "cam++")
    
    # 获取完整的模型ID
    model_configs = {
        "main": {"name": model_name, "id": get_model_id("main", model_name)},
        "vad": {"name": vad_model, "id": get_model_id("vad", vad_model)},
        "punc": {"name": punc_model, "id": get_model_id("punc", punc_model)},
        "spk": {"name": spk_model, "id": get_model_id("spk", spk_model)}
    }
    
    logger.info("检查模型配置：")
    for model_type, config in model_configs.items():
        logger.info(f"{model_type}模型: {config['name']} (ID: {config['id']})")
    
    # 检查所有模型文件是否存在
    model_paths = {
        model_type: os.path.join(model_dir, f"hub/{config['id']}")
        for model_type, config in model_configs.items()
    }
    
    logger.info("检查模型文件：")
    for model_type, path in model_paths.items():
        logger.info(f"{model_type}模型路径: {path}")
    
    # 检查是否需要下载模型
    missing_models = [path for path in model_paths.values() if not os.path.exists(path)]
    if missing_models:
        logger.info("部分模型文件不存在，开始下载...")
        ensure_dir(model_dir)
        
        # 下载所有缺失的模型
        success = True
        for model_type, config in model_configs.items():
            model_path = model_paths[model_type]
            if model_path in missing_models:
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
    
    return model_dir, {k: v["name"] for k, v in model_configs.items()}

# 初始化FunASR模型
def init_model():
    global model, hotword_processor  # 声明使用全局变量
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
        model_dir, model_names = ensure_models()
        
        try:
            # 尝试使用指定的模型
            model = AutoModel(
                model=model_names["main"],
                device=device,  # 使用检测到的设备
                model_dir=model_dir,
                vad_model=model_names["vad"],
                vad_kwargs={"max_single_segment_time": 60000},
                punc_model=model_names["punc"],
                spk_model=model_names["spk"],
                batch_size=1 if device == "cpu" else 4,  # GPU时使用更大的batch_size
                vad_model_dir=model_dir,
                disable_update=True,
                use_local=True,
                punc_model_dir=model_dir,
                spk_model_dir=model_dir
            )
            print(f"FunASR模型加载完成，使用设备: {device}")
            print(f"主模型: {model_names['main']}")
            print(f"VAD模型: {model_names['vad']}")
            print(f"标点模型: {model_names['punc']}")
            print(f"说话人模型: {model_names['spk']}")
            print(f"批处理大小: {1 if device == 'cpu' else 4}")
            
        except Exception as e:
            print(f"警告: 加载指定模型失败: {str(e)}")
            print("尝试使用默认模型配置")
            
            # 使用默认模型
            model = AutoModel(
                model="paraformer-zh",
                device=device,  # 使用检测到的设备
                model_dir=model_dir,
                vad_model="fsmn-vad",
                vad_kwargs={"max_single_segment_time": 60000},
                punc_model="ct-punc",
                spk_model="cam++",
                batch_size=1 if device == "cpu" else 4,  # GPU时使用更大的batch_size
                vad_model_dir=model_dir,
                disable_update=True,
                use_local=True,
                punc_model_dir=model_dir,
                spk_model_dir=model_dir
            )
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
        
        # 初始化热词后处理器
        print("初始化热词后处理器...")
        hotword_processor = HotwordPostProcessor()
        print("热词后处理器初始化完成")
        
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

def process_recognition_result(result):
    """处理识别结果，支持字符串和列表格式"""
    try:
        logger.debug(f"处理识别结果: {type(result)} - {result}")
        
        if isinstance(result, str):
            return result.strip()
        elif isinstance(result, list):
            if len(result) == 0:
                logger.warning("识别结果列表为空")
                return ""
            
            # 如果是列表，可能包含多个识别结果或时间戳信息
            text_parts = []
            for i, item in enumerate(result):
                try:
                    if isinstance(item, dict):
                        # 如果是字典格式，提取文本部分
                        text = item.get('text', '') or item.get('result', '') or item.get('sentence', '')
                        if text:
                            text_parts.append(text)
                    elif isinstance(item, str):
                        text_parts.append(item)
                    else:
                        logger.debug(f"列表项 {i}: 未知格式 {type(item)} - {item}")
                except Exception as e:
                    logger.warning(f"处理列表项 {i} 时出错: {str(e)}")
                    continue
                    
            return ' '.join(filter(None, text_parts)).strip()
        elif isinstance(result, dict):
            # 处理字典格式的结果
            text = result.get('text', '') or result.get('result', '') or result.get('sentence', '')
            return text.strip() if text else ""
        elif result is None:
            return ""
        else:
            logger.warning(f"未知的识别结果格式: {type(result)} - {result}")
            return str(result) if result else ""
    except Exception as e:
        logger.error(f"处理识别结果时出错: {str(e)}")
        return ""

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
                    
                    # 可选的额外热词后处理（作为原生hotword的补充）
                    if processed_result and hotwords:
                        hotword_result = hotword_processor.process_text_with_hotwords(processed_result, hotwords)
                        processed_result = hotword_result['processed_text']
                        logger.warning(f"🔥 短音频额外后处理结果: '{processed_result}' (修正{hotword_result['corrections']}处)")
                    
                    if processed_result:
                        results.append(processed_result)
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
                chunk_max = np.max(np.abs(chunk))
                chunk_energy = np.mean(chunk**2)
                
                # print(f"\n处理音频块 {chunk_num}/{total_chunks}")
                # print(f"块信息: 最大振幅={chunk_max:.6f}, 能量={chunk_energy:.6f}")
                
                # 使用能量和振幅双重判断是否为静音
                if chunk_max < 1e-3 or chunk_energy < 1e-6:
                    # print("跳过静音块")
                    continue
                
                try:
                    with torch.no_grad():
                        # 修复热词参数传递
                        if hotwords:
                            # FunASR官方格式：直接使用hotword参数，空格分隔多个热词
                            hotword_string = ' '.join(hotwords)
                            logger.warning(f"🔥 长音频块{chunk_num}使用热词: '{hotword_string}'")
                            result = model.generate(
                                input=chunk,
                                hotword=hotword_string
                            )
                        else:
                            logger.warning(f"🔥 长音频块{chunk_num}调用 model.generate，无热词")
                            result = model.generate(
                                input=chunk
                            )
                        processed_result = process_recognition_result(result)
                        
                        # 可选的额外热词后处理（作为原生hotword的补充）
                        if processed_result and hotwords:
                            hotword_result = hotword_processor.process_text_with_hotwords(processed_result, hotwords)
                            processed_result = hotword_result['processed_text']
                            logger.warning(f"🔥 长音频块{chunk_num}额外后处理结果: '{processed_result}' (修正{hotword_result['corrections']}处)")
                        
                        if processed_result:
                            results.append(processed_result)
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
            
            # 分块处理音频
            logger.info("开始音频识别...")
            result = process_audio_chunk(audio_data, sample_rate, hotwords=hotwords if hotwords else None)
            
            # 放宽对空结果的处理
            if not result or len(result.strip()) == 0:
                logger.warning("识别结果为空，尝试整体识别...")
                # 如果分块识别失败，尝试整体识别
                try:
                    with torch.no_grad():
                        logger.info("开始整体音频识别...")
                        # 修复热词参数传递
                        if hotwords:
                            # FunASR官方格式：直接使用hotword参数，空格分隔多个热词
                            hotword_string = ' '.join(hotwords)
                            logger.warning(f"整体识别使用热词: '{hotword_string}'")
                            raw_result = model.generate(
                                input=audio_data,
                                hotword=hotword_string
                            )
                        else:
                            logger.warning("整体识别调用 model.generate，无热词")
                            raw_result = model.generate(
                                input=audio_data
                            )
                        logger.debug(f"整体识别原始结果: {type(raw_result)} - {raw_result}")
                        result = process_recognition_result(raw_result)
                        
                        # 可选的额外热词后处理（作为原生hotword的补充）
                        if result and hotwords:
                            hotword_result = hotword_processor.process_text_with_hotwords(result, hotwords)
                            result = hotword_result['processed_text']
                            logger.warning(f"🔥 整体识别额外后处理结果: '{result}' (修正{hotword_result['corrections']}处)")
                        
                        logger.info(f"整体识别处理后结果: {result}")
                except Exception as e:
                    logger.error(f"整体识别失败: {str(e)}")
                    import traceback
                    logger.error(f"整体识别错误堆栈: {traceback.format_exc()}")
                    result = ""
            
            logger.info("音频识别完成")
            
            response_data = {
                "success": True,
                "text": result if result and len(result.strip()) > 0 else "",
                "audio_info": {
                    "original_filename": original_filename,
                    "file_size_kb": file_size/1024,
                    "duration_seconds": len(audio_data)/sample_rate,
                    "sample_rate": sample_rate
                }
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
