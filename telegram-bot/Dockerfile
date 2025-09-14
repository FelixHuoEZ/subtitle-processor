FROM python:3.9-slim

WORKDIR /app

# Install curl for healthcheck (and timezone data)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Expose health server port
EXPOSE 8081

# Docker healthcheck hitting deep health endpoint
HEALTHCHECK --interval=60s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:8081/health?deep=1 > /dev/null || exit 1

CMD ["python", "app.py"]
