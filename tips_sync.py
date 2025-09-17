# tips_sync.py
from sqlalchemy import select
from datetime import datetime, timezone

import os
import time
import logging
import socket
import requests

from models import SessionLocal, Tip
from globaleaks_client import TorGlobaLeaksClient, BASE, USER, PASS  # use env-backed defaults

# Questionnaire field IDs
TITLE_KEY   = "e239d748-12e9-4c3f-b401-58c699c66a4e"  # "Please summarize your report in a few words." -> short title
SUMMARY_KEY = "0f2c5077-90f3-4b0d-8346-21b5c3b8a627"  # "Describe your report in detail." -> long summary

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _tor_probe() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 9050), 2)
        s.close()
        return True
    except Exception:
        return False
    
def _resolve_gl_base() -> str:
    use_onion = (os.getenv("USE_ONION", "0").lower() in ("1", "true", "yes"))
    if use_onion:
        return os.getenv("GLOBALEAKS_ONION", "") or ""
    return os.getenv("GLOBALEAKS_BASE_URL", "") or ""

print("[tips] USE_ONION=", os.getenv("USE_ONION"))
print("[tips] GLOBALEAKS_ONION=", os.getenv("GLOBALEAKS_ONION"))
print("[tips] GLOBALEAKS_BASE_URL=", os.getenv("GLOBALEAKS_BASE_URL"))
print("[tips] GLOBALEAKS_SESSION_ID present=", bool(os.getenv("GLOBALEAKS_SESSION_ID")))
print("[tips] Tor 127.0.0.1:9050 up? ", _tor_probe())

def list_tips_via_client(client: TorGlobaLeaksClient):
    # Use manual session - no authentication needed for list call in your flow
    return client.list_tips()

def _parse_created(row):
    """Parse creation date from various possible formats"""
    iso = row.get("created_at")
    if iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except Exception as e:
            logger.debug(f"Failed to parse ISO date '{iso}': {e}")
    
    if isinstance(row.get("creation_date"), (int, float)):
        try:
            return datetime.fromtimestamp(row["creation_date"], tz=timezone.utc)
        except Exception as e:
            logger.debug(f"Failed to parse timestamp {row['creation_date']}: {e}")
    
    # Fallback to current time
    return datetime.now(tz=timezone.utc)

def _extract_answer(blob: dict, field_id: str) -> str | None:
    try:
        qlist = (blob or {}).get("questionnaires") or []
        answers = (qlist[0] or {}).get("answers") or {}
        items = answers.get(field_id) or []
        val = (items[0] or {}).get("value", "")
        return (val or "").strip() or None
    except Exception:
        return None

def _pick_title(row: dict, detail: dict | None) -> str:
    """
    Title precedence:
    1) Questionnaire short title (TITLE_KEY) from detail
    2) row['label'] or row['title'] (from list)
    3) Fallback to 'Tip <wb_tip_id>' or 'Untitled Tip'
    """
    title_from_q = _extract_answer(detail or {}, TITLE_KEY)
    if title_from_q:
        return title_from_q

    for key in ("label", "title"):
        candidate = (row.get(key) or "").strip()
        if candidate:
            return candidate

    if row.get("wb_tip_id"):
        return f"Tip {row['wb_tip_id']}"
    return "Untitled Tip"


def _pick_summary(row: dict, detail: dict | None) -> str:
    """
    Summary precedence:
    1) Questionnaire long description (SUMMARY_KEY) from detail
    2) row['summary'] from list (if present)
    """
    summary_from_q = _extract_answer(detail or {}, SUMMARY_KEY)
    if summary_from_q:
        return summary_from_q
    return (row.get("summary") or "").strip()

