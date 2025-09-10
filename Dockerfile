FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System packages needed by OCRmyPDF + wkhtmltopdf
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng \
    ocrmypdf \
    ghostscript qpdf \
    wkhtmltopdf \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App code
COPY . .

# Prepare persistent locations (Render disk will mount here)
RUN mkdir -p /var/foia/uploads /var/foia/backups

# Defaults for your config.py
ENV APP_ENV=prod \
    UPLOAD_DIR=/var/foia/uploads \
    SQLITE_PATH=/var/foia/foia.db

# Start (Render provides $PORT)
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 --timeout 120"]
