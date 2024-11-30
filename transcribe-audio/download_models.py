import os
import logging
import shutil
from modelscope import snapshot_download
from pathlib import Path
import sys

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def ensure_dir(dir_path):
    """确保目录存在，如果不存在则创建"""
    Path(dir_path).mkdir(parents=True, exist_ok=True)

def download_model(model_id, revision, cache_dir):
    """下载指定的模型"""
    try:
        logger.info(f"开始下载模型 {model_id} 到 {cache_dir}")
        
        # 设置环境变量
        os.environ['MODELSCOPE_CACHE'] = cache_dir
        os.environ['HF_HOME'] = cache_dir
        os.environ['TORCH_HOME'] = cache_dir
        
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

def main():
    # 设置模型目录
    base_dir = os.path.dirname(os.path.abspath(__file__))
    models_dir = os.path.join(base_dir, "models")
    
    # 清理旧的模型目录
    if os.path.exists(models_dir):
        logger.info("清理旧的模型目录...")
        shutil.rmtree(models_dir)
    
    # 确保模型目录存在
    ensure_dir(models_dir)
    
    # 模型配置
    models = [
        {
            "id": "damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            "revision": "v2.0.4"
        },
        {
            "id": "damo/speech_fsmn_vad_zh-cn-16k-common-pytorch",
            "revision": None  # 使用最新版本
        }
    ]
    
    # 下载所有模型
    success = True
    for model in models:
        try:
            model_path = download_model(
                model_id=model["id"],
                revision=model["revision"],
                cache_dir=models_dir
            )
            logger.info(f"模型 {model['id']} 下载成功: {model_path}")
            
            # 检查模型文件是否在正确的位置
            expected_path = os.path.join(models_dir, "hub", model["id"])
            if os.path.exists(expected_path):
                logger.info(f"模型文件位于正确的位置: {expected_path}")
            else:
                logger.warning(f"模型文件不在预期位置: {expected_path}")
                logger.warning(f"实际位置: {model_path}")
        except Exception as e:
            logger.error(f"下载模型 {model['id']} 失败: {str(e)}")
            success = False
            continue
    
    if not success:
        logger.error("部分模型下载失败，请检查错误信息并重试")
        sys.exit(1)
    else:
        logger.info(f"所有模型下载成功，模型目录: {models_dir}")
        # 列出模型目录内容
        logger.info("模型目录结构:")
        for root, dirs, files in os.walk(models_dir):
            level = root.replace(models_dir, '').count(os.sep)
            indent = ' ' * 4 * level
            logger.info(f"{indent}{os.path.basename(root)}/")
            subindent = ' ' * 4 * (level + 1)
            for f in files:
                logger.info(f"{subindent}{f}")

if __name__ == "__main__":
    main()
