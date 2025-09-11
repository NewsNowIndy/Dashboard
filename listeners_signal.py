# listeners_signal.py
import os
from datetime import datetime
from sqlalchemy import text
from config import Config
from models import SessionLocal, Project, FoiaRequest, ProjectDocument, WorkbenchDataset, MediaItem
from utils_signal import send_signal_group  # you already use this in send-alerts
from events import on  # if your events.py lacks @on, see the NOTE below

APP_BASE = os.getenv("APP_BASE_URL", getattr(Config, "APP_BASE_URL", "http://localhost:5000"))
GROUP_ID = os.getenv("SIGNAL_GROUP_ID")  # <-- set this in Render/your env

def _p(s: str) -> str:
    return s.replace("\n", " ").strip()

def _link(path: str) -> str:
    return f"{APP_BASE.rstrip('/')}/{path.lstrip('/')}"

def _send(msg: str):
    if not GROUP_ID:
        return  # silently no-op if not configured
    try:
        send_signal_group(GROUP_ID, msg)
    except Exception:
        # avoid crashing the request; logs will show traceback
        import traceback; traceback.print_exc()

@on("project.status_changed")
def _project_status_changed(_, project_id: int, old: str, new: str):
    db = SessionLocal()
    try:
        p = db.get(Project, project_id)
        if not p: return
        url = _link(f"projects/{p.slug}")
        _send(_p(f"🧭 Project status: “{p.name}” → {old} ➜ {new}\n{url}"))
    finally:
        db.close()

@on("foia.status_changed")
def _foia_status_changed(_, foia_request_id: int, old: str, new: str):
    db = SessionLocal()
    try:
        r = db.get(FoiaRequest, foia_request_id)
        if not r: return
        ref = r.reference_number or f"FOIA #{r.id}"
        url = _link(f"requests/{r.id}")
        agency = f" ({r.agency})" if r.agency else ""
        _send(_p(f"📄 FOIA status: {ref}{agency} → {old} ➜ {new}\n{url}"))
    finally:
        db.close()

@on("document.uploaded")
def _document_uploaded(_, doc_id: int, project_id: int | None = None):
    db = SessionLocal()
    try:
        d = db.get(ProjectDocument, doc_id)
        if not d: return
        proj = db.query(Project).filter(Project.id == project_id).first() if project_id else \
               db.query(Project).filter(Project.slug == d.project_slug).first()
        p_name = proj.name if proj else d.project_slug
        url = _link(f"projects/{proj.slug if proj else d.project_slug}")
        _send(_p(f"📎 New document in “{p_name}”: {d.title or d.filename}\n{url}"))
    finally:
        db.close()

@on("workbench.dataset_created")
def _dataset_created(_, dataset_id: int, project_id: int | None = None):
    db = SessionLocal()
    try:
        ds = db.get(WorkbenchDataset, dataset_id)
        if not ds: return
        proj = db.get(Project, project_id) if project_id else None
        p_label = f" (Project: {proj.name})" if proj else ""
        url = _link(f"workbench/{ds.id}")
        _send(_p(f"🧮 New workbench dataset: {ds.name}{p_label}\n{url}"))
    finally:
        db.close()

@on("media.transcribed")
def _media_transcribed(_, media_id: int, project_id: int | None = None):
    db = SessionLocal()
    try:
        m = db.get(MediaItem, media_id)
        if not m: return
        proj = db.get(Project, project_id) if project_id else m.project
        p_name = f" ({proj.name})" if proj else ""
        url = _link(f"media/{m.id}")
        _send(f"🎙️ New transcript{p_name}: {m.title or m.filename}\n{url}")
    finally:
        db.close()
