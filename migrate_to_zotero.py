#!/usr/bin/env python3
"""
migrate_to_zotero.py

Migrates Research/library/ papers into a Zotero Web Library, creating a
collection per source folder and uploading metadata + PDF attachments.

Idempotent: checkpoints successfully migrated papers to state.json so re-runs
skip anything already imported. Safe to interrupt and resume.

Usage:
    # Dry run — zero writes, prints plan
    ZOTERO_API_KEY=... ZOTERO_USER_ID=... python3 migrate_to_zotero.py --folder "TRBC1-TRBC2 Clonality" --dry-run

    # Real run
    ZOTERO_API_KEY=... ZOTERO_USER_ID=... python3 migrate_to_zotero.py --folder "TRBC1-TRBC2 Clonality"

    # All folders
    ZOTERO_API_KEY=... ZOTERO_USER_ID=... python3 migrate_to_zotero.py --all
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
from typing import Any

import certifi
import yaml

SSL_CTX = ssl.create_default_context(cafile=certifi.where())

VAULT = Path(os.environ.get("VAULT_PATH", "")).expanduser()
LIBRARY = VAULT / "library"
ATTACHMENTS = VAULT / "_attachments"
STATE_PATH = Path(__file__).parent / "state.json"
LOG_PATH = Path(__file__).parent / "migrate.log"

ZOTERO_API_KEY = os.environ.get("ZOTERO_API_KEY", "").strip()
ZOTERO_USER_ID = os.environ.get("ZOTERO_USER_ID", "").strip()
ZOTERO_BASE = f"https://api.zotero.org/users/{ZOTERO_USER_ID}"

UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "").strip()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode("ascii"), flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"collections": {}, "papers": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def http_request(method: str, url: str, data: bytes | None = None,
                 headers: dict | None = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def zot_headers(extra: dict | None = None) -> dict:
    h = {"Zotero-API-Key": ZOTERO_API_KEY, "Zotero-API-Version": "3"}
    if extra:
        h.update(extra)
    return h


def _fallback_frontmatter_parse(fm_text: str) -> dict:
    """Regex-based recovery when PyYAML fails. Extracts common scalar fields + list fields."""
    out: dict = {}
    # Scalar fields — grab raw string, strip quotes, handle first-match only
    scalar_fields = ("title", "journal", "year", "doi", "pmid", "url", "note_type", "state",
                     "study_type", "evidence_level", "subspecialty", "pdf", "source",
                     "relevance_score", "added", "last_updated")
    for field in scalar_fields:
        m = re.search(rf"^{field}:\s*(.+?)$", fm_text, re.M)
        if m:
            val = m.group(1).strip()
            val = val.strip("'\"")
            out[field] = val
    # List fields — YAML-style `- item` blocks
    for field in ("authors", "tags", "entities", "projects"):
        m = re.search(rf"^{field}:\s*\n((?:\s+-\s+.+\n?)+)", fm_text, re.M)
        if m:
            items = re.findall(r"^\s+-\s+(.+?)$", m.group(1), re.M)
            out[field] = [i.strip().strip("'\"") for i in items]
        else:
            # Flow-style: field: [a, b, c]
            m2 = re.search(rf"^{field}:\s*\[(.*?)\]", fm_text, re.M)
            if m2:
                items = [i.strip().strip("'\"") for i in m2.group(1).split(",") if i.strip()]
                out[field] = items
    return out


def read_frontmatter(md_path: Path) -> dict | None:
    txt = md_path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", txt, re.DOTALL)
    if not m:
        return None
    fm_text = m.group(1)
    try:
        result = yaml.safe_load(fm_text)
        if isinstance(result, dict):
            return result
    except Exception as e:
        log(f"  [WARN] YAML parse error for {md_path.name}: {e.__class__.__name__} — using fallback")
    # Fallback to regex-based parse
    return _fallback_frontmatter_parse(fm_text)


def clean_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = str(doi).strip()
    # Strip common URL prefixes
    for p in ("https://doi.org/", "http://doi.org/", "doi:", "DOI: "):
        if doi.lower().startswith(p.lower()):
            doi = doi[len(p):]
    return doi.strip() or None


def clean_pmid(pmid: Any) -> str | None:
    if pmid is None:
        return None
    s = str(pmid).strip().replace("'", "").replace('"', "")
    return s if s.isdigit() else None


def fetch_crossref(doi: str) -> dict | None:
    """Fetch rich bibliographic metadata from CrossRef for a DOI."""
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
    status, body, _ = http_request("GET", url, headers={"User-Agent": f"zotero-migrate/0.1 ({UNPAYWALL_EMAIL})"})
    if status != 200:
        return None
    try:
        return json.loads(body)["message"]
    except Exception:
        return None


def fetch_pubmed(pmid: str) -> dict | None:
    """Fetch PubMed summary for PMID (fallback when no DOI)."""
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id={pmid}&retmode=json"
    status, body, _ = http_request("GET", url)
    if status != 200:
        return None
    try:
        return json.loads(body).get("result", {}).get(pmid)
    except Exception:
        return None


def build_zotero_item(fm: dict, crossref: dict | None, pubmed: dict | None) -> dict:
    """Build a Zotero journalArticle item from frontmatter + enrichment."""
    doi = clean_doi(fm.get("doi"))
    pmid = clean_pmid(fm.get("pmid"))
    title = (fm.get("title") or "").strip().strip('"')

    authors = []
    if crossref and "author" in crossref:
        for a in crossref["author"]:
            first = (a.get("given") or "").strip()
            last = (a.get("family") or "").strip()
            name = (a.get("name") or "").strip()
            if first or last:
                authors.append({"creatorType": "author", "firstName": first, "lastName": last})
            elif name:
                authors.append({"creatorType": "author", "name": name})
            # else: skip entry with no identifying info
    elif fm.get("authors"):
        for a in fm["authors"]:
            if not isinstance(a, str):
                continue
            a = a.strip()
            if not a:
                continue
            # PubMed convention: "Last FM" or "Last F" — surname first, initials last
            parts = a.rsplit(" ", 1)
            if len(parts) == 2 and 1 <= len(parts[1]) <= 4 and parts[1].replace(".", "").isupper():
                authors.append({"creatorType": "author", "firstName": parts[1], "lastName": parts[0]})
            else:
                # Full name or institutional — use single-name format
                authors.append({"creatorType": "author", "name": a})

    # Journal, volume, issue, pages, date — defensive against empty lists/missing keys
    journal = fm.get("journal", "") or ""
    if crossref:
        ct = crossref.get("container-title") or []
        if ct:
            journal = ct[0]
    volume = (crossref or {}).get("volume", "") or ""
    issue = (crossref or {}).get("issue", "") or ""
    pages = (crossref or {}).get("page", "") or ""

    date = ""
    if crossref:
        for dk in ("published-print", "published-online", "issued", "created"):
            entry = crossref.get(dk)
            if entry and "date-parts" in entry and entry["date-parts"]:
                dp = entry["date-parts"][0]
                if dp:
                    date = "-".join(str(x) for x in dp if x is not None)
                    break
    if not date and fm.get("year"):
        date = str(fm["year"])

    abstract = (crossref or {}).get("abstract", "")
    if abstract:
        abstract = re.sub(r"<[^>]+>", "", abstract).strip()

    extra_lines = []
    if pmid:
        extra_lines.append(f"PMID: {pmid}")
    if pubmed and "pmc" in pubmed and pubmed.get("pmc"):
        extra_lines.append(f"PMCID: {pubmed['pmc']}")

    item = {
        "itemType": "journalArticle",
        "title": title,
        "creators": authors,
        "abstractNote": abstract,
        "publicationTitle": journal,
        "volume": str(volume) if volume else "",
        "issue": str(issue) if issue else "",
        "pages": pages,
        "date": date,
        "DOI": doi or "",
        "url": fm.get("url", ""),
        "language": "en",
        "tags": [{"tag": t} for t in (fm.get("tags") or []) if t],
        "extra": "\n".join(extra_lines)
    }
    return item


def ensure_collection(name: str, state: dict) -> str:
    """Create Zotero collection if it doesn't exist; return collection key."""
    if name in state["collections"]:
        return state["collections"][name]

    payload = [{"name": name}]
    status, body, _ = http_request(
        "POST",
        f"{ZOTERO_BASE}/collections",
        json.dumps(payload).encode("utf-8"),
        zot_headers({"Content-Type": "application/json"})
    )
    if status in (200, 201):
        resp = json.loads(body)
        key = resp["successful"]["0"]["key"]
        state["collections"][name] = key
        save_state(state)
        log(f"  [COLLECTION] Created '{name}' -> {key}")
        return key
    else:
        log(f"  [FATAL] Collection create failed: {status} {body[:200].decode('utf-8', errors='replace')}")
        sys.exit(1)


