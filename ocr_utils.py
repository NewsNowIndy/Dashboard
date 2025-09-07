import os
import tempfile
import subprocess
import fitz  # PyMuPDF
import pytesseract
from pdfminer.high_level import extract_text


def pdf_has_text(path: str) -> bool:
    try:
        text = extract_text(path)
        return bool(text and text.strip())
    except Exception:
        return False


def ocr_with_ocrmypdf(in_pdf: str, out_pdf: str) -> bool:
    try:
        result = subprocess.run([
            "ocrmypdf", "--skip-text", "--fast-web-view", "--optimize", "3", in_pdf, out_pdf
        ], capture_output=True, text=True)
        return result.returncode == 0
    except FileNotFoundError:
        return False


def ocr_with_pymupdf_tesseract(in_pdf: str, out_pdf: str) -> bool:
    # Simple fallback: render each page to image, OCR, then save as a new PDF with text layer
    try:
        doc = fitz.open(in_pdf)
        out_doc = fitz.open()
        for page in doc:
            pix = page.get_pixmap(dpi=300)
            img = fitz.Pixmap(pix, 0) if pix.alpha else pix
            img_bytes = img.tobytes("png")
            # OCR image
            txt = pytesseract.image_to_pdf_or_hocr(img_bytes, extension='pdf')
            out_doc.insert_pdf(fitz.open("pdf", txt))
        out_doc.save(out_pdf)
        out_doc.close()
        doc.close()
        return True
    except Exception:
        return False


def make_searchable(in_pdf: str, out_pdf: str) -> bool:
    if pdf_has_text(in_pdf):
        # already searchable; just copy
        try:
            import shutil
            shutil.copy2(in_pdf, out_pdf)
            return True
        except Exception:
            pass
    # try ocrmypdf first
    if ocr_with_ocrmypdf(in_pdf, out_pdf):
        return True
    # fallback
    return ocr_with_pymupdf_tesseract(in_pdf, out_pdf)