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


print("[tips] USE_ONION=", os.getenv("USE_ONION"))
print("[tips] GLOBALEAKS_ONION=", os.getenv("GLOBALEAKS_ONION"))
print("[tips] GLOBALEAKS_BASE_URL=", os.getenv("GLOBALEAKS_BASE_URL"))
print("[tips] GLOBALEAKS_SESSION_ID present=", bool(os.getenv("GLOBALEAKS_SESSION_ID")))
print("[tips] Tor 127.0.0.1:9050 up? ", _tor_probe())


def _parse_created(blob: dict) -> datetime:
    """
    Parse creation date from typical GlobaLeaks payload shapes.
    Accepts ISO8601 strings (with 'Z') or epoch numbers. Falls back to now().
    """
    if not isinstance(blob, dict):
        return datetime.now(tz=timezone.utc)

    # Common fields observed:
    # - creation_date: "2025-09-14T21:40:11.881788Z"
    # - created_at:    "2025-09-14T21:40:11.881788Z" (your local model)
    for key in ("creation_date", "created_at"):
        val = blob.get(key)
        if isinstance(val, str) and val:
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except Exception:
                pass
        if isinstance(val, (int, float)):
            try:
                return datetime.fromtimestamp(val, tz=timezone.utc)
            except Exception:
                pass

    # Nothing parsed; use now
    return datetime.now(tz=timezone.utc)


def _extract_answer(blob: dict, field_id: str) -> str | None:
    """
    Given a GlobaLeaks tip detail blob, extract the first answer value for a field.
    """
    try:
        qlist = (blob or {}).get("questionnaires") or []
        answers = (qlist[0] or {}).get("answers") or {}
        items = answers.get(field_id) or []
        val = (items[0] or {}).get("value", "")
        val = (val or "").strip()
        return val or None
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
    Sync tips from GlobaLeaks into the local DB.
    - Reads the tip list via TorGlobaLeaksClient.list_tips()
    - For each tip, fetches live detail once to extract Title/Summary from questionnaire
    - Inserts new tips or updates changed fields (status/title/summary)
    """
    start_time = time.time()
    logger.info("Starting tips sync...")

    # Instantiate client with env-provided base/user/pass; TorGlobaLeaksClient will honor USE_ONION / proxy
    client = TorGlobaLeaksClient(BASE, USER, PASS)

    logger.info("Fetching tips from GlobaLeaks...")
    rows = client.list_tips()
    logger.info(f"Retrieved {len(rows)} tips from GlobaLeaks")

    db = SessionLocal()
    inserted, updated = 0, 0

    try:
        existing = {t.glk_id: t for t in db.execute(select(Tip)).scalars().all()}
        logger.info(f"Found {len(existing)} existing tips in database")

        for r in rows:
            glk_id = r.get("id")
            if not glk_id:
                logger.warning(f"Skipping tip with no id: {r}")
                continue

            # Fetch live detail for questionnaire fields (best-effort)
            detail = {}
            try:
                detail = client.get(f"/api/recipient/rtips/{glk_id}").json()
            except Exception as e:
                logger.warning(f"Detail fetch failed for {glk_id}: {e}")

            title   = _pick_title(r, detail)
            summary = _pick_summary(r, detail)
            status  = (r.get("status") or detail.get("status") or "").strip() if isinstance(detail, dict) else (r.get("status") or "")
            created_dt = _parse_created(detail or r)

            t = existing.get(glk_id)
            if not t:
                # Insert new
                t = Tip(
                    glk_id=glk_id,
                    status=status or None,
                    title=title,
                    summary=summary,
                    created_at=created_dt,
                )
                db.add(t)
                inserted += 1
            else:
                # Update changed fields
                changed = False
                if status and status != t.status:
                    t.status = status
                    changed = True
                if title and title != t.title:
                    t.title = title
                    changed = True
                if summary and summary != t.summary:
                    t.summary = summary
                    changed = True

                # created_at is set on insert; do not rewrite unless it's missing
                if not t.created_at and created_dt:
                    t.created_at = created_dt
                    changed = True

                if changed:
                    updated += 1

        db.commit()
        elapsed = time.time() - start_time
        logger.info(f"Sync completed in {elapsed:.2f}s: {inserted} inserted, {updated} updated")
        return inserted, updated

    except Exception as e:
        logger.error(f"Database error during sync: {e}")
        db.rollback()
        raise
    finally:
        db.close()


def sync_with_retry(max_retries: int = 3, retry_delay: float = 5.0):
    """
    Wrap sync_once() with retry for transient network issues and timeouts.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return sync_once()
        except (requests.exceptions.ConnectTimeout,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError) as e:
            if attempt < max_retries:
                logger.warning(f"Sync attempt {attempt} failed: {type(e).__name__}: {e}")
                logger.info(f"Retrying in {retry_delay:.1f}s... ({max_retries - attempt} attempts left)")
                time.sleep(retry_delay)
                retry_delay *= 1.5
            else:
                logger.error(f"All {max_retries} sync attempts failed")
                raise
        except RuntimeError as e:
            # Allow retry only for timeouts; auth errors etc. should surface immediately
            if "timeout" in str(e).lower() and attempt < max_retries:
                logger.warning(f"Sync attempt {attempt} timed out: {e}")
                logger.info(f"Retrying in {retry_delay:.1f}s... ({max_retries - attempt} attempts left)")
                time.sleep(retry_delay)
                retry_delay *= 1.5
            else:
                raise
        except Exception:
            # Unknown errors: do not loop
            raise


if __name__ == "__main__":
    try:
        ins, upd = sync_with_retry()
        print(f"Sync successful: {ins} inserted, {upd} updated")
    except Exception as e:
        print(f"Sync failed: {e}")
        raise
