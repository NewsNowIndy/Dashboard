# auth.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, current_user
from models import SessionLocal, User

bp_auth = Blueprint("auth", __name__, template_folder="templates")

@bp_auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            if not user or not user.check_password(password):
                flash("Invalid email or password.", "danger")
                return redirect(url_for("auth.login"))
            login_user(user, remember=("remember" in request.form))
            flash("Welcome back!", "success")
            next_url = request.args.get("next")
            return redirect(next_url or url_for("dashboard"))
        finally:
            db.close()
    return render_template("login.html")

@bp_auth.route("/logout")
def logout():
    if current_user.is_authenticated:
        logout_user()
        flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
