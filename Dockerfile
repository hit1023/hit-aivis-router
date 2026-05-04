FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# ReDoc・Swagger UI の JS/CSS をローカルにバンドル（CDN不要）
RUN mkdir -p /srv/static && \
    wget -q -O /srv/static/redoc.standalone.js \
        https://cdn.jsdelivr.net/npm/redoc@latest/bundles/redoc.standalone.js && \
    wget -q -O /srv/static/swagger-ui-bundle.js \
        https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js && \
    wget -q -O /srv/static/swagger-ui.css \
        https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
