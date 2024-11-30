from flask import Flask, request, jsonify
import os
import logging
import sys
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

# FunASR 模型列表
FUNASR_MODELS = [
    "SenseVoiceSmall", "paraformer-zh", "paraformer-zh-streaming", "paraformer-en",
    "conformer-en", "ct-punc", "fsmn-vad", "fsmn-kws", "fa-zh", "cam++",
    "Qwen-Audio", "Qwen-Audio-Chat", "emotion2vec+large"
]

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max-limit
app.config['UPLOAD_FOLDER'] = '/app/uploads'

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
    
    # 获取所有模型名称
    model_name = os.getenv("FUNASR_MODEL", "paraformer-zh")
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
logger.info("正在加载FunASR模型...")
try:
    # 确保模型存在
    model_dir, model_names = ensure_models()
    
    try:
        # 尝试使用指定的模型
        model = AutoModel(
            model=model_names["main"],
            device="cpu",
            model_dir=model_dir,
            vad_model=model_names["vad"],
            vad_kwargs={"max_single_segment_time": 60000},
            punc_model=model_names["punc"],
            spk_model=model_names["spk"],
            batch_size=1,
            vad_model_dir=model_dir,
            disable_update=True,
            use_local=True
        )
        logger.info(f"成功加载所有模型")
    except Exception as e:
        # 如果指定模型失败，使用默认模型
        logger.warning(f"加载指定模型失败: {str(e)}")
        logger.info("尝试使用默认模型配置")
        model = AutoModel(
            model="paraformer-zh",
            device="cpu",
            model_dir=model_dir,
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 60000},
            punc_model="ct-punc",
            spk_model="cam++",
            batch_size=1,
            vad_model_dir=model_dir,
            disable_update=True,
            use_local=True
        )
        logger.info("成功加载默认模型配置")
    
    # 验证模型加载
    logger.info("验证模型加载状态...")
    test_audio = np.zeros(16000, dtype=np.float32)  # 1秒的静音用于测试
    test_result = model.generate(input=test_audio, sample_rate=16000)
    logger.info(f"模型验证结果: {test_result}")
    logger.info("FunASR模型加载完成")
except Exception as e:
    logger.error(f"加载FunASR模型失败: {str(e)}")
    import traceback
    logger.error(traceback.format_exc())  # 打印完整的错误堆栈
    sys.exit(1)

@app.route('/health')
def health_check():
    """健康检查接口"""
    return jsonify({"status": "healthy"})

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
    if isinstance(result, str):
        return result.strip()
    elif isinstance(result, list):
        # 如果是列表，可能包含多个识别结果或时间戳信息
        text_parts = []
        for item in result:
            if isinstance(item, dict):
                # 如果是字典格式，提取文本部分
                text = item.get('text', '') or item.get('result', '')
                if text:
                    text_parts.append(text)
            elif isinstance(item, str):
                text_parts.append(item)
        return ' '.join(filter(None, text_parts)).strip()
    elif result is None:
        return ""
    else:
        logger.warning(f"未知的识别结果格式: {type(result)}")
        return str(result)

def process_audio_chunk(audio_data, sample_rate, chunk_size=30*16000):
    """分块处理音频数据"""
    try:
        results = []
        total_len = len(audio_data)
        
        # 标准化音频数据
        audio_data = normalize_audio(audio_data)
        
        # 如果音频太短，直接处理整个音频
        if total_len < chunk_size:
            logger.info(f"音频长度较短({total_len/sample_rate:.2f}秒)，直接处理整个音频")
            try:
                with torch.no_grad():
                    result = model.generate(input=audio_data, sample_rate=sample_rate)
                    logger.info(f"短音频识别结果: {result}")
                    processed_result = process_recognition_result(result)
                    if processed_result:
                        results.append(processed_result)
            except Exception as e:
                logger.error(f"处理短音频时出错: {str(e)}")
        else:
            # 分块处理长音频
            overlap = int(0.5 * sample_rate)  # 0.5秒重叠
            for i in range(0, total_len, chunk_size - overlap):
                chunk = audio_data[i:min(i+chunk_size, total_len)]
                chunk_num = i//chunk_size + 1
                total_chunks = (total_len + chunk_size - 1)//chunk_size
                
                # 检查音频块的有效性
                chunk_max = np.max(np.abs(chunk))
                chunk_energy = np.mean(chunk**2)
                logger.info(f"块 {chunk_num} 信息: 最大振幅={chunk_max:.6f}, 能量={chunk_energy:.6f}")
                
                # 使用能量和振幅双重判断是否为静音
                if chunk_max < 1e-3 or chunk_energy < 1e-6:
                    logger.warning(f"块 {chunk_num} 可能是静音，跳过处理")
                    continue
                
                logger.info(f"处理音频块 {chunk_num}/{total_chunks} (长度: {len(chunk)/sample_rate:.2f}秒)")
                
                try:
                    with torch.no_grad():
                        result = model.generate(input=chunk, sample_rate=sample_rate)
                        logger.info(f"块 {chunk_num} 识别结果: {result}")
                        processed_result = process_recognition_result(result)
                        if processed_result:
                            results.append(processed_result)
                except Exception as e:
                    logger.error(f"处理音频块 {chunk_num} 时出错: {str(e)}")
                    continue
        
        final_result = " ".join(results)
        logger.info(f"完整识别结果: {final_result}")
        
        return final_result
        
    except Exception as e:
        logger.error(f"音频处理失败: {str(e)}")
        raise

