# routes_mail.py
from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
import os, io
from email_client import send_via_smtp, send_via_gmail_api
from models import SessionLocal, Contact, contact_projects
from sqlalchemy import func

bp = Blueprint("mail", __name__, url_prefix="/mail")

@bp.route("/compose", methods=["GET", "POST"])
@login_required
def compose():
    # GET
    default_from = os.getenv("GMAIL_USER", "") or os.getenv("GMAIL_SENDER", "")

    # Optional filter: /mail/compose?project_id=123
    proj_id = (request.args.get("project_id") or "").strip()

    db = SessionLocal()
    try:
        if proj_id.isdigit():
            contacts = (
                db.query(Contact)
                .join(contact_projects, contact_projects.c.contact_id == Contact.id)
                .filter(contact_projects.c.project_id == int(proj_id))
                .order_by(
                    func.lower(func.coalesce(Contact.last_name, "")),
                    func.lower(func.coalesce(Contact.first_name, "")),
                    func.lower(func.coalesce(Contact.entity, ""))
                )
                .all()
            )
        else:
            contacts = (
                db.query(Contact)
                .order_by(
                    func.lower(func.coalesce(Contact.last_name, "")),
                    func.lower(func.coalesce(Contact.first_name, "")),
                    func.lower(func.coalesce(Contact.entity, ""))
                )
                .all()
            )
    finally:
        db.close()

    return render_template(
        "mail_compose.html",
        default_from=default_from,
        contacts=contacts,          # <-- template will use this to show a picker
        project_id=proj_id or None  # optional: keep track of where we came from
    )