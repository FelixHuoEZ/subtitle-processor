FROM python:3.9-slim

# 设置pip镜像源为阿里云
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip config set install.trusted-host mirrors.aliyun.com

# 配置apt并安装系统依赖
RUN echo "deb https://mirrors.aliyun.com/debian/ bullseye main non-free contrib" > /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/debian-security bullseye-security main" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.aliyun.com/debian/ bullseye-updates main non-free contrib" >> /etc/apt/sources.list && \
    apt-get clean && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    iputils-ping \
    net-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制并安装Python依赖
COPY requirements.txt .
RUN pip install -r requirements.txt

# 复制应用文件
COPY app/app.py .
COPY app/* ./

# 创建必要的目录
RUN mkdir -p uploads videos

EXPOSE 5000

CMD ["python", "app.py"]
