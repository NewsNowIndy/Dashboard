from flask import Flask, render_template, request, redirect, url_for, send_file, flash
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import io
import os
import re

from config import Config
from models import (
    init_db, SessionLocal,
    FoiaRequest, FoiaAttachment, RequestStatus, CourtCase, FoiaEvent, SurroundingCase, ProjectDocument, Project, ProjectNote, ProjectStatus,
    WorkbenchDataset, WorkbenchRecordLink, WorkbenchPdfLink,
)
from gmail_sync import sync_once
from utils import decrypt_file_to_bytes, normalize_request_status, days_until, age_in_days, badge_for_days_left, badge_for_requested_age, send_email
from sheets_ingest import import_cases_from_csv, import_cases_from_gsheet, import_surrounding_cases_from_csv, import_surrounding_cases_from_gsheet
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge, BadRequest, ClientDisconnected
from sqlalchemy import func, or_, case

app = Flask(__name__)
app.config.from_object(Config)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024       # 512 MB cap (total request)
app.config["MAX_FORM_MEMORY_SIZE"] = 128 * 1024 * 1024     # 128 MB memory threshold for form parsing
app.config["MAX_FORM_PARTS"] = 20000                       # lots of parts for multi-file uploads
app.config["TRAP_BAD_REQUEST_ERRORS"] = True 
init_db()

# Ensure uploads dir exists
os.makedirs("data", exist_ok=True)
os.makedirs("data/projects/mcpo-plea-deals", exist_ok=True) # Storage for Projects

# -----------------------------
# Helpers
# -----------------------------

LOCAL_TZ = ZoneInfo("America/Indiana/Indianapolis")
UTC = ZoneInfo("UTC")

def today_local():
    return datetime.now(LOCAL_TZ).date()

@app.template_filter("pretty")
def pretty(s: str | None):
    if not s:
        return ""
    s = s.strip()
    overrides = {
        "defendant_name": "Defendant Name",
        "total_charges": "Total Charges",
    }
    if s in overrides:
        return overrides[s]
    # generic snake_case → Title Case
    return re.sub(r"[_\-\s]+", " ", s).title()

@app.template_filter("daysleft_badge")
def daysleft_badge(d):
    return badge_for_days_left(days_until(d))

@app.template_filter("requested_age_badge")
def requested_age_badge(d):
    return badge_for_requested_age(age_in_days(d))

@app.template_filter("daysleft_num")
def daysleft_num(d):
    n = days_until(d)
    return "" if n is None else n

@app.template_filter("localfmt")
def localfmt(dt, fmt="%m-%d-%Y %H:%M"):
    """
    Render datetimes in local Indiana (America/Indiana/Indianapolis) time.
    If dt is naive, assume it is in UTC.
    """
    if not dt:
        return ""
    if dt.tzinfo is None:
        # Assume UTC if naive (adjust if you actually store local times)
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LOCAL_TZ).strftime(fmt)

ALLOWED_EXTS = {".pdf", ".doc", ".docx", ".csv", ".xlsx", ".png", ".jpg", ".jpeg", ".gif"}

def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "project"

def _allowed_ext(filename: str) -> bool:
    return os.path.splitext(filename.lower())[1] in ALLOWED_EXTS

def build_case_charts(Model):
    db = SessionLocal()
    try:
        total_cases = db.query(func.count(Model.id)).scalar() or 0

        # Conviction type counts (convicted only)
        type_counts = dict(
            db.query(Model.conviction_type, func.count())
              .filter(Model.disposition == "Convicted")
              .filter(Model.conviction_type.isnot(None))
              .group_by(Model.conviction_type)
              .all()
        )
        conv_type = {
            "labels": ["Plea", "Jury", "Bench"],
            "data": [
                type_counts.get("Plea", 0),
                type_counts.get("Jury", 0),
                type_counts.get("Bench", 0),
            ],
        }

        # Disposition buckets
        dismissed_cnt = db.query(func.count(Model.id)).filter(Model.disposition == "Dismissed").scalar() or 0
        convicted_cnt = db.query(func.count(Model.id)).filter(Model.disposition == "Convicted").scalar() or 0
        pending_cnt = max(total_cases - dismissed_cnt - convicted_cnt, 0)
        disposition = {
            "labels": ["Dismissed", "Convicted", "Pending"],
            "data": [dismissed_cnt, convicted_cnt, pending_cnt],
        }

        # Sentences (DAYS TOTALS across all rows):
        total_days_expr = func.coalesce(
            Model.sentence_total_months,  # stores DAYS in your pipeline
            func.coalesce(Model.sentence_executed_months, 0) +
            func.coalesce(Model.sentence_suspended_months, 0)
        )
        total_days = db.query(func.coalesce(func.sum(total_days_expr), 0)).scalar() or 0
        executed_days = db.query(func.coalesce(func.sum(func.coalesce(Model.sentence_executed_months, 0)), 0)).scalar() or 0
        suspended_days = db.query(func.coalesce(func.sum(func.coalesce(Model.sentence_suspended_months, 0)), 0)).scalar() or 0

        sentences = {
            "labels": ["All Cases"],
            "total":     [int(total_days)],
            "executed":  [int(executed_days)],
            "suspended": [int(suspended_days)],
        }
        return conv_type, disposition, sentences
    finally:
        db.close()

@app.template_filter("mdy")
def mdy(d: date | None):
    if not d:
        return ""
    return d.strftime("%m-%d-%Y")

def _parse_date_any(s: str | None):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None

def _sum_safe(values):
    """Sum non-None numeric values; return int (0 if none)."""
    vals = [v for v in values if v is not None]
    return int(sum(vals)) if vals else 0

def _is_pdf_attachment(att) -> bool:
    """Return True if attachment is a PDF by filename, OCR path, or MIME type."""
    if (att.mime_type or "").lower() == "application/pdf":
        return True
    for fn in [(att.filename or ""), (att.stored_path or ""), (att.ocr_pdf_path or "")]:
        if fn.lower().endswith(".pdf"):
            return True
    return False


