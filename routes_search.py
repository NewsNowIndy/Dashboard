# routes_search.py
from flask import Blueprint, request, render_template
from sqlalchemy import text
from models import engine

bp_search = Blueprint("search", __name__)

@bp_search.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    rows = []
    if q:
        # exact words + prefix fallback
        terms = q.split()
        m_exact  = " AND ".join(f'"{t}"' for t in terms)
        m_prefix = " AND ".join(f"{t}*" for t in terms)

        sql = text("""
            WITH matches AS (
              -- Project documents
              SELECT
                d.id                AS id,
                COALESCE(NULLIF(d.title,''), d.filename) AS title,
                d.project_slug      AS project_slug,
                snippet(doc_fts, 2, '<b>', '</b>', '…', 12) AS excerpt,
                bm25(doc_fts)       AS score,
                'project'           AS src
              FROM doc_fts
              JOIN project_documents d
                ON d.id = doc_fts.doc_id
              WHERE doc_fts MATCH :m

              UNION ALL

              -- FOIA attachments (join through request -> project for slug/name)
              SELECT
                a.id                AS id,
                COALESCE(NULLIF(a.filename,''), 'Attachment ' || a.id) AS title,
                p.slug              AS project_slug,
                snippet(doc_fts, 2, '<b>', '</b>', '…', 12) AS excerpt,
                bm25(doc_fts)       AS score,
                'attachment'        AS src
              FROM doc_fts
              JOIN foia_attachments a
                ON a.id = doc_fts.doc_id
              LEFT JOIN foia_requests r
                ON r.id = a.foia_request_id
              LEFT JOIN projects p
                ON p.id = r.project_id
              WHERE doc_fts MATCH :m
            )
            SELECT * FROM matches
            ORDER BY score ASC
            LIMIT 50
        """)

        with engine.begin() as conn:
            # try exact, then fallback to prefix
            for m in (m_exact, m_prefix):
                rows = list(conn.execute(sql, {"m": m}).mappings())
                if rows:
                    break

    return render_template("search.html", q=q, rows=rows)
