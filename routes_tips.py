# routes_tips.py
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required
from sqlalchemy import desc
from models import SessionLocal, Tip, Project
from globaleaks_client import GlobaLeaksClient, BASE, USER, PASS
from requests import HTTPError

bp = Blueprint("tips", __name__, url_prefix="/tips")

@bp.route("/")
@login_required
def index():
    db = SessionLocal()
    try:
        tips = db.query(Tip).order_by(desc(Tip.created_at)).all()
        return render_template("tips_index.html", tips=tips)
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
        flash(f"Tip sync failed ({e.response.status_code}). See logs. {body[:300]}", "danger")
    return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

@bp.route("/<string:glk_id>")
@login_required
def detail(glk_id: str):
    """
    Show local summary from DB and live detail from GlobaLeaks.
    """
    # live fetch from GlobaLeaks
    detail = None
    try:
        client = GlobaleaksClient(BASE, USER, PASS)
        detail = client.get_tip(glk_id)
    except Exception as e:
        # Keep page usable even if GL fetch fails
        flash(f"Could not fetch live tip details: {e}", "warning")

    db = SessionLocal()
    try:
        t = db.query(Tip).filter(Tip.glk_id == glk_id).first()
        projects = db.query(Project).order_by(Project.name.asc()).all()
        return render_template("tip_detail.html", t=t, detail=detail, projects=projects)
    finally:
        db.close()

@bp.post("/<string:glk_id>/project")
@login_required
def set_project(glk_id: str):
    """
    Optional: link/unlink a Tip to a Project.
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
        return redirect(url_for("tips.detail", glk_id=glk_id))
    finally:
        db.close()
