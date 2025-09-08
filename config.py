import os
from dotenv import load_dotenv


load_dotenv()


class Config:
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///foia.db")
    SQLALCHEMY_ECHO = False
    SQLALCHEMY_TRACK_MODIFICATIONS = False


    DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
    ATTACH_DIR = os.path.join(DATA_DIR, 'attachments')
    OCR_CACHE = os.path.join(DATA_DIR, 'ocr_cache')


    os.makedirs(ATTACH_DIR, exist_ok=True)
    os.makedirs(OCR_CACHE, exist_ok=True)


    FERNET_KEY = os.environ["FERNET_KEY"] # must exist


    GMAIL_QUERY = os.getenv("GMAIL_QUERY", "label:inbox \"Public Records Request\"")
    ALLOWED_SENDERS = [s.strip() for s in os.getenv("ALLOWED_SENDERS", "").split(',') if s.strip()]


    # Court Cases
    MAX_UPLOAD_SIZE_MB = 50

    # Allow fetching PDF links embedded in the email HTML
    FETCH_LINKED_PDFS = True

    # Only fetch from these hostnames (lowercase). Add what you need:
    # Common patterns: "indy.gov", "mycusthelp.com", "nextrequest.com", "sharefile.com"
    ALLOWED_LINK_HOSTS = None

    MAIL_HOST = os.getenv("MAIL_HOST", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_FROM = os.getenv("MAIL_FROM", MAIL_USERNAME or "no-reply@example")
    MAIL_USE_TLS = True
    MAIL_USE_SSL = False

    ALERT_TO = os.getenv("ALERT_TO", MAIL_USERNAME)  # where to send alerts