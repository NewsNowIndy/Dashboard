# routes_media.py
import io, os, json, tempfile, subprocess, mimetypes
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, current_app
from flask_login import login_required
from werkzeug.utils import secure_filename
from sqlalchemy import text
from config import Config
from models import SessionLocal, MediaItem, Project
from search_text import extract_pdf_text  # not used here but keeping pattern
from events import emit

bp = Blueprint("media", __name__)

ALLOWED_MEDIA = {".mp3", ".wav", ".m4a", ".mp4", ".mov", ".mkv", ".aac", ".flac", ".webm"}

def _guess_mime(path):
    mt, _ = mimetypes.guess_type(path)
    return mt or "application/octet-stream"

def _index_av_fts(conn, media_id: int, title: str, body: str):
    conn.execute(text("DELETE FROM av_fts WHERE media_id=:i"), {"i": media_id})
    conn.execute(text("INSERT INTO av_fts (media_id, title, body) VALUES (:i,:t,:b)"),
                 {"i": media_id, "t": title or "", "b": body or ""})

def _transcribe(path: str):
    """
    Try faster-whisper; fall back to openai-whisper CLI if available.
    Returns (full_text, segments:list[{start,end,text}], duration_seconds or None)
    """
    # 1) faster-whisper (pip install faster-whisper)
    try:
        from faster_whisper import WhisperModel
        model_size = os.getenv("TRANSCRIBE_MODEL", "base")
        model = WhisperModel(model_size, compute_type=os.getenv("WHISPER_COMPUTE", "int8"))
        segments, info = model.transcribe(path, vad_filter=True, vad_parameters=dict(min_silence_duration_ms=500))
        segs = []
        texts = []
        for s in segments:
            segs.append({"start": float(s.start or 0), "end": float(s.end or 0), "text": s.text or ""})
            if s.text: texts.append(s.text.strip())
        return " ".join(texts).strip(), segs, int(info.duration) if getattr(info, "duration", None) else None
    except Exception:
        pass

    # 2) openai-whisper (pip install -U openai-whisper ffmpeg)
    try:
        import whisper
        model = whisper.load_model(os.getenv("TRANSCRIBE_MODEL", "base"))
        result = model.transcribe(path, word_timestamps=False, verbose=False)
        text = (result.get("text") or "").strip()
        segs = [{"start": float(s.get("start", 0.0)),
                 "end": float(s.get("end", 0.0)),
                 "text": s.get("text","")} for s in (result.get("segments") or [])]
        dur = int(result.get("duration")) if result.get("duration") else None
        return text, segs, dur
    except Exception as e:
        raise RuntimeError(f"Transcription failed: {e}")

@bp.get("/media")
@login_required
def index():
    db = SessionLocal()
    try:
        rows = db.query(MediaItem).order_by(MediaItem.created_at.desc()).all()
        return render_template("media_index.html", media=rows)
    finally:
        db.close()

@bp.route("/media/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "GET":
        db = SessionLocal()
        try:
            projects = db.query(Project).order_by(Project.name.asc()).all()
            selected_project_id = int((request.args.get("project_id") or "0") or "0")
            return render_template("media_upload.html",
                                   projects=projects,
                                   selected_project_id=selected_project_id)
        finally:
            db.close()

    # POST
    f = request.files.get("file")
    title = (request.form.get("title") or "").strip()
    proj_id_s = (request.form.get("project_id") or "").strip()
    proj_id = int(proj_id_s) if proj_id_s.isdigit() else None

    if not f or not f.filename:
        flash("Choose an audio/video file.")
        return redirect(url_for("media.upload"))

    ext = os.path.splitext(f.filename.lower())[1]
    if ext not in ALLOWED_MEDIA:
        flash("Unsupported media type.")
        return redirect(url_for("media.upload"))

    dest_dir = os.path.join(Config.DATA_DIR, "media")
    os.makedirs(dest_dir, exist_ok=True)
    filename = secure_filename(f.filename)
    path = os.path.join(dest_dir, filename)
    i = 1
    while os.path.exists(path):
        base, ex = os.path.splitext(filename)
        filename = f"{base}-{i}{ex}"
        path = os.path.join(dest_dir, filename)
        i += 1
    f.save(path)

    full_text, segments, duration = _transcribe(path)

    db = SessionLocal()
    try:
        item = MediaItem(
            project_id=proj_id,
            title=title or os.path.splitext(filename)[0],
            filename=filename,
            stored_path=path,
            mime_type=_guess_mime(path),
            duration_seconds=duration,
            transcript_text=full_text,
            transcript_json=json.dumps(segments or []),
        )
        db.add(item); db.flush()  # get item.id

        # index into av_fts
        db.execute(text("DELETE FROM av_fts WHERE media_id=:i"), {"i": item.id})
        db.execute(text("INSERT INTO av_fts (media_id, title, body) VALUES (:i,:t,:b)"),
                   {"i": item.id, "t": item.title or filename, "b": full_text or ""})

        db.commit()

        # optional: emit an event
        try:
            emit("media.transcribed", media_id=item.id, project_id=item.project_id)
        except Exception:
            current_app.logger.exception("emit(media.transcribed) failed")

        flash("Media uploaded and transcribed.")
        return redirect(url_for("media.view", media_id=item.id))
    except Exception as e:
        db.rollback()
        current_app.logger.exception("Media upload/transcribe failed")
        flash(f"Upload failed: {e}")
        return redirect(url_for("media.upload"))
    finally:
        db.close()

@bp.get("/media/<int:media_id>")
@login_required
def view(media_id):
    import json
    db = SessionLocal()
    try:
        m = db.get(MediaItem, media_id)
        if not m:
            flash("Media not found.")
            return redirect(url_for("media.index"))

        segs = []
        try:
            segs = json.loads(m.transcript_json or "[]")
        except Exception:
            segs = []

        projects = db.query(Project).order_by(Project.name.asc()).all()

        return render_template("media_view.html", m=m, segments=segs, projects=projects)
    finally:
        db.close()

@bp.post("/media/<int:media_id>/project")
@login_required
def set_project(media_id):
    db = SessionLocal()
    try:
        m = db.get(MediaItem, media_id)
        if not m:
            flash("Media not found.")
            return redirect(url_for("media.index"))
        proj_id_s = (request.form.get("project_id") or "").strip()
        m.project_id = int(proj_id_s) if proj_id_s.isdigit() else None
        db.commit()
        flash("Media project updated.")
        return redirect(url_for("media.view", media_id=media_id))
    finally:
        db.close()

@bp.get("/media/<int:media_id>/download")
@login_required
def download(media_id):
    db = SessionLocal()
    try:
        m = db.get(MediaItem, media_id)
        if not m or not (m.stored_path and os.path.exists(m.stored_path)):
            flash("File not found.")
            return redirect(url_for("media.index"))
        return send_file(m.stored_path, as_attachment=True, download_name=m.filename)
    finally:
        db.close()
