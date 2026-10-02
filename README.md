<img src="assets/logo.png" width="88" alt="Spillover logo — ink-black tile, a vessel filled with teal liquid and one drop over the rim">

# Spillover

**Rebalance your Google storage across your own accounts — locally, open source.**

Got one Google account bursting at 40 GB on a 15 GB free plan while your alt accounts sit empty? Spillover connects all your accounts, shows you exactly what's eating the space, and moves files from full accounts to empty ones — with hash verification, a trash-not-delete safety net, and a permanent local record of what went where.

Everything runs on **your PC**. No servers, no uploads to third parties, no account credentials ever leaving your machine.

---

## Demo video — coming soon

This folder has a reserved space (`demo.mp4`) where we'll put the walkthrough video. Until then, launch the UI and hit the guide above — it mirrors the flow line-by-line.

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
git clone <your-repo-url> spillover && cd spillover
python3 -m venv .venv
source .venv/bin/activate       # macOS/Linux; Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .                # gives you the `spillover` command
```

### One-time setup: create your own Google Cloud project (~2 minutes)

Spillover uses *your own* Google Cloud project (this is how tools like rclone do it):

```bash
mkdir -p ~/.spillover && \
curl -o /dev/null https://console.cloud.google.com/apis/library/drive.googleapis.com \
      https://console.cloud.google.com/apis/library/photospicker.googleapis.com \
      https://console.cloud.google.com/apis/credentials/consent \
      https://console.cloud.google.com/apis/credentials
# Download client_secret.json from the Console, rename if needed, then:
cp ~/Downloads/client_secret*.json ~/.spillover/client_secret.json
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

## Host the docs on GitHub Pages (same design)

We ship a vanilla static bundle (`spillover/web/static`) that runs offline. To host it:

### Option A: GitHub Pages (automatic)

```bash
# From the repo root
git commit -m "chore: initial release" && git push
cd spillover/web/static
# Create gh-pages branch from here (static assets only)
git checkout -b gh-pages
git add .
git commit -m "docs: deploy static site" && git push -u origin gh-pages

# In your repository Settings → Pages → Source, set "gh-pages branch".
# Your docs will be available at https://<user>.github.io/<repo>/
```

To match the look-and-feel of the web UI:

- Copy the CSS variables from [`styles.css`](spillover/web/static/styles.css):
  ```css
  :root {
    --canvas: #f2f1ec;              /* page background */
    --surface: #f8f7f3;             /* card background */
    --field: #fbfaf7;               /* input backgrounds */
    --sidebar: #edece5;             /* left rail */
    --ink: #191b1a;                 /* primary text */
    --ink-2: #3c413e;               /* secondary text */
    --muted: #5d625e;               /* muted labels */
    --faint: #8a8f88;               /* meta */
    --hairline: #ddd9cb;            /* borders */
    --hairline-2: #c9c4b4;          /* lighter borders */
    --teal: #0f766b;                /* primary action */
    --teal-deep: #0a5b50;           /* hover */
    --teal-ink: #0e6f63;            /* link ink */
    --amber-ink: #8a5a00;           /* warning */
    --red-ink: #b3261e;             /* destructive */
  }
  ```
- For a simple docs landing page, use the `--serif` heading stack (New York / Georgia / Times), `--mono` for code and buttons, and keep it flat with no shadows. Example:
  ```html
  <!DOCTYPE html>
  <html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Spillover</title>
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>
      :root {
        --bg:#f2f1ec; --text:#191b1a; --teal:#0f766b; --link:#0e6f63; --code:#3c413e;
        --font-serif: "New York",Georgia,"Times New Roman",serif;
        --font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
        --font-mono:ui-monospace,SFMono-Regular,Menlo,Monaco,"Liberation Mono",monospace;
      }
      body{background:var(--bg);color:var(--text);font-family:var(--font-sans);line-height:1.6;margin:0;padding:40px;}
      h1{font-family:var(--font-serif);font-size:2rem;margin:.3em 0}
      p,li{font-size:15px;color:var(--text)}
      code,p code,pre{font-family:var(--font-mono);background:var(--bg);padding:2px 6px;border-radius:6px;font-size:14px;}
      pre{background:#f8f7f3;padding:12px;border-radius:8px;overflow:auto;}
      a{color:var(--link);text-decoration:none;border-bottom:1px solid var(--teal);}
      a:hover{border-bottom-color:var(--teal);color:var(--teal-deep);}
      .btn{display:inline-block;background:var(--teal);color:#fff;padding:8px 14px;border-radius:8px;text-decoration:none;font-family:var(--font-mono);font-size:13px;}
      .btn:hover{background:var(--teal-deep);}
      .note{background:#faf0d8;color:var(--amber-ink);padding:10px 12px;border-radius:8px;font-family:var(--font-mono);font-size:13px;border:1px solid #d9b36a;}
    </style>
  </head>
  <body>
    <h1>Spillover</h1>
    <p>Rebalance your Google storage across your own accounts — locally, open source.</p>
    <p><strong>Everything runs on your PC.</strong> No servers, no uploads to third parties, no account credentials ever leaving your machine.</p>

    <h2>Install</h2>
    <pre><code>git clone &lt;your-repo&gt; spillover &amp;&amp; cd spillover
python3 -m venv .venv &amp;&amp; source .venv/bin/activate
pip install -r requirements.txt
pip install -e .</code></pre>

    <h2>One-time setup</h2>
    <div class="note">Create your own Google Cloud project and save client_secret.json to ~/.spillover — see the full guide in <a href="#setup">Setup</a>.</div>

    <h2>Run</h2>
    <pre><code>spillover ui                  # opens 127.0.0.1:8741
spillover ui --demo           # demo mode without signing in</code></pre>

    <h2 id="setup">Setup</h2>
    <ol>
      <li>Create a Google Cloud project: <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noopener">console.cloud.google.com/projectcreate</a></li>
      <li>Enable APIs: Drive API, Photos Picker API</li>
      <li>Create Credentials → OAuth client ID (Desktop app)</li>
      <li>Download JSON → save as <code>~/.spillover/client_secret.json</code></li>
    </ol>

    <h2>Safety</h2>
    <ul>
      <li>Dry-run by default; verify-before-trash</li>
      <li>Trash, never delete (30-day undo)</li>
      <li>Never fills a destination: 1 GB safety buffer</li>
      <li>Cross-account dedupe, shared-file protection, crash-safe resume</li>
    </ul>

    <h2 style="margin-top:2.5em;">Demo video — coming soon</h2>
    <p>Reserved location: <code>demo.mp4</code>. We'll link it once it lands.</p>
  </body>
  </html>
  ```
Commit and push again, let GitHub serve it, and hard-refresh to clear any cache.

### Option B: Qoder Sites (managed hosting)

If you want zero-config deployment with analytics and version management, we can add the project to Qoder Sites:

```bash
# After initializing the descriptor (.qoder.site.yaml or similar)
qoder sites deploy <project-name> .
```

The deployed docs will reuse the same CSS tokens and type treatment as the web UI.

---

## License

MIT — see [LICENSE](LICENSE).
