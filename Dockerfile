FROM python:3.9-slim

# 设置pip镜像源为阿里云
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip config set install.trusted-host mirrors.aliyun.com

# 配置apt并安装系统依赖
# 移除默认的 debian.sources 以避免 trixie 测试源导致 404
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    echo "deb https://mirrors.aliyun.com/debian/ bookworm main non-free-firmware" > /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/debian-security bookworm-security main non-free-firmware" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/debian/ bookworm-updates main non-free-firmware" >> /etc/apt/sources.list && \
    apt-get clean && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    iputils-ping \
    net-tools \
    firefox-esr \
    x11vnc \
    xvfb \
    openbox \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制并安装Python依赖
COPY requirements.txt .
RUN pip install -r requirements.txt

# 安装 bgutil-ytdlp-pot-provider 插件用于自动处理 PO Token
RUN pip install -U bgutil-ytdlp-pot-provider

# 复制应用文件 - 新模块化架构
COPY app/ ./app/
COPY run_app.py .
COPY test_app_startup.py .

# 创建必要的目录
RUN mkdir -p uploads videos
RUN mkdir -p /root/.mozilla/firefox/

# 设置VNC密码
RUN mkdir -p /root/.vnc && x11vnc -storepasswd password /root/.vnc/passwd

# 配置supervisor
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 暴露端口
EXPOSE 5000
EXPOSE 5900

# 使用supervisor启动所有服务
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
