# Smart Waste Reporting System — production image
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --shell /bin/bash appuser

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app.py .
COPY templates ./templates
COPY static ./static

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 5000

# PORT is set by many hosts (Railway, Render, Heroku). Default 5000 for local Docker.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --threads 2 --timeout 120 app:app"]
