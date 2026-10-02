"""Web UI API tests — demo mode, no Google credentials needed.

Spins up the real FastAPI app with uvicorn in a thread and exercises the
JSON API with urllib. Regression coverage for the POST-body bug where
``from __future__ import annotations`` made FastAPI demote body params to
query params (all POSTs returned 422).
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spillover.web.server import create_app  # noqa: E402

BASE = "http://127.0.0.1:18741"


@pytest.fixture(scope="module")
def server():
    thread = threading.Thread(
        target=lambda: uvicorn.run(
            create_app(demo=True), host="127.0.0.1", port=18741,
            log_level="error"),
        daemon=True,
    )
    thread.start()
    for _ in range(100):
        try:
            urllib.request.urlopen(BASE + "/api/mode", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.fail("demo server did not start")
    yield BASE


def get(base, path):
    with urllib.request.urlopen(base + path) as r:
        return r.status, r.read().decode()


def post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_mode(server):
    status, body = get(server, "/api/mode")
    assert status == 200
    assert json.loads(body) == {"demo": True}


def test_index_serves_spa(server):
    status, body = get(server, "/")
    assert status == 200
    assert "<title>Spillover" in body
    assert "/static/app.js" in body


def test_static_assets_exist(server):
    for asset in ("styles.css", "app.js"):
        status, _ = get(server, f"/static/{asset}")
        assert status == 200, asset


def test_accounts_list(server):
    status, body = get(server, "/api/accounts")
    assert status == 200
    accounts = json.loads(body)
    assert len(accounts) >= 2
    assert all("alias" in a and "quota" in a for a in accounts)


def test_plan_post_body_parsed(server):
    """Regression: POST /api/plan must read the JSON body (was 422)."""
    status, body = post(server, "/api/plan",
                        {"source": "main", "file_ids": ["demo1001"]})
    assert status == 200, body
    assert body["source"] == "main"
    assert isinstance(body["assignments"], list)


def test_plan_unknown_source_returns_empty(server):
    # demo provider has no files for unknown aliases -> empty plan, not 404
    status, body = post(server, "/api/plan",
                        {"source": "nope", "file_ids": ["x"]})
    assert status == 200, body
    assert body["assignments"] == []


def test_takeout_ingest_post_body_parsed(server, tmp_path):
    """Regression: POST /api/takeout/ingest must read the JSON body."""
    status, body = post(server, "/api/takeout/ingest",
                        {"dir": str(tmp_path)})
    assert status == 200, body
    assert body["files"] == 3
    assert "albums" in body


def test_takeout_ingest_missing_dir(server):
    status, body = post(server, "/api/takeout/ingest",
                        {"dir": "/does/not/exist"})
    assert status == 400


def test_ledger_search(server):
    status, body = get(server, "/api/ledger?q=wedding&limit=10")
    assert status == 200
    entries = json.loads(body)
    assert entries, "expected demo ledger entries"
    assert all("wedding" in e["filename"].lower() for e in entries)


def test_files_list(server):
    status, body = get(server, "/api/accounts/main/files")
    assert status == 200
    files = json.loads(body)
    assert len(files) > 0
    assert all("id" in f and "size" in f for f in files)


def test_plan_includes_shared_warnings(server):
    """The plan must flag shared/not-owned files (demo data has both)."""
    status, body = get(server, "/api/accounts/main/files")
    ids = [f["id"] for f in json.loads(body)]
    status, plan = post(server, "/api/plan",
                        {"source": "main", "file_ids": ids})
    assert status == 200, plan
    levels = {w["level"] for w in plan["shared_warnings"]}
    assert "shared" in levels and "not_owned" in levels
    assert plan["shared_blocked_count"] >= 2
    # without include_shared the response round-trips the default
    assert plan["include_shared"] is False


def test_auth_status_flags_broken_account(server):
    status, body = get(server, "/api/auth/status")
    assert status == 200
    st = json.loads(body)
    assert st["backup-2"]["needs_reconnect"] is True
    assert st["main"]["ok"] is True


def test_reconnect_runs_as_job(server):
    status, body = post(server, "/api/accounts/backup-2/reconnect", {})
    assert status == 200, body
    job_id = body["job_id"]
    for _ in range(50):
        _, jb = get(server, f"/api/jobs/{job_id}")
        job = json.loads(jb)
        if job["status"] != "running":
            break
        time.sleep(0.2)
    assert job["status"] == "done", job
    assert job["result"]["alias"] == "backup-2"


def test_interrupted_transfers_listed(server):
    status, body = get(server, "/api/transfers/interrupted")
    assert status == 200
    items = json.loads(body)
    assert len(items) == 1
    assert items[0]["state"] in ("pending", "in_progress")


def test_shared_files_endpoint(server):
    status, body = get(server, "/api/accounts/main/shared")
    assert status == 200
    warns = json.loads(body)
    assert any(w["level"] == "shared" for w in warns)
    assert any(w["level"] == "not_owned" for w in warns)


def test_photos_session_and_items(server):
    status, sess = post(server, "/api/photos/session", {"alias": "main"})
    assert status == 200, sess
    assert sess["session_id"] and sess["picker_uri"]
    status, body = get(
        server, f"/api/photos/session/main/{sess['session_id']}/items")
    assert status == 200
    items = json.loads(body)
    assert len(items) == 3
    assert all("filename" in i for i in items)


def test_run_stream_emits_sse_events(server):
    """POST /api/run streams SSE with a terminal done event."""
    _, body = get(server, "/api/accounts/main/files")
    ids = [f["id"] for f in json.loads(body)[:3]]
    _, plan = post(server, "/api/plan", {"source": "main", "file_ids": ids})
    req = urllib.request.Request(
        server + "/api/run",
        data=json.dumps({"plan": plan}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        text = r.read().decode()
    events = [json.loads(line[6:]) for line in text.split("\n")
              if line.startswith("data: ")]
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert types[-1] == "done"
    assert events[-1]["stats"]["done"] >= 1  # shared files skipped, not failed


def test_resume_stream_emits_done(server):
    req = urllib.request.Request(
        server + "/api/resume", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        text = r.read().decode()
    events = [json.loads(line[6:]) for line in text.split("\n")
              if line.startswith("data: ")]
    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "done"
    assert events[-1]["stats"]["done"] == 1


def test_photos_items_maps_gone_session_to_410(server, monkeypatch):
    """A 404 from Google becomes a 410 with human guidance, not a raw 404."""
    import spillover.photos as photos_mod

    def gone(alias, session_id, **kwargs):
        raise photos_mod.PickerSessionGone("Google doesn't recognise this")

    monkeypatch.setattr(photos_mod, "check_selection_done", gone)
    try:
        get(server, "/api/photos/session/main/sess-9/items")
        pytest.fail("expected HTTP 410")
    except urllib.error.HTTPError as e:
        assert e.code == 410
        assert "Google doesn't recognise" in json.loads(e.read().decode())["detail"]


def test_photos_items_maps_unfinished_selection_to_409(server, monkeypatch):
    import spillover.photos as photos_mod

    def slow(alias, session_id, **kwargs):
        raise TimeoutError("hasn't marked your selection as finished")

    monkeypatch.setattr(photos_mod, "check_selection_done", slow)
    try:
        get(server, "/api/photos/session/main/sess-9/items")
        pytest.fail("expected HTTP 409")
    except urllib.error.HTTPError as e:
        assert e.code == 409
        assert "hasn't marked" in json.loads(e.read().decode())["detail"]


def test_photos_items_maps_missing_items_to_409(server, monkeypatch):
    import spillover.photos as photos_mod

    def missing(alias, session_id, **kwargs):
        raise photos_mod.PickerItemsMissing("never got attached")

    monkeypatch.setattr(photos_mod, "check_selection_done", missing)
    try:
        get(server, "/api/photos/session/main/sess-9/items")
        pytest.fail("expected HTTP 409")
    except urllib.error.HTTPError as e:
        assert e.code == 409
        assert "never got attached" in json.loads(e.read().decode())["detail"]
