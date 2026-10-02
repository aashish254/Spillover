<br><br>
<p align="center">
  <img src="assets/logo.svg" width="88" alt="Spillover logo — ink-black tile, a vessel filled with teal liquid and one drop over the rim">
</p>
<br>

<h1 align="center">Spillover</h1>

**Rebalance your Google storage across your own accounts — locally, open source.**

Got one Google account bursting at 40 GB on a 15 GB free plan while your alt accounts sit empty? Spillover connects all your accounts, shows you exactly what's eating the space, and moves files from full accounts to empty ones — with hash verification, a trash-not-delete safety net, and a permanent local record of what went where.

Everything runs on **your PC**. No servers, no uploads to third parties, no account credentials ever leaving your machine.

---

## Demo video — coming soon

This folder has a reserved space (`demo.mp4`) where we'll put the walkthrough video. Until then, launch the UI and hit the guide above — it mirrors the flow line-by-line.

**Quick run:**
- **UI:** `spillover ui` opens http://127.0.0.1:8741  
- **Demo mode (no sign-in):** `spillover ui --demo`  
- **Custom port:** `spillover ui --port 9000 --no-browser`  

Want a screen capture now? The CLI mirror is identical to what you'd see in the web UI:

```bash
# Connect accounts (opens browser windows for OAuth)
spillover auth add --alias main
spillover auth add --alias backup-1

# See quotas at a glance
spillover quota

# Run a rebalance (scan → plan → execute)
spillover scan --alias main
spillover plan --from main
spillover run --plan ~/.spillover/plans/plan-main-*.json --execute
```

---

## What it can and can't do (read this first)

