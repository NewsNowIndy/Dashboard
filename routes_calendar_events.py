from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from datetime import datetime
from zoneinfo import ZoneInfo

from models import SessionLocal, CalendarEvent, Project, FoiaRequest
LOCAL_TZ = ZoneInfo("America/Indiana/Indianapolis")

bp_cal_events = Blueprint("cal_events", __name__, url_prefix="/calendar")

def _parse_dt_local(date_s: str, time_s: str | None):
    date_s = (date_s or "").strip()
    time_s = (time_s or "").strip() or "09:00"
    try:
        dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=LOCAL_TZ)
    except Exception:
        return None

@bp_cal_events.route("/events", methods=["GET"])
@login_required
def events_index():
    db = SessionLocal()
    try:
        events = db.query(CalendarEvent).order_by(CalendarEvent.start_dt.asc()).all()
        projects = db.query(Project).order_by(Project.name.asc()).all()
        foias = db.query(FoiaRequest).order_by(FoiaRequest.request_date.desc()).limit(200).all()
        return render_template("calendar_events.html", events=events, projects=projects, foias=foias)
    finally:
        db.close()

@bp_cal_events.route("/events/add", methods=["POST"])
@login_required
def events_add():
    title = (request.form.get("title") or "").strip()
    desc  = (request.form.get("description") or "").strip() or None
    start_date = request.form.get("start_date")
    start_time = request.form.get("start_time")  # "HH:MM", optional
    end_time   = request.form.get("end_time")    # optional

    if not title or not start_date:
        flash("Title and date are required.", "warning")
        return redirect(url_for("cal_events.events_index"))

    start_dt = _parse_dt_local(start_date, start_time)
    end_dt = _parse_dt_local(start_date, end_time) if end_time else None
    if not start_dt:
        flash("Invalid date/time.", "warning")
        return redirect(url_for("cal_events.events_index"))

    pid = request.form.get("project_id", "").strip()
    rid = request.form.get("foia_request_id", "").strip()
    project_id = int(pid) if pid.isdigit() else None
    foia_request_id = int(rid) if rid.isdigit() else None

    db = SessionLocal()
    try:
        db.add(CalendarEvent(
            title=title, description=desc, start_dt=start_dt, end_dt=end_dt,
            project_id=project_id, foia_request_id=foia_request_id
        ))
        db.commit()
        flash("Event added.")
    finally:
        db.close()
    return redirect(url_for("cal_events.events_index"))

@bp_cal_events.route("/events/<int:event_id>/delete", methods=["POST"])
@login_required
def events_delete(event_id: int):
    db = SessionLocal()
    try:
        e = db.get(CalendarEvent, event_id)
        if not e:
            flash("Event not found.", "warning")
        else:
            db.delete(e)
            db.commit()
            flash("Event deleted.")
    finally:
        db.close()
    return redirect(url_for("cal_events.events_index"))
