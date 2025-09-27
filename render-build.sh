#!/usr/bin/env bash
set -euxo pipefail

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  tesseract-ocr tesseract-ocr-eng qpdf ghostscript pngquant wkhtmltopdf
