# tips_sync.py
from sqlalchemy import select
from datetime import datetime, timezone
from models import SessionLocal, Tip
from globaleaks_client import list_tips
import requests

def _parse_created(row):
    iso = row.get("created_at")
    if iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except Exception:
            pass
    if isinstance(row.get("creation_date"), (int, float)):
        return datetime.fromtimestamp(row["creation_date"], tz=timezone.utc)
    return datetime.now(tz=timezone.utc)

def sync_once():
    try:
        rows = list_tips()
        db = SessionLocal()
        inserted, updated = 0, 0
        try:
            existing = {t.glk_id: t for t in db.execute(select(Tip)).scalars().all()}
            for r in rows:
                glk_id = r.get("id")
                if not glk_id:
                    continue
                t = existing.get(glk_id)
                created = _parse_created(r)
                title = r.get("title") or (f"Tip {r.get('wb_tip_id')}" if r.get("wb_tip_id") else "Untitled Tip")
                summary = r.get("summary") or ""
                if not t:
                    t = Tip(glk_id=glk_id, status=r.get("status"), title=title, summary=summary, created_at=created)
                    db.add(t); inserted += 1
                else:
                    if r.get("status"): t.status = r["status"]
                    if title:           t.title  = title
                    if summary:         t.summary = summary
                    updated += 1
            db.commit()
        finally:
            db.close()
        return inserted, updated
    except requests.HTTPError as e:
        resp = getattr(e, "response", None)
        body = ""
        try: body = resp.text
        except Exception: pass
        print(f"tips_sync HTTP {getattr(resp, 'status_code', '')} body: {body}")
        raise