# -----------------------------
# FOIA: Home / Search (sorted by Reference # desc)
# -----------------------------
@app.route("/")
def home():
    q = request.args.get("q", "").strip()
    db = SessionLocal()
    try:
        qry = db.query(FoiaRequest)
        if q:
            like = f"%{q}%"
            qry = qry.filter(FoiaRequest.reference_number.like(like))

        # Order by most recent request_date first
        rows = qry.order_by(FoiaRequest.request_date.desc()).limit(500).all()

        # Case-insensitive PDF count per request
        pdf_counts = {
            r.id: (
                db.query(func.count(FoiaAttachment.id))
                  .filter(FoiaAttachment.foia_request_id == r.id)
                  .filter(
                      or_(
                          func.lower(FoiaAttachment.filename).like('%.pdf'),
                          func.lower(FoiaAttachment.stored_path).like('%.pdf'),
                          func.lower(FoiaAttachment.ocr_pdf_path).like('%.pdf'),
                      )
                  )
                  .scalar() or 0
            )
            for r in rows
        }

        return render_template("index.html", rows=rows, q=q, pdf_counts=pdf_counts)
    finally:
        db.close()

# -----------------------------
# FOIA: Gmail sync
# -----------------------------
@app.route("/sync")
def sync():
    ok = sync_once()
    flash("Sync complete" if ok else "Sync failed — check logs")
    return redirect(url_for("home"))


# -----------------------------
# FOIA: Create new manual request
# -----------------------------
@app.route("/requests/new", methods=["GET", "POST"])
def new_request():
    if request.method == "POST":
        db = SessionLocal()
        try:
            status_token = normalize_request_status(request.form.get("status", "Pending"))  # -> 'PENDING' or 'COMPLETED'
            fr = FoiaRequest(
                reference_number=request.form["reference_number"].strip(),
                agency=request.form.get("agency"),
                request_date=datetime.strptime(request.form["request_date"], "%Y-%m-%d").date(),
                status=RequestStatus[status_token],   # <-- by NAME
                subject="Manual entry",
                snippet=request.form.get("notes", "")
            )
            db.add(fr)
            db.commit()
            return redirect(url_for("home"))
        finally:
            db.close()
    return render_template("request_form.html")

# -----------------------------
# FOIA: Request detail + status update
# -----------------------------
@app.route("/requests/<int:req_id>")
def request_detail(req_id):
    db = SessionLocal()
    try:
        r = db.get(FoiaRequest, req_id)
        if not r:
            flash("Request not found.")
            return redirect(url_for("home"))

        # Only PDFs (case-insensitive)
        pdf_attachments = [a for a in getattr(r, "attachments", []) if _is_pdf_attachment(a)]
        eligible_projects = (
            db.query(Project)
              .filter(Project.status.in_([ProjectStatus.ACTIVE, ProjectStatus.PLANNED]))
              .order_by(Project.name.asc())
              .all()
        )

        return render_template(
            "request_detail.html",
            r=r,
            pdf_attachments=pdf_attachments,
            eligible_projects=eligible_projects,
        )
    finally:
        db.close()

@app.post("/requests/<int:req_id>/project")
def request_set_project(req_id):
    pid = request.form.get("project_id", "").strip()  # "" or "123"
    db = SessionLocal()
    try:
        r = db.get(FoiaRequest, req_id)
        if not r:
            flash("Request not found.")
            return redirect(url_for("home"))

        if not pid:
            r.project_id = None
        else:
            # validate the chosen project exists and is Active/Planned
            p = (
                db.query(Project)
                  .filter(Project.id == int(pid))
                  .filter(Project.status.in_([ProjectStatus.ACTIVE, ProjectStatus.PLANNED]))
                  .first()
            )
            if not p:
                flash("Invalid project selection.")
                return redirect(url_for("request_detail", req_id=req_id))
            r.project_id = p.id

        db.commit()
        flash("Project updated.")
        return redirect(url_for("request_detail", req_id=req_id))
    finally:
        db.close()

@app.route("/requests/<int:req_id>/status", methods=["POST"])
def request_status(req_id):
    db = SessionLocal()
    try:
        r = db.get(FoiaRequest, req_id)  # <-- modern API
        if not r:
            flash("Request not found.")
            return redirect(url_for("home"))

        status_token = normalize_request_status(request.form.get("status", "Pending"))  # 'PENDING'/'COMPLETED'
        r.status = RequestStatus[status_token]  # <-- by NAME

        cd = request.form.get("completed_date")
        r.completed_date = _parse_date_any(cd)
        db.commit()
        return redirect(url_for("request_detail", req_id=req_id))
    finally:
        db.close()

@app.post("/requests/<int:req_id>/dates")
def update_request_dates(req_id: int):
    db = SessionLocal()
    try:
        r = db.get(FoiaRequest, req_id)
        if not r:
            flash("Request not found.")
            return redirect(url_for("home"))

        requested_s = request.form.get("requested_date")
        completed_s = request.form.get("completed_date")

        new_requested = _parse_date_any(requested_s)
        new_completed = _parse_date_any(completed_s)

        # Only update if a value was provided (blank keeps current / sets None)
        r.request_date = new_requested
        r.completed_date = new_completed

        db.commit()
        flash("Dates updated.")
        return redirect(url_for("request_detail", req_id=req_id))
    finally:
        db.close()

# -----------------------------
# FOIA: Delete a request (and its files)
# -----------------------------
@app.route("/requests/<int:req_id>/delete", methods=["POST"])
def request_delete(req_id):
    db = SessionLocal()
    try:
        r = db.get(FoiaRequest, req_id)
        if not r:
            flash("Request not found.")
            return redirect(url_for("home"))

        # Delete attachment files from disk first
        atts = db.query(FoiaAttachment).filter(FoiaAttachment.foia_request_id == r.id).all()
        for a in atts:
            # encrypted blob
            if a.stored_path and os.path.exists(a.stored_path):
                try:
                    os.remove(a.stored_path)
                except Exception:
                    pass
            # OCR pdf
            if a.ocr_pdf_path and os.path.exists(a.ocr_pdf_path):
                try:
                    os.remove(a.ocr_pdf_path)
                except Exception:
                    pass

        # Delete DB rows (attachments, events, then request)
        db.query(FoiaAttachment).filter(FoiaAttachment.foia_request_id == r.id).delete()
        db.query(FoiaEvent).filter(FoiaEvent.foia_request_id == r.id).delete()
        db.delete(r)
        db.commit()
        flash("Request deleted.")
        return redirect(url_for("home"))
    finally:
        db.close()

