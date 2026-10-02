"""Spillover command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import auth, inventory, ledger, sharing
from .config import PLANS_DIR, ensure_dirs
from .drive import DriveClient
from .planner import Destination, plan_moves
from .takeout import collect_items, extract_archives, summarize
from .transfer import execute as run_moves
from .transfer import resume as resume_moves
from .transfer import upload_local


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def parse_size(s: str) -> int:
    s = s.strip().upper()
    mult = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4, "B": 1}
    for suffix, m in sorted(mult.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * m)
    return int(s)


# -- auth ---------------------------------------------------------------
def cmd_auth_add(args):
    email = auth.add_account(args.alias)
    print(f"Connected '{args.alias}' ({email}).")


def cmd_auth_reconnect(args):
    email = auth.reconnect_account(args.alias)
    print(f"Reconnected '{args.alias}' ({email}).")


def cmd_auth_list(_args):
    accounts = auth.load_accounts()
    if not accounts:
        print("No accounts connected. Run: spillover auth add --alias main")
        return
    for alias, info in accounts.items():
        status = auth.check_account(alias)
        tag = "ok" if status["ok"] else "NEEDS RECONNECT"
        print(f"  {alias:15} {info.get('email', ''):30} {tag}")


def cmd_auth_remove(args):
    auth.remove_account(args.alias)
    print(f"Removed '{args.alias}'.")


# -- quota / scan --------------------------------------------------------
def cmd_quota(args):
    accounts = auth.load_accounts()
    aliases = [args.alias] if args.alias else list(accounts)
    if not aliases:
        print("No accounts connected. Run: spillover auth add --alias main")
        return
    for alias in aliases:
        q = DriveClient(alias).quota()
        pct = (q["usage"] / q["limit"] * 100) if q["limit"] else 0
        bar = "#" * int(pct / 5) + "-" * (20 - int(pct / 5))
        print(
            f"{alias:15} [{bar}] {pct:5.1f}%  "
            f"{fmt_bytes(q['usage'])} / {fmt_bytes(q['limit'])}"
        )


def cmd_scan(args):
    summary = inventory.scan(args.alias)
    print(f"\n{args.alias}: {summary['quota_files']} quota-relevant files, "
          f"{fmt_bytes(summary['quota_bytes'])} total")
    print("Top space by type:")
    for ext, size in summary["top_extensions"]:
        print(f"  {ext:12} {fmt_bytes(size):>10}")


# -- plan / run ----------------------------------------------------------
def _select_files(scan_data: dict, min_size: int, types: set[str] | None) -> list[dict]:
    files = scan_data["files"]
    out = [f for f in files if f["size"] >= min_size]
    if types:
        out = [f for f in out if Path(f["name"]).suffix.lower().lstrip(".") in types]
    return out


def cmd_plan(args):
    ensure_dirs()
    scan_data = inventory.load_scan(args.source)
    files = _select_files(
        scan_data,
        parse_size(args.min_size),
        set(t.strip().lower() for t in args.types.split(",")) if args.types else None,
    )
    if not files:
        print("No files match the filters. Try lowering --min-size.")
        return

    accounts = auth.load_accounts()
    dest_aliases = args.to.split(",") if args.to else [
        a for a in accounts if a != args.source
    ]
    if not dest_aliases:
        print("No destination accounts. Connect at least one more account.")
        return
    dests = []
    for alias in dest_aliases:
        q = DriveClient(alias).quota()
        dests.append(Destination(alias=alias, free_bytes=q["free"]))

    assignments, unplaced = plan_moves(files, dests)
    total = sum(f["size"] for f in files)
    movable = sum(a.file["size"] for a in assignments)
    print(f"\nPlan: {len(assignments)} files ({fmt_bytes(movable)}) movable, "
          f"{len(unplaced)} files ({fmt_bytes(total - movable)}) don't fit anywhere.")
    if unplaced:
        print("Unplaced (too big for any destination's free space):")
        for f in sorted(unplaced, key=lambda x: x["size"], reverse=True)[:10]:
            print(f"  {fmt_bytes(f['size']):>10}  {f['name']}")

    # Shared-file safety: these are skipped at run time unless --include-shared.
    ok_files, blocked = sharing.partition([a.file for a in assignments])
    if blocked and not args.include_shared:
        print(f"\n{len(blocked)} file(s) are shared with others or not solely "
              "owned — they will be SKIPPED by default:")
        for f in sorted(blocked, key=lambda x: x["size"], reverse=True)[:10]:
            c = sharing.classify(f)
            print(f"  [{c['level']:9}] {fmt_bytes(f['size']):>10}  {f['name']}")
        print("Add --include-shared to move them anyway (share links break, "
              "collaborators lose access).")

    plan = {
        "created_at": int(time.time()),
        "source": args.source,
        "include_shared": bool(args.include_shared),
        "assignments": [
            {"file": a.file, "dest_alias": a.dest_alias} for a in assignments
        ],
    }
    name = args.save or f"plan-{args.source}-{int(time.time())}.json"
    path = PLANS_DIR / name
    path.write_text(json.dumps(plan, indent=1))
    print(f"\nPlan saved to {path}")
    print("Preview it with: spillover run --plan "
          f"{path}   (add --execute to actually move files)")


def cmd_run(args):
    from .planner import Assignment

    plan = json.loads(Path(args.plan).read_text())
    assignments = [
        Assignment(file=a["file"], dest_alias=a["dest_alias"])
        for a in plan["assignments"]
    ]
    if not args.execute and not args.yes:
        print("This is a DRY RUN preview.")
    if args.execute and not args.yes:
        answer = input(
            f"\nMove {len(assignments)} files from '{plan['source']}'? "
            "Sources will be TRASHED after verified copy (recoverable 30 days). "
            "[y/N] "
        )
        if answer.strip().lower() != "y":
            print("Aborted.")
            return
    include_shared = args.include_shared or bool(plan.get("include_shared"))
    run_moves(assignments, src_alias=plan["source"], dry_run=not args.execute,
              include_shared=include_shared)


# -- resume ----------------------------------------------------------------
def cmd_resume(args):
    stats = resume_moves(src_alias=args.source)
    if not stats["resumed"]:
        return
    print(f"Resumed {stats['resumed']}: {stats['done']} completed, "
          f"{stats['failed']} failed.")


# -- takeout --------------------------------------------------------------
def cmd_takeout(args):
    root = extract_archives(Path(args.dir))
    items = collect_items(root)
    info = summarize(items)
    print(f"\nTakeout: {info['files']} media files, {fmt_bytes(info['bytes'])}")
    for album, n in sorted(info["albums"].items(), key=lambda x: -x[1])[:10]:
        print(f"  {n:6} files  {album}")

    accounts = auth.load_accounts()
    dest_aliases = args.to.split(",") if args.to else list(accounts)
    if not dest_aliases:
        print("No destination accounts connected.")
        return

    # Simple round-robin by free space, album by album, keeping the buffer.
    quotas = {a: DriveClient(a).quota()["free"] for a in dest_aliases}
    usable = {a: max(q - 1024**3, 0) for a, q in quotas.items()}

    plan = []  # (item, dest_alias)
    unplaced = []
    for item in sorted(items, key=lambda i: i.path.stat().st_size, reverse=True):
        size = item.path.stat().st_size
        cands = [a for a in dest_aliases if usable[a] >= size]
        if not cands:
            unplaced.append(item)
            continue
        dest = min(cands, key=lambda a: usable[a])
        plan.append((item, dest))
        usable[dest] -= size

    print(f"\n{len(plan)} files fit across: "
          + ", ".join(f"{a} ({fmt_bytes(quotas[a] - usable[a] - 1024**3)} planned)"
                      for a in dest_aliases))
    if unplaced:
        print(f"{len(unplaced)} files don't fit in remaining free space.")

    if not args.execute:
        print("\nDRY RUN — nothing uploaded. Add --execute to perform it.")
        return

    from tqdm import tqdm

    ok, failed = 0, 0
    for item, dest in tqdm(plan, desc="Uploading takeout", unit="file"):
        try:
            upload_local(item.path, dest, subfolder=item.album)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            tqdm.write(f"FAILED {item.path.name}: {exc}")
            ledger.record_move(
                filename=item.path.name, size_bytes=item.path.stat().st_size,
                sha256=None, src_alias="takeout", src_file_id=None,
                dst_alias=dest, dst_file_id=None, status="failed",
                note=str(exc)[:300],
            )
    print(f"\nTakeout ingest: {ok} uploaded, {failed} failed.")
    print("Remember: delete the originals in Google Photos yourself — "
          "Google allows no API for that.")


# -- photos picker ----------------------------------------------------------
def cmd_photos_create(args):
    from . import photos

    s = photos.create_picker_session(args.alias)
    print("\nOpen this link in a browser window signed in to Google as the")
    print(f"'{args.alias}' account, and select the photos/videos")
    print("you want Spillover to move:\n")
    print(f"  {s['picker_uri']}\n")
    print("Picks made while signed in as a different Google account won't")
    print("come through. Then run:")
    print(f"  spillover photos transfer --alias {args.alias} "
          f"--session-id {s['session_id']} --to dest1,dest2")


def cmd_photos_list(args):
    from . import photos

    try:
        items = photos.list_picked(args.alias, args.session_id)
    except photos.PickerSessionGone as exc:
        print(f"\n{exc}")
        return
    except TimeoutError as exc:
        print(f"\n{exc}")
        return
    if not items:
        print("Nothing selected yet — open the picker link and pick some items.")
        return
    for it in items:
        print(f"  {it['filename']:40} {it['type']:6} {it['createTime'][:10]}")


def cmd_photos_transfer(args):
    from . import photos

    dests = [d.strip() for d in args.to.split(",")]
    stats = photos.transfer_picked(
        args.alias, args.session_id, dests)
    print(f"\nTransferred {stats['done']} item(s) ({fmt_bytes(stats['bytes'])}), "
          f"{stats['failed']} failed.")
    print("\nGoogle's Photos API has no delete endpoint, so the originals are")
    print("still in Google Photos. A deletion checklist was written to:")
    print(f"  {stats['manifest_path']}")
    print("Delete the originals by hand — only after confirming the copies.")


# -- ledger ---------------------------------------------------------------
def cmd_find(args):
    rows = ledger.search(args.query, limit=args.limit)
    if not rows:
        print("No matching moves in the ledger.")
        return
    for r in rows:
        print(f"[{r['status']:6}] {r['filename']}  "
              f"{r['src_alias']} -> {r['dst_alias']}  "
              f"{fmt_bytes(r['size_bytes'])}  {r['moved_at']}")


def cmd_ui(args):
    from .web.server import serve
    try:
        import fastapi  # noqa: F401
    except ImportError:
        print("The web UI needs extra packages: pip install fastapi 'uvicorn[standard]'")
        raise SystemExit(1)
    serve(port=args.port, demo=args.demo, open_browser=not args.no_browser)


def cmd_history(args):
    for r in ledger.recent(limit=args.limit):
        print(f"[{r['status']:6}] {r['filename']}  "
              f"{r['src_alias']} -> {r['dst_alias']}  "
              f"{fmt_bytes(r['size_bytes'])}  {r['moved_at']}")


# -- parser ----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="spillover",
        description="Rebalance Google Drive storage across your own accounts, locally.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="Connect Google accounts")
    asub = a.add_subparsers(dest="auth_cmd", required=True)
    p_add = asub.add_parser("add", help="Connect a new account via OAuth")
    p_add.add_argument("--alias", required=True, help="Short name, e.g. main")
    p_add.set_defaults(func=cmd_auth_add)
    p_rec = asub.add_parser("reconnect",
                            help="Re-run sign-in for an account whose token "
                                 "expired or was revoked")
    p_rec.add_argument("--alias", required=True)
    p_rec.set_defaults(func=cmd_auth_reconnect)
    p_list = asub.add_parser("list", help="List connected accounts")
    p_list.set_defaults(func=cmd_auth_list)
    p_rm = asub.add_parser("remove", help="Disconnect an account")
    p_rm.add_argument("--alias", required=True)
    p_rm.set_defaults(func=cmd_auth_remove)

    p_quota = sub.add_parser("quota", help="Show storage usage per account")
    p_quota.add_argument("--alias", help="Only this account")
    p_quota.set_defaults(func=cmd_quota)

    p_scan = sub.add_parser("scan", help="Inventory an account's Drive")
    p_scan.add_argument("--alias", required=True)
    p_scan.set_defaults(func=cmd_scan)

    p_plan = sub.add_parser("plan", help="Propose moves from a full account")
    p_plan.add_argument("--from", dest="source", required=True)
    p_plan.add_argument("--to", help="Comma-separated destination aliases")
    p_plan.add_argument("--min-size", default="50MB",
                        help="Only files >= this size (default 50MB)")
    p_plan.add_argument("--types", help="Comma-separated extensions, e.g. mp4,jpg")
    p_plan.add_argument("--save", help="Plan filename")
    p_plan.add_argument("--include-shared", action="store_true",
                        help="Also move files shared with others / not solely "
                             "owned (skipped by default)")
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser("run", help="Execute a saved plan (dry-run by default)")
    p_run.add_argument("--plan", required=True, help="Path to plan JSON")
    p_run.add_argument("--execute", action="store_true",
                       help="Actually move files (default is dry-run)")
    p_run.add_argument("--yes", action="store_true",
                       help="Skip the confirmation prompt")
    p_run.add_argument("--include-shared", action="store_true",
                       help="Also move files shared with others / not solely "
                            "owned (skipped by default)")
    p_run.set_defaults(func=cmd_run)

    p_resume = sub.add_parser(
        "resume",
        help="Resume transfers interrupted by a crash or kill",
    )
    p_resume.add_argument("--from", dest="source",
                          help="Only resume transfers from this source alias")
    p_resume.set_defaults(func=cmd_resume)

    p_take = sub.add_parser("takeout", help="Ingest a Google Photos Takeout export")
    p_take.add_argument("--dir", required=True,
                        help="Folder with takeout-*.zip or extracted tree")
    p_take.add_argument("--to", help="Comma-separated destination aliases")
    p_take.add_argument("--execute", action="store_true")
    p_take.set_defaults(func=cmd_takeout)

    p_ph = sub.add_parser("photos",
                          help="Move Google Photos picks (no export needed)")
    phsub = p_ph.add_subparsers(dest="photos_cmd", required=True)
    p_phc = phsub.add_parser("create",
                             help="Create a picker session and print the link")
    p_phc.add_argument("--alias", required=True,
                       help="Source account (its Photos library)")
    p_phc.set_defaults(func=cmd_photos_create)
    p_phl = phsub.add_parser("list",
                             help="List what was picked in a session")
    p_phl.add_argument("--alias", required=True)
    p_phl.add_argument("--session-id", required=True)
    p_phl.set_defaults(func=cmd_photos_list)
    p_pht = phsub.add_parser("transfer",
                             help="Transfer the picked items to Drive accounts")
    p_pht.add_argument("--alias", required=True)
    p_pht.add_argument("--session-id", required=True)
    p_pht.add_argument("--to", required=True,
                       help="Comma-separated destination aliases")
    p_pht.set_defaults(func=cmd_photos_transfer)

    p_find = sub.add_parser("find", help="Search the ledger: where is my file?")
    p_find.add_argument("query")
    p_find.add_argument("--limit", type=int, default=50)
    p_find.set_defaults(func=cmd_find)

    p_hist = sub.add_parser("history", help="Recent moves")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    p_ui = sub.add_parser("ui", help="Open the local web UI")
    p_ui.add_argument("--port", type=int, default=8741)
    p_ui.add_argument("--demo", action="store_true",
                      help="Run with demo data (no Google accounts needed)")
    p_ui.add_argument("--no-browser", action="store_true")
    p_ui.set_defaults(func=cmd_ui)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