def create_item(item_payload: dict, collection_key: str) -> str | None:
    """POST an item to Zotero; return its key on success."""
    item_payload["collections"] = [collection_key]
    payload = [item_payload]
    status, body, _ = http_request(
        "POST",
        f"{ZOTERO_BASE}/items",
        json.dumps(payload).encode("utf-8"),
        zot_headers({"Content-Type": "application/json"})
    )
    if status in (200, 201):
        resp = json.loads(body)
        if "successful" in resp and "0" in resp["successful"]:
            return resp["successful"]["0"]["key"]
        if "failed" in resp and "0" in resp["failed"]:
            log(f"    [ERROR] Item create failed: {resp['failed']['0']}")
    else:
        log(f"    [ERROR] HTTP {status}: {body[:200].decode('utf-8', errors='replace')}")
    return None


def attach_pdf(item_key: str, pdf_path: Path) -> bool:
    """Attach a PDF to an existing Zotero item. 4-step upload flow."""
    file_size = pdf_path.stat().st_size
    filename = pdf_path.name
    pdf_bytes = pdf_path.read_bytes()
    md5 = hashlib.md5(pdf_bytes).hexdigest()
    mtime = int(pdf_path.stat().st_mtime * 1000)

    # Step 1: Create attachment item
    attach_payload = [{
        "itemType": "attachment",
        "parentItem": item_key,
        "linkMode": "imported_file",
        "title": filename,
        "filename": filename,
        "contentType": "application/pdf",
        "charset": ""
    }]
    status, body, _ = http_request(
        "POST",
        f"{ZOTERO_BASE}/items",
        json.dumps(attach_payload).encode("utf-8"),
        zot_headers({"Content-Type": "application/json"})
    )
    if status not in (200, 201):
        log(f"    [ERROR] Attach item create failed: {status}")
        return False
    resp = json.loads(body)
    if "0" not in resp.get("successful", {}):
        log(f"    [ERROR] Attach item: {resp}")
        return False
    attach_key = resp["successful"]["0"]["key"]

    # Step 2: Get upload authorization
    auth_body = urllib.parse.urlencode({
        "md5": md5, "filename": filename, "filesize": file_size, "mtime": mtime
    }).encode("utf-8")
    status, body, _ = http_request(
        "POST",
        f"{ZOTERO_BASE}/items/{attach_key}/file",
        auth_body,
        zot_headers({"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"})
    )
    if status != 200:
        log(f"    [ERROR] Upload auth failed: {status} {body[:200].decode('utf-8', errors='replace')}")
        return False
    auth = json.loads(body)
    if auth.get("exists"):
        log(f"    [PDF] already exists on server (dedup)")
        return True

    # Step 3: Upload to S3
    upload_url = auth["url"]
    prefix = auth["prefix"].encode("utf-8") if isinstance(auth["prefix"], str) else auth["prefix"]
    suffix = auth["suffix"].encode("utf-8") if isinstance(auth["suffix"], str) else auth["suffix"]
    upload_body = prefix + pdf_bytes + suffix
    status, _, _ = http_request("POST", upload_url, upload_body, {"Content-Type": auth["contentType"]})
    if status not in (200, 201):
        log(f"    [ERROR] S3 upload failed: {status}")
        return False

    # Step 4: Register upload
    reg_body = urllib.parse.urlencode({"upload": auth["uploadKey"]}).encode("utf-8")
    status, body, _ = http_request(
        "POST",
        f"{ZOTERO_BASE}/items/{attach_key}/file",
        reg_body,
        zot_headers({"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"})
    )
    if status in (200, 204):
        return True
    log(f"    [ERROR] Upload register failed: {status}")
    return False


