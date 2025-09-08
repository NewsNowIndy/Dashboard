from flask import Blueprint, Response
from models import SessionLocal, Project, FoiaRequest, RequestStatus
from calendar_feed import build_calendar

bp_calendar = Blueprint("calendar", __name__)

@bp_calendar.route("/calendar/all.ics")
def calendar_all():
    db = SessionLocal()
    try:
        projects = db.query(Project).all()
        foias = db.query(FoiaRequest).filter(FoiaRequest.status == RequestStatus.PENDING).all()
        ics = build_calendar(projects, foias)
        return Response(ics, mimetype="text/calendar; charset=utf-8")
    finally:
        db.close()
