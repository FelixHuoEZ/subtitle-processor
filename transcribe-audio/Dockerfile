FROM nvidia/cuda:11.8.0-base-ubuntu22.04

# 设置Python版本
ENV PYTHON_VERSION=3.9
ENV DEBIAN_FRONTEND=noninteractive

# 安装Python和系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python${PYTHON_VERSION} \
    python3-pip \
    python3-setuptools \
    ffmpeg \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    || (sleep 2 && apt-get update && apt-get install -y --no-install-recommends \
    python${PYTHON_VERSION} \
    python3-pip \
    python3-setuptools \
    ffmpeg \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*)

# 设置工作目录
WORKDIR /app

# 首先只复制requirements.txt
COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir torch==2.0.1+cu118 torchaudio==2.0.2+cu118 --extra-index-url https://download.pytorch.org/whl/cu118 && \
    pip install --no-cache-dir onnx onnxconverter-common && \
    pip install --no-cache-dir "funasr @ git+https://github.com/alibaba-damo-academy/FunASR.git"

# 复制应用程序文件
COPY . .

# 创建必要的目录
RUN mkdir -p /app/uploads /app/models

# 设置环境变量
ENV MODEL_DIR=/app/models \
    MODELSCOPE_CACHE=/app/models \
    HF_HOME=/app/models \
    TORCH_HOME=/app/models \
    PYTHONUNBUFFERED=1

# 暴露端口
EXPOSE 10095

# 启动应用
CMD ["python3", "app.py"]