# -----------------------------
# FOIA: Attachments (encrypted + OCR copy)
# -----------------------------
@app.route("/attachments/<int:att_id>")
def download_attachment(att_id):
    db = SessionLocal()
    try:
        a = db.query(FoiaAttachment, att_id)
        if not a:
            flash("Attachment not found.")
            return redirect(url_for("home"))

        # Only allow PDFs to be downloaded
        if not _is_pdf_attachment(a):
            flash("Only PDF attachments are available.")
            return redirect(url_for("request_detail", req_id=a.foia_request_id))

        buf = decrypt_file_to_bytes(a.stored_path)
        return send_file(io.BytesIO(buf), as_attachment=True, download_name=a.filename or "attachment.pdf")
    finally:
        db.close()

@app.route("/attachments/<int:att_id>/ocr")
def download_ocr(att_id):
    db = SessionLocal()
    try:
        a = db.query(FoiaAttachment, att_id)
        if not a or not a.ocr_pdf_path or not os.path.exists(a.ocr_pdf_path):
            flash("No OCR copy available")
            return redirect(url_for("request_detail", req_id=a.foia_request_id if a else 0))
        return send_file(a.ocr_pdf_path, as_attachment=True, download_name=f"OCR-{a.filename or 'attachment.pdf'}")
    finally:
        db.close()

# -----------------------------
# FOIA: Export CSV
# -----------------------------
@app.route("/export.csv")
def export_csv():
    db = SessionLocal()
    try:
        rows = db.query(FoiaRequest).order_by(FoiaRequest.reference_number.asc()).all()
        out = io.StringIO()
        out.write("reference_number,request_date,status,completed_date\n")
        for r in rows:
            out.write(f"{r.reference_number},{r.request_date or ''},{r.status.value},{r.completed_date or ''}\n")
        return send_file(
            io.BytesIO(out.getvalue().encode("utf-8")),
            as_attachment=True,
            download_name="foia_export.csv",
            mimetype="text/csv"
        )
    finally:
        db.close()

@app.post("/requests/<int:req_id>/agency")
def update_request_agency(req_id: int):
    db = SessionLocal()
    try:
        r = db.get(FoiaRequest, req_id)
        if not r:
            flash("Request not found.")
            return redirect(url_for("home"))

        r.agency = (request.form.get("agency", "").strip() or None)
        db.commit()
        flash("Agency updated.")
        return redirect(url_for("request_detail", req_id=req_id))
    finally:
        db.close()

@app.post("/requests/<int:req_id>/meta")
def update_request_meta(req_id: int):
    db = SessionLocal()
    try:
        r = db.get(FoiaRequest, req_id)
        if not r:
            flash("Request not found.")
            return redirect(url_for("home"))

        # Editable fields
        r.agency = (request.form.get("agency", "").strip() or None)
        # Treat FoiaRequest.snippet as "Notes"
        r.snippet = (request.form.get("notes", "").strip() or None)

        db.commit()
        flash("Request details updated.")
        return redirect(url_for("request_detail", req_id=req_id))
    finally:
        db.close()

# -----------------------------
# Court Cases: Dashboard
# -----------------------------
@app.route("/cases/dashboard")
def cases_dashboard():
    db = SessionLocal()

    # Total rows to compute Pending (None disposition)
    total_cases = db.query(func.count(CourtCase.id)).scalar() or 0

    # Conviction type counts (Plea/Jury/Bench) for convicted only
    type_counts = dict(
        db.query(CourtCase.conviction_type, func.count())
          .filter(CourtCase.disposition == "Convicted")
          .filter(CourtCase.conviction_type.isnot(None))
          .group_by(CourtCase.conviction_type)
          .all()
    )
    conv_type = {
        "labels": ["Plea", "Jury", "Bench"],
        "data": [
            type_counts.get("Plea", 0),
            type_counts.get("Jury", 0),
            type_counts.get("Bench", 0),
        ],
    }

    # Disposition buckets
    dismissed_cnt = db.query(func.count(CourtCase.id)).filter(CourtCase.disposition == "Dismissed").scalar() or 0
    convicted_cnt = db.query(func.count(CourtCase.id)).filter(CourtCase.disposition == "Convicted").scalar() or 0
    pending_cnt = max(total_cases - dismissed_cnt - convicted_cnt, 0)

    disposition = {
        "labels": ["Dismissed", "Convicted", "Pending"],
        "data": [dismissed_cnt, convicted_cnt, pending_cnt],
    }

    # Sentences chart (DAYS; totals across ALL rows):
    # total_days_per_row = COALESCE(Sentence, COALESCE(Executed,0) + COALESCE(Suspended,0))
    total_days_expr = func.coalesce(
        CourtCase.sentence_total_months,  # holds DAYS (not months)
        func.coalesce(CourtCase.sentence_executed_months, 0) +
        func.coalesce(CourtCase.sentence_suspended_months, 0)
    )

    total_days = db.query(func.coalesce(func.sum(total_days_expr), 0)).scalar() or 0
    executed_days = db.query(func.coalesce(func.sum(func.coalesce(CourtCase.sentence_executed_months, 0)), 0)).scalar() or 0
    suspended_days = db.query(func.coalesce(func.sum(func.coalesce(CourtCase.sentence_suspended_months, 0)), 0)).scalar() or 0

    sentences = {
        "labels": ["All Cases"],
        "total":     [int(total_days)],
        "executed":  [int(executed_days)],
        "suspended": [int(suspended_days)],
    }

    db.close()
    return render_template(
        "court_cases.html",
        conv_type=conv_type,
        disposition=disposition,
        sentences=sentences,
    )

# ===== Surrounding Counties =====

@app.route("/surrounding/dashboard")
def surrounding_cases_dashboard():
    conv_type, disposition, sentences = build_case_charts(SurroundingCase)
    return render_template(
        "surrounding_cases.html",   # your new template
        conv_type=conv_type,
        disposition=disposition,
        sentences=sentences,
    )

