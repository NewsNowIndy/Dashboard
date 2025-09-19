# routes_calendar_ui.py
from __future__ import annotations
from flask import Blueprint, render_template, request, current_app, url_for, redirect, flash
from flask_login import login_required
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import os, requests
from icalendar import Calendar
import recurring_ical_events
from urllib.parse import urljoin
from models import SessionLocal, Project, FoiaRequest

try:
    from calendar_feed import build_calendar  # returns an icalendar.Calendar
except Exception:
    build_calendar = None

bp = Blueprint("calendar_ui", __name__, url_prefix="/calendar")

LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TZ", "America/Indiana/Indianapolis"))

def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday=0

def _parse_start(query_start: str | None) -> date:
    if not query_start:
        return _monday_of(datetime.now(LOCAL_TZ).date())
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return _monday_of(datetime.strptime(query_start, fmt).date())
        except Exception:
            pass
    return _monday_of(datetime.now(LOCAL_TZ).date())

def _ics_url() -> str:
    """
    Resolve the ICS feed URL. Prefer CALENDAR_ICS_URL, else build an absolute
    URL to /calendar.ics on this same host.
    """
    explicit = os.getenv("CALENDAR_ICS_URL", "").strip()
    if explicit:
        return explicit

    # Always build absolute to this host (works behind Render, proxies, etc.)
    # request.url_root already includes scheme+host and trailing slash.
    return urljoin(request.url_root, "calendar.ics")

def _normalize_to_calendar(obj) -> Calendar | None:
    """
    Accepts a Calendar, ICS bytes/str, or an object with to_ical(),
    and returns an icalendar.Calendar (or None if it can't).
    """
    if obj is None:
        return None
    if isinstance(obj, Calendar):
        return obj
    if isinstance(obj, (bytes, bytearray, str)):
        try:
            return Calendar.from_ical(obj)
        except Exception:
            return None
    # objects that provide to_ical()
    to_ical = getattr(obj, "to_ical", None)
    if callable(to_ical):
        try:
            return Calendar.from_ical(to_ical())
        except Exception:
            return None
    return None

def _build_calendar_local() -> Calendar | None:
    if build_calendar is None:
        return None
    db = SessionLocal()
    try:
        projects = db.query(Project).all()
        foias    = db.query(FoiaRequest).all()
        try:
            raw = build_calendar(projects, foias, custom_events=[])
        except TypeError:
            raw = build_calendar(projects, foias)
        return _normalize_to_calendar(raw)
    finally:
        db.close()

@bp.route("/")
@login_required
def week_view():
    start_d = _parse_start(request.args.get("start"))
    end_d = start_d + timedelta(days=7)

    cal: Calendar | None = None

    # 1) Try local first (fastest, avoids hairpin)
    cal = _build_calendar_local()

    if cal is None:
        ics_url = _ics_url()
        try:
            resp = requests.get(ics_url, timeout=10)
            resp.raise_for_status()
            cal = _normalize_to_calendar(resp.text)
        except Exception as e:
            current_app.logger.warning("Calendar failed to load via HTTP (%s)", e)
            flash(f"Calendar failed to load ({e}). Check CALENDAR_ICS_URL or /calendar.ics.", "warning")
            return redirect(url_for("dashboard"))

    if cal is None:
        flash("Calendar feed could not be parsed.", "warning")
        return redirect(url_for("dashboard"))

    # Expand recurring events in [start, end)
    start_dt = datetime.combine(start_d, datetime.min.time()).replace(tzinfo=LOCAL_TZ)
    end_dt   = datetime.combine(end_d,   datetime.min.time()).replace(tzinfo=LOCAL_TZ)

    events = []
    for ev in recurring_ical_events.of(cal).between(start_dt, end_dt):
        try:
            summary = (str(ev.get("summary")) or "").strip()
            location = (str(ev.get("location")) or "").strip()
            desc = (str(ev.get("description")) or "").strip()
            dtstart = ev.decoded("dtstart")
            dtend   = ev.decoded("dtend", None)

            def _aware(dt):
                if isinstance(dt, date) and not isinstance(dt, datetime):
                    return datetime.combine(dt, datetime.min.time(), tzinfo=LOCAL_TZ)
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=LOCAL_TZ)
                return dt.astimezone(LOCAL_TZ)

            s = _aware(dtstart)
            e = _aware(dtend) if dtend else (s + timedelta(hours=1))

            events.append({
                "summary": summary or "(No title)",
                "location": location,
                "description": desc,
                "start": s,
                "end": e,
                "day_key": _monday_of(s.date()) + timedelta(days=s.weekday())
            })
        except Exception:
            continue

    by_day = { (start_d + timedelta(days=i)): [] for i in range(7) }
    for ev in events:
        dk = ev["start"].date()
        if dk in by_day:
            by_day[dk].append(ev)
    for k in by_day:
        by_day[k].sort(key=lambda x: x["start"])

    prev_start = (start_d - timedelta(days=7)).strftime("%Y-%m-%d")
    next_start = (start_d + timedelta(days=7)).strftime("%Y-%m-%d")

    return render_template(
        "calendar_week.html",
        start=start_d, end=end_d - timedelta(days=1),
        days=list(by_day.items()),
        prev_url=url_for("calendar_ui.week_view", start=prev_start),
        next_url=url_for("calendar_ui.week_view", start=next_start)
    )

@bp.post("/events/create", endpoint="events_create")
@login_required
def events_create():
    """
    Minimal stub so the Add Event modal can submit without 404/BuildError.
    Later, insert into your CalendarEvent table and redirect back.
    """
    title = (request.form.get("title") or "").strip()
    # Optional linkages (blank by default)
    project_id = request.form.get("project_id") or None
    foia_id = request.form.get("foia_id") or None
    start = request.form.get("start")
    end   = request.form.get("end")
    # TODO: parse datetimes, insert DB row, etc.

    flash("Event creation endpoint hit. Wire it to save an event next.", "info")
    # send user back to the calendar week view (or referrer)
    return redirect(request.referrer or url_for("calendar_ui.week_view"))
