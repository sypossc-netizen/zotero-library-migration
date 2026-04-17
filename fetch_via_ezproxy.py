#!/usr/bin/env python3
"""
fetch_via_ezproxy.py

Fetches paywalled PDFs for Zotero items via URMC/U of Rochester EZproxy
(ezp.lib.rochester.edu), using Playwright with a persistent browser
context so Duo/Shibboleth login is required once per extended period.

Flow:
    1. First run — opens visible Chrome, navigates to a test proxied URL,
       waits for the user to complete SSO + Duo. Once you're on an
       authenticated page, press ENTER in the terminal.
    2. Subsequent runs — reuses saved cookies (persistent context at
       ~/.config/zotero-migration/browser-state/). Skip login automatically
       if the session is still valid.
    3. For each missing paper: navigate via
       https://ezp.lib.rochester.edu/login?url=https://doi.org/{doi},
       extract citation_pdf_url meta or PDF link, download via authenticated
       request, attach to Zotero.

Usage:
    ZOTERO_API_KEY=... ZOTERO_USER_ID=... python3 fetch_via_ezproxy.py [--dry-run] [--max N]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import certifi
import yaml
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

SSL_CTX = ssl.create_default_context(cafile=certifi.where())

VAULT = Path(os.environ.get("VAULT_PATH", "")).expanduser()
LIBRARY = VAULT / "library"
ATTACHMENTS = VAULT / "_attachments"
STATE_PATH = Path(__file__).parent / "state.json"
LOG_PATH = Path(__file__).parent / "ezproxy-fetch.log"
BROWSER_STATE = Path.home() / ".config" / "zotero-migration" / "browser-state"

ZOTERO_API_KEY = os.environ.get("ZOTERO_API_KEY", "").strip()
ZOTERO_USER_ID = os.environ.get("ZOTERO_USER_ID", "").strip()
ZOTERO_BASE = f"https://api.zotero.org/users/{ZOTERO_USER_ID}"
EMAIL = os.environ.get("UNPAYWALL_EMAIL", "").strip()

PROXY = os.environ.get("EZPROXY_HOST", "login.ezpminer.urmc.rochester.edu").strip()
LOGIN_TEST_URL = os.environ.get(
    "EZPROXY_LOGIN_TEST_URL",
    "https://www.urmc.rochester.edu/libraries/miner/mdl.aspx?redirect=1186983216&proxy=on",
).strip()


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── HTTP (for Zotero uploads) ──────────────────────────────────

def http_request(method, url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b"", dict(e.headers or {})


def zot_headers(extra=None):
    h = {"Zotero-API-Key": ZOTERO_API_KEY, "Zotero-API-Version": "3"}
    if extra:
        h.update(extra)
    return h


def clean_doi(doi):
    if not doi:
        return None
    doi = str(doi).strip()
    for p in ("https://doi.org/", "http://doi.org/", "doi:", "DOI: "):
        if doi.lower().startswith(p.lower()):
            doi = doi[len(p):]
    return doi.strip() or None


def clean_pmid(pmid):
    if pmid is None:
        return None
    s = str(pmid).strip().replace("'", "").replace('"', "")
    return s if s.isdigit() else None


def read_frontmatter(md_path):
    try:
        txt = md_path.read_text(encoding="utf-8")
    except:
        return {}
    m = re.match(r"^---\n(.*?)\n---", txt, re.DOTALL)
    if not m:
        return {}
    try:
        result = yaml.safe_load(m.group(1))
        if isinstance(result, dict):
            return result
    except:
        pass
    out = {}
    for f in ("title", "year", "pmid", "doi"):
        mm = re.search(rf"^{f}:\s*(.+?)$", m.group(1), re.M)
        if mm:
            out[f] = mm.group(1).strip().strip("'\"")
    return out


def sanitize(name):
    return re.sub(r'[/\\:*?"<>|]', "-", name).strip()[:200]


def is_pdf_bytes(data):
    return data and len(data) > 1024 and b"%PDF" in data[:1024]


def proxify(url: str) -> str:
    """Rewrite a bare publisher URL through the Miner EZproxy.
    https://www.example.com/path  →  https://www-example-com.ezpminer.urmc.rochester.edu/path
    Idempotent: already-proxied URLs returned unchanged.
    """
    if not url or not url.startswith("http"):
        return url
    if PROXY in url:
        return url
    m = re.match(r"^(https?)://([^/]+)(/.*)?$", url)
    if not m:
        return url
    scheme, host, path = m.group(1), m.group(2), m.group(3) or ""
    # Don't rewrite ncbi/doi/pubmed (we want the raw resolver)
    if host in ("doi.org", "www.doi.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov"):
        return url
    host_rewritten = host.replace(".", "-")
    # EZproxy convention: strip the "login." prefix from the proxy host when building rewritten URLs.
    proxy_domain = PROXY[len("login."):] if PROXY.startswith("login.") else PROXY
    return f"{scheme}://{host_rewritten}.{proxy_domain}{path}"


# ── Zotero attach ─────────────────────────────────────────────

def attach_pdf_to_zotero(item_key, pdf_path):
    file_size = pdf_path.stat().st_size
    filename = pdf_path.name
    pdf_bytes = pdf_path.read_bytes()
    md5 = hashlib.md5(pdf_bytes).hexdigest()
    mtime = int(pdf_path.stat().st_mtime * 1000)

    payload = [{
        "itemType": "attachment", "parentItem": item_key,
        "linkMode": "imported_file", "title": filename, "filename": filename,
        "contentType": "application/pdf", "charset": ""
    }]
    status, body, _ = http_request("POST", f"{ZOTERO_BASE}/items",
        json.dumps(payload).encode(), zot_headers({"Content-Type": "application/json"}))
    if status not in (200, 201):
        return False
    resp = json.loads(body)
    if "0" not in resp.get("successful", {}):
        return False
    attach_key = resp["successful"]["0"]["key"]

    auth_body = urllib.parse.urlencode({
        "md5": md5, "filename": filename, "filesize": file_size, "mtime": mtime
    }).encode()
    status, body, _ = http_request("POST", f"{ZOTERO_BASE}/items/{attach_key}/file",
        auth_body, zot_headers({"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"}))
    if status != 200:
        return False
    auth = json.loads(body)
    if auth.get("exists"):
        return True

    prefix = auth["prefix"].encode() if isinstance(auth["prefix"], str) else auth["prefix"]
    suffix = auth["suffix"].encode() if isinstance(auth["suffix"], str) else auth["suffix"]
    status, _, _ = http_request("POST", auth["url"], prefix + pdf_bytes + suffix,
        {"Content-Type": auth["contentType"]})
    if status not in (200, 201):
        return False

    reg_body = urllib.parse.urlencode({"upload": auth["uploadKey"]}).encode()
    status, _, _ = http_request("POST", f"{ZOTERO_BASE}/items/{attach_key}/file",
        reg_body, zot_headers({"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"}))
    return status in (200, 204)


# ── Browser-based PDF fetch ────────────────────────────────────

def _looks_logged_in(url: str) -> bool:
    """True if URL looks like an authenticated proxy page (not a login prompt)."""
    if not url:
        return False
    u = url.lower()
    # If we're still on the /login?url= page, we haven't completed auth
    if "/login?url=" in u:
        return False
    # Shibboleth / SSO / Duo intermediate pages
    if any(s in u for s in ("shibboleth", "/idp/", "duosecurity", "/mfa", "sso.")):
        return False
    # If hostname contains the proxy or is a proxied publisher, we're in
    if PROXY in u or "ezpminer" in u:
        return True
    # Any non-SSO URL that loaded after the login flow likely means we're in
    return True


async def ensure_logged_in(context, page):
    """Navigate to a test URL and loop until the browser URL looks authenticated."""
    try:
        await page.goto(LOGIN_TEST_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"[WARN] initial nav: {e}")

    log("")
    log("[ACTION REQUIRED]")
    log("  A browser window should now show URMC SSO / Duo login.")
    log("  Complete login. The URL should change away from '/login?url=...' once done.")
    log("")
    while True:
        await asyncio.sleep(1)
        url = page.url
        log(f"[CHECK] URL: {url[:120]}")
        if _looks_logged_in(url):
            log(f"[OK] Looks authenticated. Proceeding.")
            return True
        log("[STILL ON LOGIN] Press ENTER to recheck, or type 'force' then ENTER to proceed anyway.")
        resp = input().strip().lower()
        if resp == "force":
            log("[FORCED] Proceeding despite login-looking URL.")
            return True


async def extract_pdf_url(page, doi):
    """Look for PDF URL in page: citation_pdf_url meta → PDF link heuristics."""
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except:
        pass

    # 1. citation_pdf_url meta tag (most journals set this)
    try:
        meta_url = await page.evaluate(
            "() => document.querySelector('meta[name=\"citation_pdf_url\"]')?.content || null"
        )
        if meta_url:
            return meta_url
    except:
        pass

    # 2. og:url or direct PDF meta tags used by some publishers
    try:
        og = await page.evaluate("""() => {
            const names = ['prism:url', 'dc.identifier', 'pdfUrl'];
            for (const n of names) {
                const el = document.querySelector(`meta[name="${n}"], meta[property="${n}"]`);
                if (el && el.content && /\\.pdf/i.test(el.content)) return el.content;
            }
            return null;
        }""")
        if og:
            return og
    except:
        pass

    # 3. Any anchor with PDF-like href
    try:
        pdf_links = await page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href]'));
            return links
                .map(a => a.href)
                .filter(h => /\\.pdf(\\?|$|#)|\\/pdf\\/|pdfdirect|pdfft|getpdf|full\\.pdf|\\/article\\/\\d+\\/download/i.test(h))
                .slice(0, 5);
        }""")
        if pdf_links:
            return pdf_links[0]
    except:
        pass

    # 4. Iframe with PDF src (Blood/ASH sometimes embed)
    try:
        iframe_src = await page.evaluate("""() => {
            const frames = Array.from(document.querySelectorAll('iframe'));
            for (const f of frames) {
                if (f.src && /\\.pdf|pdfviewer|pdf-viewer/i.test(f.src)) return f.src;
            }
            return null;
        }""")
        if iframe_src:
            return iframe_src
    except:
        pass

    return None


