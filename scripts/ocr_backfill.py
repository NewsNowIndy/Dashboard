# scripts/ocr_backfill.py

# --- make project root importable BEFORE anything else ---
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tempfile
from sqlalchemy import text
from config import Config
from models import SessionLocal, engine, ProjectDocument, FoiaAttachment
from ocr_utils import make_searchable           # must run: ocrmypdf -l eng --skip-text in out
from utils import decrypt_file_to_bytes
from search_text import extract_pdf_text


def ensure_fts():
    with engine.begin() as conn:
        conn.execute(text("""
          CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
            doc_id UNINDEXED,
            title,
            body,
            content='',
            tokenize='porter'
          );
        """))


def is_pdf_row(fn, mt):
    fn = (fn or "").lower()
    mt = (mt or "").lower()
    return fn.endswith(".pdf") or mt.startswith("application/pdf")


def ocr_in_place(path):
    """Make the file at `path` searchable; replace atomically."""
    if not path or not os.path.exists(path):
        return False
    tmp_out = path + ".ocr.tmp.pdf"
    ok = False
    try:
        ok = bool(make_searchable(path, tmp_out, lang=getattr(Config, "OCR_LANG", "eng")))
        if ok and os.path.exists(tmp_out):
            os.replace(tmp_out, path)  # atomic on same fs
            return True
    finally:
        try:
            os.unlink(tmp_out)
        except Exception:
            pass
    return False


def main():
    os.makedirs(getattr(Config, "OCR_CACHE", "ocr_cache"), exist_ok=True)
    ensure_fts()
    with engine.begin() as conn:
        conn.execute(text("""
        -- Keep FTS rows for BOTH project documents and attachments
        DELETE FROM doc_fts
        WHERE doc_id NOT IN (
            SELECT id FROM project_documents
            UNION
            SELECT id FROM foia_attachments
        )
        """))

    db = SessionLocal()
    try:
        # ---- ProjectDocument: OCR in-place and (re)index FTS for search page ----
        docs = (
            db.query(ProjectDocument)
              .filter(
                  (ProjectDocument.filename.ilike('%.pdf')) |
                  (ProjectDocument.mime_type.ilike('application/pdf%'))
              )
              .all()
        )
        pd_done = 0
        for d in docs:
            if d.stored_path and os.path.exists(d.stored_path):
                # OCR in place; --skip-text avoids duplicate OCR when text already exists
                try:
                    if ocr_in_place(d.stored_path):
                        pd_done += 1
                except Exception:
                    pass

                # (Re)index into FTS used by /search
                try:
                    body = extract_pdf_text(d.stored_path) or ""
                except Exception:
                    body = ""
                db.execute(text("DELETE FROM doc_fts WHERE doc_id=:id"), {"id": d.id})
                db.execute(text("""
                    INSERT INTO doc_fts (doc_id, title, body)
                    VALUES (:id, :title, :body)
                """), {"id": d.id, "title": (d.title or d.filename or "Untitled"), "body": body})

        # ---- FoiaAttachment: produce searchable copy into OCR_CACHE (viewing uses ocr_pdf_path) ----
        atts = (
            db.query(FoiaAttachment)
              .filter(
                  FoiaAttachment.filename.ilike('%.pdf') |
                  FoiaAttachment.mime_type.ilike('application/pdf%')
              )
              .all()
        )
        fa_done = 0
        for a in atts:
            enc = a.stored_path or ""

            # If no OCR copy yet, create one (if we have the encrypted blob on disk)
            if (not a.ocr_pdf_path) and enc and os.path.exists(enc):
                try:
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_in:
                        tmp_in.write(decrypt_file_to_bytes(enc))
                        tmp_in.flush()
                        out_pdf = os.path.join(Config.OCR_CACHE, f"att-{a.id}.pdf")
                        ok = make_searchable(tmp_in.name, out_pdf, lang=getattr(Config, "OCR_LANG", "eng"))
                    try:
                        os.unlink(tmp_in.name)
                    except Exception:
                        pass

                    if ok and os.path.exists(out_pdf):
                        a.ocr_pdf_path = out_pdf
                        fa_done += 1
                except Exception:
                    pass

            # (Re)index FOIA attachment into FTS for /search
            src = None
            try:
                if a.ocr_pdf_path and os.path.exists(a.ocr_pdf_path):
                    src = a.ocr_pdf_path
                elif enc and os.path.exists(enc):
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_in:
                        tmp_in.write(decrypt_file_to_bytes(enc))
                        tmp_in.flush()
                        src = tmp_in.name
                else:
                    # nothing readable to index
                    continue

                try:
                    body = extract_pdf_text(src) or ""
                except Exception:
                    body = ""

                db.execute(text("DELETE FROM doc_fts WHERE doc_id=:id"), {"id": a.id})
                db.execute(text("""
                    INSERT INTO doc_fts (doc_id, title, body)
                    VALUES (:id, :title, :body)
                """), {"id": a.id, "title": (a.filename or f"Attachment {a.id}"), "body": body})
            finally:
                # clean up temp decrypt if we made one
                if src and (src != a.ocr_pdf_path):
                    try:
                        os.unlink(src)
                    except Exception:
                        pass

        db.commit()
        print(f"OCR backfill complete. ProjectDocument OCR'd: {pd_done}; FoiaAttachment OCR'd: {fa_done}.")
        # Optional: quick count
        total_fts = db.execute(text("SELECT COUNT(*) FROM doc_fts")).scalar()
        print("doc_fts rows:", total_fts)

    finally:
        db.close()


if __name__ == "__main__":
    main()
