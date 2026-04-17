# zotero-library-migration

Scripts for migrating a folder-based markdown paper library (Obsidian-style vault with YAML frontmatter) into [Zotero](https://www.zotero.org/), then backfilling PDF attachments from vault files, Unpaywall open-access sources, PubMed Central, and (optionally) an EZproxy-authenticated institutional library session via Playwright.

Migrated 311 papers with 84% PDF coverage in a single overnight run against a real URMC vault.

## What's here

| Script | Purpose |
|---|---|
| `migrate_to_zotero.py` | Primary migration. Reads markdown notes from `VAULT_PATH/library/`, creates one Zotero collection per sub-folder, uploads metadata + any locally cached PDF. Idempotent via `state.json`. |
| `backfill_pdfs.py` | Fuzzy-matches loose PDFs in `VAULT_PATH/_attachments/` to Zotero items by author-anchor + year + Jaccard title overlap, attaches matches. |
| `unpaywall_fetch.py` | For Zotero items still missing PDFs, queries Unpaywall by DOI and pulls the best OA copy. |
| `fetch_all_pdfs.py` | Broader fetch: PMC Open-Access tarballs, all Unpaywall locations, browser-UA retries. |
| `fetch_via_ezproxy.py` | Playwright-driven EZproxy fetch for paywalled content. Opens a real browser, you log in once (SSO/Duo), session persists in `~/.config/zotero-migration/browser-state/`. |
| `stamp_zotero_keys.py` | Writes the assigned Zotero item key back into each markdown note's frontmatter for round-trip linking. |

## Prerequisites

- Python 3.10+
- A Zotero account + API key (https://www.zotero.org/settings/keys — needs read/write on your personal library)
- For `fetch_via_ezproxy.py` only: Playwright + an institutional EZproxy that uses hostname-rewriting (most do)

## Setup

```bash
git clone https://github.com/sypossc-netizen/zotero-library-migration.git
cd zotero-library-migration

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Only if you plan to use the EZproxy script:
python3 -m playwright install chromium
```

## Configuration

All config is environment variables — no file to edit.

| Variable | Required by | Description |
|---|---|---|
| `VAULT_PATH` | all | Absolute path to your vault root. Scripts look for `library/` and `_attachments/` beneath it. |
| `ZOTERO_API_KEY` | all | From https://www.zotero.org/settings/keys |
| `ZOTERO_USER_ID` | all | Your numeric user ID (shown on the same settings page) |
| `UNPAYWALL_EMAIL` | unpaywall, fetch-all, migrate | Required by the Unpaywall API — use a real email |
| `EZPROXY_HOST` | ezproxy only | Your institution's EZproxy hostname. Default: `login.ezpminer.urmc.rochester.edu` |
| `EZPROXY_LOGIN_TEST_URL` | ezproxy only | A resource URL that forces your EZproxy login flow |

Put these in a shell `.env` you source before running, or export them inline:

```bash
export VAULT_PATH="$HOME/Documents/MyVault"
export ZOTERO_API_KEY="..."
export ZOTERO_USER_ID="..."
export UNPAYWALL_EMAIL="you@school.edu"
```

## Vault assumptions

Markdown notes under `VAULT_PATH/library/<Collection Name>/*.md` with YAML frontmatter containing at least a title and (ideally) a DOI or PMID:

```yaml
---
title: "Your paper title"
doi: "10.1234/example"
pmid: "12345678"
year: 2024
journal: "Journal Name"
authors: ["Last, First", "Other, Author"]
pdf: "../_attachments/paper.pdf"   # optional; resolved relative to the note
---
```

Loose PDFs in `VAULT_PATH/_attachments/` are picked up by `backfill_pdfs.py` even without a frontmatter `pdf:` link.

## Usage

Run in this order (each step is idempotent — safe to re-run, state lives in `state.json`):

```bash
# 1. Dry run one folder first to validate frontmatter parsing
python3 migrate_to_zotero.py --folder "Collection Name" --dry-run

# 2. Real migration
python3 migrate_to_zotero.py --all

# 3. Match loose PDFs to Zotero items
python3 backfill_pdfs.py

# 4. Grab open-access PDFs from Unpaywall
python3 unpaywall_fetch.py

# 5. (Optional) wider net: PMC + all Unpaywall locations
python3 fetch_all_pdfs.py

# 6. (Optional) paywalled content via institutional proxy
python3 fetch_via_ezproxy.py
#    → opens a browser window, complete SSO once, leave it logged in.
#      Session persists in ~/.config/zotero-migration/browser-state/

# 7. Stamp Zotero item keys back onto your markdown notes
python3 stamp_zotero_keys.py
```

## Notes on EZproxy

`fetch_via_ezproxy.py` assumes the standard EZproxy hostname-rewriting convention: publisher `www.example.com` is reached at `www-example-com.<proxy-domain>`. The `<proxy-domain>` is derived from `EZPROXY_HOST` by stripping a leading `login.` — if your institution uses a different pattern, tweak `proxify()`.

Known gotcha at the University of Rochester: the River Campus proxy (`ezp.lib.rochester.edu`) does **not** cover biomedical publishers (Blood, Nature, Wiley, Elsevier, Oxford, etc.). Use the Miner Library proxy (`login.ezpminer.urmc.rochester.edu`) for medical content.

## State files

- `state.json` — checkpoint of what's been migrated (Zotero item keys, attached PDFs, sources). Keep it. Deleting it and re-running will create 311 duplicate items in your Zotero library.
- `*.log` — per-script logs, ignored by git.
- `missing-pdfs-report.csv` — written by `backfill_pdfs.py` / `unpaywall_fetch.py` listing what's still unattached.

## License

MIT