@app.route("/surrounding/upload", methods=["GET", "POST"])
def surrounding_cases_upload():
    if request.method == "POST":
        f = request.files.get("file")
        if not f:
            flash("No file uploaded")
            return redirect(url_for("surrounding_cases_upload"))

        filename = secure_filename(f.filename or "")
        if not filename.lower().endswith(".csv"):
            flash("Please upload a .csv file")
            return redirect(url_for("surrounding_cases_upload"))

        path = os.path.join("data", filename)
        f.save(path)

        try:
            n = import_surrounding_cases_from_csv(path)  # <-- targets SurroundingCase
        except Exception as e:
            flash(f"Import error: {e}")
            return redirect(url_for("surrounding_cases_upload"))

        flash(f"Imported/updated {n} surrounding-county cases")
        return redirect(url_for("surrounding_cases_dashboard"))

    # Reuse your upload form template, or make a second one if you want different copy
    return render_template("upload_cases.html")

@app.route("/surrounding/import_gsheet", methods=["POST"])
def surrounding_cases_import_gsheet():
    url = request.form.get("sheet_url", "").strip()
    if not url:
        flash("Provide a Google Sheet URL or ID")
        return redirect(url_for("surrounding_cases_upload"))
    service_json = Config.__dict__.get("GOOGLE_SHEETS_SERVICE_JSON") or os.getenv("GOOGLE_SHEETS_SERVICE_JSON")
    if not service_json:
        flash("Set GOOGLE_SHEETS_SERVICE_JSON in .env")
        return redirect(url_for("surrounding_cases_upload"))

    try:
        n = import_surrounding_cases_from_gsheet(url, service_json)  # <-- targets SurroundingCase
    except Exception as e:
        flash(f"Import error: {e}")
        return redirect(url_for("surrounding_cases_upload"))

    flash(f"Imported/updated {n} cases from Google Sheet (Surrounding Counties)")
    return redirect(url_for("surrounding_cases_dashboard"))

# -----------------------------
# Court Cases: Upload CSV / Import GSheet
# -----------------------------
@app.route("/cases/upload", methods=["GET", 'POST'])
def cases_upload():
    if request.method == "POST":
        f = request.files.get("file")
        if not f:
            flash("No file uploaded")
            return redirect(url_for("cases_upload"))

        filename = secure_filename(f.filename or "")
        if not filename.lower().endswith(".csv"):
            flash("Please upload a .csv file")
            return redirect(url_for("cases_upload"))

        path = os.path.join("data", filename)
        f.save(path)

        try:
            n = import_cases_from_csv(path)
        except Exception as e:
            flash(f"Import error: {e}")
            return redirect(url_for("cases_upload"))

        flash(f"Imported/updated {n} cases")
        return redirect(url_for("cases_dashboard"))

    return render_template("upload_cases.html")


@app.route("/cases/import_gsheet", methods=["POST"])
def cases_import_gsheet():
    url = request.form.get("sheet_url", "").strip()
    if not url:
        flash("Provide a Google Sheet URL or ID")
        return redirect(url_for("cases_upload"))
    service_json = Config.__dict__.get("GOOGLE_SHEETS_SERVICE_JSON") or os.getenv("GOOGLE_SHEETS_SERVICE_JSON")
    if not service_json:
        flash("Set GOOGLE_SHEETS_SERVICE_JSON in .env")
        return redirect(url_for("cases_upload"))

    try:
        n = import_cases_from_gsheet(url, service_json)
    except Exception as e:
        flash(f"Import error: {e}")
        return redirect(url_for("cases_upload"))

    flash(f"Imported/updated {n} cases from Google Sheet")
    return redirect(url_for("cases_dashboard"))


# Convenience redirect: /cases -> /cases/dashboard
@app.route("/cases")
@app.route("/cases/")
def cases_index():
    return redirect(url_for("cases_dashboard"))

# -------- MCPO Plea Deals: list & upload --------
@app.post("/projects/mcpo-plea-deals/status")
def project_mcpo_update_status():
    new_status = (request.form.get("status", "").strip())
    db = SessionLocal()
    try:
        proj = db.query(Project).filter(Project.slug == "mcpo-plea-deals").first()
        if not proj:
            flash("Project not found.")
            return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

        valid = {s.value for s in ProjectStatus}
        if new_status not in valid:
            flash("Invalid status.")
            return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

        proj.status = ProjectStatus(new_status)
        db.commit()
        flash("Status updated.")
        return redirect(url_for("project_detail", slug="mcpo-plea-deals"))
    finally:
        db.close()

@app.post("/projects/mcpo-plea-deals/notes/add")
def project_mcpo_add_note():
    title = (request.form.get("title", "").strip())
    body  = (request.form.get("body", "").strip() or None)

    if not title:
        flash("Note title is required.")
        return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

    db = SessionLocal()
    try:
        proj = db.query(Project).filter(Project.slug == "mcpo-plea-deals").first()
        if not proj:
            flash("Project not found.")
            return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

        n = ProjectNote(project_id=proj.id, title=title, body=body)
        db.add(n)
        db.commit()
        flash("Note added.")
        return redirect(url_for("project_detail", slug="mcpo-plea-deals"))
    finally:
        db.close()

@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    flash("Upload too large. Max total size is 512 MB.")
    return redirect(url_for("project_detail", slug="mcpo-plea-deals")), 413

