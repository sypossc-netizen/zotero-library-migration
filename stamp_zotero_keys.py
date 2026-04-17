#!/usr/bin/env python3
"""
stamp_zotero_keys.py

Adds `zotero_key` and `zotero_url` fields to each migrated paper's frontmatter
based on state.json. Non-destructive: preserves all existing frontmatter and
body, only appends two new lines if zotero_key is not already present.

Idempotent: re-runs skip notes that already have a zotero_key.

Usage:
    python3 stamp_zotero_keys.py --dry-run    # preview
    python3 stamp_zotero_keys.py              # apply
    python3 stamp_zotero_keys.py --force      # overwrite existing zotero_key
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"
LIBRARY = Path(os.environ.get("VAULT_PATH", "")).expanduser() / "library"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing zotero_key/zotero_url fields")
    args = ap.parse_args()

    state = json.loads(STATE_PATH.read_text())
    papers = state["papers"]
    print(f"[INFO] {len(papers)} papers in state.json")

    stats = {"stamped": 0, "skip_already": 0, "skip_missing": 0, "skip_no_fm": 0,
             "updated": 0}

    for note_key, info in papers.items():
        md = LIBRARY / note_key
        if not md.exists():
            stats["skip_missing"] += 1
            continue

        txt = md.read_text(encoding="utf-8")
        m = re.match(r"^(---\n)(.*?)(\n---)(.*)$", txt, re.DOTALL)
        if not m:
            stats["skip_no_fm"] += 1
            continue

        open_delim, fm, close_delim, body = m.groups()

        item_key = info["item_key"]
        zkey_line = f"zotero_key: {item_key}"
        zurl_line = f"zotero_url: zotero://select/library/items/{item_key}"

        has_key = bool(re.search(r"^zotero_key:", fm, re.M))

        if has_key and not args.force:
            stats["skip_already"] += 1
            continue

        if has_key and args.force:
            new_fm = re.sub(r"^zotero_key:.*$", zkey_line, fm, flags=re.M)
            new_fm = re.sub(r"^zotero_url:.*$", zurl_line, new_fm, flags=re.M)
            if not re.search(r"^zotero_url:", new_fm, re.M):
                # ensure url present
                new_fm = new_fm + "\n" + zurl_line
            stats["updated"] += 1
        else:
            # Append to end of frontmatter (before closing ---)
            new_fm = fm.rstrip() + "\n" + zkey_line + "\n" + zurl_line
            stats["stamped"] += 1

        new_txt = open_delim + new_fm + close_delim + body

        if not args.dry_run:
            md.write_text(new_txt, encoding="utf-8")

    print()
    print(f"[RESULT] {'DRY RUN' if args.dry_run else 'APPLIED'}")
    print(f"  newly stamped: {stats['stamped']}")
    print(f"  updated:       {stats['updated']}")
    print(f"  skipped (already had key, use --force to overwrite): {stats['skip_already']}")
    print(f"  skipped (note file missing):   {stats['skip_missing']}")
    print(f"  skipped (no frontmatter block): {stats['skip_no_fm']}")


if __name__ == "__main__":
    main()