@app.route('/recognize', methods=['POST'])
def recognize_audio():
    """处理音频文件并返回识别结果"""
    temp_files = []  # 用于跟踪需要清理的临时文件
    
    try:
        if 'audio' not in request.files:
            return jsonify({"error": "没有找到音频文件"}), 400
            
        audio_file = request.files['audio']
        if not audio_file:
            return jsonify({"error": "空的音频文件"}), 400
        
        # 获取原始文件信息
        original_filename = audio_file.filename
        file_size = len(audio_file.read())
        audio_file.seek(0)  # 重置文件指针
        logger.info(f"接收到音频文件: {original_filename}, 大小: {file_size/1024:.2f}KB")
        
        # 保存上传的音频文件
        orig_audio_path = os.path.join(app.config['UPLOAD_FOLDER'], 'temp_audio_orig')
        audio_file.save(orig_audio_path)
        temp_files.append(orig_audio_path)
        
        try:
            # 转换音频格式
            wav_path = convert_audio_to_wav(orig_audio_path)
            temp_files.append(wav_path)
            logger.info(f"音频已转换为WAV格式: {wav_path}")
            
            # 读取转换后的音频文件
            audio_data, sample_rate = sf.read(wav_path)
            logger.info(f"音频采样率: {sample_rate}Hz, 数据长度: {len(audio_data)}, 数据类型: {audio_data.dtype}")
            
            # 检查音频数据是否为空或无效
            if len(audio_data) == 0:
                logger.error("音频数据为空")
                return jsonify({"error": "音频数据为空"}), 400
                
            if np.all(np.abs(audio_data) < 1e-6):
                logger.error("音频数据全为静音")
                return jsonify({"error": "音频数据全为静音"}), 400
            
            # 分块处理音频
            logger.info("开始音频识别...")
            result = process_audio_chunk(audio_data, sample_rate)
            
            # 放宽对空结果的处理
            if not result or len(result.strip()) == 0:
                logger.warning("识别结果为空，尝试整体识别...")
                # 如果分块识别失败，尝试整体识别
                try:
                    with torch.no_grad():
                        result = model.generate(input=audio_data, sample_rate=sample_rate)
                        logger.info(f"整体识别结果: {result}")
                        result = process_recognition_result(result)
                except Exception as e:
                    logger.error(f"整体识别失败: {str(e)}")
                    result = ""
            
            logger.info("音频识别完成")
            
            response_data = {
                "success": True,
                "text": result if result and len(result.strip()) > 0 else "",
                "audio_info": {
                    "original_filename": original_filename,
                    "file_size_kb": file_size/1024,
                    "duration_seconds": len(audio_data)/sample_rate,
                    "sample_rate": sample_rate,
                    "max_amplitude": float(np.max(np.abs(audio_data)))
                }
            }
            logger.info(f"返回结果: {json.dumps(response_data, ensure_ascii=False)}")
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
                    logger.info(f"已清理临时文件: {temp_file}")
            except Exception as e:
                logger.error(f"清理临时文件失败: {str(e)}")

if __name__ == '__main__':
    # 确保上传目录存在
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    logger.info("启动FunASR服务...")
    app.run(host='0.0.0.0', port=10095, threaded=True)
