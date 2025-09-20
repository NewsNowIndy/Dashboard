# routes_webcapture.py
from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, send_from_directory, abort, send_file
from flask_login import login_required, current_user
from models import SessionLocal, Project, WebCapture
from webcapture import capture_url
from datetime import datetime, timezone
import hashlib, os
from pathlib import Path
from sqlalchemy.orm import selectinload, joinedload
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

def url_to_png(url: str, out_path: str, ua: str) -> bool:
    import shutil, subprocess, os
    wki = shutil.which("wkhtmltoimage")
    if wki:
        cmd = [
            wki,
            "--enable-local-file-access",
            "--load-error-handling", "ignore",
            "--load-media-error-handling", "ignore",
            "--custom-header", "User-Agent", ua,
            "--javascript-delay", "2000",
            "--width", "1280",          # pick a sane viewport
            "--quality", "92",
            url, out_path,
        ]
        try:
            subprocess.check_call(cmd)
            return os.path.exists(out_path) and os.path.getsize(out_path) > 0
        except Exception:
            current_app.logger.exception("wkhtmltoimage failed")

    # Fallback: Playwright (if available)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(user_agent=ua, viewport={"width":1280,"height":900})
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            page.screenshot(path=out_path, full_page=True)
            browser.close()
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        current_app.logger.exception("Playwright screenshot failed")
        return False

