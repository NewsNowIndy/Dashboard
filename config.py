import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config:
    # ---- Environment toggle ----
    # APP_ENV=prod on Render; default dev locally
    APP_ENV = os.getenv("APP_ENV", "dev").lower()

    DATABASE_URL = os.getenv("DATABASE_URL")

    # ---- Storage roots (DB file + uploads) ----
    if APP_ENV == "prod":
        # Render: point at your Persistent Disk
        SQLITE_PATH = os.getenv("SQLITE_PATH", "/var/foia/foia.db")
        DATA_DIR    = os.getenv("UPLOAD_DIR", "/var/foia/uploads")
    else:
        # Local dev: use a local instance folder (outside git)
        SQLITE_PATH = os.getenv("SQLITE_PATH", os.path.join(BASE_DIR, "foia.db"))
        DATA_DIR    = os.getenv("UPLOAD_DIR", os.path.join(BASE_DIR, "data"))

    # Derived subdirs (keep your existing names)
    ATTACH_DIR   = os.path.join(DATA_DIR, "attachments")
    OCR_CACHE    = os.path.join(DATA_DIR, "ocr_cache")
    PROJECTS_DIR = os.path.join(DATA_DIR, "projects")
    WORKBENCH_DIR= os.path.join(DATA_DIR, "workbench")
    CASES_DIR    = os.path.join(DATA_DIR, "cases")

    # Ensure directories exist
    for p in (DATA_DIR, ATTACH_DIR, OCR_CACHE, PROJECTS_DIR, WORKBENCH_DIR, CASES_DIR):
        os.makedirs(p, exist_ok=True)


    SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-change-me")
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{SQLITE_PATH}?check_same_thread=false"
    SQLALCHEMY_ECHO = False
    SQLALCHEMY_TRACK_MODIFICATIONS = False


    # Must exist in env (same as your current code)
    FERNET_KEY = os.environ["FERNET_KEY"]

    # ---- Gmail / mail (unchanged defaults) ----
    GMAIL_QUERY = os.getenv("GMAIL_QUERY", "label:inbox \"Public Records Request\"")
    ALLOWED_SENDERS = [s.strip() for s in os.getenv("ALLOWED_SENDERS", "").split(',') if s.strip()]

    MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))
    FETCH_LINKED_PDFS = os.getenv("FETCH_LINKED_PDFS", "true").lower() == "true"
    ALLOWED_LINK_HOSTS = None

    MAIL_HOST = os.getenv("MAIL_HOST", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_FROM = os.getenv("MAIL_FROM", MAIL_USERNAME or "no-reply@example")
    MAIL_USE_TLS = True
    MAIL_USE_SSL = False

    ALERT_TO = os.getenv("ALERT_TO", MAIL_USERNAME)

    # Optional: used by abs_url() helper
    APP_BASE_URL = os.getenv("APP_BASE_URL", "")