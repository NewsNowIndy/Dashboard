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
from models import SessionLocal, Project, FoiaRequest, CalendarEvent
from sqlalchemy import DateTime

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

def _parse_dt_local(s: str | None):
    # Accepts 'YYYY-MM-DDTHH:MM' (from <input type="datetime-local">) or date-only 'YYYY-MM-DD'
    if not s:
        return None
    s = s.strip()
    try:
        # datetime-local
        return datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=LOCAL_TZ)
    except Exception:
        pass
    try:
        # date-only -> start of day
        d = datetime.strptime(s, "%Y-%m-%d").date()
        return datetime.combine(d, datetime.min.time()).replace(tzinfo=LOCAL_TZ)
    except Exception:
        return None

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

def _resolve_ce_cols():
    """
    Return (start_col_name, end_col_name) present on CalendarEvent.
    Strategy:
      1) Try common attribute names.
      2) Inspect __table__.columns for DateTime columns with 'start'/'begin' and 'end'/'finish' in the name.
      3) Fallback to the first (start) and second (end) DateTime columns if present.
    """
    # 1) direct attributes
    start_candidates = ("starts_at", "start_at", "start_time", "start", "begins_at")
    end_candidates   = ("ends_at",   "end_at",   "end_time",   "end",   "finishes_at")

    start_name = next((n for n in start_candidates if hasattr(CalendarEvent, n)), None)
    end_name   = next((n for n in end_candidates   if hasattr(CalendarEvent, n)), None)

    # 2) table inspection (only if needed)
    if not start_name or end_name is None:
        dt_cols = []
        try:
            for col in getattr(CalendarEvent, "__table__", None).columns:
                if isinstance(col.type, DateTime):
                    dt_cols.append(col.name)
        except Exception:
            dt_cols = []

        if not start_name:
            # prefer names with 'start'/'begin'
            pref = [n for n in dt_cols if "start" in n.lower() or "begin" in n.lower()]
            start_name = pref[0] if pref else (dt_cols[0] if dt_cols else None)

        if end_name is None:
            # prefer names with 'end'/'finish'
            pref = [n for n in dt_cols if "end" in n.lower() or "finish" in n.lower()]
            if pref:
                end_name = pref[0]
            else:
                # if there are >=2 datetime columns, use the second as end
                end_name = dt_cols[1] if len(dt_cols) >= 2 else None

    if not start_name:
        # Helpful error that lists available attributes/columns
        avail = []
        try:
            avail = [c.name for c in getattr(CalendarEvent, "__table__", None).columns]
        except Exception:
            pass
        raise AttributeError(
            "CalendarEvent has no usable start column. "
            "Tried common names and inspection. "
            f"Available columns: {', '.join(avail) if avail else 'unknown'}"
        )

    return start_name, end_name

CE_START_NAME, CE_END_NAME = _resolve_ce_cols()
CE_START_COL = getattr(CalendarEvent, CE_START_NAME)
CE_END_COL   = getattr(CalendarEvent, CE_END_NAME) if CE_END_NAME else None

def _get_start(ev):
    return getattr(ev, CE_START_NAME)

def _get_end(ev):
    return getattr(ev, CE_END_NAME) if CE_END_NAME else None

def _set_start(ev, dt):
    setattr(ev, CE_START_NAME, dt)

def _set_end(ev, dt):
    if CE_END_NAME:
        setattr(ev, CE_END_NAME, dt)

