# FreeRouter Operator Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY operator/pyproject.toml operator/README.md ./
RUN pip install --no-cache-dir -e .

COPY operator/handler.py operator/main.py ./

CMD ["free-router-operator"]