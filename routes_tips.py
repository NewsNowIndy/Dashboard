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

@bp.route("/")
@login_required
def index():
    db = SessionLocal()
    try:
        tips = db.query(Tip).order_by(desc(Tip.created_at)).all()

        # derive titles for any blank ones so the UI shows them right now
        from tip_helpers import derive_titles_for_missing
        derived_titles = derive_titles_for_missing(tips)

        # Whether a session is already present in this Flask process
        gl_session_present = bool(os.getenv("GLOBALEAKS_SESSION_ID"))

        return render_template(
            "tips_index.html",
            tips=tips,
            derived_titles=derived_titles,
            gl_session_present=gl_session_present
        )
    finally:
        db.close()

@bp.route("/sync", methods=["POST", "GET"])
@login_required
def sync_now():
    """
    Optionally accept a session_id from the modal. If provided, set it in-process and,
    if requested, write it to .env so the next run has it too. Then perform the sync.
    """
    # If the modal was used, fields will be present (POST)
    session_id = (request.form.get("session_id") or "").strip()
    remember = (request.form.get("remember") == "1")

    # Apply the session id for this running process (works immediately)
    if session_id:
        os.environ["GLOBALEAKS_SESSION_ID"] = session_id

        if remember:
            ok, err = _update_dotenv_var("GLOBALEAKS_SESSION_ID", session_id, path=".env")
            if ok:
                flash("Saved GlobaLeaks session ID to .env (will be used on restart).", "success")
            else:
                flash(f"Could not update .env: {err}", "warning")

    # If we STILL have no session id available, stop and ask user to paste it
    if not os.getenv("GLOBALEAKS_SESSION_ID"):
        flash("GlobaLeaks Session ID is required. Paste it in the Sync dialog.", "warning")
        return redirect(url_for("tips.index"))

    # Now attempt the sync
    from tips_sync import sync_once
    try:
        ins, upd = sync_once()
        flash(f"Synchronized tips: +{ins} new, {upd} updated", "success")
    except HTTPError as e:
        body = getattr(e.response, "text", "")
        code = getattr(e.response, "status_code", "???")
        flash(f"Tip sync failed ({code}). See logs. {body[:300]}", "danger")
        return redirect(url_for("tips.index"))
    except Exception as e:
        flash(f"Tip sync failed: {e}", "danger")
        return redirect(url_for("tips.index"))

    # After syncing, send the user somewhere useful; your app currently goes to the project page
    return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

@bp.route("/<string:tip_id>")
@login_required
def detail(tip_id):
    db = SessionLocal()
    try:
        projects = db.query(Project).order_by(Project.name.asc()).all()
        client = TorGlobaLeaksClient('', '', '')

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
