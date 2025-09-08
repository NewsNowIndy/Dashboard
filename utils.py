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
import os, re, time

fernet = Fernet(Config.FERNET_KEY)

LOCAL_TZ = ZoneInfo("America/Indiana/Indianapolis")

SMS_GATEWAY_RX = re.compile(r"^\s*(\d{10})@tmomail\.net\s*$")

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

def _as_sms_text(subject: str, body: str) -> str:
    # Keep it very short; avoid links and punctuation spam
    txt = f"{subject}: {body}".strip()
    txt = re.sub(r'https?://\S+', '', txt)
    txt = txt.replace('\n', ' ')
    txt = ' '.join(txt.split())
    return txt[:150]  # a bit shorter than 160

def send_email(Config, to, subject, body):
    to = (to or "").strip()
    if not to:
        raise ValueError("send_email called with empty recipient")
    msg = EmailMessage()
    from_addr = getattr(Config, "SMTP_FROM", None) or os.getenv("SMTP_USER") or getattr(Config, "SMTP_USER", None)
    msg["From"] = from_addr or "alerts@example.com"
    msg["To"] = to

    if SMS_GATEWAY_RX.match(to):
        # SMS-safe: short, plain text, ASCII if possible
        sms = _as_sms_text(subject, body)
        msg["Subject"] = "ALERT"
        try:
            sms.encode("ascii")
            msg.set_content(sms, subtype="plain", charset="us-ascii")
        except UnicodeEncodeError:
            msg.set_content(sms, subtype="plain", charset="utf-8")
    else:
        msg["Subject"] = subject or "Alert"
        msg.set_content(body or "")

    host = os.getenv("SMTP_HOST", getattr(Config, "SMTP_HOST", "smtp.gmail.com"))
    port = int(os.getenv("SMTP_PORT", getattr(Config, "SMTP_PORT", 587)))
    use_tls = os.getenv("SMTP_USE_TLS", str(getattr(Config, "SMTP_USE_TLS", "1"))).lower() not in ("0","false","no")
    user = os.getenv("SMTP_USER", getattr(Config, "SMTP_USER", None))
    pwd  = os.getenv("SMTP_PASS", getattr(Config, "SMTP_PASS", None))

    attempts = 0
    delay = 2
    while True:
        attempts += 1
        s = smtplib.SMTP(host, port, timeout=30)
        try:
            if use_tls:
                s.starttls()
            if user and pwd:
                s.login(user, pwd)
            s.send_message(msg)
            return True
        except smtplib.SMTPDataError as e:
            # 421/451 = temporary; back off and retry a few times
            if e.smtp_code in (421, 451) and attempts < 5:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise
        finally:
            try: s.quit()
            except Exception: pass

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