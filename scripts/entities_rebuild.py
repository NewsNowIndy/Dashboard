# scripts/entities_rebuild.py
import os, sys, re, datetime
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import text
from models import engine

PERSON_RX = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b")
AGENCY_HINTS = {"sheriff","police","department","office","prosecutor","county","state","city","board","court","division","bureau","agency","authority"}

def looks_like_org(s: str) -> bool:
    lw = s.lower()
    return any(w in lw for w in AGENCY_HINTS) or s.isupper()

with engine.begin() as conn:
    # ensure tables
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
        created_at TEXT
      )
    """))

    # clear and rebuild
    conn.execute(text("DELETE FROM entity_mentions"))
    conn.execute(text("DELETE FROM entities"))

    rows = list(conn.execute(text("""
      SELECT d.id   AS doc_id,
             COALESCE(NULLIF(d.title,''), d.filename) AS title,
             f.body AS body
      FROM doc_fts f
      JOIN project_documents d ON d.id = f.doc_id
    """)).mappings())

    ent_cache = {}  # (name.lower(), kind) -> id
    now = datetime.datetime.utcnow().isoformat()

    def upsert(name: str, kind: str) -> int:
        key = (name.lower(), kind)
        eid = ent_cache.get(key)
        if eid:
            return eid
        conn.execute(text("INSERT INTO entities (name, kind) VALUES (:n,:k)"), {"n": name, "k": kind})
        eid = conn.execute(text("SELECT last_insert_rowid()")).scalar()
        ent_cache[key] = eid
        return eid

    for r in rows:
        blob = f"{r['title'] or ''}\n{r['body'] or ''}"

        # people
        for m in PERSON_RX.finditer(blob):
            cand = m.group(1)
            if len(cand) < 5:
                continue
            eid = upsert(cand, "person")
            conn.execute(
                text("INSERT INTO entity_mentions (entity_id, doc_id, created_at) VALUES (:e,:d,:ts)"),
                {"e": eid, "d": r["doc_id"], "ts": now}
            )

        # orgs (very naive)
        for phrase in re.findall(r"[A-Za-z][A-Za-z&.\-]+(?:\s+[A-Za-z&.\-]+){1,4}", blob):
            if len(phrase) < 6:
                continue
            if looks_like_org(phrase):
                eid = upsert(phrase.strip(), "org")
                conn.execute(
                    text("INSERT INTO entity_mentions (entity_id, doc_id, created_at) VALUES (:e,:d,:ts)"),
                    {"e": eid, "d": r["doc_id"], "ts": now}
                )

print("Entities rebuilt. Visit /entities")
