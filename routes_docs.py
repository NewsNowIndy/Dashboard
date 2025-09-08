from flask import Blueprint, send_file, abort, Response, current_app as app
from models import SessionLocal, ProjectDocument
import mimetypes, os

bp_docs = Blueprint("docs", __name__)

@bp_docs.get("/docs/<int:doc_id>/inline")
def inline_view(doc_id: int):
    # return the raw PDF bytes for pdf.js viewer
    db = SessionLocal()
    try:
        d = db.get(ProjectDocument, doc_id)
        if not d or not d.stored_path:
            abort(404)
        return send_file(d.stored_path, mimetype="application/pdf")
    finally:
        db.close()
