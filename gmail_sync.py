import base64
import os
import re
from datetime import datetime, date
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse
import html

import requests
from email.utils import parsedate_to_datetime
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request

from config import Config
from models import SessionLocal, FoiaRequest, FoiaAttachment, FoiaEvent, RequestStatus
from utils import encrypt_file, decrypt_file_to_bytes
from ocr_utils import make_searchable

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
REF_RE = re.compile(r"\b[A-Z]\d{6}-\d{6}\b", re.I)

os.makedirs(getattr(Config, "ATTACH_DIR", "attachments"), exist_ok=True)
os.makedirs(getattr(Config, "OCR_CACHE", "ocr_cache"), exist_ok=True)


def gmail_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _match_allowed_sender(headers: List[Dict[str, str]]) -> bool:
    allowed = getattr(Config, "ALLOWED_SENDERS", None)
    if not allowed:
        return True
    from_addr = None
    for h in headers:
        if h.get("name", "").lower() == "from":
            from_addr = h.get("value", "").lower()
            break
    if not from_addr:
        return False
    return any(dom.lower() in from_addr for dom in allowed)


def parse_reference(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = REF_RE.search(text)
    return m.group(0) if m else None


def guess_is_ack(subject: str, body: str) -> bool:
    s = (subject or "") + "\n" + (body or "")
    keys = [
        "acknowledge", "received", "reference number",
        "your request has been received", "we have received your request",
    ]
    ls = s.lower()
    return any(k in ls for k in keys)


def guess_is_response(subject: str, body: str, attachments_present: bool) -> bool:
    if attachments_present:
        return True
    s = (subject or "") + "\n" + (body or "")
    keys = ["attached", "fulfill", "fulfilled", "records provided", "response to your request"]
    ls = s.lower()
    return any(k in ls for k in keys)


def _decode_text_parts(payload: Dict[str, Any]) -> str:
    """Return concatenated text & html bodies (decoded)."""
    collected: List[str] = []
    def walk(p: Dict[str, Any]):
        mime = p.get("mimeType", "")
        body = p.get("body", {}) or {}
        data = body.get("data")
        parts = p.get("parts", []) or []
        if data and mime.startswith("text/"):
            try:
                collected.append(base64.urlsafe_b64decode(data).decode(errors="ignore"))
            except Exception:
                pass
        for sp in parts:
            walk(sp)
    walk(payload)
    return "\n".join(collected)


def _flatten_parts(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    def walk(p: Dict[str, Any]):
        out.append(p)
        for sp in p.get("parts", []) or []:
            walk(sp)
    walk(payload)
    return out


def _cap_to_today(d: date) -> date:
    today = datetime.now().date()
    return d if d <= today else today


def _parse_gmail_date(date_header_value: str) -> datetime:
    try:
        dt = parsedate_to_datetime(date_header_value)
        if dt and dt.tzinfo is None:
            return dt
        return dt.astimezone(tz=None).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow()


def _get_or_create_request(db, ref: str, subject: str, snippet: str, dt: datetime,
                           thread_id: str, msg_id: str) -> FoiaRequest:
    reqs = (db.query(FoiaRequest)
              .filter(FoiaRequest.reference_number == ref)
              .order_by(FoiaRequest.id.asc())
              .all())
    if not reqs:
        fr = FoiaRequest(
            reference_number=ref,
            agency=None,
            request_date=_cap_to_today(dt.date()),
            status=RequestStatus.PENDING,
            subject=subject,
            snippet=snippet,
            thread_id=thread_id,
            first_message_id=msg_id,
        )
        db.add(fr); db.flush()
        return fr
    fr = reqs[0]
    if len(reqs) > 1:
        dupes = reqs[1:]
        for d in dupes:
            (db.query(FoiaAttachment)
               .filter(FoiaAttachment.foia_request_id == d.id)
               .update({FoiaAttachment.foia_request_id: fr.id}))
            (db.query(FoiaEvent)
               .filter(FoiaEvent.foia_request_id == d.id)
               .update({FoiaEvent.foia_request_id: fr.id}))
            db.delete(d)
        db.flush()
    if not fr.thread_id:
        fr.thread_id = thread_id
    if not fr.first_message_id:
        fr.first_message_id = msg_id
    if fr.completed_date:
        fr.completed_date = _cap_to_today(fr.completed_date)
    return fr


# -------- Attachments from Gmail (true MIME attachments) --------
def _collect_pdf_blobs(payload: Dict[str, Any], svc, message_id: str) -> List[Tuple[str, bytes]]:
    """Return (filename, raw_bytes) for real PDF attachments in Gmail payload."""
    pdfs: List[Tuple[str, bytes]] = []
    for part in _flatten_parts(payload):
        body = part.get("body") or {}
        mime = (part.get("mimeType") or "").lower()
        filename = (part.get("filename") or "").strip()
        if not (filename.lower().endswith(".pdf") or mime == "application/pdf"):
            continue

        if body.get("data"):
            try:
                raw = base64.urlsafe_b64decode(body["data"])
            except Exception:
                continue
            if not filename:
                filename = "attachment-inline.pdf"
            elif not filename.lower().endswith(".pdf"):
                root, _ = os.path.splitext(filename)
                filename = root + ".pdf"
            pdfs.append((filename, raw))
            continue

        att_id = body.get("attachmentId")
        if att_id:
            att = (svc.users().messages().attachments()
                   .get(userId="me", messageId=message_id, id=att_id)
                   .execute())
            data_b64 = att.get("data")
            if not data_b64:
                continue
            try:
                raw = base64.urlsafe_b64decode(data_b64)
            except Exception:
                continue
            if not filename:
                filename = f"attachment-{att_id}.pdf"
            elif not filename.lower().endswith(".pdf"):
                root, _ = os.path.splitext(filename)
                filename = root + ".pdf"
            pdfs.append((filename, raw))
            continue
    return pdfs


# -------- NEW: Linked PDFs through redirectors (e.g., SendGrid) --------
_HREF_RE = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.I)

def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    allow = getattr(Config, "ALLOWED_LINK_HOSTS", None)
    # If None or empty -> allow all (for testing)
    if not allow:
        return True
    allow = [h.lower() for h in allow]
    return any(host == h or host.endswith("." + h) for h in allow)

def _filename_from_response(url: str, resp: requests.Response) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*=UTF-8\'\'([^;]+)', cd) or re.search(r'filename=["\']?([^"\';]+)', cd)
    if m:
        name = os.path.basename(m.group(1))
    else:
        path_name = os.path.basename(urlparse(resp.url).path) or "document.pdf"
        name = path_name
    if not name.lower().endswith(".pdf"):
        root, _ = os.path.splitext(name)
        name = root + ".pdf"
    return name

