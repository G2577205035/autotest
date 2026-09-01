FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY config/config.example.yml /app/config/config.example.yml
RUN python -m pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 liema \
    && mkdir -p /app/data /app/runtime /app/artifacts /app/instance /app/logs \
    && chown -R liema:liema /app

USER liema

CMD ["uvicorn", "auto_test.web:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
