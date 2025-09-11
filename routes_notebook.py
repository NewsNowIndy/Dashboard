# routes_notebook.py
from flask import Blueprint, request, redirect, url_for, flash
from flask_login import login_required
from models import SessionLocal, Project, CaseNotebookEntry

bp = Blueprint("notebook", __name__)

@bp.post("/projects/<slug>/notebook/add")
@login_required
def add(slug):
    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.slug == slug).first()
        if not p:
            flash("Project not found."); return redirect(url_for("projects_index"))
        kind = (request.form.get("kind") or "fact").strip().lower()
        title = (request.form.get("title") or "").strip()
        body = (request.form.get("body") or "").strip() or None
        source_url = (request.form.get("source_url") or "").strip() or None
        if not title:
            flash("Title is required."); return redirect(url_for("project_detail", slug=slug))
        n = CaseNotebookEntry(project_id=p.id, kind=kind, title=title, body=body, source_url=source_url)
        db.add(n); db.commit()
        flash("Notebook entry added.")
        return redirect(url_for("project_detail", slug=slug))
    finally:
        db.close()

@bp.post("/notebook/<int:entry_id>/pin")
@login_required
def pin(entry_id):
    db = SessionLocal()
    try:
        n = db.get(CaseNotebookEntry, entry_id)
        if not n: 
            flash("Entry not found."); return redirect(url_for("projects_index"))
        n.is_pinned = not (n.is_pinned or False)
        db.commit()
        flash("Updated.")
        # redirect back to its project
        slug = db.query(Project.slug).filter(Project.id == n.project_id).scalar()
        return redirect(url_for("project_detail", slug=slug))
    finally:
        db.close()

@bp.post("/notebook/<int:entry_id>/delete")
@login_required
def delete(entry_id):
    db = SessionLocal()
    try:
        n = db.get(CaseNotebookEntry, entry_id)
        if not n: 
            flash("Entry not found."); return redirect(url_for("projects_index"))
        slug = db.query(Project.slug).filter(Project.id == n.project_id).scalar()
        db.delete(n); db.commit()
        flash("Deleted.")
        return redirect(url_for("project_detail", slug=slug))
    finally:
        db.close()