def _download_linked_pdfs_from_body(html_text: str) -> List[Tuple[str, bytes]]:
    if not getattr(Config, "FETCH_LINKED_PDFS", False) or not html_text:
        return []

    hrefs = _HREF_RE.findall(html_text)
    if not hrefs:
        print("[SYNC] No hrefs found in HTML.")
        return []

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; FOIA-Dashboard/1.0)",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://mail.google.com/",
    })

    blobs: List[Tuple[str, bytes]] = []
    for url in hrefs:
        if not url.lower().startswith("http"):
            continue
        try:
            r = session.get(url, timeout=30, allow_redirects=True)
        except Exception as e:
            print(f"[SYNC] Link fetch error {url}: {e}")
            continue

        final_url = r.url
        final_host = (urlparse(final_url).hostname or "").lower()
        print(f"[SYNC] Checked link -> final_url={final_url} host={final_host} status={r.status_code} ctype={r.headers.get('Content-Type')}")

        if not _host_allowed(final_host):
            print(f"[SYNC] Skipping (host not allowed): {final_host}")
            continue

        content_type = (r.headers.get("Content-Type") or "").lower()
        content = r.content or b""
        is_pdf = content_type.startswith("application/pdf") or content[:5] == b"%PDF-"
        if not is_pdf:
            print(f"[SYNC] Skipping (not PDF): {final_url}")
            continue

        fname = _filename_from_response(final_url, r)
        blobs.append((fname, content))

    print(f"[SYNC] Linked PDFs fetched: {len(blobs)}")
    return blobs


