#!/usr/bin/env bash
set -euxo pipefail

# 1) System packages needed by OCR & PDF bits
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  tesseract-ocr \
  tesseract-ocr-eng \
  qpdf \
  ghostscript \
  pngquant \
  wkhtmltopdf

# keep the image small
rm -rf /var/lib/apt/lists/*

# 2) Python deps
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3) (optional) your signal-cli step, if you want it in build layer
# ./scripts/install-signal-cli.sh  # or inline your existing commands
