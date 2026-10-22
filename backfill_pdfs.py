#!/usr/bin/env python3
"""
backfill_pdfs.py

Second-pass PDF attachment for Zotero items whose initial migration didn't
find a matching PDF. Uses title-keyword + year scoring to match PDFs more
aggressively than the author+year regex in migrate_to_zotero.py.

Does NOT create new items or modify existing metadata — only adds attachments
to items that have pdf_attached=false in state.json. Updates state.json on
success.

Usage:
    ZOTERO_API_KEY=... ZOTERO_USER_ID=... python3 backfill_pdfs.py [--dry-run]
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
LOG_PATH = Path(__file__).parent / "backfill.log"

ZOTERO_API_KEY = os.environ.get("ZOTERO_API_KEY", "").strip()
ZOTERO_USER_ID = os.environ.get("ZOTERO_USER_ID", "").strip()
ZOTERO_BASE = f"https://api.zotero.org/users/{ZOTERO_USER_ID}"

STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "but",
    "with", "from", "by", "as", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "can", "could",
    "should", "may", "might", "must", "new", "novel", "study", "report",
    "analysis", "evaluation", "assessment", "role", "use", "using", "based",
    "approach", "method", "methods", "results", "conclusion", "conclusions",
    "via", "into", "vs", "versus", "diagnosis", "treatment", "therapy",
    "clinical", "single", "case", "series", "review", "systematic"
}


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode("ascii"), flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_request(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def zot_headers(extra=None):
    h = {"Zotero-API-Key": ZOTERO_API_KEY, "Zotero-API-Version": "3"}
    if extra:
        h.update(extra)
    return h


def tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, split, filter stopwords + short tokens."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    toks = text.split()
    return {t for t in toks if len(t) >= 3 and t not in STOPWORDS}


def extract_year(text: str) -> str | None:
    m = re.search(r"\b(19|20)\d{2}\b", text)
    return m.group(0) if m else None


def read_frontmatter(md_path: Path) -> dict:
    """Simple YAML-first, regex-fallback frontmatter read."""
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
    # Fallback
    out = {}
    for field in ("title", "year", "pmid", "doi", "pdf", "journal"):
        match = re.search(rf"^{field}:\s*(.+?)$", fm_text, re.M)
        if match:
            out[field] = match.group(1).strip().strip("'\"")
    return out


def extract_author(note_stem: str) -> str | None:
    """Extract first-author surname from note filename. Returns None for single-initial placeholders."""
    # Strip leading "Unknown" placeholders
    s = note_stem.strip()
    # Patterns: "Surname YYYY ...", "Surname et al YYYY ...", "Surname and X YYYY ..."
    m = re.match(r"^([A-Z][\wáéíóúñçü\-]{2,})\b", s)
    if not m:
        return None
    author = m.group(1)
    # Reject degenerate cases
    if author.lower() in {"unknown", "the", "a"}:
        return None
    return author


def score_match(note_title: str, note_stem: str, note_year: str | None,
                pdf_name: str) -> float:
    """Score a candidate PDF filename against a note. Higher = better.

    Rules:
      - Extract author from note stem. If author is meaningful (>=3 chars), REQUIRE
        it to appear (case-insensitive substring) in the PDF stem. Prevents the
        "same year + topic word" false-positive pattern.
      - If no meaningful author (e.g., "A. 2009 ..."), allow title-only match but
        require higher overlap count to compensate.
    """
    pdf_stem = Path(pdf_name).stem
    note_tokens = tokenize(note_title) | tokenize(note_stem)
    pdf_tokens = tokenize(pdf_stem)

    if not note_tokens or not pdf_tokens:
        return 0.0

    pdf_year = extract_year(pdf_stem)
    year_match = 1.0 if (note_year and pdf_year and note_year == pdf_year) else 0.0

    overlap = note_tokens & pdf_tokens
    if not overlap:
        return 0.0
    jaccard = len(overlap) / len(note_tokens | pdf_tokens)
    overlap_count = len(overlap)

    # Universal year parity: if both have a year and they differ, reject
    if note_year and pdf_year and note_year != pdf_year:
        return 0.0

    author = extract_author(note_stem)
    if author and len(author) >= 3:
        if author.lower() not in pdf_stem.lower():
            return 0.0
        score = year_match * 2.0 + jaccard + (overlap_count * 0.2) + 1.5
    else:
        if overlap_count < 4 or jaccard < 0.3 or not year_match:
            return 0.0
        score = year_match * 2.0 + jaccard + (overlap_count * 0.2)
    return score


def find_best_pdf(note_path: Path, collection: str,
                  already_attached: set[str]) -> Path | None:
    """Find the best-scoring PDF in attachment roots for this note."""
    fm = read_frontmatter(note_path)

    # First: try explicit pdf field (may succeed where migrate script didn't retry)
    pdf_link = fm.get("pdf", "")
    if pdf_link:
        m = re.match(r"\[\[([^\]]+?\.pdf)\]\]", pdf_link)
        filename = m.group(1) if m else pdf_link.strip()
        if filename.endswith(".pdf"):
            for root in (ATTACHMENTS / collection, ATTACHMENTS):
                if root.is_dir():
                    for p in root.rglob(filename):
                        if p.is_file() and str(p) not in already_attached:
                            return p

    # Score-based fuzzy match in folder PDFs
    title = str(fm.get("title") or note_path.stem)
    year = str(fm.get("year") or "") or extract_year(note_path.stem)
    stem = note_path.stem

    candidates = []
    for root in (ATTACHMENTS / collection,):
        if not root.is_dir():
            continue
        for p in root.glob("*.pdf"):
            if str(p) in already_attached:
                continue
            s = score_match(title, stem, year, p.name)
            if s > 0:
                candidates.append((s, p))

    if not candidates:
        return None
    candidates.sort(reverse=True, key=lambda x: x[0])
    best_score, best_pdf = candidates[0]

    # Require a reasonable threshold — score ≥ 2.0 means year match + at least some overlap
    if best_score < 2.0:
        return None
    # If top two are close, flag ambiguity and return None
    if len(candidates) > 1 and candidates[1][0] > best_score * 0.9 and candidates[1][0] >= 2.0:
        log(f"    [AMBIG] {stem[:50]} has multiple strong matches; skipping: "
            f"{[(round(s,2), p.name[:40]) for s, p in candidates[:3]]}")
        return None
    return best_pdf


def attach_pdf(item_key: str, pdf_path: Path) -> bool:
    """4-step Zotero file upload."""
    file_size = pdf_path.stat().st_size
    filename = pdf_path.name
    pdf_bytes = pdf_path.read_bytes()
    md5 = hashlib.md5(pdf_bytes).hexdigest()
    mtime = int(pdf_path.stat().st_mtime * 1000)

    # Step 1: attachment item
    payload = [{
        "itemType": "attachment", "parentItem": item_key,
        "linkMode": "imported_file", "title": filename, "filename": filename,
        "contentType": "application/pdf", "charset": ""
    }]
    status, body, _ = http_request(
        "POST", f"{ZOTERO_BASE}/items",
        json.dumps(payload).encode("utf-8"),
        zot_headers({"Content-Type": "application/json"})
    )
    if status not in (200, 201):
        log(f"    [ERROR] attach item create: {status}")
        return False
    resp = json.loads(body)
    if "0" not in resp.get("successful", {}):
        log(f"    [ERROR] attach item: {resp}")
        return False
    attach_key = resp["successful"]["0"]["key"]

    # Step 2: upload auth
    auth_body = urllib.parse.urlencode({
        "md5": md5, "filename": filename, "filesize": file_size, "mtime": mtime
    }).encode("utf-8")
    status, body, _ = http_request(
        "POST", f"{ZOTERO_BASE}/items/{attach_key}/file", auth_body,
        zot_headers({"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"})
    )
    if status != 200:
        log(f"    [ERROR] upload auth: {status}")
        return False
    auth = json.loads(body)
    if auth.get("exists"):
        return True

    # Step 3: S3 upload
    prefix = auth["prefix"].encode() if isinstance(auth["prefix"], str) else auth["prefix"]
    suffix = auth["suffix"].encode() if isinstance(auth["suffix"], str) else auth["suffix"]
    upload_body = prefix + pdf_bytes + suffix
    status, _, _ = http_request("POST", auth["url"], upload_body,
                                {"Content-Type": auth["contentType"]})
    if status not in (200, 201):
        log(f"    [ERROR] S3 upload: {status}")
        return False

    # Step 4: register
    reg_body = urllib.parse.urlencode({"upload": auth["uploadKey"]}).encode()
    status, _, _ = http_request(
        "POST", f"{ZOTERO_BASE}/items/{attach_key}/file", reg_body,
        zot_headers({"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"})
    )
    return status in (200, 204)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not ZOTERO_API_KEY or not ZOTERO_USER_ID:
        print("[FATAL] creds missing", file=sys.stderr)
        sys.exit(1)

    state = json.loads(STATE_PATH.read_text())
    # Track PDFs already attached across all papers so we don't double-attach
    already = {info.get("pdf_path", "") for info in state["papers"].values()
               if info.get("pdf_path")}

    candidates = [(k, info) for k, info in state["papers"].items()
                  if not info.get("pdf_attached")]

    log(f"[START] {len(candidates)} items without PDFs; dry_run={args.dry_run}")

    per_folder = {}
    attached = 0
    ambiguous = 0
    not_found = 0

    for i, (note_key, info) in enumerate(candidates, 1):
        note_path = LIBRARY / note_key
        collection = info["collection"]
        per_folder.setdefault(collection, {"attached": 0, "not_found": 0})

        pdf = find_best_pdf(note_path, collection, already)
        if not pdf:
            per_folder[collection]["not_found"] += 1
            not_found += 1
            continue

        log(f"  [{i}/{len(candidates)}] {note_path.stem[:50]}  →  {pdf.name[:50]}")
        if args.dry_run:
            per_folder[collection]["attached"] += 1
            attached += 1
            continue

        ok = attach_pdf(info["item_key"], pdf)
        if ok:
            info["pdf_attached"] = True
            info["pdf_path"] = str(pdf)
            already.add(str(pdf))
            STATE_PATH.write_text(json.dumps(state, indent=2))
            per_folder[collection]["attached"] += 1
            attached += 1
        time.sleep(0.3)

    log(f"\n[DONE] attached={attached}, not_found={not_found}")
    log("Per folder:")
    for f, v in sorted(per_folder.items()):
        log(f"  {f}: +{v['attached']} PDFs  ({v['not_found']} remain unmatched)")


if __name__ == "__main__":
    main()
