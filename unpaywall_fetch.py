#!/usr/bin/env python3
"""
unpaywall_fetch.py

Third-pass: fetch open-access PDFs from Unpaywall for Zotero items still
missing attachments. Saves the PDF to _attachments/{collection}/ for durability
then attaches to Zotero.

Idempotent via state.json — marks pdf_attached=true on success.

Usage:
    ZOTERO_API_KEY=... ZOTERO_USER_ID=... python3 unpaywall_fetch.py [--dry-run]
"""

from __future__ import annotations

import argparse
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

SSL_CTX = ssl.create_default_context(cafile=certifi.where())

VAULT = Path(os.environ.get("VAULT_PATH", "")).expanduser()
LIBRARY = VAULT / "library"
ATTACHMENTS = VAULT / "_attachments"
STATE_PATH = Path(__file__).parent / "state.json"
LOG_PATH = Path(__file__).parent / "unpaywall.log"

ZOTERO_API_KEY = os.environ.get("ZOTERO_API_KEY", "").strip()
ZOTERO_USER_ID = os.environ.get("ZOTERO_USER_ID", "").strip()
ZOTERO_BASE = f"https://api.zotero.org/users/{ZOTERO_USER_ID}"
UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "").strip()


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode("ascii"), flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_request(method, url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except Exception as e:
        return 0, str(e).encode(), {}


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


def read_frontmatter(md_path):
    try:
        txt = md_path.read_text(encoding="utf-8")
    except:
        return {}
    m = re.match(r"^---\n(.*?)\n---", txt, re.DOTALL)
    if not m:
        return {}
    fm_text = m.group(1)
    try:
        result = yaml.safe_load(fm_text)
        if isinstance(result, dict):
            return result
    except:
        pass
    out = {}
    for f in ("title", "year", "pmid", "doi", "pdf", "journal"):
        m = re.search(rf"^{f}:\s*(.+?)$", fm_text, re.M)
        if m:
            out[f] = m.group(1).strip().strip("'\"")
    return out


def unpaywall_oa_url(doi):
    """Return best open-access PDF URL from Unpaywall, or None."""
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='')}?email={UNPAYWALL_EMAIL}"
    status, body, _ = http_request("GET", url)
    if status != 200:
        return None, None
    try:
        data = json.loads(body)
    except:
        return None, None
    if not data.get("is_oa"):
        return None, None
    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf") or best.get("url")
    host_type = best.get("host_type", "")
    return pdf_url, host_type


def download_pdf(url, dest_path):
    """Download PDF. Returns True on success."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 zotero-migrate"})
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            if resp.status != 200:
                return False
            ctype = resp.headers.get("Content-Type", "").lower()
            body = resp.read()
            if b"%PDF" not in body[:1024]:
                return False
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(body)
            return True
    except Exception as e:
        log(f"    [ERROR] download {url[:60]}: {e}")
        return False


def attach_pdf(item_key, pdf_path):
    """Upload PDF to Zotero as child attachment."""
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
        log(f"    [ERROR] attach create {status}")
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
    upload_body = prefix + pdf_bytes + suffix
    status, _, _ = http_request("POST", auth["url"], upload_body,
        {"Content-Type": auth["contentType"]})
    if status not in (200, 201):
        return False

    reg_body = urllib.parse.urlencode({"upload": auth["uploadKey"]}).encode()
    status, _, _ = http_request("POST", f"{ZOTERO_BASE}/items/{attach_key}/file",
        reg_body, zot_headers({"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"}))
    return status in (200, 204)


def sanitize_filename(name):
    return re.sub(r'[/\\:*?"<>|]', "-", name).strip()[:200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not ZOTERO_API_KEY or not ZOTERO_USER_ID:
        sys.exit(1)

    state = json.loads(STATE_PATH.read_text())
    candidates = [(k, info) for k, info in state["papers"].items()
                  if not info.get("pdf_attached")]

    log(f"[START] {len(candidates)} items without PDFs; dry_run={args.dry_run}")

    stats = {"oa_found": 0, "downloaded": 0, "attached": 0,
             "no_doi": 0, "not_oa": 0, "dl_failed": 0, "att_failed": 0}
    per_folder = {}

    for i, (note_key, info) in enumerate(candidates, 1):
        note_path = LIBRARY / note_key
        fm = read_frontmatter(note_path)
        doi = clean_doi(fm.get("doi"))
        if not doi:
            stats["no_doi"] += 1
            continue

        folder = info["collection"]
        per_folder.setdefault(folder, 0)

        pdf_url, host = unpaywall_oa_url(doi)
        time.sleep(0.15)  # Unpaywall rate limit
        if not pdf_url:
            stats["not_oa"] += 1
            continue
        stats["oa_found"] += 1

        log(f"  [{i}/{len(candidates)}] OA found ({host}): {note_path.stem[:50]}")
        if args.dry_run:
            continue

        # Save to _attachments/{folder}/ with derived filename
        title = str(fm.get("title") or note_path.stem)[:80]
        year = str(fm.get("year") or "")
        author_m = re.match(r"^([A-Z][\w-]+)", note_path.stem)
        author = author_m.group(1) if author_m else "paper"
        filename = sanitize_filename(f"{author} {year} - {title}.pdf")
        dest = ATTACHMENTS / folder / filename

        if not dest.exists():
            if not download_pdf(pdf_url, dest):
                stats["dl_failed"] += 1
                continue
        stats["downloaded"] += 1

        if attach_pdf(info["item_key"], dest):
            info["pdf_attached"] = True
            info["pdf_path"] = str(dest)
            info["pdf_source"] = f"unpaywall:{host}"
            STATE_PATH.write_text(json.dumps(state, indent=2))
            stats["attached"] += 1
            per_folder[folder] += 1
        else:
            stats["att_failed"] += 1
        time.sleep(0.3)

    log(f"\n[DONE] {stats}")
    log("Per folder:")
    for f, n in sorted(per_folder.items()):
        log(f"  {f}: +{n} PDFs")


if __name__ == "__main__":
    main()
