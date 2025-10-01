FROM python:3.9-slim

WORKDIR /app

# Install timezone data without pulling curl (network mirror may block curl deps)
RUN apt-get update \
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