def _aware_local(dt):
    """Return a LOCAL_TZ-aware datetime for any datetime/date input."""
    if not dt:
        return None
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return datetime.combine(dt, datetime.min.time(), tzinfo=LOCAL_TZ)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)

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
                "day_key": _monday_of(s.date()) + timedelta(days=s.weekday()),
                "source": "ics",
                "custom_id": None,
            })
        except Exception:
            continue

    # 🔽 NEW: also pull custom DB events overlapping this week
    db = SessionLocal()
    try:
        # overlap condition: start < end && (end or start+1h) > start
        q = db.query(CalendarEvent).filter(CE_START_COL < end_dt)  # OK to leave as-is for SQL
        db_events = q.all()
        for ce in db_events:
            ce_start = _aware_local(_get_start(ce))
            if not ce_start:
                continue
            ce_end = _aware_local(_get_end(ce)) or (ce_start + timedelta(hours=1))
            if ce_end <= start_dt:  # both aware now
                continue

            events.append({
                "summary": ce.title or "(No title)",
                "location": getattr(ce, "location", "") or "",
                "description": getattr(ce, "notes", "") or "",
                "start": ce_start,
                "end": ce_end,
                "source": "db",
                "custom_id": ce.id,
            })

        # Bucket by day
        by_day = { (start_d + timedelta(days=i)): [] for i in range(7) }
        for ev in events:
            dk = ev["start"].date()
            if dk in by_day:
                by_day[dk].append(ev)
        for k in by_day:
            by_day[k].sort(key=lambda x: x["start"])

        prev_start = (start_d - timedelta(days=7)).strftime("%Y-%m-%d")
        next_start = (start_d + timedelta(days=7)).strftime("%Y-%m-%d")

        # 🔽 Keep modal options populated on calendar page
        all_projects = db.query(Project).order_by(Project.name.asc()).all()
        all_foias = (
            db.query(FoiaRequest)
              .order_by(FoiaRequest.request_date.desc())
              .limit(1000)
              .all()
        )
    finally:
        db.close()

    return render_template(
        "calendar_week.html",
        start=start_d, end=end_d - timedelta(days=1),
        days=list(by_day.items()),
        prev_url=url_for("calendar_ui.week_view", start=prev_start),
        next_url=url_for("calendar_ui.week_view", start=next_start),
        all_projects=all_projects,
        all_foias=all_foias,
    )

@bp.post("/events/create")
@login_required
def events_create():
    """
    Create a CalendarEvent from the Add Event modal.
    Expected form fields:
      title, start, end, location, notes, project_id, foia_request_id
    """
    title = (request.form.get("title") or request.form.get("summary") or "").strip()
    start_s = request.form.get("start")
    end_s   = request.form.get("end")
    location = (request.form.get("location") or "").strip() or None
    notes    = (request.form.get("notes") or request.form.get("description") or "").strip() or None

    starts_at = _parse_dt_local(start_s)
    ends_at   = _parse_dt_local(end_s)

    # If only start provided, default to 1 hour long
    if starts_at and not ends_at:
        ends_at = starts_at + timedelta(hours=1)

    # optional links
    pid = request.form.get("project_id", "").strip()
    rid = request.form.get("foia_request_id", "").strip()
    project_id = int(pid) if pid.isdigit() else None
    foia_id    = int(rid) if rid.isdigit() else None

    if not title or not starts_at:
        flash("Title and Start are required.", "warning")
        return redirect(request.referrer or url_for("calendar_ui.week_view"))

    db = SessionLocal()
    try:
        ev = CalendarEvent()
        # core fields
        ev.title = title
        _set_start(ev, starts_at)
        if ends_at:
            _set_end(ev, ends_at)
        ev.location = location
        ev.notes = notes
        # optional FKs (keep your field names as-is)
        ev.project_id = project_id
        ev.foia_request_id = foia_id

        db.add(ev)
        db.commit()
        flash("Event created.", "success")
    finally:
        db.close()

    return redirect(request.referrer or url_for("calendar_ui.week_view"))

@bp.post("/events/<int:event_id>/delete")
@login_required
def events_delete(event_id: int):
    db = SessionLocal()
    try:
        ev = db.get(CalendarEvent, event_id)
        if not ev:
            flash("Event not found.", "warning")
        else:
            db.delete(ev)
            db.commit()
            flash("Event deleted.", "success")
    finally:
        db.close()
    return redirect(request.referrer or url_for("calendar_ui.week_view"))
