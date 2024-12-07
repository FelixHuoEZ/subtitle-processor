FROM python:3.9-slim

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    || (sleep 2 && apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*)

# 设置工作目录
WORKDIR /app

# 首先只复制requirements.txt
COPY requirements.txt .

# 安装Python依赖
RUN pip install -i https://mirrors.aliyun.com/pypi/simple/ --no-cache-dir -r requirements.txt && \
    pip install -i https://mirrors.aliyun.com/pypi/simple/ --no-cache-dir torch torchaudio && \
    pip install -i https://mirrors.aliyun.com/pypi/simple/ --no-cache-dir onnx onnxconverter-common && \
    pip install -i https://mirrors.aliyun.com/pypi/simple/ --no-cache-dir "funasr @ git+https://github.com/alibaba-damo-academy/FunASR.git"

# 复制应用程序文件
COPY . .

# 创建必要的目录
RUN mkdir -p /app/uploads /app/models

# 设置环境变量
ENV MODEL_DIR=/app/models \
    MODELSCOPE_CACHE=/app/models \
    HF_HOME=/app/models \
    TORCH_HOME=/app/models

# 暴露端口
EXPOSE 10095

# 启动应用
CMD ["python", "app.py"]