def find_pdf(fm: dict, folder: str, note_stem: str = "") -> Path | None:
    """Find PDF file in _attachments/ based on frontmatter pdf wikilink, with fuzzy fallback."""
    pdf_link = fm.get("pdf", "")
    if pdf_link:
        # Strip wikilink [[...]]
        m = re.match(r"\[\[([^\]]+?\.pdf)\]\]", pdf_link)
        if m:
            filename = m.group(1)
        else:
            filename = pdf_link.strip()

        if filename.endswith(".pdf"):
            candidate = ATTACHMENTS / folder / filename
            if candidate.exists():
                return candidate
            # Search globally in the attachments root
            for hit in ATTACHMENTS.rglob(filename):
                if hit.is_file():
                    return hit

    # Fuzzy fallback: match by first-author + year from note stem (e.g. "Maciocia 2017 ...")
    m = re.match(r"^([A-Z][\wáéíóúñç\-]+)\s+(\d{4})", note_stem)
    if not m:
        return None
    author, year = m.group(1), m.group(2)
    candidates = []
    root = ATTACHMENTS / folder
    if root.is_dir():
        for p in root.glob("*.pdf"):
            name = p.name.lower()
            if author.lower() in name and year in name:
                candidates.append(p)
    if len(candidates) == 1:
        return candidates[0]
    # Multiple matches — take first, log ambiguity
    if candidates:
        log(f"    [WARN] fuzzy PDF match ambiguous for {note_stem[:40]}: picked {candidates[0].name}")
        return candidates[0]
    return None