def sync_once():
    """
    Sync tips from GlobaLeaks to local database with enhanced error handling.
    """
    base = _resolve_gl_base()
    if not base:
        raise RuntimeError("GlobaLeaks base URL is not configured (set GLOBALEAKS_BASE_URL or USE_ONION + GLOBALEAKS_ONION).")

    start_time = time.time()
    logger.info("Starting tips sync...")
    logger.info("Fetching tips from GlobaLeaks...")

    client = TorGlobaLeaksClient(base, os.getenv("GLOBALEAKS_USERNAME", ""), os.getenv("GLOBALEAKS_PASSWORD", ""))
    rows = list_tips_via_client(client)
    logger.info(f"Retrieved {len(rows)} tips from GlobaLeaks")
    
    try:
        db = SessionLocal()
        inserted, updated = 0, 0
        
        try:
            existing_tips = {t.glk_id: t for t in db.execute(select(Tip)).scalars().all()}
            logger.info(f"Found {len(existing_tips)} existing tips in database")
            
            for r in rows:
                glk_id = r.get("id")
                if not glk_id:
                    logger.warning(f"Skipping tip with no ID: {r}")
                    continue

                # Pull detail (needed to read questionnaire answers)
                detail = {}
                try:
                    detail = client.get(f"/api/recipient/rtips/{glk_id}").json()
                except Exception as e:
                    logger.warning(f"Failed to fetch detail for {glk_id}: {e}")

                # Prefer questionnaire Title; fallback to label/title; finally a generic one
                title_from_q = _extract_answer(detail or r, TITLE_KEY)
                title = (
                    title_from_q
                    or (r.get("label") or "").strip()
                    or (r.get("title") or "").strip()
                    or (f"Tip {r.get('wb_tip_id')}" if r.get("wb_tip_id") else "Untitled Tip")
                )

                # Optional: also persist the long Summary if you want it available offline
                summary_from_q = _extract_answer(detail or r, SUMMARY_KEY)
                summary = (summary_from_q or r.get("summary") or "").strip()

                t = existing_tips.get(glk_id)
                if not t:
                    t = Tip(
                        glk_id=glk_id,
                        status=r.get("status"),
                        title=title,
                        summary=summary,
                        created_at=_parse_created(r),
                    )
                    db.add(t)
                    inserted += 1
                else:
                    updated_fields = []
                    if r.get("status") and r["status"] != t.status:
                        t.status = r["status"]; updated_fields.append("status")
                    if title and title != t.title:
                        t.title = title; updated_fields.append("title")
                    if summary and summary != t.summary:
                        t.summary = summary; updated_fields.append("summary")
                    if updated_fields:
                        updated += 1
                    
            db.commit()
            elapsed = time.time() - start_time
            logger.info(f"Sync completed in {elapsed:.2f}s: {inserted} inserted, {updated} updated")
            return inserted, updated
            
        except Exception as db_error:
            logger.error(f"Database error during sync: {db_error}")
            db.rollback()
            raise
        finally:
            db.close()
        
    except requests.HTTPError as e:
        resp = getattr(e, "response", None)
        status_code = getattr(resp, 'status_code', 'unknown')
        
        body = ""
        try: 
            body = resp.text if resp else ""
        except Exception: 
            pass
        
        error_msg = f"HTTP {status_code} error from GlobaLeaks"
        if body:
            error_msg += f": {body[:500]}"
        
        logger.error(error_msg)
        
        if status_code == 401:
            logger.error("Authentication failed - check GLOBALEAKS_USERNAME/PASSWORD or GLOBALEAKS_SESSION_ID")
        elif status_code == 403:
            logger.error("Access forbidden - check user permissions")
        elif status_code == 412:
            logger.error("Session expired - will retry with fresh login on next attempt")
        elif status_code in (502, 503, 504):
            logger.error("GlobaLeaks server error - may be temporary")
        
        raise
        
    except requests.exceptions.ConnectTimeout:
        logger.error(f"Connection timeout to {base or 'unknown server'}")
        logger.error("Try increasing timeouts with GL_CONNECT_TIMEOUT and GL_MAX_TIMEOUT env vars")
        raise
        
    except requests.exceptions.ReadTimeout:
        logger.error("Read timeout from GlobaLeaks server")
        logger.error("Server may be slow - try increasing GL_MAX_TIMEOUT")
        raise
        
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error: {e}")
        logger.error("Check network connectivity and server URL")
        raise
        
    except RuntimeError as e:
        error_str = str(e)
        if "Login refused" in error_str:
            logger.error("GlobaLeaks authentication failed")
            logger.error("Check credentials and server accessibility")
            logger.error("Enable DEBUG=1 for detailed authentication logs")
        elif "timeout" in error_str.lower():
            logger.error("Request timed out")
            logger.error("Try increasing GL_CONNECT_TIMEOUT and GL_MAX_TIMEOUT")
        else:
            logger.error(f"Runtime error: {error_str}")
        raise
        
    except Exception as e:
        logger.error(f"Unexpected error during tips sync: {type(e).__name__}: {e}")
        raise

def sync_with_retry(max_retries=3, retry_delay=5):
    """
    Sync tips with retry logic for transient failures
    """
    for attempt in range(1, max_retries + 1):
        try:
            return sync_once()
        except (requests.exceptions.ConnectTimeout, 
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError) as e:
            if attempt < max_retries:
                logger.warning(f"Sync attempt {attempt} failed with {type(e).__name__}: {e}")
                logger.info(f"Retrying in {retry_delay}s... ({max_retries - attempt} attempts remaining)")
                time.sleep(retry_delay)
                retry_delay *= 1.5  # Exponential backoff
            else:
                logger.error(f"All {max_retries} sync attempts failed")
                raise
        except RuntimeError as e:
            if "timeout" in str(e).lower() and attempt < max_retries:
                logger.warning(f"Sync attempt {attempt} timed out: {e}")
                logger.info(f"Retrying in {retry_delay}s... ({max_retries - attempt} attempts remaining)")
                time.sleep(retry_delay)
                retry_delay *= 1.5
            else:
                raise
        except Exception:
            raise

if __name__ == "__main__":
    try:
        inserted, updated = sync_with_retry()
        print(f"Sync successful: {inserted} inserted, {updated} updated")
    except Exception as e:
        print(f"Sync failed: {e}")
        exit(1)