def sync_once() -> bool:
    svc = gmail_service()
    db = SessionLocal()
    try:
        gmail_query = getattr(Config, "GMAIL_QUERY", None)

        messages: List[Dict[str, str]] = []
        resp = svc.users().messages().list(userId="me", q=gmail_query, maxResults=100).execute()
        messages.extend(resp.get("messages", []) or [])
        while resp.get("nextPageToken"):
            resp = (svc.users().messages()
                    .list(userId="me", q=gmail_query, pageToken=resp["nextPageToken"])
                    .execute())
            messages.extend(resp.get("messages", []) or [])

        for m in messages:
            msg = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
            payload = msg.get("payload", {}) or {}
            headers = payload.get("headers", []) or []

            if not _match_allowed_sender(headers):
                continue

            subject = next((h["value"] for h in headers if h.get("name", "").lower() == "subject"), "")
            date_hdr = next((h["value"] for h in headers if h.get("name", "").lower() == "date"), "")
            try:
                dt = _parse_gmail_date(date_hdr)
            except Exception:
                dt = datetime.utcnow()
            snippet = msg.get("snippet", "")

            body_text = _decode_text_parts(payload)
            ref = parse_reference(subject) or parse_reference(body_text)
            if not ref:
                continue

            # Real Gmail attachments
            attach_blobs = _collect_pdf_blobs(payload, svc, msg["id"])
            # Linked PDFs via redirect (e.g., SendGrid -> portal)
            linked_blobs = _download_linked_pdfs_from_body(body_text)

            has_any_pdfs = bool(attach_blobs or linked_blobs)
            is_ack = guess_is_ack(subject, body_text)
            is_resp = guess_is_response(subject, body_text, attachments_present=has_any_pdfs)

            fr = _get_or_create_request(
                db, ref=ref, subject=subject, snippet=snippet, dt=dt,
                thread_id=msg.get("threadId"), msg_id=msg.get("id")
            )

            # event per message (de-duped by message_id)
            existing_event = (db.query(FoiaEvent)
                                .filter(FoiaEvent.message_id == msg.get("id"))
                                .first())
            if not existing_event:
                db.add(FoiaEvent(
                    foia_request_id=fr.id,
                    event_type=("ack" if is_ack and not is_resp else ("response" if is_resp else "note")),
                    timestamp=dt,
                    message_id=msg.get("id"),
                    body=body_text[:20000],
                ))

            def _store_pdf(fname: str, raw_bytes: bytes):
                safe_name = f"{msg['id']}-{fname}"
                raw_path = os.path.join(Config.ATTACH_DIR, f"raw-{safe_name}")
                enc_path = os.path.join(Config.ATTACH_DIR, f"{safe_name}.enc")

                # de-dupe
                exists = (db.query(FoiaAttachment)
                            .filter(FoiaAttachment.stored_path == enc_path)
                            .first())
                if exists:
                    return

                with open(raw_path, "wb") as f:
                    f.write(raw_bytes)
                encrypt_file(raw_path, enc_path)
                try:
                    os.remove(raw_path)
                except Exception:
                    pass

                ocr_pdf_path = None
                try:
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_in:
                        tmp_in.write(decrypt_file_to_bytes(enc_path))
                        tmp_in.flush()
                        out_pdf = os.path.join(Config.OCR_CACHE, f"{safe_name}.pdf")
                        if make_searchable(tmp_in.name, out_pdf):
                            ocr_pdf_path = out_pdf
                        try:
                            os.unlink(tmp_in.name)
                        except Exception:
                            pass
                except Exception:
                    ocr_pdf_path = None

                db.add(FoiaAttachment(
                    foia_request_id=fr.id,
                    filename=fname,
                    mime_type="application/pdf",
                    size=len(raw_bytes),
                    stored_path=enc_path,
                    ocr_pdf_path=ocr_pdf_path,
                    is_encrypted=True,
                ))

            for fname, raw in attach_blobs + linked_blobs:
                _store_pdf(fname, raw)

            if is_resp and fr.status != RequestStatus.COMPLETE:
                fr.status = RequestStatus.COMPLETE
                fr.completed_date = _cap_to_today(dt.date())
            if fr.completed_date:
                fr.completed_date = _cap_to_today(fr.completed_date)

            db.commit()

        return True
    except HttpError as e:
        print("Gmail API error:", e); return False
    except Exception as e:
        print("Sync error:", e); return False
    finally:
        db.close()
