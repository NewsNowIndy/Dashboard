# listeners_signal.py
# pyright: reportUnusedFunction=false
import os
from urllib.parse import urljoin
from datetime import datetime
from sqlalchemy import text
from config import Config
from models import SessionLocal, Project, FoiaRequest, ProjectDocument, WorkbenchDataset, MediaItem, FoiaRequest
from utils_signal import send_signal_group  # you already use this in send-alerts
from events import on  # if your events.py lacks @on, see the NOTE below

APP_BASE = os.getenv("APP_BASE_URL", getattr(Config, "APP_BASE_URL", "http://localhost:5000"))
GROUP_ID = os.getenv("SIGNAL_GROUP_ID")  # <-- set this in Render/your env

def _p(s: str) -> str:
    return s.replace("\n", " ").strip()

def _abs(path: str) -> str:
    return urljoin(APP_BASE, path.lstrip("/"))

def _group_id() -> str | None:
    return os.getenv("SIGNAL_GROUP_ID", getattr(Config, "SIGNAL_GROUP_ID", None))

def _send(text: str) -> None:
    gid = _group_id()
    if not gid:
        print("[signal] No SIGNAL_GROUP_ID set; skipping send. Message was:\n", text)
        return
    try:
        send_signal_group(gid, text)
        print("[signal] sent to group:", gid)
    except Exception as e:
        print("[signal] ERROR sending to Signal:", e)

# --- Document uploaded ---
@on("document.uploaded")
def _on_doc_uploaded(_name, doc_id: int, project_id: int | None = None):
    db = SessionLocal()
    try:
        d = db.get(ProjectDocument, doc_id)

        # resolve project whether d exists or not
        if project_id:
            p = db.get(Project, project_id)
        else:
            p = db.query(Project).filter(Project.slug == (d.project_slug if d else None)).first() if d else None

        # SAFE title computation (don't touch attributes if d is None)
        title = f"doc #{doc_id}"
        if d:
            title = (d.title or d.filename or title)

        p_name = f" ({p.name})" if p else ""
        url = _abs(f"projects/{p.slug}") if p else APP_BASE

        _send(f"📄 New document{p_name}: {title}\n{url}")
    finally:
        db.close()

# --- Project status changed ---
@on("project.status_changed")
def _on_project_status(_name, project_id: int, old: str, new: str):
    db = SessionLocal()
    try:
        p = db.get(Project, project_id)
        if not p: return
        url = _abs(f"projects/{p.slug}")
        _send(f"📌 Project status: {p.name} — {old} → {new}\n{url}")
    finally:
        db.close()

# --- FOIA status changed ---
@on("foia.status_changed")
def _on_foia_status(_name, foia_request_id: int, old: str, new: str):
    db = SessionLocal()
    try:
        r = db.get(FoiaRequest, foia_request_id)
        if not r: return
        proj = db.get(Project, r.project_id) if r.project_id else None
        p_name = f" ({proj.name})" if proj else ""
        url = _abs(f"requests/{r.id}")
        ref = r.reference_number or f"Request #{r.id}"
        _send(f"📬 FOIA status{p_name}: {ref} — {old} → {new}\n{url}")
    finally:
        db.close()

# --- Workbench dataset created ---
@on("workbench.dataset_created")
def _on_dataset_created(_name, dataset_id: int, project_id: int | None = None):
    db = SessionLocal()
    try:
        ds = db.get(WorkbenchDataset, dataset_id)
        proj = db.get(Project, project_id) if project_id else None
        p_name = f" ({proj.name})" if proj else ""
        url = _abs(f"workbench/{ds.id}") if ds else APP_BASE
        _send(f"🧮 New dataset{p_name}: {ds.name if ds else f'#{dataset_id}'}\n{url}")
    finally:
        db.close()

print("[signal] listeners_signal loaded; APP_BASE =", APP_BASE)

@on("media.transcribed")
def _on_media_transcribed(_, media_id: int, project_id: int | None = None):
    db = SessionLocal()
    try:
        m = db.get(MediaItem, media_id)
        if not m: return
        proj = db.get(Project, project_id) if project_id else m.project
        p_name = f" ({proj.name})" if proj else ""
        url = _abs(f"media/{m.id}")
        _send(f"🎙️ New transcript{p_name}: {m.title or m.filename}\n{url}")
    finally:
        db.close()
