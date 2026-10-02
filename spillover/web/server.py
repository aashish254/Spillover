"""Spillover web UI: local FastAPI server + JSON API over the existing engine."""

import json
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import _version, auth, config, inventory, ledger, planner, photos, sharing, takeout, transfer
from ..drive import DriveClient

STATIC_DIR = Path(__file__).parent / "static"
_jobs: dict[str, dict] = {}
_pool = ThreadPoolExecutor(max_workers=4)


def _group_duplicates(scans: dict[str, list[dict]], limit: int = 200) -> dict:
    """Group cached files by (name, size) across accounts.

    Matching on name+size alone keeps this instant: it reads the cached scans
    and never calls Google. Files under 1 KiB and zero-size Docs/Sheets are
    excluded, and a group only counts when it spans two or more accounts.
    """
    from collections import defaultdict

    groups: dict[tuple, list] = defaultdict(list)
    for alias, files in scans.items():
        for f in files:
            if f["size"] > 1024:
                groups[(f["name"], f["size"])].append({
                    "alias": alias,
                    "file_id": f["id"],
                    "name": f["name"],
                    "size": f["size"],
                    "mimeType": f.get("mimeType", ""),
                    "modifiedTime": f.get("modifiedTime", ""),
                    "sha256": f.get("sha256", ""),
                    "shared": bool(f.get("shared")),
                    "owned_by_me": f.get("owned_by_me", True),
                })
    dupes = []
    for (name, size), copies in groups.items():
        if len(copies) < 2 or len({c["alias"] for c in copies}) < 2:
            continue
        # Two files with the same name and size but different recorded hashes
        # are not duplicates; drop them rather than invite a bad delete.
        hashes = {c["sha256"] for c in copies if c["sha256"]}
        if len(hashes) > 1:
            continue
        dupes.append({
            "name": name, "size": size, "copies": copies,
            "hash_verified": bool(hashes),
            "wasted_bytes": size * (len(copies) - 1),
        })
    dupes.sort(key=lambda d: d["wasted_bytes"], reverse=True)
    return {
        "duplicates": dupes[:limit],
        "total_groups": len(dupes),
        "total_wasted_bytes": sum(d["wasted_bytes"] for d in dupes),
        "scanned_aliases": sorted(scans),
    }


# ---------------------------------------------------------------- providers

class RealProvider:
    name = "real"

    def list_accounts(self) -> list[dict]:
        out = []
        for alias, info in auth.load_accounts().items():
            try:
                q = DriveClient(alias).quota()
            except Exception as exc:  # noqa: BLE001
                q = {"error": str(exc)[:160]}
            out.append({"alias": alias, "email": info.get("email", alias),
                        "quota": q})
        return out

    def connect(self, alias: str) -> dict:
        email = auth.add_account(alias)
        return {"alias": alias, "email": email}

    def disconnect(self, alias: str) -> None:
        auth.remove_account(alias)

    def scan_summary(self, alias: str) -> dict:
        return inventory.scan(alias)

    def get_files(self, alias: str) -> list[dict]:
        return inventory.load_scan(alias)["files"]

    def scans(self) -> dict[str, list[dict]]:
        """Cached scan for every connected account, skipping unscanned ones."""
        out: dict[str, list[dict]] = {}
        for alias in auth.load_accounts():
            try:
                out[alias] = inventory.load_scan(alias)["files"]
            except FileNotFoundError:
                continue
        return out

    def trash_file(self, alias: str, file_id: str) -> None:
        DriveClient(alias).trash(file_id)

    def duplicates(self) -> dict:
        return _group_duplicates(self.scans())

    def run_plan(self, plan: dict, progress_cb=None) -> dict:
        assignments = [planner.Assignment(a["file"], a["dest_alias"])
                       for a in plan["assignments"]]
        return transfer.execute(assignments, plan["source"],
                                dry_run=False, progress_cb=progress_cb,
                                include_shared=bool(plan.get("include_shared")))

    def resume(self, progress_cb=None) -> dict:
        return transfer.resume(progress_cb=progress_cb)

    def interrupted_transfers(self) -> list[dict]:
        return ledger.find_interrupted()

    def auth_status(self) -> dict:
        out = {}
        for alias, info in auth.load_accounts().items():
            status = auth.check_account(alias)
            status["email"] = info.get("email", alias)
            out[alias] = status
        return out

    def reconnect(self, alias: str) -> dict:
        email = auth.reconnect_account(alias)
        return {"alias": alias, "email": email}

    def shared_files(self, alias: str) -> list[dict]:
        return sharing.warnings(inventory.load_scan(alias)["files"])

    def ledger(self, query: str = "", limit: int = 50) -> list[dict]:
        return ledger.search(query, limit) if query else ledger.recent(limit)

    # -- photos picker ------------------------------------------------
    def photos_create_session(self, alias: str) -> dict:
        return photos.create_picker_session(alias)

    def photos_list_picked(self, alias: str, session_id: str) -> list[dict]:
        return photos.list_picked(alias, session_id)

    def photos_transfer(self, alias: str, session_id: str,
                        dest_aliases: list[str], progress_cb=None,
                        subfolder: str = "Photos Picker") -> dict:
        return photos.transfer_picked(alias, session_id, dest_aliases,
                                      subfolder=subfolder,
                                      progress_cb=progress_cb)


