# syntax=docker/dockerfile:1.6
FROM python:3.11-slim
ARG TARGETARCH
ARG DENO_VERSION=2.6.5

# 设置pip镜像源为阿里云
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip config set install.trusted-host mirrors.aliyun.com

# 配置apt并安装系统依赖
# 移除默认的 debian.sources 以避免 trixie 测试源导致 404
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    echo "deb https://mirrors.aliyun.com/debian/ bookworm main non-free-firmware" > /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/debian-security bookworm-security main non-free-firmware" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/debian/ bookworm-updates main non-free-firmware" >> /etc/apt/sources.list

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    iputils-ping \
    net-tools \
    nodejs \
    unzip \
    # firefox-esr \
    # x11vnc \
    # xvfb \
    # openbox \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Install Deno for yt-dlp JS challenge solving.
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) DENO_ARCH="x86_64-unknown-linux-gnu" ;; \
      arm64) DENO_ARCH="aarch64-unknown-linux-gnu" ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${DENO_ARCH}.zip" -o /tmp/deno.zip; \
    unzip -q /tmp/deno.zip -d /usr/local/bin; \
    rm -f /tmp/deno.zip; \
    chmod +x /usr/local/bin/deno

WORKDIR /app

# 复制并安装Python依赖
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

# 复制应用文件 - 新模块化架构
COPY app/ ./app/
COPY run_app.py .
COPY test_app_startup.py .

# 创建必要的目录（保留注释方便恢复图形环境）
RUN mkdir -p uploads videos
# RUN mkdir -p /root/.mozilla/firefox/
# RUN mkdir -p /root/.vnc && x11vnc -storepasswd password /root/.vnc/passwd
# 配置supervisor
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 暴露端口
EXPOSE 5000
# EXPOSE 5900

# 使用supervisor启动所有服务
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
