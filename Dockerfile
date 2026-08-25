FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Pillow needs these at runtime for JPEG/PNG/WebP and FreeType text rendering.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
        libfreetype6 \
        libwebp7 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        "fastapi>=0.115" "uvicorn[standard]>=0.32" \
        "sqlalchemy[asyncio]>=2.0.36" "asyncpg>=0.30" "alembic>=1.14" \
        "redis>=5.2" "pydantic>=2.10" "pydantic-settings>=2.6" \
        "httpx>=0.28" "jinja2>=3.1" "python-multipart>=0.0.19" \
        "pillow>=11.0" "cryptography>=44.0" "structlog>=24.4" \
        "itsdangerous>=2.2" "argon2-cffi>=23.1"

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN mkdir -p /data/media

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
