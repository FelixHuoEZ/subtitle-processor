FROM python:3.9-slim
ARG TARGETARCH

WORKDIR /app

# Switch apt sources to faster mirror before installing dependencies
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && echo "deb https://mirrors.aliyun.com/debian trixie main non-free-firmware" > /etc/apt/sources.list \
    && echo "deb https://mirrors.aliyun.com/debian trixie-updates main non-free-firmware" >> /etc/apt/sources.list \
    && echo "deb https://mirrors.aliyun.com/debian-security trixie-security main non-free-firmware" >> /etc/apt/sources.list

# Install timezone data without pulling curl (network mirror may block curl deps)
RUN --mount=type=cache,id=apt-cache-${TARGETARCH},target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=apt-lists-${TARGETARCH},target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Expose health server port
EXPOSE 8081

# Docker healthcheck hitting deep health endpoint
HEALTHCHECK --interval=60s --timeout=5s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health?deep=1').read()"]

CMD ["python", "app.py"]
