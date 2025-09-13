# policies.py
from flask import Blueprint, render_template, make_response
from datetime import date

policies = Blueprint("policies", __name__, url_prefix="/p")

@policies.after_request
def add_headers(resp):
    # Discourage search indexing and reduce exposure surface.
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; base-uri 'none'; frame-ancestors 'none';"
    resp.headers.setdefault("Cache-Control", "public, max-age=300")
    return resp

@policies.route("/whistleblowing-policy")
def whistle_policy():
    return make_response(render_template("policies/whistleblowing.html", last_updated=date.today()))

@policies.route("/privacy")
def privacy_policy():
    return make_response(render_template("policies/privacy.html", last_updated=date.today()))