- **Google Drive: fully supported.** Scan, plan, move, verify — all automatic.
- **Google Photos: two supported routes.** Since March 2025, Google blocks all third-party apps from reading your full photo library and provides **no API to delete photos**. So for Photos:
  - **Picker (recommended):** `spillover photos create --alias main` prints a Google picker link — select what to move in Google's own UI, and Spillover copies your picks (hash-verified) into your other accounts' Drive storage. Also in the web UI under Takeout → Photos picker. Spillover only ever sees the items you pick.
  - **Takeout:** export via [Google Takeout](https://takeout.google.com), then `spillover takeout --dir ~/takeout --execute` distributes everything.
  - Either way, **you bulk-delete the originals in the Photos UI yourself** (Google's rule, not ours — no tool on earth can do this via API in 2026). Spillover writes a deletion checklist to `~/.spillover/photo_manifests/`.
- **Gmail: never touched.** Spillover doesn't even request Gmail scopes.

---

## Quick start — copy/paste into terminal

One-liner install + one-time setup:

```bash
# 1. Clone and install
git clone https://github.com/aashish254/Spillover.git spillover && cd spillover
python3 -m venv .venv
source .venv/bin/activate       # macOS/Linux; Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .                # gives you the `spillover` command
```

### One-time setup: create your own Google Cloud project (~2 minutes)

Spillover uses *your own* Google Cloud project (this is how tools like rclone do it):

1. Create a Google Cloud project: [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate)
2. Enable APIs: [Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com), [Photos Picker API](https://console.cloud.google.com/apis/library/photospicker.googleapis.com)
3. Create Credentials → OAuth client ID (Desktop app): [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials)
4. Download JSON → save as `~/.spillover/client_secret.json`:
   ```bash
   mkdir -p ~/.spillover && cp ~/Downloads/client_secret*.json ~/.spillover/client_secret.json
   ```

> In testing mode Google expires the login every ~7 days — just re-run `spillover auth reconnect --alias <name>` (or hit Reconnect in the web UI). (Publishing the project to "Production" makes tokens long-lived but keeps an "unverified app" warning screen.)

---

## Use it — point and click or command line

### Web UI (recommended)

```bash
spillover ui                  # opens http://127.0.0.1:8741 in your browser
spillover ui --demo           # try it with fake data, no Google sign-in needed
spillover ui --port 9000      # pick your own port
spillover ui --no-browser     # don't auto-open the browser
```

The UI walks you through: **Accounts** (quotas at a glance) →
**Rebalance** (pick a full account → scan → choose files → dry-run plan →
confirm → live progress) → **Takeout** (point at an unzipped Google Takeout
folder, distribute across accounts) → **Duplicates** (find duplicates across accounts, keep-one-copy, trash-the-rest) → **Ledger** (every move, searchable).

Everything still runs locally; the browser page never sends anything anywhere.

### Terminal

```bash
# 1. Connect accounts (a browser window opens for each — sign in normally)
spillover auth add --alias main
spillover auth add --alias alt1
spillover auth add --alias alt2

# 2. See all quotas at a glance
spillover quota

# 3. Inventory the full account
spillover scan --alias main

# 4. Propose moves (files ≥50MB; --types mp4,jpg to narrow it)
spillover plan --from main --min-size 50MB

# 5. Preview, then actually move (trash-not-delete, hash-verified)
spillover run --plan ~/.spillover/plans/plan-main-XXXX.json
spillover run --plan ~/.spillover/plans/plan-main-XXXX.json --execute

# Photos via Takeout export
spillover takeout --dir ~/takeout --execute

# Photos via the picker (no export download needed)
spillover photos create --alias main          # prints a Google picker link
# open the link, select photos/videos, then:
spillover photos list --alias main --session-id <id>      # review your picks
spillover photos transfer --alias main --session-id <id> --to backup-1,backup-2
# Google's Photos API has no delete: a deletion checklist is written to
# ~/.spillover/photo_manifests/ — remove the originals by hand afterwards.

# Resume transfers interrupted by a crash or kill (never re-uploads a
# destination copy that was already uploaded and verified)
spillover resume
spillover resume --from main

# Reconnect an account whose token expired or was revoked
spillover auth reconnect --alias backup-1

# Where did my file go?
spillover find vacation.mp4
spillover history
```

---

## Safety design

- **Dry-run by default.** `run` only previews until you pass `--execute`,
  and even then asks for confirmation.
- **Verify-before-trash.** A source file is trashed only after the
  destination copy's SHA-256 matches the downloaded bytes. Mismatch → source
  untouched, move logged as failed.
- **Trash, never delete.** Originals sit in the source account's trash for
  30 days — full undo window.
- **Never fills a destination.** 1 GB safety buffer per account, always.
- **Shared files are skipped by default.** Files shared with other people,
  or owned by someone else, are flagged at plan time and skipped at run
  time — moving them would break share links or strand collaborators. Pass
  `--include-shared` (CLI) or tick the checkbox (web UI) to move them
  anyway, explicitly.
- **Crash-safe transfers.** Every file is tracked as a transaction
  (`pending → in_progress → done/failed` with the last completed stage).
  If a run dies mid-way, `spillover resume` (or the Resume button in the
  web UI) continues from the first unfinished stage — a destination copy
  that was already uploaded and verified is never re-uploaded.
- **Ledger.** Every move (done, failed, or skipped) is recorded in
  `~/.spillover/ledger.db` — filename, size, hash, source → destination.
- **Narrow scopes.** Drive scope plus the Photos *picker* scope only. The
  picker exposes solely the items you select — never your whole library.
  No Gmail, no contacts, nothing else.
- **Local only.** Tokens live in your OS keyring; the ledger and scans live
  in `~/.spillover`. Nothing is sent anywhere except to Google's APIs.

---

## Limits to know

- Google caps uploads at **750 GB/day per account** — big moves are
  automatically spread by the retry/backoff engine; just re-run `run`.
- Moving a file changes its owner; **existing share links on moved files
  break**. Shared files are skipped by default (see above) — use
  `--include-shared` only when you mean it.
- Google's **Photos API cannot delete** anything, so picker and Takeout
  transfers always leave the originals behind. The transfer writes a
  manual-deletion checklist (`~/.spillover/photo_manifests/`) — work through
  it in Google Photos yourself after confirming the copies.
- Pooling free accounts to avoid paid storage sits in a gray area of
  Google's Terms of Service. This tool is for managing *your own* data
  across *your own* accounts; use your judgment.

---

## Project layout

```
spillover/
  auth.py       OAuth per account, tokens in OS keyring, reconnect flow
  drive.py      Retry-hardened Drive API wrapper
  inventory.py  Full Drive scan + "what's eating space" summary
  planner.py    Bin-packing: which files go to which account
  transfer.py   Download → hash → upload → verify → trash → ledger,
                with transaction tracking + resume
  sharing.py    Shared/not-owned file detection and warnings
  photos.py     Google Photos Picker API flow + deletion manifests
  takeout.py    Google Photos Takeout ingest (zip parts + JSON sidecars)
  ledger.py     SQLite record of every move + transfer transactions
  cli.py        `spillover` command
```

---

## License

MIT — see [LICENSE](LICENSE).
