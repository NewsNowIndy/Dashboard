# scripts/rebuild_entities.py
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import os, re, datetime, tempfile
from sqlalchemy import text
from models import engine
from utils import decrypt_file_to_bytes
from search_text import extract_pdf_text  # your pdfminer+OCR fallback helper

NAME_RX = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b")
AGENCY_HINTS = {"sheriff","police","department","office","prosecutor","county",
                "state","city","board","court","commission","district"}

def looks_like_org(s: str) -> bool:
    lw = s.lower()
    return any(w in lw for w in AGENCY_HINTS) or s.isupper()

def insert_mentions(conn, blob: str, doc_id: int, source: str, now: str):
    # People
    for m in NAME_RX.finditer(blob):
        cand = m.group(1)
        if len(cand) < 5:
            continue
        eid = upsert(conn, cand, "person")
        conn.execute(text("""
          INSERT INTO entity_mentions (entity_id, doc_id, created_at, source)
          VALUES (:e,:d,:ts,:src)
        """), {"e": eid, "d": doc_id, "ts": now, "src": source})

    # Orgs
    for phrase in re.findall(r"[A-Za-z][A-Za-z&.\-]+(?:\s+[A-Za-z&.\-]+){1,4}", blob):
        if len(phrase) < 6:
            continue
        if looks_like_org(phrase):
            eid = upsert(conn, phrase.strip(), "org")
            conn.execute(text("""
              INSERT INTO entity_mentions (entity_id, doc_id, created_at, source)
              VALUES (:e,:d,:ts,:src)
            """), {"e": eid, "d": doc_id, "ts": now, "src": source})

def upsert(conn, name, kind):
    key = (name.lower(), kind)
    if key in ENT_CACHE:
        return ENT_CACHE[key]
    conn.execute(text("INSERT INTO entities (name, kind) VALUES (:n,:k)"),
                 {"n": name, "k": kind})
    eid = conn.execute(text("SELECT last_insert_rowid()")).scalar()
    ENT_CACHE[key] = eid
    return eid

ENT_CACHE = {}

with engine.begin() as conn:
    # Ensure tables & column
    conn.execute(text("""
      CREATE TABLE IF NOT EXISTS entities (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL
      )
    """))
    conn.execute(text("""
      CREATE TABLE IF NOT EXISTS entity_mentions (
        id INTEGER PRIMARY KEY,
        entity_id INTEGER NOT NULL,
        doc_id INTEGER NOT NULL,
        created_at TEXT,
        source TEXT NOT NULL DEFAULT 'project'
      )
    """))

    # Clear all so this pass is authoritative for both sources
    conn.execute(text("DELETE FROM entity_mentions"))
    conn.execute(text("DELETE FROM entities"))

    now = datetime.datetime.utcnow().isoformat()

    # -------- 1) PROJECT DOCS via FTS --------
    docs = list(conn.execute(text("""
      SELECT d.id AS doc_id,
             COALESCE(NULLIF(d.title,''), d.filename) AS title,
             f.body AS body
      FROM doc_fts f
      JOIN project_documents d ON d.id = f.doc_id
    """)).mappings())

    for r in docs:
        blob = ((r["title"] or "") + " " + (r["body"] or ""))
        insert_mentions(conn, blob, r["doc_id"], source="project", now=now)

    # -------- 2) FOIA ATTACHMENTS (PDFs only) --------
    atts = list(conn.execute(text("""
      SELECT a.id AS att_id,
             a.filename,
             a.stored_path,
             a.ocr_pdf_path,
             a.mime_type
      FROM foia_attachments a
      WHERE
        ( (a.mime_type IS NOT NULL AND lower(a.mime_type) LIKE 'application/pdf%')
          OR lower(COALESCE(a.filename,'')) LIKE '%.pdf'
          OR lower(COALESCE(a.stored_path,'')) LIKE '%.pdf'
          OR lower(COALESCE(a.ocr_pdf_path,'')) LIKE '%.pdf')
    """)).mappings())

    for a in atts:
        # Prefer an OCR’d path on disk if present
        path = None
        tmp_path = None
        try:
            if a["ocr_pdf_path"] and os.path.exists(a["ocr_pdf_path"]):
                path = a["ocr_pdf_path"]
            elif a["stored_path"] and os.path.exists(a["stored_path"]):
                # Decrypt to a temp file and extract
                data = decrypt_file_to_bytes(a["stored_path"])
                fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
                os.close(fd)
                with open(tmp_path, "wb") as fh:
                    fh.write(data)
                path = tmp_path

            if not path:
                continue

            body = extract_pdf_text(path) or ""
            title = a["filename"] or "attachment.pdf"
            blob = (title + " " + body)
            if blob.strip():
                insert_mentions(conn, blob, a["att_id"], source="attachment", now=now)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except: pass

print("Entities rebuilt for project documents and attachments.")
