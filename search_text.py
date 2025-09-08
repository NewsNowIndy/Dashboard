# search_text.py
from pdfminer.high_level import extract_text as _pdfminer_extract
import os, subprocess

# --- OCR deps ---
try:
    import fitz  # PyMuPDF
    from PIL import Image
    import pytesseract
except Exception:
    fitz = None
    Image = None
    pytesseract = None

# Optional: allow overriding the tesseract binary path (e.g., /opt/homebrew/bin/tesseract)
_TESS = os.getenv("TESSERACT_CMD")
if _TESS and pytesseract:
    pytesseract.pytesseract.tesseract_cmd = _TESS


def _pdfminer_text(path: str) -> str:
    try:
        return _pdfminer_extract(path) or ""
    except Exception:
        return ""


def _ocr_pdf_text(path: str, lang: str = "eng", dpi: int = 300, max_pages: int | None = None) -> str:
    """
    Render pages with PyMuPDF and OCR with Tesseract. Returns a plaintext string.
    """
    if not (fitz and Image and pytesseract):
        return ""  # OCR unavailable

    try:
        doc = fitz.open(path)
    except Exception:
        return ""

    parts: list[str] = []
    n = len(doc)
    page_range = range(n if max_pages is None else min(max_pages, n))

    for i in page_range:
        try:
            page = doc[i]
            pix = page.get_pixmap(dpi=dpi)
            mode = "RGBA" if pix.alpha else "RGB"
            img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
            if mode == "RGBA":
                img = img.convert("RGB")
            txt = pytesseract.image_to_string(img, lang=lang)
            if txt:
                parts.append(txt)
        except Exception:
            # skip bad page; continue
            continue

    return "\n".join(parts).strip()


def extract_pdf_text(path: str, *, ocr_fallback: bool = True, ocr_lang: str = "eng") -> str:
    """
    Primary entry point used elsewhere in the app.
    1) Try pdfminer (fast; works for digital PDFs)
    2) If very little text found AND ocr_fallback=True, run OCR.
    """
    txt = _pdfminer_text(path)
    if ocr_fallback and len((txt or "").strip()) < 20:
        ocr_txt = _ocr_pdf_text(path, lang=ocr_lang)
        if len((ocr_txt or "").strip()) > len((txt or "").strip()):
            return ocr_txt
    return txt or ""

def make_searchable_pdf(in_path: str, *, lang: str = "eng") -> str:
    """
    Runs ocrmypdf to produce a searchable PDF if needed.
    Returns the path to the NEW file (e.g., <in_path>.ocr.pdf).
    """
    base, ext = os.path.splitext(in_path)
    out_path = f"{base}.ocr.pdf"
    # --skip-text: don't re-OCR pages that already have text
    # You can add extras like --deskew, --rotate-pages, etc.
    subprocess.run(
        ["ocrmypdf", "-l", lang, "--skip-text", in_path, out_path],
        check=True
    )
    return out_path
