FROM nvidia/cuda:11.8.0-base-ubuntu22.04
ARG TARGETARCH
ARG UBUNTU_RELEASE=jammy

# 设置Python版本
ENV PYTHON_VERSION=3.9
ENV DEBIAN_FRONTEND=noninteractive

# 安装Python和系统依赖
RUN set -eux; \
    ARCH="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    if [ "${ARCH}" = "arm64" ]; then \
        BASE_URL="https://mirrors.aliyun.com/ubuntu-ports"; \
    else \
        BASE_URL="https://mirrors.aliyun.com/ubuntu"; \
    fi; \
    printf "deb %s %s main restricted universe multiverse\n" "${BASE_URL}" "${UBUNTU_RELEASE}" > /etc/apt/sources.list; \
    printf "deb %s %s-updates main restricted universe multiverse\n" "${BASE_URL}" "${UBUNTU_RELEASE}" >> /etc/apt/sources.list; \
    printf "deb %s %s-backports main restricted universe multiverse\n" "${BASE_URL}" "${UBUNTU_RELEASE}" >> /etc/apt/sources.list; \
    printf "deb %s %s-security main restricted universe multiverse\n" "${BASE_URL}" "${UBUNTU_RELEASE}" >> /etc/apt/sources.list; \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        python${PYTHON_VERSION} \
        python3-pip \
        python3-setuptools \
        ffmpeg \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 首先只复制requirements.txt
COPY requirements.txt .

# 安装Python依赖
RUN set -eux; \
    pip install --no-cache-dir -r requirements.txt; \
    ARCH="$(dpkg --print-architecture)"; \
    if [ "${ARCH}" = "amd64" ]; then \
        pip install --no-cache-dir torch==2.0.1+cu118 torchaudio==2.0.2+cu118 --extra-index-url https://download.pytorch.org/whl/cu118; \
    else \
        pip install --no-cache-dir torchaudio==2.0.2; \
    fi; \
    pip install --no-cache-dir onnx onnxconverter-common; \
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