def make_provider(demo: bool):
    if demo:
        from .demo import DemoProvider
        return DemoProvider()
    return RealProvider()


def _stream_progress(run_fn):
    """SSE wrapper for a long job: run_fn(progress_cb) executes in a thread.

    progress_cb(done, total, event) forwards live events; the final stats or
    error are emitted as a terminal event.
    """
    events: queue.Queue = queue.Queue()
    result_box: dict = {}

    def progress_cb(done, total, event):
        events.put({"done": done, "total": total, **event})

    def _runner():
        try:
            result_box["stats"] = run_fn(progress_cb)
        except Exception as exc:  # noqa: BLE001
            result_box["error"] = str(exc)[:300]
        events.put(None)  # sentinel

    def gen():
        threading.Thread(target=_runner, daemon=True).start()
        yield 'data: {"type":"start"}\n\n'
        while True:
            e = events.get()
            if e is None:
                break
            yield f"data: {json.dumps({'type':'progress', **e})}\n\n"
        if "error" in result_box:
            yield f"data: {json.dumps({'type':'error','error':result_box['error']})}\n\n"
        else:
            yield f"data: {json.dumps({'type':'done','stats':result_box['stats']})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------- app

def create_app(demo: bool = False) -> FastAPI:
    provider = make_provider(demo)
    app = FastAPI(title="Spillover", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/mode")
    def mode():
        return {"demo": demo}

    @app.get("/api/version")
    def version():
        return {"version": _version.VERSION}

    @app.get("/api/setup")
    def setup_status():
        from ..config import CLIENT_SECRET_FILE
        return {
            "client_secret_exists": CLIENT_SECRET_FILE.exists(),
            "client_secret_path": str(CLIENT_SECRET_FILE),
        }

    # -- accounts ----------------------------------------------------
    @app.get("/api/accounts")
    def accounts():
        return provider.list_accounts()

    class ConnectBody(BaseModel):
        alias: str = ""

    @app.post("/api/accounts")
    def connect(body: ConnectBody):
        alias = body.alias.strip()
        if not alias:
            # Auto-generate: run the OAuth flow first to get email,
            # then derive alias from the email prefix.
            creds, email = auth._run_oauth_flow()
            prefix = email.split("@")[0].lower()
            # Sanitize: keep only alphanumeric, dash, underscore
            prefix = "".join(c if c.isalnum() or c in "-_" else "" for c in prefix)
            # Avoid collisions with existing aliases
            existing = set(auth.load_accounts().keys())
            alias = prefix
            n = 2
            while alias in existing:
                alias = f"{prefix}-{n}"
                n += 1
            # Store the credentials under the auto-generated alias
            import keyring
            keyring.set_password(
                auth.KEYRING_SERVICE, f"token:{alias}", creds.refresh_token)
            accounts = auth.load_accounts()
            accounts[alias] = {"email": email}
            auth.save_accounts(accounts)
            return {"alias": alias, "email": email}
        try:
            return provider.connect(alias)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)[:300])

    @app.delete("/api/accounts/{alias}")
    def disconnect(alias: str):
        try:
            provider.disconnect(alias)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)[:300])
        return {"ok": True}

    @app.get("/api/duplicates")
    def find_duplicates():
        """Files that sit in two or more accounts, from cached scans only."""
        return provider.duplicates()

    @app.delete("/api/accounts/{alias}/files/{file_id}")
    def delete_file(alias: str, file_id: str):
        """Trash a specific file on the given account's Drive."""
        try:
            provider.trash_file(alias, file_id)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)[:300])

    # -- scan jobs ---------------------------------------------------
    def _do_scan(job_id: str, alias: str):
        job = _jobs[job_id]
        try:
            job["progress"] = {"stage": "Scanning Drive…", "pct": 10}
            summary = provider.scan_summary(alias)
            job["progress"] = {"stage": "Done", "pct": 100}
            job["status"] = "done"
            job["result"] = summary
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(exc)[:300]

    @app.post("/api/accounts/{alias}/scan")
    def start_scan(alias: str):
        job_id = uuid.uuid4().hex[:12]
        _jobs[job_id] = {"status": "running",
                         "progress": {"stage": "Starting…", "pct": 0}}
        _pool.submit(_do_scan, job_id, alias)
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "unknown job")
        return job

    @app.get("/api/accounts/{alias}/files")
    def files(alias: str):
        try:
            return provider.get_files(alias)
        except FileNotFoundError:
            raise HTTPException(404, "no scan yet — scan this account first")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)[:300])

    # -- auth status & reconnect ----------------------------------------
    @app.get("/api/auth/status")
    def auth_status():
        return provider.auth_status()

    def _do_reconnect(job_id: str, alias: str):
        job = _jobs[job_id]
        try:
            job["progress"] = {"stage": "Waiting for browser sign-in…", "pct": 50}
            result = provider.reconnect(alias)
            job["progress"] = {"stage": "Done", "pct": 100}
            job["status"] = "done"
            job["result"] = result
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(exc)[:300]

    @app.post("/api/accounts/{alias}/reconnect")
    def reconnect(alias: str):
        job_id = uuid.uuid4().hex[:12]
        _jobs[job_id] = {"status": "running",
                         "progress": {"stage": "Opening sign-in…", "pct": 10}}
        _pool.submit(_do_reconnect, job_id, alias)
        return {"job_id": job_id}

    # -- plan & run --------------------------------------------------
    class PlanBody(BaseModel):
        source: str
        min_size: int = 50 * 1024**2
        kinds: list[str] = []  # mime prefixes, e.g. ["video/", "image/"]
        file_ids: list[str] = []  # explicit selection overrides filters
        include_shared: bool = False  # explicit: move shared files too

    @app.post("/api/plan")
    def make_plan(body: PlanBody):
        try:
            files = provider.get_files(body.source)
        except FileNotFoundError:
            raise HTTPException(404, "no scan yet — scan the source first")
        if body.file_ids:
            wanted = set(body.file_ids)
            candidates = [f for f in files if f["id"] in wanted]
        else:
            candidates = [
                f for f in files
                if f["size"] >= body.min_size
                and (not body.kinds or any(
                    f["mimeType"].startswith(k) for k in body.kinds))
            ]
        dests = []
        for acct in provider.list_accounts():
            if acct["alias"] == body.source:
                continue
            q = acct.get("quota") or {}
            free = q.get("free")
            if free is None and q.get("limit"):
                free = q["limit"] - q.get("usage", 0)
            if free and free > 0:
                dests.append(planner.Destination(acct["alias"], free))
        if not dests:
            raise HTTPException(400, "no destination accounts with free space")
        assignments, unplaced = planner.plan_moves(candidates, dests)
        per_dest: dict[str, dict] = {}
        for a in assignments:
            d = per_dest.setdefault(a.dest_alias, {"files": 0, "bytes": 0})
            d["files"] += 1
            d["bytes"] += a.file["size"]
        # Shared-file safety: which candidates would be skipped by default.
        shared_warnings = sharing.warnings(
            [a.file for a in assignments] + list(unplaced))
        movable, flagged = sharing.partition(
            [a.file for a in assignments])
        return {
            "source": body.source,
            "include_shared": body.include_shared,
            "assignments": [{"file": a.file, "dest_alias": a.dest_alias}
                            for a in assignments],
            "unplaced": unplaced,
            "per_dest": per_dest,
            "total_bytes": sum(a.file["size"] for a in assignments),
            "shared_warnings": shared_warnings,
            "movable_count": len(movable),
            "shared_blocked_count": len(flagged),
        }

    class RunBody(BaseModel):
        plan: dict

    @app.post("/api/run")
    def run_plan(body: RunBody):
        # The job runs in a worker thread and progress events are streamed
        # back, so the browser stays live even for multi-hour real transfers.
        return _stream_progress(lambda cb: provider.run_plan(body.plan, cb))

    # -- crash resume -------------------------------------------------
    @app.get("/api/transfers/interrupted")
    def interrupted():
        return provider.interrupted_transfers()

    @app.post("/api/resume")
    def resume():
        return _stream_progress(lambda cb: provider.resume(cb))

    # -- shared-file review -------------------------------------------
    @app.get("/api/accounts/{alias}/shared")
    def shared(alias: str):
        try:
            return provider.shared_files(alias)
        except FileNotFoundError:
            raise HTTPException(404, "no scan yet — scan this account first")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)[:300])

    # -- photos picker ------------------------------------------------
    class PhotosSessionBody(BaseModel):
        alias: str

    @app.post("/api/photos/session")
    def photos_session(body: PhotosSessionBody):
        try:
            return provider.photos_create_session(body.alias)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)[:300])

    @app.get("/api/photos/session/{alias}/{session_id}/items")
    def photos_items(alias: str, session_id: str):
        # The provider waits for the picker session to settle and raises
        # PickerSessionGone / PickerItemsMissing / TimeoutError with human
        # guidance on failure.
        try:
            return provider.photos_list_picked(alias, session_id)
        except photos.PickerSessionGone as exc:
            raise HTTPException(410, str(exc))
        except photos.PickerItemsMissing as exc:
            raise HTTPException(409, str(exc))
        except TimeoutError as exc:
            raise HTTPException(409, str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)[:300])

    class PhotosTransferBody(BaseModel):
        alias: str
        session_id: str
        dest_aliases: list[str]
        folder: str = ""

    @app.post("/api/photos/transfer")
    def photos_transfer(body: PhotosTransferBody):
        if not body.dest_aliases:
            raise HTTPException(400, "pick at least one destination account")
        subfolder = body.folder.strip() or "Photos Picker"
        return _stream_progress(
            lambda cb: provider.photos_transfer(
                body.alias, body.session_id, body.dest_aliases, cb,
                subfolder=subfolder))

    # -- ledger ------------------------------------------------------
    @app.get("/api/ledger")
    def ledger_list(q: str = "", limit: int = 50):
        return provider.ledger(q, limit)

    class VerifyBody(BaseModel):
        entries: list[dict]  # [{dst_alias, dst_file_id, sha256, filename}]

    @app.post("/api/transfers/verify")
    def verify_transfers(body: VerifyBody):
        results = []
        cache: dict[str, "DriveClient"] = {}
        for e in body.entries[:50]:
            alias = e.get("dst_alias", "")
            fid = e.get("dst_file_id", "")
            if not alias or not fid:
                results.append({"dst_file_id": fid, "exists": False,
                                "hash_match": None, "error": "missing info"})
                continue
            try:
                if alias not in cache:
                    cache[alias] = DriveClient(alias)
                dc = cache[alias]
                remote_hash = dc.remote_sha256(fid)
                exists = remote_hash is not None
                expected = e.get("sha256")
                hash_match = (remote_hash == expected) if (exists and expected) else None
                results.append({"dst_file_id": fid, "exists": exists,
                                "hash_match": hash_match,
                                "drive_link": f"https://drive.google.com/file/d/{fid}/view",
                                "error": ""})
            except Exception as exc:  # noqa: BLE001
                results.append({"dst_file_id": fid, "exists": False,
                                "hash_match": None,
                                "error": str(exc)[:200]})
        return results

    # -- takeout -----------------------------------------------------
    class TakeoutBody(BaseModel):
        dir: str

    @app.post("/api/takeout/ingest")
    def takeout_ingest(body: TakeoutBody):
        root = Path(body.dir).expanduser()
        if not root.exists():
            raise HTTPException(400, "directory not found")
        if provider.name == "demo":
            items = [
                {"name": "IMG_2023_0401.jpg", "size": 4_200_000,
                 "album": "Photos from 2023", "path": str(root / "a.jpg")},
                {"name": "VID_2023_0712.mp4", "size": 380_000_000,
                 "album": "Photos from 2023", "path": str(root / "b.mp4")},
                {"name": "IMG_2024_0119.jpg", "size": 5_100_000,
                 "album": "Photos from 2024", "path": str(root / "c.jpg")},
            ]
            return {"files": len(items),
                    "bytes": sum(i["size"] for i in items),
                    "albums": {"Photos from 2023": 2, "Photos from 2024": 1},
                    "items": items}
        try:
            extracted = takeout.extract_archives(root)
            items = takeout.collect_items(extracted)
            info = takeout.summarize(items)
            return {
                "files": info["files"], "bytes": info["bytes"],
                "albums": info["albums"],
                "items": [{"name": i.path.name, "size": i.size,
                           "album": i.album, "path": str(i.path)}
                          for i in items[:5000]],
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc)[:300])

    class DistributeBody(BaseModel):
        items: list[dict]  # {name, path}
        dests: list[str]

    @app.post("/api/takeout/distribute")
    def takeout_distribute(body: DistributeBody):
        if provider.name == "demo":
            def gen():
                total = len(body.items)
                yield 'data: {"type":"start"}\n\n'
                for i, it in enumerate(body.items, start=1):
                    time.sleep(0.15)
                    dest = body.dests[i % len(body.dests)]
                    yield ("data: " + json.dumps(
                        {"type": "progress", "done": i, "total": total,
                         "name": it["name"], "dest_alias": dest,
                         "status": "done"}) + "\n\n")
                yield f"data: {json.dumps({'type':'done','stats':{'done':total,'failed':0}})}\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")

        events: queue.Queue = queue.Queue()

        def _runner():
            try:
                per_dest: dict[str, list] = {d: [] for d in body.dests}
                for i, it in enumerate(body.items):
                    per_dest[body.dests[i % len(body.dests)]].append(it)
                done = failed = 0
                total = len(body.items)
                n = 0
                for dest, items in per_dest.items():
                    for it in items:
                        n += 1
                        try:
                            transfer.upload_local(
                                Path(it["path"]), dest,
                                subfolder=f"Takeout/{it.get('album', '')}".rstrip("/"))
                            done += 1
                            events.put({"done": n, "total": total,
                                        "name": it["name"],
                                        "dest_alias": dest, "status": "done"})
                        except Exception as exc:  # noqa: BLE001
                            failed += 1
                            events.put({"done": n, "total": total,
                                        "name": it["name"],
                                        "dest_alias": dest, "status": "failed",
                                        "note": str(exc)[:200]})
                events.put({"stats": {"done": done, "failed": failed}})
            except Exception as exc:  # noqa: BLE001
                events.put({"error": str(exc)[:300]})
            events.put(None)

        def gen():
            threading.Thread(target=_runner, daemon=True).start()
            yield 'data: {"type":"start"}\n\n'
            while True:
                e = events.get()
                if e is None:
                    break
                if "stats" in e:
                    yield f"data: {json.dumps({'type':'done','stats':e['stats']})}\n\n"
                elif "error" in e:
                    yield f"data: {json.dumps({'type':'error','error':e['error']})}\n\n"
                else:
                    yield f"data: {json.dumps({'type':'progress', **e})}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def serve(port: int = 8741, demo: bool = False, open_browser: bool = True):
    import webbrowser
    import uvicorn
    config.ensure_dirs()
    url = f"http://127.0.0.1:{port}"
    print(f"Spillover UI at {url}" + (" (demo data)" if demo else ""))
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(demo=demo), host="127.0.0.1", port=port, log_level="warning")
