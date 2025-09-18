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

@bp.route("/")
@login_required
def week_view():
    start_d = _parse_start(request.args.get("start"))
    end_d = start_d + timedelta(days=7)

    # Load ICS text
    ics_url = _ics_url()
    try:
        resp = requests.get(ics_url, timeout=10)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.text)
    except Exception as e:
        flash(f"Calendar failed to load ({e}). Check CALENDAR_ICS_URL or /calendar.ics.", "warning")
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

            # Normalize to aware datetimes in LOCAL_TZ
            def _aware(dt):
                if isinstance(dt, date) and not isinstance(dt, datetime):
                    # All-day event
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
                "day_key": _monday_of(s.date()) + timedelta(days=s.weekday())  # actual day
            })
        except Exception:
            continue

    # Bucket by day
    by_day = { (start_d + timedelta(days=i)): [] for i in range(7) }
    for ev in events:
        dk = ev["start"].date()
        if dk in by_day:
            by_day[dk].append(ev)

    # Sort each day by start time
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
