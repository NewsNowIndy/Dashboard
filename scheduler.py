# scheduler.py
import atexit
from datetime import datetime, timezone
try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:
    BackgroundScheduler = None

from models import SessionLocal, Project, FoiaRequest, RequestStatus
from events import emit

def start_scheduler(app):
    if BackgroundScheduler is None:
        app.logger.warning("APScheduler not installed; skipping scheduler.")
        return

    sched = BackgroundScheduler(timezone="America/Indiana/Indianapolis")

    def run_checks():
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)

            # Project due date reminders (14, 7, 1 days before)
            projects = db.query(Project).filter(Project.deadline.isnot(None)).all()
            for p in projects:
                days_out = (p.deadline - now.date()).days
                if days_out in (14, 7, 1):
                    emit("project.reminder", project_id=p.id, days=days_out)

            # FOIA pending cadence (14, 21, 30, then every 7)
            foias = (
                db.query(FoiaRequest)
                  .filter(FoiaRequest.status == RequestStatus.PENDING)
                  .filter(FoiaRequest.request_date.isnot(None))
                  .all()
            )
            for r in foias:
                delta = (now.date() - r.request_date).days
                if delta in {14, 21, 30} or (delta >= 37 and (delta - 30) % 7 == 0):
                    emit("foia.followup", foia_id=r.id, days=delta)
        finally:
            db.close()

    sched.add_job(run_checks, "cron", minute="5")
    sched.start()
    atexit.register(lambda: sched.shutdown())
