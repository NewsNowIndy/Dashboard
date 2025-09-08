from flask import Blueprint, render_template, abort
from sqlalchemy import text
from models import engine

bp_entities = Blueprint("entities", __name__)

@bp_entities.route("/entities")
def entities_index():
    sql = text("""
    WITH mentions_resolved AS (
      SELECT m.entity_id, 'project:' || m.doc_id AS doc_key, p.name AS project_name
      FROM entity_mentions m
      JOIN project_documents d ON m.source='project' AND d.id=m.doc_id
      LEFT JOIN projects p ON p.slug=d.project_slug
      UNION ALL
      SELECT m.entity_id, 'attachment:' || m.doc_id AS doc_key, p.name AS project_name
      FROM entity_mentions m
      JOIN foia_attachments fa ON m.source='attachment' AND fa.id=m.doc_id
      LEFT JOIN foia_requests r ON r.id=fa.foia_request_id
      LEFT JOIN projects p ON p.id=r.project_id
    )
    SELECT e.id, e.name, e.kind,
           COUNT(DISTINCT mr.doc_key) AS doc_count,
           GROUP_CONCAT(DISTINCT mr.project_name) AS project_names
    FROM entities e
    LEFT JOIN mentions_resolved mr ON mr.entity_id = e.id
    GROUP BY e.id
    ORDER BY doc_count DESC, e.name ASC
    """)
    with engine.begin() as conn:
        rows = list(conn.execute(sql).mappings())
    return render_template("entities_index.html", rows=rows)

@bp_entities.route("/entities/<int:entity_id>")
def entity_detail(entity_id):
    with engine.begin() as conn:
        ent = conn.execute(text("SELECT id, name, kind FROM entities WHERE id=:id"),
                           {"id": entity_id}).mappings().first()
        if not ent:
            abort(404)
        docs = list(conn.execute(text("""
          SELECT d.id, d.title, d.project_slug, p.name AS project_name, d.uploaded_at
          FROM entity_mentions m
          JOIN project_documents d ON d.id = m.doc_id
          LEFT JOIN projects p ON p.slug = d.project_slug
          WHERE m.entity_id = :id
          ORDER BY d.uploaded_at DESC
        """), {"id": entity_id}).mappings())
    return render_template("entity_detail.html", ent=ent, docs=docs)