@app.route("/projects/mcpo-plea-deals/upload", methods=["POST"])
def project_mcpo_upload():
    # Be strict about content type
    ctype = request.headers.get("Content-Type", "")
    if "multipart/form-data" not in ctype.lower():
        app.logger.warning("Upload rejected: missing multipart/form-data content type")
        flash("Upload failed: form encoding missing (multipart/form-data).")
        return redirect(url_for("project_detail", slug="mcpo-plea-deals")), 400

    try:
        files = request.files.getlist("files[]")
        if not files:
            # Fallback if browser didn’t send array name (some older browsers)
            files = request.files.getlist("files")
    except (BadRequest, ClientDisconnected):
        app.logger.exception("BadRequest/ClientDisconnected while parsing upload")
        flash("Upload aborted while sending files. Please try again (you can try fewer/smaller files).")
        return redirect(url_for("project_detail", slug="mcpo-plea-deals")), 400

    if not files:
        flash("Choose one or more files to upload.")
        return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

    uploaded, skipped = 0, []
    dest_dir = os.path.join("data", "projects", "mcpo-plea-deals")
    os.makedirs(dest_dir, exist_ok=True)

    db = SessionLocal()
    try:
        for f in files:
            if not f or not f.filename:
                continue

            filename = secure_filename(f.filename)
            ext = os.path.splitext(filename.lower())[1]
            if ext not in ALLOWED_EXTS:
                skipped.append(f"{filename} (unsupported type)")
                continue

            # Avoid overwriting by suffixing
            base, ext_real = os.path.splitext(filename)
            stored_path = os.path.join(dest_dir, filename)
            i = 1
            while os.path.exists(stored_path):
                filename = f"{base}-{i}{ext_real}"
                stored_path = os.path.join(dest_dir, filename)
                i += 1

            try:
                f.save(stored_path)
            except ClientDisconnected:
                app.logger.exception("ClientDisconnected while saving %r", filename)
                skipped.append(f"{filename} (client disconnected)")
                continue
            except Exception:
                app.logger.exception("Error saving %r", filename)
                skipped.append(f"{filename} (save error)")
                continue

            try:
                size = os.path.getsize(stored_path)
            except Exception:
                size = 0

            mime = f.mimetype or None

            doc = ProjectDocument(
                project_slug="mcpo-plea-deals",
                title=filename,
                filename=filename,
                stored_path=stored_path,
                mime_type=mime,
                size=size,
                notes=None
            )
            db.add(doc)
            uploaded += 1

        db.commit()
    finally:
        db.close()

    msg = []
    if uploaded:
        msg.append(f"Uploaded {uploaded} file{'s' if uploaded != 1 else ''}.")
    if skipped:
        msg.append("Skipped: " + "; ".join(skipped))
    flash(" ".join(msg) if msg else "No files processed.")
    return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

# -------- MCPO Plea Deals: edit title/notes --------
@app.post("/projects/doc/<int:doc_id>/update")
def project_doc_update(doc_id: int):
    title = (request.form.get("title", "").strip() or None)
    notes = (request.form.get("notes", "").strip() or None)

    db = SessionLocal()
    try:
        d = db.query(ProjectDocument, doc_id)
        if not d:
            flash("Document not found.")
            return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

        d.title = title or d.title
        d.notes = notes
        db.commit()
        flash("Document updated.")
    finally:
        db.close()

    return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

# -------- MCPO Plea Deals: delete --------
@app.post("/projects/doc/<int:doc_id>/delete")
def project_doc_delete(doc_id: int):
    db = SessionLocal()
    try:
        d = db.query(ProjectDocument, doc_id)
        if not d:
            flash("Document not found.")
            return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

        # remove file from disk
        try:
            if d.stored_path and os.path.exists(d.stored_path):
                os.remove(d.stored_path)
        except Exception:
            pass

        db.delete(d)
        db.commit()
        flash("Document deleted.")
    finally:
        db.close()
    return redirect(url_for("project_detail", slug="mcpo-plea-deals"))

# -------- MCPO Plea Deals: download --------
@app.get("/projects/doc/<int:doc_id>/download")
def project_doc_download(doc_id: int):
    db = SessionLocal()
    try:
        d = db.get(ProjectDocument, doc_id)  # <- returns the row or None
        if not d or not d.stored_path or not os.path.exists(d.stored_path):
            flash("File not found.")
            # try to keep user on the right project if we can
            slug = getattr(d, "project_slug", "mcpo-plea-deals")
            return redirect(url_for("project_detail", slug=slug))
        return send_file(d.stored_path, as_attachment=True, download_name=d.filename)
    finally:
        db.close()

@app.route("/dashboard")
def dashboard():
    db = SessionLocal()
    try:
        pending = (
            db.query(FoiaRequest)
              .filter(FoiaRequest.status == RequestStatus.PENDING)
              .order_by(FoiaRequest.request_date.desc())
              .all()
        )
        active_projects = (
            db.query(Project)
              .filter(Project.status == ProjectStatus.ACTIVE)
              .order_by(Project.name.asc())
              .all()
        )
        planned_projects = (
            db.query(Project)
              .filter(Project.status == ProjectStatus.PLANNED)
              .order_by(Project.name.asc())
              .all()
        )
        return render_template(
            "dashboard.html",
            pending=pending,
            active_projects=active_projects,
            planned_projects=planned_projects,  # NEW
        )
    finally:
        db.close()

@app.get("/projects/<slug>")
def project_detail(slug):
    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.slug == slug).first()
        if not p:
            flash("Project not found.")
            return redirect(url_for("projects_index"))

        # 5 most recent notes
        notes = (
            db.query(ProjectNote)
              .filter(ProjectNote.project_id == p.id)
              .order_by(ProjectNote.created_at.desc())
              .limit(5)
              .all()
        )

        # documents for this project (reusing your ProjectDocument with slug)
        docs = (
            db.query(ProjectDocument)
              .filter(ProjectDocument.project_slug == slug)
              .order_by(ProjectDocument.uploaded_at.desc())
              .all()
        )
        datasets = (
            db.query(WorkbenchDataset)
            .filter(WorkbenchDataset.project_id == p.id)
            .order_by(WorkbenchDataset.uploaded_at.desc())
            .all()
        )
        return render_template("project_detail.html", project=p, notes=notes, docs=docs, datasets=datasets)
    finally:
        db.close()

@app.post("/projects/<slug>/status")
def project_update_status(slug):
    new_status = request.form.get("status", "").strip()
    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.slug == slug).first()
        if not p:
            flash("Project not found.")
            return redirect(url_for("projects_index"))

        # Validate
        valid = {s.value for s in ProjectStatus}
        if new_status not in valid:
            flash("Invalid status.")
            return redirect(url_for("project_detail", slug=slug))

        p.status = ProjectStatus(new_status)
        db.commit()
        flash("Status updated.")
        return redirect(url_for("project_detail", slug=slug))
    finally:
        db.close()

@app.post("/projects/<slug>/notes/add")
def project_add_note(slug):
    title = (request.form.get("title", "").strip())
    body  = (request.form.get("body", "").strip() or None)
    if not title:
        flash("Note title is required.")
        return redirect(url_for("project_detail", slug=slug))

    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.slug == slug).first()
        if not p:
            flash("Project not found.")
            return redirect(url_for("projects_index"))

        n = ProjectNote(project_id=p.id, title=title, body=body)
        db.add(n)
        db.commit()
        flash("Note added.")
        return redirect(url_for("project_detail", slug=slug))
    finally:
        db.close()

