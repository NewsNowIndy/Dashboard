# routes_contacts.py
from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from sqlalchemy import func
from models import SessionLocal, Contact, ContactType, Project, contact_projects
from sqlalchemy.orm import joinedload

bp = Blueprint("contacts", __name__, url_prefix="/contacts")

@bp.route("/")
@login_required
def index():
    q     = (request.args.get("q") or "").strip()
    proj  = (request.args.get("project_id") or "").strip()
    kind  = (request.args.get("kind") or "").strip()

    db = SessionLocal()
    try:
        qry = (db.query(Contact)
                 .options(joinedload(Contact.projects))
                 .order_by(
                     func.lower(func.coalesce(Contact.last_name, "")),
                     func.lower(func.coalesce(Contact.first_name, "")),
                     func.lower(func.coalesce(Contact.entity, ""))
                 ))

        if q:
            like = f"%{q}%"
            qry = qry.filter(
                (Contact.first_name.ilike(like)) |
                (Contact.last_name.ilike(like))  |
                (Contact.entity.ilike(like))     |
                (Contact.email.ilike(like))      |
                (Contact.phone.ilike(like))
            )

        if proj.isdigit():
            qry = (qry.join(contact_projects, contact_projects.c.contact_id == Contact.id)
                      .filter(contact_projects.c.project_id == int(proj)))

        if kind in {t.value for t in ContactType}:
            qry = qry.filter(Contact.kind == ContactType(kind))

        rows = qry.limit(1000).all()
        projects = db.query(Project).order_by(Project.name.asc()).all()
        return render_template("contacts_index.html", rows=rows, q=q, projects=projects, kind=kind, project_id=proj)
    finally:
        db.close()

@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    db = SessionLocal()
    try:
        if request.method == "POST":
            ids = [int(x) for x in request.form.getlist("project_ids[]") if x.isdigit()]
            projects = db.query(Project).filter(Project.id.in_(ids)).all() if ids else []

            c = Contact(
                first_name=(request.form.get("first_name") or "").strip() or None,
                last_name =(request.form.get("last_name")  or "").strip() or None,
                entity    =(request.form.get("entity")     or "").strip() or None,
                email     =(request.form.get("email")      or "").strip() or None,
                phone     =(request.form.get("phone")      or "").strip() or None,
                kind      =(request.form.get("kind")       or None) or None,
                projects  = projects,
            )
            db.add(c); db.commit()
            flash("Contact saved.", "success")
            return redirect(url_for("contacts.index"))

        projects = db.query(Project).order_by(Project.name.asc()).all()
        return render_template("contacts_form.html", contact=None, projects=projects)
    finally:
        db.close()

@bp.route("/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def edit(cid: int):
    db = SessionLocal()
    try:
        c = db.get(Contact, cid)
        if not c:
            flash("Contact not found.", "warning")
            return redirect(url_for("contacts.index"))

        if request.method == "POST":
            c.first_name = (request.form.get("first_name") or "").strip() or None
            c.last_name  = (request.form.get("last_name")  or "").strip() or None
            c.entity     = (request.form.get("entity")     or "").strip() or None
            c.email      = (request.form.get("email")      or "").strip() or None
            c.phone      = (request.form.get("phone")      or "").strip() or None
            c.kind       = (request.form.get("kind")       or None) or None

            ids = [int(x) for x in request.form.getlist("project_ids[]") if x.isdigit()]
            c.projects = db.query(Project).filter(Project.id.in_(ids)).all() if ids else []
            db.commit()
            flash("Contact updated.", "success")
            return redirect(url_for("contacts.index"))

        projects = db.query(Project).order_by(Project.name.asc()).all()
        return render_template("contacts_form.html", contact=c, projects=projects)
    finally:
        db.close()

@bp.post("/<int:cid>/delete")
@login_required
def delete(cid: int):
    db = SessionLocal()
    try:
        c = db.get(Contact, cid)
        if not c:
            flash("Contact not found.", "warning")
            return redirect(url_for("contacts.index"))
        db.delete(c); db.commit()
        flash("Contact deleted.", "success")
        return redirect(url_for("contacts.index"))
    finally:
        db.close()
