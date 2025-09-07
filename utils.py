from cryptography.fernet import Fernet
from sqlalchemy.types import TypeDecorator, LargeBinary
from sqlalchemy import event
from sqlalchemy.engine import Engine
import base64
from config import Config
from datetime import date, datetime, timedelta
import smtplib, ssl
from email.message import EmailMessage
from zoneinfo import ZoneInfo

fernet = Fernet(Config.FERNET_KEY)

LOCAL_TZ = ZoneInfo("America/Indiana/Indianapolis")

def today_local() -> date:
    return datetime.now(LOCAL_TZ).date()

def days_until(d: date | None) -> int | None:
    if not d: return None
    return (d - today_local()).days

def age_in_days(d: date | None) -> int | None:
    if not d: return None
    return (today_local() - d).days

def badge_for_days_left(n: int | None) -> str:
    # >21 green, 21..14 yellow, <14 red (including negatives)
    if n is None: return "bg-secondary"
    if n > 21: return "bg-success"
    if 14 <= n <= 21: return "bg-warning text-dark"
    return "bg-danger"

def badge_for_requested_age(n: int | None) -> str:
    # yellow if >21, red if >30
    if n is None: return "bg-secondary"
    if n > 30: return "bg-danger"
    if n > 21: return "bg-warning text-dark"
    return "bg-secondary"

def send_email(cfg, to_addr: str, subject: str, body: str):
    """
    Minimal SMTP sender. Configure in Config:
      MAIL_HOST, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD, MAIL_FROM, MAIL_USE_TLS, MAIL_USE_SSL
    """
    msg = EmailMessage()
    msg["From"] = getattr(cfg, "MAIL_FROM", getattr(cfg, "MAIL_USERNAME", "no-reply@example"))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    host = getattr(cfg, "MAIL_HOST", "smtp.gmail.com")
    port = int(getattr(cfg, "MAIL_PORT", 587))
    user = getattr(cfg, "MAIL_USERNAME", None)
    pwd  = getattr(cfg, "MAIL_PASSWORD", None)
    use_ssl = bool(getattr(cfg, "MAIL_USE_SSL", False))
    use_tls = bool(getattr(cfg, "MAIL_USE_TLS", not use_ssl))

    if use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context) as s:
            if user and pwd: s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            if use_tls: s.starttls(context=ssl.create_default_context())
            if user and pwd: s.login(user, pwd)
            s.send_message(msg)

# Encrypt/decrypt small text fields
class EncryptedBytes(TypeDecorator):
    impl = LargeBinary
    cache_ok = True


    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, str):
            value = value.encode('utf-8')
        return fernet.encrypt(value)


    def process_result_value(self, value, dialect):
        if value is None:
            return value
        try:
            return fernet.decrypt(value).decode('utf-8')
        except Exception:
            return None

# SQLite pragmas (integrity)
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception:
        pass


def encrypt_file(in_path: str, out_path: str):
    with open(in_path, 'rb') as f:
        ct = fernet.encrypt(f.read())
    with open(out_path, 'wb') as f:
        f.write(ct)

def decrypt_file_to_bytes(enc_path: str) -> bytes:
    with open(enc_path, 'rb') as f:
        return fernet.decrypt(f.read())
    
def normalize_request_status(s: str) -> str:
    if not s:
        return "Pending"
    s = s.strip().lower()
    if s in {"pending"}:
        return "Pending"
    if s in {"completed", "complete", "done", "closed"}:
        return "COMPLETED"
    # fallback
    return "Pending"