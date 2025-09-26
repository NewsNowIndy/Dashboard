# routes_tips.py
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required
from sqlalchemy import desc
from models import SessionLocal, Tip, Project
from globaleaks_client import TorGlobaLeaksClient, BASE, USER, PASS
from requests import HTTPError
import os

bp = Blueprint("tips", __name__, url_prefix="/tips")

def _update_dotenv_var(key: str, value: str, path: str = ".env"):
    """
    Add or replace KEY=VALUE in a .env file. Creates the file if it doesn't exist.
    Keeps all other lines intact.
    """
    try:
        lines = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()

        found = False
        out = []
        for line in lines:
            if line.strip().startswith(f"{key}="):
                out.append(f"{key}={value}")
                found = True
            else:
                out.append(line)

        if not found:
            out.append(f"{key}={value}")

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + ("\n" if out and not out[-1].endswith("\n") else ""))
        return True, None
    except Exception as e:
        return False, str(e)
    
def _resolve_gl_base() -> str:
    """
    Decide which base URL to use for GlobaLeaks.
    - If USE_ONION is truthy, prefer GLOBALEAKS_ONION.
    - Else use GLOBALEAKS_BASE_URL.
    Returns "" if neither is set.
    """
    use_onion = (os.getenv("USE_ONION", "0").lower() in ("1", "true", "yes"))
    if use_onion:
        return os.getenv("GLOBALEAKS_ONION", "") or ""
    return os.getenv("GLOBALEAKS_BASE_URL", "") or ""

@bp.route("/")
@login_required
def index():
    db = SessionLocal()
    try:
        tips = db.query(Tip).order_by(desc(Tip.created_at)).all()

        # derive titles for any blank ones so the UI shows them right now
        from tip_helpers import derive_titles_for_missing
        derived_titles = derive_titles_for_missing(tips)

        return render_template("tips_index.html", tips=tips, derived_titles=derived_titles)
    finally:
        db.close()

@bp.route("/sync", methods=["POST", "GET"])
@login_required
def sync_now():
    # Convenience sync from the UI
    from tips_sync import sync_once
    try:
        ins, upd = sync_once()
        flash(f"Synchronized tips: +{ins} new, {upd} updated", "success")
    except HTTPError as e:
        body = getattr(e.response, "text", "")
        code = getattr(e.response, "status_code", "unknown")
        flash(f"Tip sync failed ({code}). See logs. {body[:300]}", "danger")
    except RuntimeError as e:
        flash(f"Tip sync failed: {e}", "danger")
    return redirect(url_for("tips.index"))

@bp.route("/<string:tip_id>")
@login_required
def detail(tip_id):
    base = _resolve_gl_base()
    if not base:
        flash("GlobaLeaks base URL is not configured. Set GLOBALEAKS_BASE_URL (or USE_ONION=1 and GLOBALEAKS_ONION).", "warning")
        return redirect(url_for("tips.index"))

    db = SessionLocal()
    try:
        projects = db.query(Project).order_by(Project.name.asc()).all()
        # Build the client with an explicit base so we never rely on global state.
        client = TorGlobaLeaksClient(base, os.getenv("GLOBALEAKS_USERNAME", ""), os.getenv("GLOBALEAKS_PASSWORD", ""))

        TITLE_KEY = "e239d748-12e9-4c3f-b401-58c699c66a4e"     # short title
        SUMMARY_KEY = "0f2c5077-90f3-4b0d-8346-21b5c3b8a627"   # long description

        def _extract_answer(live: dict, field_id: str) -> str | None:
            try:
                qlist = live.get("questionnaires") or []
                answers = (qlist[0] or {}).get("answers") or {}
                items = answers.get(field_id) or []
                val = (items[0] or {}).get("value", "")
                return (val or "").strip() or None
            except Exception:
                return None

        try:
            r = client.get(f"/api/recipient/rtips/{tip_id}")
            live = r.json()

            title_text = _extract_answer(live, TITLE_KEY) or (live.get("label") or "").strip() or None
            summary_text = _extract_answer(live, SUMMARY_KEY) or (live.get("summary") or "").strip() or None

            return render_template(
                "tip_detail.html",
                tip=live,
                detail=live,
                projects=projects,
                title_text=title_text,
                summary_text=summary_text,
                live=True,
            )
        except Exception:
            # Fallback to DB if live fetch fails
            t = db.query(Tip).filter(Tip.glk_id == tip_id).first()
            if not t:
                flash("Tip not found locally.", "warning")
                return redirect(url_for("tips.index"))
            return render_template(
                "tip_detail.html",
                tip=t,
                detail=None,
                projects=projects,
                title_text=(t.title or None),
                summary_text=(t.summary or None),
                live=False,
            )
    finally:
        db.close()

@bp.post("/<string:glk_id>/project")
@login_required
def set_project(glk_id: str):
    """
    Link/unlink a Tip to a Project.
    """
    pid = (request.form.get("project_id") or "").strip()
    db = SessionLocal()
    try:
        t = db.query(Tip).filter(Tip.glk_id == glk_id).first()
        if not t:
            flash("Tip not found.")
            return redirect(url_for("tips.index"))
        t.project_id = int(pid) if pid.isdigit() else None
        db.commit()
        flash("Project updated.")
        return redirect(url_for("tips.detail", tip_id=glk_id))
    finally:
        db.close()