def migrate_folder(folder_name: str, dry_run: bool, state: dict) -> dict:
    folder = LIBRARY / folder_name
    if not folder.is_dir():
        log(f"[FATAL] Folder not found: {folder}")
        sys.exit(1)

    papers = sorted(p for p in folder.iterdir()
                    if p.suffix == ".md" and not p.name.startswith("_")
                    and p.name not in ("index.md", "README.md"))

    log(f"[FOLDER] {folder_name} — {len(papers)} paper notes")

    summary = {"total": len(papers), "skipped": 0, "success": 0, "pdf_attached": 0, "failed": 0, "no_pdf": 0}

    if dry_run:
        with_doi = with_pmid = with_pdf = 0
        for md in papers:
            fm = read_frontmatter(md) or {}
            if clean_doi(fm.get("doi")):
                with_doi += 1
            if clean_pmid(fm.get("pmid")):
                with_pmid += 1
            pdf = find_pdf(fm, folder_name, md.stem)
            if pdf:
                with_pdf += 1
        log(f"  [DRY] {with_doi}/{len(papers)} have DOI, {with_pmid}/{len(papers)} have PMID, {with_pdf}/{len(papers)} have PDF")
        return summary

    collection_key = ensure_collection(folder_name, state)

    for i, md in enumerate(papers, 1):
        outcome = process_paper(md, folder_name, collection_key, state, prefix=f"  [{i}/{len(papers)}] ")
        for k, v in outcome.items():
            summary[k] = summary.get(k, 0) + v

    return summary


