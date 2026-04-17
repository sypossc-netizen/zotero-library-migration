#!/usr/bin/env python3
"""
fetch_all_pdfs.py

Comprehensive PDF recovery: for every Zotero item still missing a PDF, try
in priority order:
    1. PMC OA tgz package (most reliable — works for any PMC OA paper)
    2. Unpaywall oa_locations[] — try all locations with browser UA
    3. Direct publisher URL with browser UA (retry 403s from previous pass)

Downloads saved to _attachments/{collection}/ and attached to the Zotero item.
Idempotent via state.json.

Usage:
    ZOTERO_API_KEY=... ZOTERO_USER_ID=... python3 fetch_all_pdfs.py [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import ssl
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import certifi
import yaml

SSL_CTX = ssl.create_default_context(cafile=certifi.where())

VAULT = Path(os.environ.get("VAULT_PATH", "")).expanduser()
LIBRARY = VAULT / "library"
ATTACHMENTS = VAULT / "_attachments"
STATE_PATH = Path(__file__).parent / "state.json"
LOG_PATH = Path(__file__).parent / "fetch-all.log"

ZOTERO_API_KEY = os.environ.get("ZOTERO_API_KEY", "").strip()
ZOTERO_USER_ID = os.environ.get("ZOTERO_USER_ID", "").strip()
ZOTERO_BASE = f"https://api.zotero.org/users/{ZOTERO_USER_ID}"
EMAIL = os.environ.get("UNPAYWALL_EMAIL", "").strip()

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
COURTEOUS_UA = f"Mozilla/5.0 (compatible; ZoteroMigrate/1.0; mailto:{EMAIL})"

ELINK = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
PMC_OA = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_request(method, url, data=None, headers=None, timeout=90, ua=COURTEOUS_UA):
    url = url.replace(" ", "%20")
    h = {"User-Agent": ua}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b"", dict(e.headers or {})
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
    fm_text = m.group(1)
    try:
        result = yaml.safe_load(fm_text)
        if isinstance(result, dict):
            return result
    except:
        pass
    out = {}
    for f in ("title", "year", "pmid", "doi"):
        m = re.search(rf"^{f}:\s*(.+?)$", fm_text, re.M)
        if m:
            out[f] = m.group(1).strip().strip("'\"")
    return out


def sanitize_filename(name):
    return re.sub(r'[/\\:*?"<>|]', "-", name).strip()[:200]


def is_pdf_bytes(data):
    return data and len(data) > 1024 and b"%PDF" in data[:1024]


# ── Source 1: PMC OA ────────────────────────────────────────────

def pmid_to_pmcid(pmid):
    url = f"{ELINK}?dbfrom=pubmed&id={pmid}&linkname=pubmed_pmc&retmode=json"
    status, body, _ = http_request("GET", url, timeout=30)
    time.sleep(0.5)
    if status != 200:
        return None
    try:
        data = json.loads(body)
        for ls in data.get("linksets", []):
            for link_db in ls.get("linksetdbs", []):
                if link_db.get("linkname") == "pubmed_pmc":
                    ids = link_db.get("links", [])
                    if ids:
                        return f"PMC{ids[0]}"
    except:
        pass
    return None


def pmc_oa_pdf(pmcid):
    """Download PMC OA package tgz and extract PDF. Returns bytes or None."""
    url = f"{PMC_OA}?id={pmcid}"
    status, body, _ = http_request("GET", url, timeout=30)
    time.sleep(0.5)
    if status != 200:
        return None
    try:
        root = ET.fromstring(body)
        if root.find(".//error") is not None:
            return None
        tgz_url = None
        for link in root.findall(".//link"):
            if link.get("format") == "tgz":
                tgz_url = (link.get("href") or "").replace("ftp://", "https://")
                break
        if not tgz_url:
            return None
        status, tgz_bytes, _ = http_request("GET", tgz_url, timeout=180)
        if status != 200 or not tgz_bytes:
            return None
        with tarfile.open(fileobj=io.BytesIO(tgz_bytes)) as tgz:
            pdfs = [m for m in tgz.getnames() if m.endswith(".pdf")]
            if not pdfs:
                return None
            f = tgz.extractfile(pdfs[0])
            if f is None:
                return None
            return f.read()
    except Exception as e:
        log(f"    [WARN] PMC OA failed for {pmcid}: {e}")
        return None


# ── Source 2: Unpaywall (try all locations) ─────────────────────

def unpaywall_locations(doi):
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='/:')}?email={EMAIL}"
    status, body, _ = http_request("GET", url, timeout=30)
    time.sleep(0.15)
    if status != 200:
        return []
    try:
        data = json.loads(body)
    except:
        return []
    if not data.get("is_oa"):
        return []
    # Collect all locations, prefer PMC / repository / publisher in that order
    locs = data.get("oa_locations") or []
    def priority(loc):
        ht = loc.get("host_type", "")
        url_for_pdf = loc.get("url_for_pdf") or ""
        url = loc.get("url") or ""
        if "ncbi.nlm.nih.gov/pmc" in (url_for_pdf + url).lower():
            return 0
        if ht == "repository":
            return 1
        if ht == "publisher":
            return 2
        return 3
    return sorted(locs, key=priority)


def download_url_as_pdf(url, use_browser_ua=False):
    """Download URL and return bytes if valid PDF. Handles redirects via urllib default."""
    ua = BROWSER_UA if use_browser_ua else COURTEOUS_UA
    headers = {"Accept": "application/pdf,*/*;q=0.8"}
    status, body, respheaders = http_request("GET", url, headers=headers, timeout=180, ua=ua)
    if status != 200:
        return None, f"HTTP {status}"
    if is_pdf_bytes(body):
        return body, "ok"
    # Some endpoints return HTML landing pages even when expecting PDF; check content-type
    ctype = respheaders.get("Content-Type", "").lower()
    if "pdf" in ctype and body:
        # Might have binary PDF with non-standard header
        return (body if len(body) > 1024 else None), "ok" if len(body) > 1024 else "empty"
    return None, f"not-pdf (ctype={ctype[:40]}, len={len(body)})"


# ── Zotero upload ──────────────────────────────────────────────

def attach_pdf(item_key, pdf_path):
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
    upload_body = prefix + pdf_bytes + suffix
    status, _, _ = http_request("POST", auth["url"], upload_body,
        {"Content-Type": auth["contentType"]})
    if status not in (200, 201):
        return False

    reg_body = urllib.parse.urlencode({"upload": auth["uploadKey"]}).encode()
    status, _, _ = http_request("POST", f"{ZOTERO_BASE}/items/{attach_key}/file",
        reg_body, zot_headers({"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"}))
    return status in (200, 204)


def fetch_pdf_for_paper(fm, info, dry_run):
    """Try all sources; return (pdf_bytes, source_label) or (None, reason)."""
    doi = clean_doi(fm.get("doi"))
    pmid = clean_pmid(fm.get("pmid"))

    # Strategy 1: PMC OA via PMID
    if pmid:
        pmcid = pmid_to_pmcid(pmid)
        if pmcid:
            if dry_run:
                return b"DRY", f"pmc-oa:{pmcid}"
            pdf = pmc_oa_pdf(pmcid)
            if pdf and is_pdf_bytes(pdf):
                return pdf, f"pmc-oa:{pmcid}"

    # Strategy 2: Unpaywall all locations
    if doi:
        locs = unpaywall_locations(doi)
        for loc in locs:
            pdf_url = loc.get("url_for_pdf") or loc.get("url")
            host = loc.get("host_type", "?")
            if not pdf_url:
                continue
            if dry_run:
                return b"DRY", f"unpaywall:{host}"
            # Try courteous UA first, then browser UA
            for ua_try in (False, True):
                pdf, reason = download_url_as_pdf(pdf_url, use_browser_ua=ua_try)
                if pdf and is_pdf_bytes(pdf):
                    return pdf, f"unpaywall:{host}:{'browser' if ua_try else 'polite'}"
                time.sleep(0.3)

    return None, "no-source"


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

    stats = {"attached": 0, "failed": 0, "by_source": {}}
    per_folder = {}

    for i, (note_key, info) in enumerate(candidates, 1):
        note_path = LIBRARY / note_key
        fm = read_frontmatter(note_path)
        folder = info["collection"]
        per_folder.setdefault(folder, 0)

        pdf_bytes, source = fetch_pdf_for_paper(fm, info, args.dry_run)
        if not pdf_bytes:
            continue

        src_key = source.split(":")[0]
        stats["by_source"][src_key] = stats["by_source"].get(src_key, 0) + 1
        log(f"  [{i}/{len(candidates)}] [{source}] {note_path.stem[:55]}")

        if args.dry_run:
            stats["attached"] += 1
            per_folder[folder] += 1
            continue

        # Save PDF to vault
        title = str(fm.get("title") or note_path.stem)[:70]
        year = str(fm.get("year") or "")
        author_m = re.match(r"^([A-Z][\w\-]+)", note_path.stem)
        author = author_m.group(1) if author_m else "paper"
        fname = sanitize_filename(f"{author} {year} - {title}.pdf")
        dest = ATTACHMENTS / folder / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pdf_bytes)

        if attach_pdf(info["item_key"], dest):
            info["pdf_attached"] = True
            info["pdf_path"] = str(dest)
            info["pdf_source"] = source
            STATE_PATH.write_text(json.dumps(state, indent=2))
            stats["attached"] += 1
            per_folder[folder] += 1
        else:
            stats["failed"] += 1
        time.sleep(0.3)

    log(f"\n[DONE] {stats}")
    log("Per folder:")
    for f, n in sorted(per_folder.items()):
        log(f"  {f}: +{n} PDFs")


if __name__ == "__main__":
    main()
