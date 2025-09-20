# routes_webcapture.py
from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, send_from_directory, abort
from flask_login import login_required, current_user
from models import SessionLocal, Project, WebCapture
from webcapture import capture_url
from datetime import datetime, timezone
import hashlib, os
from pathlib import Path
from sqlalchemy.orm import selectinload

bp = Blueprint("webcap", __name__, url_prefix="/webcap")

WEB_CAP_DIR = Path(os.getenv("WEB_CAP_DIR", "webcap_store")).resolve()
WEB_CAP_DIR.mkdir(parents=True, exist_ok=True)

def _store_root():
    # Reuse your UPLOAD_FOLDER; fall back to instance path
    base = current_app.config.get("UPLOAD_FOLDER") or current_app.instance_path
    root = os.path.join(base, "webcap")
    os.makedirs(root, exist_ok=True)
    return root

def _utcnow():
    return datetime.now(timezone.utc)

@bp.get("/")
@login_required
def index():
    db = SessionLocal()
    try:
        q = db.query(WebCapture).options(selectinload(WebCapture.project))
        if hasattr(WebCapture, "captured_at"):
            rows = q.order_by(WebCapture.captured_at.desc(), WebCapture.id.desc()).all()
        else:
            rows = q.order_by(WebCapture.id.desc()).all()
        # after .all(), project is fully loaded; safe to close the session
        return render_template("webcap_index.html", rows=rows)
    finally:
        db.close()

@bp.get("/new")
@login_required
def new():
    db = SessionLocal()
    try:
        projects = db.query(Project).order_by(Project.name.asc()).all()
    finally:
        db.close()
    return render_template("webcap_new.html", projects=projects)

@bp.post("/create")
@login_required
def create():
    # Local import so this works even if you didn't change the module imports yet
    from datetime import timezone
    import hashlib
    import os

    url = (request.form.get("url") or "").strip()
    title = (request.form.get("title") or "").strip()
    project_id = (request.form.get("project_id") or "").strip()
    notes = (request.form.get("notes") or "").strip() or None

    if not url:
        flash("URL is required.", "warning")
        return redirect(request.referrer or url_for("webcap.new"))

    # Build paths we can store on the model regardless of how capture_url writes files
    stamp = datetime.now(timezone.utc)
    sha = hashlib.sha256(f"{url}|{stamp.isoformat()}".encode("utf-8")).hexdigest()
    html_rel = f"{sha}/page.html"
    png_rel  = f"{sha}/screenshot.png"

    # Do the capture (to our storage root, grouped by day)
    ua = request.headers.get("User-Agent")
    root = _store_root()
    day_dir = os.path.join(root, datetime.utcnow().strftime("%Y-%m-%d"))
    try:
        result = capture_url(url, day_dir, user_agent=ua)  # expected keys: image_path, meta_path, sha256_html, sha256_image
    except Exception as e:
        current_app.logger.exception("capture_url failed")
        flash(f"Capture failed: {e}", "danger")
        return redirect(request.referrer or url_for("webcap.new"))

    # ---- Build model-safe kwargs (support different column names) ----
    def _first_attr(model, *candidates):
        for n in candidates:
            if hasattr(model, n):
                return n
        return None

    # Map model-specific field names if they exist
    screenshot_field       = _first_attr(WebCapture, "png_path", "screenshot_path", "image_path")
    html_field             = _first_attr(WebCapture, "html_path", "html_file", "html_relpath")
    meta_field             = _first_attr(WebCapture, "meta_path", "metadata_path")
    image_artifact_field   = _first_attr(WebCapture, "image_path", "artifact_image_path")  # separate from screenshot if present
    ts_field               = _first_attr(WebCapture, "captured_at", "created_at", "timestamp")

    base = {
        "project_id": int(project_id) if project_id.isdigit() else None,
        "url": url,
        "title": title or url,
        "notes": notes,
        "user_agent": ua,
        "source_ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "captured_by": (getattr(current_user, "email", None) or getattr(current_user, "username", None)),
        "sha256_html": result.get("sha256_html"),
        "sha256_image": result.get("sha256_image"),
    }

    # Fill dynamic fields if present on your model
    if html_field:
        base[html_field] = html_rel
    if screenshot_field:
        base[screenshot_field] = png_rel
    if meta_field and result.get("meta_path"):
        base[meta_field] = os.path.relpath(result["meta_path"], root)
    if image_artifact_field and result.get("image_path"):
        base[image_artifact_field] = os.path.relpath(result["image_path"], root)
    if ts_field:
        base[ts_field] = stamp

    # Only pass attributes that actually exist on the model to avoid TypeError
    wc_kwargs = {k: v for k, v in base.items() if hasattr(WebCapture, k)}

    db = SessionLocal()
    try:
        wc = WebCapture(**wc_kwargs)
        db.add(wc)
        db.commit()
        flash("Web capture saved.", "success")

        if getattr(wc, "project_id", None):
            proj = db.get(Project, wc.project_id)
            if proj:
                return redirect(url_for("project_detail", slug=proj.slug))

        return redirect(url_for("webcap.view", cap_id=wc.id))
    finally:
        db.close()

@bp.get("/view/<int:cap_id>")
@login_required
def view(cap_id: int):
    db = SessionLocal()
    try:
        wc = (
            db.query(WebCapture)
              .options(selectinload(WebCapture.project))
              .get(cap_id)
        )
        if not wc:
            abort(404)
        return render_template("webcap_view.html", cap=wc)
    finally:
        db.close()

@bp.get("/file/<path:relpath>")
@login_required
def file(relpath: str):
    # serve stored artifacts read-only
    root = _store_root()
    abs_path = os.path.join(root, relpath)
    if not abs_path.startswith(root) or not os.path.exists(abs_path):
        abort(404)
    directory, filename = os.path.split(abs_path)
    return send_from_directory(directory, filename, as_attachment=False)

@bp.post("/delete/<int:cap_id>")
@login_required
def delete(cap_id: int):
    db = SessionLocal()
    try:
        wc = db.get(WebCapture, cap_id)
        if not wc:
            flash("Not found.", "warning")
            return redirect(request.referrer or url_for("dashboard"))
        # best-effort file cleanup
        root = _store_root()
        for rp in (wc.html_path, wc.image_path, wc.meta_path):
            if rp:
                ap = os.path.join(root, rp)
                try: os.remove(ap)
                except Exception: pass
        db.delete(wc)
        db.commit()
        flash("Capture deleted.", "success")
    finally:
        db.close()
    return redirect(request.referrer or url_for("dashboard"))