def process_paper(md: Path, folder_name: str, collection_key: str, state: dict, prefix: str = "  ") -> dict:
    """Process one paper note. Returns delta counts for summary."""
    out = {"skipped": 0, "success": 0, "pdf_attached": 0, "failed": 0, "no_pdf": 0}

    note_key = str(md.relative_to(LIBRARY)) if LIBRARY in md.parents else str(md)
    if note_key in state["papers"]:
        log(f"{prefix}[SKIP] Already migrated: {md.stem}")
        out["skipped"] = 1
        return out

    fm = read_frontmatter(md)
    if not fm:
        log(f"{prefix}[SKIP] No frontmatter: {md.stem}")
        out["skipped"] = 1
        return out

    doi = clean_doi(fm.get("doi"))
    pmid = clean_pmid(fm.get("pmid"))

    if not doi and not pmid:
        log(f"{prefix}[SKIP] No DOI or PMID: {md.stem}")
        out["skipped"] = 1
        return out

    # DOI/PMID dedup across renames/moves (e.g. _inbox → library/<cluster>)
    existing_item_key = None
    for paper_key, paper_info in state["papers"].items():
        if doi and paper_info.get("doi") == doi:
            existing_item_key = paper_info.get("item_key")
            break
        if pmid and paper_info.get("pmid") == pmid:
            existing_item_key = paper_info.get("item_key")
            break
    if existing_item_key:
        log(f"{prefix}[DEDUP] Already in Zotero by DOI/PMID: {md.stem}")
        state["papers"][note_key] = {
            "item_key": existing_item_key,
            "collection": folder_name,
            "doi": doi,
            "pmid": pmid,
            "pdf_attached": True,
            "migrated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "dedup_ref": True,
        }
        save_state(state)
        out["skipped"] = 1
        return out

    log(f"{prefix}{md.stem[:70]}")
    crossref = fetch_crossref(doi) if doi else None
    pubmed = fetch_pubmed(pmid) if pmid else None
    time.sleep(0.3)

    item = build_zotero_item(fm, crossref, pubmed)
    item_key = create_item(item, collection_key)
    if not item_key:
        out["failed"] = 1
        return out

    pdf_path = find_pdf(fm, folder_name, md.stem)
    pdf_attached = False
    if pdf_path:
        log(f"    [PDF] attaching {pdf_path.name} ({pdf_path.stat().st_size/1024:.0f} KB)")
        pdf_attached = attach_pdf(item_key, pdf_path)
        if pdf_attached:
            out["pdf_attached"] = 1
    else:
        out["no_pdf"] = 1

    state["papers"][note_key] = {
        "item_key": item_key,
        "collection": folder_name,
        "doi": doi,
        "pmid": pmid,
        "pdf_attached": pdf_attached,
        "migrated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_state(state)
    out["success"] = 1
    time.sleep(0.3)
    return out


def resolve_collection(md: Path) -> str:
    """Derive collection name from a note's path."""
    try:
        rel = md.relative_to(LIBRARY)
        parts = rel.parts
        if len(parts) >= 2:
            return parts[0]
    except ValueError:
        pass
    if "_inbox" in md.parts:
        return "_Inbox"
    return "_Uncategorized"


def migrate_file(file_path: Path, state: dict) -> dict:
    """Sync a single paper note to Zotero."""
    if not file_path.exists() or file_path.suffix != ".md":
        log(f"[SKIP] Not a markdown file: {file_path}")
        return {"skipped": 1}
    collection_name = resolve_collection(file_path)
    collection_key = ensure_collection(collection_name, state)
    return process_paper(file_path, collection_name, collection_key, state, prefix=f"[FILE] ")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", help="library subfolder to migrate")
    ap.add_argument("--all", action="store_true", help="migrate all folders")
    ap.add_argument("--file", help="single note path to sync (for auto-sync hook)")
    ap.add_argument("--dry-run", action="store_true", help="preview without writes")
    args = ap.parse_args()

    if not ZOTERO_API_KEY or not ZOTERO_USER_ID:
        print("[FATAL] ZOTERO_API_KEY and ZOTERO_USER_ID must be set", file=sys.stderr)
        sys.exit(1)

    if not args.folder and not args.all and not args.file:
        print("[FATAL] specify --folder NAME, --all, or --file PATH", file=sys.stderr)
        sys.exit(1)

    state = load_state()

    if args.file:
        md = Path(args.file).resolve()
        outcome = migrate_file(md, state)
        log(f"[FILE SUMMARY] {md.name}: {outcome}")
        return

    if args.all:
        folders = sorted(d.name for d in LIBRARY.iterdir() if d.is_dir() and not d.name.startswith("_"))
    else:
        folders = [args.folder]

    grand = {"total": 0, "skipped": 0, "success": 0, "pdf_attached": 0, "failed": 0, "no_pdf": 0}
    for f in folders:
        summary = migrate_folder(f, args.dry_run, state)
        for k, v in summary.items():
            grand[k] = grand.get(k, 0) + v
        log(f"  [FOLDER SUMMARY] {f}: {summary}")

    log(f"[GRAND TOTAL] {grand}")


if __name__ == "__main__":
    main()