# Sort All Projects by status: Active, Planned, Completed
@app.get("/projects")
def projects_index():
    db = SessionLocal()
    try:
        order = case(
            (Project.status == ProjectStatus.ACTIVE, 0),
            (Project.status == ProjectStatus.PLANNED, 1),
            (Project.status == ProjectStatus.COMPLETED, 2),
            else_=3
        )
        projects = db.query(Project).order_by(order, Project.name.asc()).all()
        return render_template("projects_index.html", projects=projects)
    finally:
        db.close()

@app.get("/projects/new")
def projects_new():
    return render_template("projects_new.html")

@app.post("/projects/new")
def projects_create():
    name = (request.form.get("name", "").strip())
    slug = (request.form.get("slug", "").strip()) or _slugify(name)
    status = (request.form.get("status", "Planned").strip())

    if not name:
        flash("Project name is required.")
        return redirect(url_for("projects_new"))

    if status not in {"Planned", "Active", "Completed"}:
        flash("Invalid status.")
        return redirect(url_for("projects_new"))

    db = SessionLocal()
    try:
        # Ensure unique slug
        exists = db.query(Project).filter(Project.slug == slug).first()
        if exists:
            flash("Slug already exists. Please choose a different slug.")
            return redirect(url_for("projects_new"))

        p = Project(name=name, slug=slug, status=ProjectStatus(status))
        db.add(p)
        db.commit()
        flash("Project created.")
        return redirect(url_for("project_detail", slug=slug))
    finally:
        db.close()

@app.cli.command("send-alerts")
def send_alerts():
    """
    Project deadline alerts (21, 14, 7 days before) AND
    FOIA pending reminders (every 7 days since last reminder).
    """
    db = SessionLocal()
    try:
        today = today_local()

        # ---- Project deadline alerts ----
        projects = db.query(Project).filter(Project.deadline.isnot(None)).all()
        for p in projects:
            dleft = days_until(p.deadline)
            if dleft in (21, 14, 7):
                # avoid double-send if we already sent today
                if p.last_deadline_alert != today:
                    subject = f"[FOIA Monitor] {p.name}: {dleft} days to deadline"
                    body = f"Project: {p.name}\nDeadline: {p.deadline}\nDays left: {dleft}\n\n{url_for('project_detail', slug=p.slug, _external=True)}"
                    send_email(Config, Config.ALERT_TO, subject, body)
                    p.last_deadline_alert = today

        # ---- FOIA every-7-days reminders (Pending only) ----
        pendings = db.query(FoiaRequest).filter(FoiaRequest.status == RequestStatus.PENDING).all()
        for r in pendings:
            last = r.last_reminder_at
            due = (last is None) or ((today - last) >= timedelta(days=7))
            if due:
                ref = r.reference_number or f"Request #{r.id}"
                subject = f"[FOIA Monitor] Reminder: {ref} still pending"
                when = r.request_date and r.request_date.strftime("%m-%d-%Y") or "unknown"
                body = f"{ref} is still pending.\nRequested: {when}\n\n{url_for('request_detail', req_id=r.id, _external=True)}"
                send_email(Config, Config.ALERT_TO, subject, body)
                r.last_reminder_at = today

        db.commit()
        print("Alerts processed.")
    finally:
        db.close()

@app.post("/projects/<slug>/deadline")
def project_update_deadline(slug):
    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.slug == slug).first()
        if not p:
            flash("Project not found.")
            return redirect(url_for("projects_index"))
        s = (request.form.get("deadline") or "").strip()
        p.deadline = datetime.strptime(s, "%Y-%m-%d").date() if s else None
        db.commit()
        flash("Deadline updated.")
        return redirect(url_for("project_detail", slug=slug))
    finally:
        db.close()

@app.get("/workbench")
def workbench_index():
    db = SessionLocal()
    try:
        ds = db.query(WorkbenchDataset).order_by(WorkbenchDataset.uploaded_at.desc()).all()
        return render_template("workbench_index.html", datasets=ds)
    finally:
        db.close()

