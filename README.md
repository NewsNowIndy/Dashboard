# FOIA & Court Case Dashboard

1. Create a Google Cloud project, enable **Gmail API**, create OAuth client (Desktop), download `credentials.json` to this folder.
2. `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
3. Generate a `FERNET_KEY` and set `.env` (see sample in main doc). First run will open a browser to authorize Gmail.
4. `python app.py` and open http://127.0.0.1:5000
5. Click **Sync** to pull FOIA emails, then open a request to download attachments (decrypted) or OCR’d copies.
6. Use **New Request** for manual in-person submissions.
7. **Export** downloads a CSV of reference number, request date, status, completed date.
8. **Court Cases** → **Import Data** to upload a CSV or import from Google Sheets.

### CSV Format for Court Cases
Required column: `cause_number`
Recommended columns (case-insensitive):
- `defendant_name`
- `file_date` (YYYY-MM-DD or MM/DD/YYYY)
- `charges`
- `disposition` (Dismissed/Convicted/Pending/etc.)
- `conviction_type` (Plea/Jury/Bench/N/A)
- `conviction_date` (date)
- `sentence_total_months`
- `sentence_executed_months`
- `sentence_suspended_months`
- `max_sentence_months`
```

---

## 6) Notes & Extensions

- **Reference Numbers**: Regex matches `P######-######`. If your agencies use other patterns, add them to `REF_RE` in `gmail_sync.py`.
- **Security**: Metadata fields are encrypted at rest (subject/snippet/events) and files are encrypted as `.enc`. Downloads decrypt on the fly.
- **OCR**: If `ocrmypdf` isn’t available, fallback uses PyMuPDF + Tesseract. For heavy volumes, prefer `ocrmypdf`.
- **Automation**: Add a cron job or `systemd` timer to hit `/sync` or run `sync_once()` on a schedule.
- **Access Control**: Put behind a VPN or add simple auth (Flask-Login) if you plan to expose it beyond localhost.
- **Filters**: Edit `.env` `ALLOWED_SENDERS` to only ingest from specific domains.

---

## 7) Troubleshooting

- **Gmail auth loop**: Delete `token.json` and re-run `python app.py` to re‑authorize.
- **ocrmypdf not found**: Install system package or rely on fallback OCR.
- **Attachments not decrypted**: Ensure `FERNET_KEY` in `.env` matches the one used when encrypting.
- **Large CSVs**: Increase upload size or import via command line using `sheets_ingest.import_cases_from_csv()`.