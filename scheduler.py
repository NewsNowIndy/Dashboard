# scheduler.py
import atexit
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import SchedulerAlreadyRunningError  # add this

from models import SessionLocal, Project, FoiaRequest, RequestStatus
from events import emit
from tips_sync import sync_once

_sched = None  # keep a module-level singleton

def start_scheduler(app):
    global _sched
    if _sched is not None:
        app.logger.info("Scheduler already initialized; skipping.")
        return

    sched = BackgroundScheduler(timezone="America/Indiana/Indianapolis")

    def run_checks():
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            projects = db.query(Project).filter(Project.deadline.isnot(None)).all()
            for p in projects:
                days_out = (p.deadline - now.date()).days
                if days_out in (14, 7, 1):
                    emit("project.reminder", project_id=p.id, days=days_out)

            foias = (db.query(FoiaRequest)
                       .filter(FoiaRequest.status == RequestStatus.PENDING)
                       .filter(FoiaRequest.request_date.isnot(None))
                       .all())
            for r in foias:
                delta = (now.date() - r.request_date).days
                if delta in {14, 21, 30} or (delta >= 37 and (delta - 30) % 7 == 0):
                    emit("foia.followup", foia_id=r.id, days=delta)
        finally:
            db.close()

    def _sync_tips():
        with app.app_context():
            try:
                ins, upd = sync_once()
                print(f"[tips-sync] +{ins} / {upd}")
            except Exception as e:
                print("[tips-sync] error:", e)

    # Give jobs stable IDs and replace if they already exist (hot reload safety)
    sched.add_job(_sync_tips, "interval", minutes=5, id="tips_sync", replace_existing=True)
    sched.add_job(run_checks, "cron", minute="5", id="run_checks", replace_existing=True)

    try:
        sched.start()
    except SchedulerAlreadyRunningError:
        app.logger.info("Scheduler was already running; ignoring second start.")

    atexit.register(lambda: sched.shutdown(wait=False))
    _sched = sched
