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
import subprocess, shutil, tempfile

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

def _wkhtmltopdf_available():
    return shutil.which("wkhtmltopdf") is not None

def url_to_pdf(url: str, out_path: str) -> bool:
    import subprocess, shutil, logging
    log = logging.getLogger("app")

    ua = os.getenv("WEBCAP_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                                         "Chrome/124.0.0.0 Safari/537.36")

    wkhtml = shutil.which("wkhtmltopdf")
    if wkhtml:
        cmd = [
            wkhtml, "--enable-local-file-access",
            "--load-error-handling", "ignore",           # don’t abort on blocked subresources
            "--custom-header", "User-Agent", ua,         # spoof UA
            "--no-stop-slow-scripts", "--javascript-delay", "2000",
            url, out_path
        ]
        try:
            subprocess.check_call([
                "wkhtmltopdf",
                "--enable-local-file-access",
                "--custom-header", "User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                "Chrome/124.0.0.0 Safari/537.36",
                url, out_path
            ])
            return True
        except Exception:
            current_app.logger.error("wkhtmltopdf failed", exc_info=True)
            # Fallback to Playwright (requires playwright + chromium in build)
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    context = browser.new_context(user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ))
                    page = context.new_page()
                    page.goto(url, wait_until="networkidle")
                    page.pdf(path=out_path, print_background=True)
                    browser.close()
                return True
            except Exception:
                current_app.logger.error("Playwright PDF failed", exc_info=True)
                return False

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
    """
    Create a web capture for a URL (HTML + screenshot + meta), hash+timestamp it,
    optionally attach to a project, and (if the model supports it) save a PDF too.
    """
    from datetime import timezone

    url = (request.form.get("url") or "").strip()
    title = (request.form.get("title") or "").strip()
    project_id = (request.form.get("project_id") or "").strip()
    notes = (request.form.get("notes") or "").strip() or None

    if not url:
        flash("URL is required.", "warning")
        return redirect(request.referrer or url_for("webcap.new"))

    # --- run capture ---
    ua = request.headers.get("User-Agent")
    root = _store_root()  # base dir we serve from via /webcap/file/<relpath>
    # keep capture artifacts grouped by day (easier housekeeping)
    day_dir = os.path.join(root, datetime.utcnow().strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)

    try:
        result = capture_url(url, day_dir, user_agent=ua)
        # expected keys (best effort): html_path, image_path, meta_path, sha256_html, sha256_image
    except Exception as e:
        current_app.logger.exception("capture_url failed")
        flash(f"Capture failed: {e}", "danger")
        return redirect(request.referrer or url_for("webcap.new"))

    # Build short hash for filenames/identity; include timestamp for uniqueness.
    stamp = datetime.now(timezone.utc)
    sha = hashlib.sha256((url + "|" + stamp.isoformat()).encode("utf-8")).hexdigest()[:16]

    # Make relative paths (so we can serve with /webcap/file/<rel>)
    def _rel(path):
        return os.path.relpath(path, root) if path else None

    html_rel = _rel(result.get("html_path"))
    img_rel  = _rel(result.get("image_path"))
    meta_rel = _rel(result.get("meta_path"))

    # --- dynamic model field mapping helpers ---
    def _first_attr(model, *candidates):
        for n in candidates:
            if hasattr(model, n):
                return n
        return None

    # Map common, potentially-varying column names
    screenshot_field      = _first_attr(WebCapture, "png_path", "screenshot_path", "image_path")
    html_field            = _first_attr(WebCapture, "html_path", "html_file", "html_relpath")
    meta_field            = _first_attr(WebCapture, "meta_path", "metadata_path")
    image_artifact_field  = _first_attr(WebCapture, "image_path", "artifact_image_path")  # if you store BOTH screenshot_path AND image_path, this lets you keep the raw artifact too
    ts_field              = _first_attr(WebCapture, "captured_at", "created_at", "timestamp")

    # Base kwargs (only fields that likely exist on most schemas)
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

    # Fill dynamic path/timestamp fields if present on the model
    if html_field and html_rel:
        base[html_field] = html_rel
    if screenshot_field and img_rel:
        base[screenshot_field] = img_rel
    if meta_field and meta_rel:
        base[meta_field] = meta_rel
    if image_artifact_field and img_rel and image_artifact_field not in base:
        base[image_artifact_field] = img_rel
    if ts_field:
        base[ts_field] = stamp

    # --- optional: render PDF if model supports a pdf_path-like column ---
    pdf_field = _first_attr(WebCapture, "pdf_path", "pdf_relpath")
    if pdf_field:
        try:
            # put PDF next to other artifacts in the same day directory
            pdf_abs = os.path.join(day_dir, f"{sha}.pdf")
            if url_to_pdf(url, pdf_abs):
                base[pdf_field] = os.path.relpath(pdf_abs, root)
            else:
                current_app.logger.warning("PDF generation failed for %s", url)
        except Exception:
            current_app.logger.exception("PDF generation threw an exception for %s", url)

    # Only pass attributes that actually exist on the model to avoid TypeError
    wc_kwargs = {k: v for k, v in base.items() if hasattr(WebCapture, k)}

    db = SessionLocal()
    try:
        wc = WebCapture(**wc_kwargs)
        db.add(wc)
        db.commit()
        flash("Web capture saved.", "success")

        # If attached to a project, redirect there; else go to the capture view
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