def url_to_pdf(url: str, out_path: str) -> dict:
    """Try wkhtmltopdf first, then Playwright. Return a metadata dict."""
    import logging, shutil, subprocess, os
    log = logging.getLogger("app")

    ua = os.getenv(
        "WEBCAP_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    wkhtml = shutil.which("wkhtmltopdf")
    if wkhtml:
        cmd = [
            wkhtml,
            "--enable-local-file-access",
            # ask nicely to ignore subresource failures
            "--load-error-handling", "ignore",
            "--load-media-error-handling", "ignore",
            "--custom-header", "User-Agent", ua,
            "--no-stop-slow-scripts",
            "--javascript-delay", "2000",
            url, out_path,
        ]
        try:
            subprocess.check_call(cmd)
            return {"ok": True, "engine": "wkhtmltopdf", "user_agent": ua,
                    "http_status": None, "content_type": None, "error": None}
        except subprocess.CalledProcessError as e:
            # On Render, cookie/consent and CDN 404s often cause code 1.
            # If PDF exists and is non-empty, accept it.
            if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
                log.warning("wkhtmltopdf returned %s but PDF exists; proceeding", e.returncode)
                return {"ok": True, "engine": "wkhtmltopdf", "user_agent": ua,
                        "http_status": None, "content_type": None,
                        "error": f"wkhtmltopdf exit {e.returncode} (ignored)"}
            # else, we will try Playwright
            current_app.logger.error("wkhtmltopdf failed", exc_info=True)

    # Fallback: Playwright (only if installed in your Render image)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(user_agent=ua)
            page = context.new_page()
            # Be less strict than "networkidle" for news sites with trackers
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # give the page a moment for late content/banner to render
            page.wait_for_timeout(1500)
            page.pdf(path=out_path, print_background=True)
            status = resp.status if resp else None
            ctype = resp.headers.get("content-type") if resp else None
            browser.close()
        return {"ok": True, "engine": "playwright", "user_agent": ua,
                "http_status": status, "content_type": ctype, "error": None}
    except Exception as e:
        current_app.logger.error("Playwright PDF failed", exc_info=True)
        return {"ok": False, "engine": None, "user_agent": ua,
                "http_status": None, "content_type": None, "error": str(e)}
            
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

@bp.get("/")
@login_required
def index():
    db = SessionLocal()
    try:
        q = db.query(WebCapture).options(selectinload(WebCapture.project))
        rows = (
            q.order_by(WebCapture.captured_at.desc(), WebCapture.id.desc()).all()
            if hasattr(WebCapture, "captured_at")
            else q.order_by(WebCapture.id.desc()).all()
        )
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
    import re
    import requests

    url = (request.form.get("url") or "").strip()
    project_id_raw = request.form.get("project_id")
    project_id = int(project_id_raw) if project_id_raw and project_id_raw.isdigit() else None

    user_title = (request.form.get("title") or "").strip() or None

    if not url:
        flash("Provide a URL to capture.", "warning")
        return redirect(url_for("webcap.index"))

    # Storage root: instance/webcap/YYYY-MM-DD/
    day_dir = os.path.join(
        current_app.instance_path, "webcap",
        datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    os.makedirs(day_dir, exist_ok=True)

    token = hashlib.sha1(f"{url}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:16]
    pdf_abs  = os.path.join(day_dir, f"{token}.pdf")
    html_abs = os.path.join(day_dir, f"{token}.html")
    png_abs  = os.path.join(day_dir, f"{token}.png")

    # -------- 1) PDF capture --------
    meta = url_to_pdf(url, pdf_abs)

    # Hash + size (PDF)
    sha = None
    size = None
    if meta.get("ok") and os.path.exists(pdf_abs):
        try:
            sha = _sha256(pdf_abs)
        except Exception:
            current_app.logger.exception("sha256 failed for pdf")
        try:
            size = os.path.getsize(pdf_abs)
        except Exception:
            size = None

    # Relative PDF path (from instance/)
    rel_pdf_path = os.path.relpath(pdf_abs, current_app.instance_path) if os.path.exists(pdf_abs) else None

    # -------- 2) Fetch & archive HTML + extract <title> --------
    ua = (
        meta.get("user_agent")
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/124.0.0.0 Safari/537.36"
    )
    extracted_title = None
    rel_html_path = None
    sha_html = None

    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=20,
            allow_redirects=True,
        )
        ctype = (r.headers.get("content-type") or "").lower()
        if r.ok and ("text/html" in ctype or ctype.startswith("text/")) and r.content:
            # save the raw HTML
            with open(html_abs, "wb") as f:
                f.write(r.content)
            if os.path.exists(html_abs):
                rel_html_path = os.path.relpath(html_abs, current_app.instance_path)
                try:
                    sha_html = hashlib.sha256(r.content).hexdigest()
                except Exception:
                    sha_html = None

            # extract a simple <title> (best-effort)
            try:
                # decode with apparent encoding; fallback to requests' text
                html_text = r.text
                m = re.search(r"<title[^>]*>(.*?)</title\s*>", html_text, re.I | re.S)
                if m:
                    # collapse whitespace
                    t = re.sub(r"\s+", " ", m.group(1)).strip()
                    if t:
                        title = t
            except Exception:
                current_app.logger.exception("title extraction failed")
    except Exception:
        current_app.logger.exception("HTML fetch failed")

    # -------- 3) Optional screenshot (best-effort) --------
    rel_png_path = None
    sha_img = None
    try:
        # url_to_png(url, out_path, user_agent) should return True/False if you implemented it
        if 'url_to_png' in globals():
            png_ok = url_to_png(url, png_abs, ua)
            if png_ok and os.path.exists(png_abs):
                rel_png_path = os.path.relpath(png_abs, current_app.instance_path)
                try:
                    sha_img = _sha256(png_abs)
                except Exception:
                    sha_img = None
    except Exception:
        current_app.logger.exception("Screenshot (url_to_png) failed")

    final_title = user_title or extracted_title

    # -------- 4) Persist DB row (all fields preserved) --------
    fwd_for = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or None
    src_ip = fwd_for or request.remote_addr

    db = SessionLocal()
    try:
        cap = WebCapture(
            project_id=project_id,

            url=url,
            title=final_title,                 # <-- populated from <title>, else None

            # stored file paths (relative to instance/)
            html_path=rel_html_path,     # <-- archived HTML path if fetched
            image_path=rel_png_path,     # <-- optional screenshot
            meta_path=None,              # keep if you add a JSON sidecar later
            pdf_path=rel_pdf_path,

            # hashes
            sha256_html=sha_html,
            sha256_image=sha_img,

            # chain-of-custody
            captured_at=datetime.utcnow(),
            captured_by=(getattr(current_user, "email", None) or None),
            user_agent=ua,
            source_ip=src_ip,
            notes=(meta.get("error") if not meta.get("ok") else None),

            # engine + http-ish
            engine=meta.get("engine"),
            http_status=meta.get("http_status"),
            content_type=meta.get("content_type"),

            # file stats + error (PDF)
            sha256=sha,
            size_bytes=size,
            error=meta.get("error"),
        )
        db.add(cap)
        db.commit()

        flash(
            "Web capture saved." if meta.get("ok")
            else "Capture saved with errors — see details.",
            "success" if meta.get("ok") else "warning"
        )
        return redirect(url_for("webcap.view", cap_id=cap.id))
    finally:
        db.close()

@bp.get("/<int:cap_id>", endpoint="view")
@login_required
def webcap_view(cap_id):
    db = SessionLocal()
    try:
        cap = (
            db.query(WebCapture)
              .options(joinedload(WebCapture.project))
              .filter(WebCapture.id == cap_id)
              .first()
        )
        if not cap:
            flash("Capture not found.", "warning")
            return redirect(url_for("webcap.index"))
        return render_template("webcap_view.html", cap=cap)
    finally:
        db.close()

@bp.get("/<int:cap_id>/download", endpoint="download")
@login_required
def webcap_download(cap_id: int):
    """Download the generated PDF for a web capture."""
    db = SessionLocal()
    try:
        cap = db.query(WebCapture).filter(WebCapture.id == cap_id).first()
        if not cap:
            flash("Capture not found.", "warning")
            return redirect(url_for("webcap.index"))

        rel = (cap.pdf_path or "").strip()
        if not rel:
            flash("PDF file not found for this capture.", "warning")
            return redirect(url_for("webcap.view", cap_id=cap.id))

        abs_path = os.path.join(current_app.instance_path, rel)
        if not os.path.exists(abs_path):
            flash("PDF file not found on disk.", "warning")
            return redirect(url_for("webcap.view", cap_id=cap.id))

        return send_file(
            abs_path,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=os.path.basename(abs_path) or f"capture-{cap.id}.pdf",
        )
    finally:
        db.close()

@bp.get("/file/<path:relpath>")
@login_required
def file(relpath: str):
    # Serve artifacts from instance/…/webcap
    abs_path = os.path.join(current_app.instance_path, relpath)
    if not abs_path.startswith(current_app.instance_path) or not os.path.exists(abs_path):
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