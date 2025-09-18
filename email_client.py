# email_client.py
from __future__ import annotations
import os, base64, smtplib, mimetypes
from typing import Iterable, Optional, Tuple
from email.message import EmailMessage

# --- Optional Gmail API libs (only needed if you use Gmail API sending) ---
try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import InstalledAppFlow
    HAVE_GMAIL_LIBS = True
except Exception:
    HAVE_GMAIL_LIBS = False

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

def _build_mime(from_addr: str, to_addrs: Sequence[str], subject: str, body: str,
                attachments: Optional[Iterable[Tuple[str, bytes]]] = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content(body)

    for (filename, content) in (attachments or []):
        ctype, _ = mimetypes.guess_type(filename)
        maintype, subtype = (ctype.split("/", 1) if ctype else ("application", "octet-stream"))
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return msg

# ---------- Mode A: SMTP + App Password (RECOMMENDED for your setup) ----------
def send_via_smtp(
    to: Iterable[str],
    subject: str,
    body: str,
    attachments: Optional[Iterable[Tuple[str, bytes]]] = None
) -> None:
    # Prefer your MAIL_* vars; fall back to common alternates
    user = (
        os.getenv("MAIL_USERNAME")
        or os.getenv("SMTP_USER")
        or os.getenv("GMAIL_USER")
        or ""
    )
    app_pw = (
        os.getenv("MAIL_PASSWORD")
        or os.getenv("SMTP_PASS")
        or os.getenv("GMAIL_APP_PASSWORD")
        or ""
    )
    if not user or not app_pw:
        raise RuntimeError("MAIL_USERNAME/MAIL_PASSWORD (or SMTP_USER/SMTP_PASS) not set")

    msg = _build_mime(user, to, subject, body, attachments)

    # Defaults suit Gmail; override with SMTP_HOST/SMTP_PORT/SMTP_SSL/SMTP_STARTTLS if needed
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    use_ssl = (os.getenv("SMTP_SSL", "1").lower() not in {"0", "false", "no"})
    use_starttls = (os.getenv("SMTP_STARTTLS", "0").lower() in {"1", "true", "yes"})

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            smtp.login(user, app_pw)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if use_starttls:
                smtp.starttls()
            smtp.login(user, app_pw)
            smtp.send_message(msg)

# ---------- Mode B: Gmail API OAuth (optional) ----------
def _gmail_service():
    if not HAVE_GMAIL_LIBS:
        raise RuntimeError("Gmail API libs missing. pip install google-auth-oauthlib google-api-python-client")

    # Reuse the same filenames you already use for sync:
    cred_path = os.getenv("GMAIL_OAUTH_CLIENT_JSON", "credentials.json")
    token_path = os.getenv("GMAIL_OAUTH_TOKEN_JSON", "token.json")

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        from google.auth.transport.requests import Request
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(cred_path, SCOPES)
            # If running on a server with no browser, you can swap to flow.run_console()
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)

def send_via_gmail_api(to: Iterable[str], subject: str, body: str,
                       attachments: Optional[Iterable[Tuple[str, bytes]]] = None,
                       from_addr: Optional[str] = None) -> None:
    svc = _gmail_service_send()
    sender = from_addr or os.getenv("GMAIL_SENDER", "") or os.getenv("MAIL_USERNAME", "")
    if not sender:
        raise RuntimeError("Set GMAIL_SENDER or MAIL_USERNAME for Gmail API send")

    msg = _build_mime(sender, list(to), subject, body, attachments)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()

# ---------- One-call helper that picks the best available path ----------
def send_email(to: Iterable[str], subject: str, body: str,
               attachments: Optional[Iterable[Tuple[str, bytes]]] = None,
               from_addr: Optional[str] = None) -> None:
    """
    Prefer SMTP with MAIL_USERNAME/MAIL_PASSWORD (app password).
    If not set, try Gmail API OAuth.
    """
    if os.getenv("MAIL_USERNAME") and os.getenv("MAIL_PASSWORD"):
        return send_via_smtp(to, subject, body, attachments, from_addr=from_addr)
    return send_via_gmail_api(to, subject, body, attachments, from_addr=from_addr)
