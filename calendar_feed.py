from datetime import datetime, timedelta
from flask import Response
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Indiana/Indianapolis")

def _ics_escape(s: str) -> str:
    return (s or "").replace("\\","\\\\").replace(",","\\,").replace(";","\\;").replace("\n","\\n")

def _vevent(uid: str, dtstart: datetime, summary: str, desc: str=""):
    d = dtstart.astimezone(TZ).strftime("%Y%m%dT%H%M%S")
    now = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
    return (
      "BEGIN:VEVENT\r\n"
      f"UID:{uid}\r\n"
      f"DTSTAMP:{now}\r\n"
      f"DTSTART;TZID=America/Indiana/Indianapolis:{d}\r\n"
      f"SUMMARY:{_ics_escape(summary)}\r\n"
      f"DESCRIPTION:{_ics_escape(desc)}\r\n"
      "END:VEVENT\r\n"
    )

def build_calendar(projects, foias):
    body = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//FOIA Dashboard//EN\r\n"
    # Project due date reminders: 21, 14, 7 days before
    for p in projects:
        if not getattr(p, "deadline_at", None):
            continue
        for days in (21,14,7):
            dt = p.deadline_at - timedelta(days=days)
            body += _vevent(f"proj-{p.id}-dminus{days}@foia", dt,
                            f"[Project] {p.name} – due in {days} days")
    # FOIA follow-ups: day 14 and 30
    for r in foias:
        start = getattr(r, "requested_at", None)
        if not start:
            continue
        for days in (14,30):
            dt = start + timedelta(days=days)
            ref = getattr(r, "reference_number", "Unknown")
            ag = getattr(r, "agency", "Unknown")
            body += _vevent(f"foia-{r.id}-d{days}@foia", dt,
                            f"[FOIA] Follow-up {ref} ({ag})", "Follow up on pending request.")
    body += "END:VCALENDAR\r\n"
    return body
