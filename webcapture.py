# webcapture.py
import os, hashlib, json, time
from urllib.parse import urlparse
import requests
from datetime import datetime

# Screenshot via Playwright if available; gracefully degrade to HTML-only
def _try_screenshot(url, out_png_path, viewport=(1280, 2000), timeout_ms=20000):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
            page = ctx.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            # full page
            page.screenshot(path=out_png_path, full_page=True)
            title = page.title()
            browser.close()
            return title or None
    except Exception:
        return None

def _sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()

def safe_basename(url: str) -> str:
    p = urlparse(url)
    base = (p.netloc + p.path).strip("/").replace("/", "_")
    return base or "capture"

def capture_url(url: str, dest_dir: str, user_agent: str | None = None) -> dict:
    os.makedirs(dest_dir, exist_ok=True)
    headers = {"User-Agent": user_agent} if user_agent else {}

    # 1) HTML
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    html_bytes = r.content
    sha_html = _sha256_bytes(html_bytes)

    base = safe_basename(url)
    ts = int(time.time())

    html_name = f"{base}_{ts}.html"
    html_path = os.path.join(dest_dir, html_name)
    with open(html_path, "wb") as f:
        f.write(html_bytes)

    # 2) Screenshot (best effort)
    img_name = f"{base}_{ts}.png"
    img_path = os.path.join(dest_dir, img_name)
    title = _try_screenshot(url, img_path)
    sha_img = None
    if os.path.exists(img_path):
        with open(img_path, "rb") as f:
            sha_img = _sha256_bytes(f.read())
    else:
        img_path = None  # none if screenshot failed

    # 3) metadata (immutable snapshot record)
    meta = {
        "url": url,
        "title": title,
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "sha256_html": sha_html,
        "sha256_image": sha_img,
        "html_filename": os.path.basename(html_path),
        "image_filename": os.path.basename(img_path) if img_path else None,
    }
    meta_name = f"{base}_{ts}.json"
    meta_path = os.path.join(dest_dir, meta_name)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return {
        "title": title,
        "html_path": html_path,
        "image_path": img_path,
        "meta_path": meta_path,
        "sha256_html": sha_html,
        "sha256_image": sha_img,
    }