async def fetch_pdf_for_doi(context, page, doi, pmid):
    """Navigate to the paper via EZproxy and return PDF bytes if possible."""
    target = None
    if doi:
        target = f"https://{PROXY}/login?url=https://doi.org/{doi}"
    elif pmid:
        target = f"https://{PROXY}/login?url=https://pubmed.ncbi.nlm.nih.gov/{pmid}"
    else:
        return None, "no-identifier"

    # Set up a PDF response interceptor — many publishers load PDFs via XHR/fetch
    # after the page renders (Wiley, Blood, etc.)
    intercepted_pdf = [None]

    async def on_response(response):
        if intercepted_pdf[0] is not None:
            return
        try:
            ctype = (response.headers.get("content-type") or "").lower()
            if "pdf" in ctype:
                body = await response.body()
                if is_pdf_bytes(body) and len(body) > 10 * 1024:  # >10 KB, avoid error PDFs
                    intercepted_pdf[0] = body
        except Exception:
            pass

    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    try:
        resp = await page.goto(target, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        return None, "nav-timeout"
    except Exception as e:
        msg = str(e).splitlines()[0][:80] if str(e) else e.__class__.__name__
        return None, f"nav-error: {msg}"

    await asyncio.sleep(2)

    if "/login?url=" in page.url.lower() or "shibboleth" in page.url.lower():
        return None, "session-expired"

    if resp:
        ctype = (resp.headers.get("content-type") or "").lower()
        if "pdf" in ctype:
            try:
                body = await resp.body()
                if is_pdf_bytes(body):
                    return body, "direct-pdf"
            except:
                pass

    pdf_url = await extract_pdf_url(page, doi)
    if pdf_url:
        pdf_url = proxify(pdf_url)
        log(f"    [pdf-url] {pdf_url[:100]}")

        # Strategy A: HTTP GET via authenticated context
        try:
            r = await context.request.get(pdf_url, timeout=60000,
                                            ignore_https_errors=True,
                                            headers={
                                                "Accept": "application/pdf,*/*;q=0.8",
                                                "Referer": page.url,
                                            })
            ctype = (r.headers.get("content-type") or "").lower()
            body = await r.body()
            if "pdf" in ctype and is_pdf_bytes(body):
                return body, "http-get"
            log(f"    [http-get-not-pdf] ctype={ctype[:40]}, size={len(body)}")
        except Exception as e:
            log(f"    [http-get-error] {str(e).splitlines()[0][:100]}")

        # Strategy B: navigate to PDF URL, catching (a) download events, (b) any PDF
        # response via the interceptor, (c) direct PDF rendering
        try:
            async with page.expect_download(timeout=15000) as dl_info:
                try:
                    await page.goto(pdf_url, timeout=20000,
                                     wait_until="domcontentloaded")
                except Exception:
                    pass
            download = await dl_info.value
            dl_path = await download.path()
            if dl_path:
                body = Path(str(dl_path)).read_bytes()
                if is_pdf_bytes(body):
                    return body, "browser-download"
        except PWTimeout:
            pass  # no download event; interceptor may still have caught it
        except Exception as e:
            log(f"    [download-event-error] {str(e).splitlines()[0][:100]}")

    # Strategy C: click any "Download PDF" button on whichever page we're on
    try:
        clicked = await page.evaluate("""() => {
            const selectors = [
                'a[data-track-action*="download pdf" i]',
                'a.pdf-download',
                'button.js-btn-download-pdf',
                'a[aria-label*="Download PDF" i]',
                'button[aria-label*="Download PDF" i]',
                'a[href*="downloadpdf"]',
                'a[href*="getpdf"]',
                'a[download][href*=".pdf"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) { el.click(); return sel; }
            }
            // Text-based fallback
            const all = Array.from(document.querySelectorAll('a, button'));
            for (const el of all) {
                const txt = (el.textContent || '').toLowerCase().trim();
                if (txt === 'download pdf' || txt === 'pdf' || txt.startsWith('download pdf')) {
                    el.click();
                    return 'text-match';
                }
            }
            return null;
        }""")
        if clicked:
            log(f"    [click-dl] selector={clicked}")
            # Wait for either download event or intercepted response
            for _ in range(10):
                await asyncio.sleep(1)
                if intercepted_pdf[0]:
                    break
    except Exception as e:
        log(f"    [click-error] {str(e).splitlines()[0][:100]}")

    # Final check — did the response interceptor catch a PDF at any point?
    if intercepted_pdf[0]:
        return intercepted_pdf[0], "intercepted"

    # Strategy C: after nav, check if page itself is a PDF (render or redirect)
    last_ctype = "unknown"
    try:
        resp2 = await page.goto(pdf_url, wait_until="domcontentloaded", timeout=30000)
        if resp2:
            last_ctype = (resp2.headers.get("content-type") or "").lower()
            if "pdf" in last_ctype:
                body = await resp2.body()
                if is_pdf_bytes(body):
                    return body, "nav-pdf"
        # Scan the rendered page for a new PDF URL (viewer pages)
        pdf_url2 = await extract_pdf_url(page, doi)
        if pdf_url2 and pdf_url2 != pdf_url:
            log(f"    [retry-url] {pdf_url2[:100]}")
            r2 = await context.request.get(pdf_url2, timeout=120000,
                                            ignore_https_errors=True,
                                            headers={"Accept": "application/pdf,*/*;q=0.8"})
            body2 = await r2.body()
            if is_pdf_bytes(body2):
                return body2, "pdf-link-hop"
    except Exception as e:
        log(f"    [strategy-c-error] {str(e).splitlines()[0][:100]}")

    return None, f"not-pdf (ctype={last_ctype[:30]})"


async def run(args):
    state = json.loads(STATE_PATH.read_text())
    candidates = [(k, info) for k, info in state["papers"].items()
                  if not info.get("pdf_attached")]
    if args.max:
        candidates = candidates[:args.max]
    log(f"[START] {len(candidates)} items to attempt; dry_run={args.dry_run}")

    BROWSER_STATE.mkdir(parents=True, exist_ok=True)
    stats = {"attached": 0, "failed": 0, "reasons": {}}

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_STATE),
            headless=False,
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
            ignore_https_errors=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await ensure_logged_in(context, page)
        except Exception as e:
            log(f"[FATAL] login check failed: {e}")
            await context.close()
            return

        for i, (note_key, info) in enumerate(candidates, 1):
            note_path = LIBRARY / note_key
            fm = read_frontmatter(note_path)
            doi = clean_doi(fm.get("doi"))
            pmid = clean_pmid(fm.get("pmid"))
            folder = info["collection"]

            log(f"  [{i}/{len(candidates)}] {note_path.stem[:60]}")
            pdf_bytes, reason = await fetch_pdf_for_doi(context, page, doi, pmid)
            if not pdf_bytes:
                stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
                log(f"    [skip] {reason}")
                continue

            log(f"    [ok] {reason}  {len(pdf_bytes)//1024} KB")

            if args.dry_run:
                stats["attached"] += 1
                continue

            # Save PDF locally
            title = str(fm.get("title") or note_path.stem)[:70]
            year = str(fm.get("year") or "")
            author_m = re.match(r"^([A-Z][\w\-]+)", note_path.stem)
            author = author_m.group(1) if author_m else "paper"
            fname = sanitize(f"{author} {year} - {title}.pdf")
            dest = ATTACHMENTS / folder / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(pdf_bytes)

            if attach_pdf_to_zotero(info["item_key"], dest):
                info["pdf_attached"] = True
                info["pdf_path"] = str(dest)
                info["pdf_source"] = f"ezproxy:{reason}"
                STATE_PATH.write_text(json.dumps(state, indent=2))
                stats["attached"] += 1
            else:
                stats["failed"] += 1

            await asyncio.sleep(1.0)  # courteous delay

        await context.close()

    log(f"\n[DONE] {stats}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, help="limit attempts (useful for smoke testing)")
    args = ap.parse_args()
    if not ZOTERO_API_KEY or not ZOTERO_USER_ID:
        print("[FATAL] ZOTERO_API_KEY / ZOTERO_USER_ID required", file=sys.stderr)
        sys.exit(1)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