@app.route("/workbench/upload", methods=["GET", "POST"])
def workbench_upload():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename.strip():
            flash("No file selected.", "warning")
            return redirect(url_for("workbench_upload"))

        # Dataset name: explicit form value or fallback to uploaded filename
        name = (request.form.get("name") or f.filename).strip()

        # Optional: project to associate the dataset with
        proj_id_s = (request.form.get("project_id", "") or "").strip()
        proj_id = int(proj_id_s) if proj_id_s.isdigit() else None

        # Save upload to data/workbench/
        dest_dir = os.path.join("data", "workbench")
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, secure_filename(f.filename))
        f.save(path)

        db = SessionLocal()
        try:
            # If a project_id was provided, verify it exists and is eligible (optional safeguard)
            if proj_id is not None:
                project = (
                    db.query(Project)
                      .filter(Project.id == proj_id)
                      .first()
                )
                if not project:
                    flash("Selected project was not found. Dataset will be created without a project link.", "warning")
                    proj_id = None

            # Create dataset record (set defendant_col default to your CSV header)
            ds = WorkbenchDataset(
                name=name,
                stored_path=path,
                defendant_col="defendant_name",  # change if your CSV uses a different header
                project_id=proj_id,
            )
            db.add(ds)
            db.commit()  # ensures ds.id is available

            # Scan CSV and attempt to link rows to CourtCase by exact defendant_name (case-insensitive)
            with open(path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                # Optionally: warn if expected column is missing
                if ds.defendant_col not in (reader.fieldnames or []):
                    flash(
                        f"Warning: '{ds.defendant_col}' column not found in CSV. "
                        "Links to cases will be blank unless you update defendant_col.",
                        "warning",
                    )

                for i, row in enumerate(reader):
                    raw_def = (row.get(ds.defendant_col) or "").strip()
                    matched_case_id = None
                    match_type = None

                    if raw_def:
                        case = (
                            db.query(CourtCase)
                              .filter(func.lower(CourtCase.defendant_name) == raw_def.lower())
                              .first()
                        )
                        if case:
                            matched_case_id = case.id
                            match_type = "exact"

                    db.add(
                        WorkbenchRecordLink(
                            dataset_id=ds.id,
                            row_index=i,
                            raw_defendant=raw_def,
                            matched_case_id=matched_case_id,
                            match_type=match_type,
                        )
                    )

            db.commit()
            flash("Dataset uploaded, scanned, and linked.", "success")
            return redirect(url_for("workbench_view", ds_id=ds.id))

        except Exception as e:
            db.rollback()
            app.logger.exception("Error processing uploaded dataset")
            flash(f"Upload failed: {e}", "danger")
            return redirect(url_for("workbench_upload"))

        finally:
            db.close()

    # ----- GET: render upload form with eligible projects (requested correction) -----
    if request.method == "GET":
        db = SessionLocal()
        try:
            eligible_projects = (
                db.query(Project)
                  .filter(Project.status.in_([ProjectStatus.ACTIVE, ProjectStatus.PLANNED]))
                  .order_by(Project.name.asc())
                  .all()
            )
            return render_template("workbench_upload.html", eligible_projects=eligible_projects)
        finally:
            db.close()

@app.get("/workbench/<int:ds_id>")
def workbench_view(ds_id):
    import csv
    db = SessionLocal()
    try:
        ds = db.query(WorkbenchDataset).get(ds_id)
        if not ds:
            flash("Dataset not found.")
            return redirect(url_for("workbench_index"))

        # Link/match stats (as before)
        total_links = db.query(WorkbenchRecordLink).filter_by(dataset_id=ds_id).count()
        matched = db.query(WorkbenchRecordLink)\
                    .filter_by(dataset_id=ds_id)\
                    .filter(WorkbenchRecordLink.matched_case_id.isnot(None))\
                    .count()

        # Load CSV to compute charts flexibly
        csv_rows = []
        fieldnames = []
        try:
            with open(ds.stored_path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                fieldnames = reader.fieldnames or []
                for r in reader:
                    csv_rows.append(r)
        except Exception as e:
            flash(f"Error reading CSV: {e}")

        # If nothing in CSV, render minimal page
        if not csv_rows or not fieldnames:
            return render_template(
                "workbench_view.html",
                ds=ds, total=total_links, matched=matched,
                top_rows=[], labels=[], counts=[],
                columns=fieldnames, numeric_cols=[],
                group_by=None, metric=None, limit=10,
                eligible_projects=db.query(Project).order_by(Project.name.asc()).all()
            )

        # Identify numeric columns (simple heuristic)
        def _is_number(x):
            try:
                float(x)
                return True
            except Exception:
                return False

        numeric_cols = set()
        for fn in fieldnames:
            # consider numeric if at least one value parses to number
            for r in csv_rows[:200]:  # sample
                v = (r.get(fn) or "").strip()
                if v != "" and _is_number(v):
                    numeric_cols.add(fn)
                    break

        # Controls (with sensible defaults)
        default_group = ds.defendant_col if ds.defendant_col in fieldnames else fieldnames[0]
        group_by = (request.args.get("group_by") or default_group)
        preferred_metric = "Total_Charges" if "Total_Charges" in fieldnames else "row_count"
        metric = (request.args.get("metric") or "preferred_metric")  # "row_count" or any numeric column
        limit = request.args.get("limit") or "10"
        try:
            limit = max(1, min(100, int(limit)))
        except Exception:
            limit = 10

        # Aggregate
        from collections import defaultdict
        agg = defaultdict(float)

        if metric == "row_count":
            # Count rows per group (=> "total charges" if each row = one charge)
            for r in csv_rows:
                key = (r.get(group_by) or "").strip() or "(blank)"
                agg[key] += 1.0
        else:
            # Sum a numeric column per group
            for r in csv_rows:
                key = (r.get(group_by) or "").strip() or "(blank)"
                v = (r.get(metric) or "").strip()
                if v != "" and _is_number(v):
                    agg[key] += float(v)

        # Sort and take top N
        items = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        labels = [k for (k, _v) in items]
        counts = [int(v) if metric == "row_count" else float(v) for (_k, v) in items]

        pdf_matches = []
        pdf_totals = []
        if ds.project_id:
            # sum per PDF
            from sqlalchemy import desc
            pdf_rows = (
                db.query(WorkbenchPdfLink.doc_id, ProjectDocument.title, func.sum(WorkbenchPdfLink.score).label("total"))
                .join(ProjectDocument, ProjectDocument.id == WorkbenchPdfLink.doc_id)
                .filter(WorkbenchPdfLink.dataset_id == ds.id)
                .group_by(WorkbenchPdfLink.doc_id, ProjectDocument.title)
                .order_by(desc("total"))
                .all()
            )
            pdf_totals = pdf_rows # list of (doc_id, title, total)

            # detail per PDF: top key_values
            # (for compactness, fetch top 5 per PDF)
            pdf_matches_map = {}
            for doc_id, title, _total in pdf_rows:
                kvs = (
                    db.query(WorkbenchPdfLink.key_value, func.sum(WorkbenchPdfLink.score).label("s"))
                    .filter(WorkbenchPdfLink.dataset_id == ds.id, WorkbenchPdfLink.doc_id == doc_id)
                    .group_by(WorkbenchPdfLink.key_value)
                    .order_by(desc("s"))
                    .limit(5)
                    .all()
                )
                pdf_matches_map[doc_id] = {"title": title, "pairs": kvs}
            pdf_matches = pdf_matches_map

        # Provide list of projects for the link form
        eligible_projects = (
            db.query(Project)
              .filter(Project.status.in_([ProjectStatus.ACTIVE, ProjectStatus.PLANNED, ProjectStatus.COMPLETED]))
              .order_by(Project.name.asc())
              .all()
        )

        return render_template(
            "workbench_view.html",
            ds=ds,
            total=total_links,    # from link table
            matched=matched,      # from link table
            top_rows=items, labels=labels, counts=counts,
            columns=fieldnames, numeric_cols=sorted(numeric_cols),
            group_by=group_by, metric=metric, limit=limit,
            eligible_projects=eligible_projects, pdf_matches=pdf_matches, pdf_totals=pdf_totals
        )
    finally:
        db.close()

@app.post("/workbench/<int:ds_id>/project")
def workbench_set_project(ds_id: int):
    db = SessionLocal()
    try:
        ds = db.query(WorkbenchDataset).get(ds_id)
        if not ds:
            flash("Dataset not found.")
            return redirect(url_for("workbench_index"))

        proj_id_s = request.form.get("project_id", "").strip()
        if not proj_id_s:
            ds.project_id = None
        else:
            p = db.query(Project).filter(Project.id == int(proj_id_s)).first()
            if not p:
                flash("Invalid project.")
                return redirect(url_for("workbench_view", ds_id=ds_id))
            ds.project_id = p.id

        db.commit()
        flash("Dataset project updated.")
        return redirect(url_for("workbench_view", ds_id=ds_id))
    finally:
        db.close()

@app.get("/workbench/<int:ds_id>/export.csv")
def workbench_export_csv(ds_id: int):
    import csv
    from io import StringIO
    db = SessionLocal()
    try:
        ds = db.query(WorkbenchDataset).get(ds_id)
        if not ds:
            flash("Dataset not found.")
            return redirect(url_for("workbench_index"))

        # -- load CSV rows
        rows = []
        with open(ds.stored_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fieldnames = reader.fieldnames or []
            for r in reader:
                rows.append(r)

        if not rows:
            flash("No data to export.")
            return redirect(url_for("workbench_view", ds_id=ds_id))

        # -- params
        def _is_number(x):
            try: float(x); return True
            except: return False

        group_by = (request.args.get("group_by") or (ds.defendant_col if ds.defendant_col in fieldnames else fieldnames[0]))
        metric = (request.args.get("metric") or "row_count")
        try:
            limit = max(1, min(1000, int(request.args.get("limit", "1000"))))
        except: limit = 1000

        # -- aggregate
        from collections import defaultdict
        agg = defaultdict(float)
        if metric == "row_count":
            for r in rows:
                k = (r.get(group_by) or "").strip() or "(blank)"
                agg[k] += 1
        else:
            for r in rows:
                k = (r.get(group_by) or "").strip() or "(blank)"
                v = (r.get(metric) or "").strip()
                if v != "" and _is_number(v):
                    agg[k] += float(v)

        items = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:limit]

        # -- pretty header names
        def _pretty(t):
            overrides = {"defendant_name": "Defendant Name", "row_count": "Total Charges", "total_charges": "Total Charges"}
            if t in overrides: return overrides[t]
            return re.sub(r"[_\-\s]+", " ", t).title()

        out = StringIO()
        writer = csv.writer(out)
        writer.writerow([_pretty(group_by), _pretty(metric)])
        for k, v in items:
            writer.writerow([k, int(v) if metric == "row_count" else v])

        return send_file(
            io.BytesIO(out.getvalue().encode("utf-8")),
            as_attachment=True,
            download_name=f"workbench_{ds_id}_{group_by}_{metric}.csv",
            mimetype="text/csv"
        )
    finally:
        db.close()

@app.post("/workbench/<int:ds_id>/scan_pdfs")
def workbench_scan_pdfs(ds_id: int):
    """
    Scan PDFs for occurrences of keys from the dataset's group_by column.
    Supports ?scope=project (default) or ?scope=all.
    """
    import csv
    import re
    from pdfminer.high_level import extract_text

    db = SessionLocal()
    try:
        ds = db.get(WorkbenchDataset, ds_id)
        if not ds:
            flash("Dataset not found.", "warning")
            return redirect(url_for("workbench_index"))

        group_by = request.args.get("group_by") or (ds.defendant_col or "")
        if not group_by:
            flash("No category (group_by) selected.", "warning")
            return redirect(url_for("workbench_view", ds_id=ds_id))

        # load/unique keys
        keys = []
        with open(ds.stored_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if group_by not in (reader.fieldnames or []):
                flash(f"Column '{group_by}' not found in dataset.", "warning")
                return redirect(url_for("workbench_view", ds_id=ds_id))
            for row in reader:
                v = (row.get(group_by) or "").strip()
                if v:
                    keys.append(v)
        keys = sorted(set(keys), key=str.lower)
        if not keys:
            flash("No keys found in dataset column.", "warning")
            return redirect(url_for("workbench_view", ds_id=ds_id, group_by=group_by))

        scope = (request.args.get("scope") or "project").lower()

        # choose documents to scan
        docs_q = db.query(ProjectDocument)
        if scope == "project":
            if not ds.project_id:
                flash("Link this dataset to a project first, or use scope=all.", "warning")
                return redirect(url_for("workbench_view", ds_id=ds_id))
            proj_slug = db.query(Project.slug).filter(Project.id == ds.project_id).scalar()
            docs_q = docs_q.filter(ProjectDocument.project_slug == proj_slug)

        # only PDFs (MIME or extension)
        docs = (
            docs_q.filter(
                or_(
                    (ProjectDocument.mime_type != None) & (func.lower(ProjectDocument.mime_type).like('application/pdf%')),
                    func.lower(ProjectDocument.filename).like('%.pdf')
                )
            )
            .order_by(ProjectDocument.id.asc())
            .all()
        )

        # clear old matches for this dataset
        db.query(WorkbenchPdfLink).filter(WorkbenchPdfLink.dataset_id == ds_id).delete()

        # precompile key regexes
        patterns = [(k, re.compile(r"\b" + re.escape(k.lower()) + r"\b")) for k in keys]

        for d in docs:
            try:
                txt = extract_text(d.stored_path) or ""
            except Exception:
                txt = ""
            lt = txt.lower()

            for key, rx in patterns:
                cnt = len(rx.findall(lt))
                if cnt:
                    db.add(WorkbenchPdfLink(
                        dataset_id=ds.id,
                        doc_id=d.id,
                        key_value=key,
                        score=cnt,
                        # created_at defaults ok if your model sets it; otherwise you can add it
                    ))

        db.commit()
        msg_scope = "all PDFs" if scope == "all" else "project PDFs"
        flash(f"Scanned {len(docs)} {msg_scope}; matches saved.", "success")
        return redirect(url_for("workbench_view", ds_id=ds_id, group_by=group_by))
    finally:
        db.close()

# -----------------------------
# Entrypoint
# -----------------------------
if __name__ == "__main__":
    # If AirPlay is using 5000 on macOS, change port here (e.g., 5001)
    app.run(debug=True, host="0.0.0.0", port=5000)
