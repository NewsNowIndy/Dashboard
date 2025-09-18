# routes_mail.py
from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
import os, io
from email_client import send_via_smtp, send_via_gmail_api

bp = Blueprint("mail", __name__, url_prefix="/mail")

@bp.route("/compose", methods=["GET", "POST"])
@login_required
def compose():
    if request.method == "POST":
        to = [t.strip() for t in (request.form.get("to") or "").split(",") if t.strip()]
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        mode = (request.form.get("mode") or "smtp").lower()

        if not to or not subject:
            flash("To and Subject are required.", "warning")
            return redirect(url_for("mail.compose"))

        # attachments (optional)
        attachments = []
        for f in (request.files.getlist("attachments") or []):
            if not f or not f.filename:
                continue
            buf = io.BytesIO()
            f.save(buf); buf.seek(0)
            attachments.append((f.filename, buf.read()))

        try:
            if mode == "gmailapi":
                send_via_gmail_api(to, subject, body, attachments)
            else:
                send_via_smtp(to, subject, body, attachments)
            flash("Email sent.", "success")
            return redirect(url_for("mail.compose"))
        except Exception as e:
            flash(f"Send failed: {e}", "danger")
            return redirect(url_for("mail.compose"))

    # GET -> show form with optional prefilled values
    default_from = (
        os.getenv("MAIL_USERNAME")  # <- prefer your SMTP app user
        or os.getenv("GMAIL_USER")
        or os.getenv("GMAIL_SENDER", "")
    )
    return render_template(
        "mail_compose.html",
        default_from=default_from,
        default_to=request.args.get("to", ""),
        default_subject=request.args.get("subject", ""),
        default_body=request.args.get("body", ""),
        default_mode=(request.args.get("mode", "smtp")).lower()
    )
