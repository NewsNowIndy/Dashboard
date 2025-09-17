# tip_helpers.py
from typing import Iterable, Dict
from globaleaks_client import TorGlobaLeaksClient, BASE, USER, PASS

TITLE_KEY = "e239d748-12e9-4c3f-b401-58c699c66a4e"

def _extract_answer(blob: dict, field_id: str) -> str | None:
    try:
        qlist = (blob or {}).get("questionnaires") or []
        answers = (qlist[0] or {}).get("answers") or {}
        items = answers.get(field_id) or []
        val = (items[0] or {}).get("value", "")
        val = (val or "").strip()
        return val or None
    except Exception:
        return None

def derive_titles_for_missing(tips: Iterable) -> Dict[str, str]:
    """
    For Tip rows with blank title, fetch live detail and derive the short title
    so the UI can render it immediately (without waiting for a sync).
    Returns {glk_id: derived_title}.
    """
    client = TorGlobaLeaksClient(BASE, USER, PASS)
    out: Dict[str, str] = {}
    for t in tips:
        if getattr(t, "title", None):
            continue
        glk = getattr(t, "glk_id", None)
        if not glk:
            continue
        try:
            detail = client.get(f"/api/recipient/rtips/{glk}").json()
            derived = _extract_answer(detail, TITLE_KEY) or (detail.get("label") or "").strip()
            if derived:
                out[glk] = derived
        except Exception:
            # best-effort only
            pass
    return out
