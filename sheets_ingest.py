import re
import pandas as pd
from datetime import datetime
from models import SessionLocal, CourtCase, SurroundingCase
from utils import normalize_request_status

# ---------- Helpers ----------

def _norm_col_map(cols):
    """Normalize incoming CSV headers and map normalized -> original header."""
    norm = {}
    for c in cols:
        k = re.sub(r"\s+", "_", str(c).strip().lower())
        norm[k] = c
    return norm

def parse_date(s):
    """Return a date object if s looks like a date (supports 2-digit years)."""
    if pd.isna(s) or s == "":
        return None
    s_str = str(s).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s_str.split(" ")[0], fmt).date()
        except Exception:
            pass
    return None

def _to_days(v):
    """
    Parse a numeric value in DAYS from the CSV. Return int days or None.
    Accepts blanks/'nan'/strings. 0 -> None.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    try:
        val = float(s)
    except Exception:
        m = re.search(r"(-?\d+(?:\.\d+)?)", s)
        if not m:
            return None
        val = float(m.group(1))
    iv = int(round(val))
    return iv if iv != 0 else None

def _existing_max_suffix(db, base_cn: str) -> int:
    """
    Find the highest numeric suffix among rows like 'base_cn-<n>'.
    Returns 0 if none exist.
    """
    rows = db.query(CourtCase.cause_number)\
             .filter(CourtCase.cause_number.like(f"{base_cn}-%")).all()
    max_sfx = 0
    for (cn,) in rows:
        try:
            sfx = int(str(cn).rsplit("-", 1)[1])
            if sfx > max_sfx:
                max_sfx = sfx
        except Exception:
            continue
    return max_sfx

# ---------- Public entry points ----------

def import_cases_from_csv(path: str) -> int:
    df = pd.read_csv(path)
    return _upsert_cases(df)

def import_cases_from_gsheet(url_or_id: str, service_json: str) -> int:
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(service_json, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(url_or_id) if url_or_id.startswith("http") else gc.open_by_key(url_or_id)
    ws = sh.sheet1
    data = ws.get_all_records()
    df = pd.DataFrame(data)
    return _upsert_cases(df)

# ---------- Core upsert ----------

def _upsert_cases(df) -> int:
    cols = _norm_col_map(df.columns)

    def K(*norm_keys):
        for nk in norm_keys:
            if nk in cols:
                return cols[nk]
        return None

    if "cause_number" not in cols:
        raise ValueError("Missing required column: cause_number")

    # Accept corrected + older header spellings
    k_def_name  = K("defendant_name")
    k_file_date = K("file_date")
    k_charges   = K("charges")
    k_conv_date = K("conviction_date")                 # "Conviction Date"
    k_sentence  = K("sentence")                        # DAYS
    k_executed  = K("executed", "exectued")            # tolerate 'Exectued'
    k_suspended = K("suspended")
    k_max_sent  = K("max_sentence", "max_sentence_months", "max_sentence_")
    k_notes     = K("notes")                           # "Plea" | "Jury" | "Bench"

    db = SessionLocal()
    upserted = 0

    # Track counters for this import, and cache existing max suffixes per base cause_number
    seen_counter = {}
    existing_max_cache = {}

    try:
        for _, row in df.iterrows():
            base_raw = row[cols["cause_number"]]
            base_cn = str(base_raw).strip() if base_raw is not None else ""
            if not base_cn:
                continue

            # Determine next suffix for this base cause_number
            if base_cn not in existing_max_cache:
                existing_max_cache[base_cn] = _existing_max_suffix(db, base_cn)
                seen_counter[base_cn] = 0
            seen_counter[base_cn] += 1
            suffix = existing_max_cache[base_cn] + seen_counter[base_cn]
            cn = f"{base_cn}-{suffix}"  # unique key stored in DB

            cc = db.query(CourtCase).filter(CourtCase.cause_number == cn).one_or_none()
            if not cc:
                cc = CourtCase(cause_number=cn)
                db.add(cc)

            # Basics
            cc.defendant_name = (row[k_def_name] if k_def_name else None)
            cc.file_date = parse_date(row[k_file_date]) if k_file_date else None
            cc.charges = str(row[k_charges]).strip() if k_charges and not pd.isna(row[k_charges]) else None

            # Conviction Date → disposition
            conv_raw = (row[k_conv_date] if k_conv_date else None)
            conv_str = (str(conv_raw).strip() if conv_raw not in (None, float("nan")) else "")

            if conv_str.lower().startswith("dismiss"):
                disposition = "Dismissed"
                conviction_date = None
                # Dismissed rows carry no sentence/notes
                sent_total = sent_exec = sent_susp = max_sentence = None
                conviction_type = None

            elif conv_str.lower().startswith("transfer"):
                # Pending; keep whatever numbers are present (usually blank)
                disposition = None
                conviction_date = None
                sent_total   = _to_days(row[k_sentence])   if k_sentence  else None
                sent_exec    = _to_days(row[k_executed])   if k_executed  else None
                sent_susp    = _to_days(row[k_suspended])  if k_suspended else None
                max_sentence = _to_days(row[k_max_sent])   if k_max_sent  else None
                conviction_type = None

            else:
                conviction_date = parse_date(conv_str)
                disposition = "Convicted" if conviction_date else None

                # Store RAW DAYS from CSV
                sent_total   = _to_days(row[k_sentence])   if k_sentence  else None
                sent_exec    = _to_days(row[k_executed])   if k_executed  else None
                sent_susp    = _to_days(row[k_suspended])  if k_suspended else None
                max_sentence = _to_days(row[k_max_sent])   if k_max_sent  else None

                # Conviction type from Notes for convicted only
                notes = (str(row[k_notes]).strip() if k_notes and not pd.isna(row[k_notes]) else "")
                conviction_type = (notes or None) if disposition == "Convicted" else None

            # Assign to model (field names say 'months' but now hold DAYS)
            cc.disposition = disposition
            cc.conviction_type = conviction_type
            cc.conviction_date = conviction_date
            cc.sentence_total_months = sent_total
            cc.sentence_executed_months = sent_exec
            cc.sentence_suspended_months = sent_susp
            cc.max_sentence_months = max_sentence

            upserted += 1

        db.commit()
        return upserted
    finally:
        db.close()

def import_cases_from_csv(path: str) -> int:
    df = pd.read_csv(path)
    return _upsert_cases_for_model(df, CourtCase)

def import_cases_from_gsheet(url_or_id: str, service_json: str) -> int:
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    creds = Credentials.from_service_account_file(service_json, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(url_or_id) if url_or_id.startswith('http') else gc.open_by_key(url_or_id)
    ws = sh.sheet1
    data = ws.get_all_records()
    df = pd.DataFrame(data)
    return _upsert_cases_for_model(df, CourtCase)

# New public functions for Surrounding Counties:
def import_surrounding_cases_from_csv(path: str) -> int:
    df = pd.read_csv(path)
    return _upsert_cases_for_model(df, SurroundingCase)

def import_surrounding_cases_from_gsheet(url_or_id: str, service_json: str) -> int:
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    creds = Credentials.from_service_account_file(service_json, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(url_or_id) if url_or_id.startswith('http') else gc.open_by_key(url_or_id)
    ws = sh.sheet1
    data = ws.get_all_records()
    df = pd.DataFrame(data)
    return _upsert_cases_for_model(df, SurroundingCase)
