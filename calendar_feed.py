# calendar_feed.py
from datetime import datetime, time, timedelta
from flask import Response, Blueprint
from zoneinfo import ZoneInfo
from models import SessionLocal, Project, FoiaRequest

LOCAL_TZID = "America/Indiana/Indianapolis"
LOCAL_TZ = ZoneInfo(LOCAL_TZID)
UTC = ZoneInfo("UTC")

bp = Blueprint("calendar", __name__)

_FMT_CANDIDATES = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%m-%d-%Y %H:%M:%S", "%m-%d-%Y %H:%M", "%m-%d-%Y",
    "%m/%d/%Y",
]

def _ics_escape(s: str) -> str:
    return (s or "").replace("\\","\\\\").replace(",","\\,").replace(";","\\;").replace("\n","\\n")

def _fold(line: str) -> str:
    b = line.encode("utf-8"); out=[]
    while len(b) > 75:
        out.append(b[:75].decode("utf-8","ignore")); b=b[75:]
    out.append(b.decode("utf-8","ignore"))
    return ("\r\n ").join(out)

def _line(prop: str, value: str) -> str:
    return _fold(f"{prop}:{value}") + "\r\n"

def _ensure_local(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)

def _fmt_local(dt: datetime) -> str:
    return _ensure_local(dt).strftime("%Y%m%dT%H%M%S")

def _fmt_utc(dt: datetime) -> str:
    return _ensure_local(dt).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

def _coerce_local_dt(value, default_t: time = time(9, 0)):
    """Accept datetime/date/str → tz-aware local datetime (or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_local(value)
    # date → combine with default time
    try:
        from datetime import date as _date
        if isinstance(value, _date):
            return datetime.combine(value, default_t, tzinfo=LOCAL_TZ)
    except Exception:
        pass
    # string
    if isinstance(value, str):
        s = value.strip()
        for fmt in _FMT_CANDIDATES:
            try:
                dt = datetime.strptime(s, fmt)
                if fmt in ("%Y-%m-%d", "%m-%d-%Y", "%m/%d/%Y"):
                    dt = datetime.combine(dt.date(), default_t)
                return dt.replace(tzinfo=LOCAL_TZ)
            except ValueError:
                continue
    return None

def _vtz_indianapolis() -> str:
    return (
        "BEGIN:VTIMEZONE\r\n"
        f"TZID:{LOCAL_TZID}\r\n"
        "X-LIC-LOCATION:America/Indiana/Indianapolis\r\n"
        "BEGIN:DAYLIGHT\r\n"
        "TZOFFSETFROM:-0500\r\n"
        "TZOFFSETTO:-0400\r\n"
        "TZNAME:EDT\r\n"
        "DTSTART:20070311T020000\r\n"
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\n"
        "END:DAYLIGHT\r\n"
        "BEGIN:STANDARD\r\n"
        "TZOFFSETFROM:-0400\r\n"
        "TZOFFSETTO:-0500\r\n"
        "TZNAME:EST\r\n"
        "DTSTART:20071104T020000\r\n"
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\n"
        "END:STANDARD\r\n"
        "END:VTIMEZONE\r\n"
    )

def _vevent(uid: str, dtstart: datetime, summary: str, desc: str = "", duration: timedelta = timedelta(minutes=30)) -> str:
    start_local = _fmt_local(dtstart)
    end_local = _fmt_local(_ensure_local(dtstart) + duration)
    now_utc = _fmt_utc(datetime.now(LOCAL_TZ))
    parts = [
        "BEGIN:VEVENT\r\n",
        _line("UID", uid),
        _line("DTSTAMP", now_utc),  # keep DTSTAMP in UTC
        _line(f"DTSTART;TZID={LOCAL_TZID}", start_local),
        _line(f"DTEND;TZID={LOCAL_TZID}", end_local),
        _line("SUMMARY", _ics_escape(summary)),
    ]
    if desc:
        parts.append(_line("DESCRIPTION", _ics_escape(desc)))
    parts.append("END:VEVENT\r\n")
    return "".join(parts)

def build_calendar(projects, foias) -> str:
    body = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//FOIA Dashboard//EN\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        f"X-WR-CALNAME:FOIA Dashboard\r\n"
        f"X-WR-TIMEZONE:{LOCAL_TZID}\r\n"
    )
    body += _vtz_indianapolis()

    # Projects: offsets before Project.deadline (date)
    for p in projects or []:
        name = getattr(p, "name", "Untitled Project")
        deadline = _coerce_local_dt(getattr(p, "deadline", None))  # <-- use .deadline
        if not deadline:
            continue
        for days in (21, 14, 7, 3, 2, 1):
            start = deadline - timedelta(days=days)
            body += _vevent(
                uid=f"proj-{getattr(p,'id','x')}-dminus{days}@foia",
                dtstart=start,
                summary=f"[Project] {name} – due in {days} day{'s' if days!=1 else ''}",
                desc=f"Project due {_ensure_local(deadline):%Y-%m-%d %H:%M %Z}",
            )

    # FOIA: offsets after FoiaRequest.request_date (date)
    for r in foias or []:
        ref = getattr(r, "reference_number", "Unknown")
        ag = getattr(r, "agency", "Unknown")
        requested = _coerce_local_dt(getattr(r, "request_date", None))  # <-- use .request_date
        if not requested:
            continue
        for days in (7, 14, 21, 30):
            start = requested + timedelta(days=days)
            body += _vevent(
                uid=f"foia-{getattr(r,'id','x')}-d{days}@foia",
                dtstart=start,
                summary=f"[FOIA] Follow-up {ref} ({ag}) — +{days} days",
                desc="Follow up on pending request.",
            )

    body += "END:VCALENDAR\r\n"
    return body

@bp.route("/calendar.ics")
def calendar_feed():
    db = SessionLocal()
    try:
        projects = db.query(Project).all()
        foias = db.query(FoiaRequest).all()
        ics = build_calendar(projects, foias)
        print(f"[calendar.ics] bytes={len(ics)} events={ics.count('BEGIN:VEVENT')}")
        return Response(
            ics,
            mimetype="text/calendar; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=foia_dashboard.ics"},
        )
    finally:
        db.close()

# Optional alias so existing template link keeps working:
@bp.route("/calendar-all.ics", endpoint="calendar_all")
def calendar_all():
    return calendar_feed()
