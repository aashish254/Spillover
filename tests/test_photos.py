"""Photos picker tests: session flow, transfer, manifest, hash mismatch.

The PickerAPI's HTTP layer is faked — no network, no credentials.
"""

import hashlib
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["HOME"] = _tmp  # must precede importing spillover.config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from spillover import ledger, photos  # noqa: E402
from spillover.photos import PickerAPI  # noqa: E402


class FakeResp:
    def __init__(self, payload=None, content=b""):
        self._payload = payload
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk):
        yield self._content


class FakeHTTP:
    """Pretends to be the Picker + Library APIs."""

    def __init__(self, picked=("pic1",), corrupt=()):
        self.picked = list(picked)
        self.corrupt = set(corrupt)
        self.media_set = True

    def post(self, url, json=None):
        return FakeResp({"id": "sess-1", "pickerUri": "https://picker/x",
                         "expireTime": "2030-01-01T00:00:00Z"})

    def get(self, url, params=None, stream=False):
        if url.endswith("/v1/sessions/sess-1"):
            return FakeResp({"id": "sess-1", "mediaItemsSet": self.media_set})
        if "mediaItems" in url and "sessions" in url:
            return FakeResp({"mediaItems": [
                {"id": p,
                 "mediaFile": {"filename": f"{p}.jpg",
                               "mimeType": "image/jpeg",
                               "baseUrl": f"https://lh3/{p}"},
                 "createTime": "2024-05-01T10:00:00Z", "type": "PHOTO"}
                for p in self.picked]})
        if "/v1/mediaItems/" in url:
            pid = url.rsplit("/", 1)[-1]
            return FakeResp({"id": pid,
                             "mediaFile": {"filename": f"{pid}.jpg",
                                           "mimeType": "image/jpeg",
                                           "baseUrl": f"https://lh3/{pid}"}})
        if url.startswith("https://lh3/"):
            pid = url[len("https://lh3/"):].split("=")[0]
            data = f"bytes-of-{pid}".encode()
            if pid in self.corrupt:
                data = b"corrupted-on-the-wire"
            return FakeResp(content=data)
        raise AssertionError(f"unexpected GET {url}")


class FakeDrive:
    instances = {}

    def __new__(cls, alias):
        if alias in cls.instances:
            return cls.instances[alias]
        inst = super().__new__(cls)
        cls.instances[alias] = inst
        return inst

    def __init__(self, alias):
        if getattr(self, "_ready", False):
            return
        self.files = {}
        self._ready = True

    def ensure_folder(self, name, parent_id=None):
        return f"folder-{name}"

    def upload(self, local, folder_id, name=None):
        data = Path(local).read_bytes()
        fid = f"up-{len(self.files)}"
        self.files[fid] = data
        return {"id": fid, "size": str(len(data))}

    def remote_sha256(self, file_id):
        return hashlib.sha256(self.files[file_id]).hexdigest()


@pytest.fixture(autouse=True)
def fakes(monkeypatch, tmp_path):
    FakeDrive.instances = {}
    monkeypatch.setattr(photos, "DriveClient", FakeDrive)
    monkeypatch.setattr(photos, "PHOTO_MANIFESTS_DIR", tmp_path / "manifests")
    monkeypatch.setattr(photos, "TMP_DIR", tmp_path / "tmp")
    (tmp_path / "tmp").mkdir()
    with ledger._connect() as conn:
        conn.execute("DELETE FROM moves")
    yield


def _api(**kw):
    return PickerAPI("main", http=FakeHTTP(**kw))


def test_create_session_returns_picker_link():
    s = photos.create_picker_session("main", picker=_api())
    assert s["session_id"] == "sess-1"
    assert s["picker_uri"].startswith("https://")


def test_wait_for_selection_returns_when_user_picks():
    s = photos.wait_for_selection("main", "sess-1", timeout_s=5, poll_s=0,
                                  picker=_api())
    assert s["mediaItemsSet"] is True


def test_wait_for_selection_times_out():
    http = FakeHTTP()
    http.media_set = False
    api = PickerAPI("main", http=http)
    with pytest.raises(TimeoutError):
        photos.wait_for_selection("main", "sess-1", timeout_s=0, poll_s=0,
                                  picker=api)


def test_transfer_picked_uploads_and_writes_manifest():
    api = _api(picked=("pic1", "pic2"))
    stats = photos.transfer_picked("main", "sess-1", ["dst1", "dst2"],
                                   picker=api)
    assert stats["done"] == 2 and stats["failed"] == 0
    assert stats["bytes"] == len(b"bytes-of-pic1") + len(b"bytes-of-pic2")
    # Round-robin across destinations.
    rows = [r for r in ledger.recent(10) if r["src_alias"] == "photos:main"]
    assert {r["dst_alias"] for r in rows} == {"dst1", "dst2"}
    assert all(r["status"] == "done" for r in rows)
    # Manifest exists (JSON + Markdown) and names both files.
    manifest = Path(stats["manifest_path"])
    assert manifest.exists() and manifest.suffix == ".json"
    md = manifest.with_suffix(".md")
    assert md.exists()
    text = md.read_text()
    assert "pic1.jpg" in text and "pic2.jpg" in text
    assert "no delete endpoint" in text


def test_nothing_picked_means_nothing_to_do():
    # The real picker UI can't finish with zero selections, so an empty
    # item list after "done" means the picks never attached — surface the
    # human error instead of silently transferring nothing.
    with pytest.raises(photos.PickerItemsMissing, match="Try again"):
        photos.list_picked("main", "sess-1", picker=_api(picked=()),
                           settle_s=0.05, poll_s=0.01)


def test_upload_failure_is_recorded_not_raised(monkeypatch):
    class BrokenDrive:
        def __init__(self, alias):
            pass

        def ensure_folder(self, name, parent_id=None):
            return "folder"

        def upload(self, local, folder_id, name=None):
            raise RuntimeError("quota exceeded")

        def remote_sha256(self, file_id):
            raise AssertionError("unreachable")

    monkeypatch.setattr(photos, "DriveClient", BrokenDrive)
    stats = photos.transfer_picked("main", "sess-1", ["dst1"],
                                   picker=_api(picked=("pic1",)))
    assert stats["done"] == 0 and stats["failed"] == 1
    rows = ledger.search("pic1")
    assert rows and rows[0]["status"] == "failed"


class Fake404(Exception):
    """Mimics requests.HTTPError for a 404 response."""

    def __init__(self):
        super().__init__(
            "404 Client Error: Not Found for url: "
            "https://photospicker.googleapis.com/v1/sessions/sess-1")
        self.response = type("Resp", (), {"status_code": 404})()


class GoneHTTP(FakeHTTP):
    def get(self, url, params=None, stream=False):
        if url.endswith("/v1/sessions/sess-1"):
            raise Fake404()
        return super().get(url, params=params, stream=stream)


class SlowHTTP(FakeHTTP):
    """mediaItemsSet flips true only after a few polls."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = 0

    def get(self, url, params=None, stream=False):
        if url.endswith("/v1/sessions/sess-1"):
            self.calls += 1
            return FakeResp({"id": "sess-1",
                             "mediaItemsSet": self.calls >= 3})
        return super().get(url, params=params, stream=stream)


def test_check_selection_done_returns_when_already_set():
    api = PickerAPI("a", http=FakeHTTP())
    s = photos.check_selection_done("a", "sess-1", picker=api)
    assert s["mediaItemsSet"] is True


def test_check_selection_done_polls_until_set():
    api = PickerAPI("a", http=SlowHTTP())
    s = photos.check_selection_done("a", "sess-1", wait_s=30, poll_s=0.01,
                                    picker=api)
    assert s["mediaItemsSet"] is True


def test_check_selection_done_translates_404_into_guidance():
    api = PickerAPI("a", http=GoneHTTP())
    with pytest.raises(photos.PickerSessionGone, match="expired"):
        photos.check_selection_done("a", "sess-1", picker=api)


def test_check_selection_done_times_out_when_user_never_finishes():
    http = FakeHTTP()
    http.media_set = False
    api = PickerAPI("a", http=http)
    with pytest.raises(TimeoutError, match="hasn't marked"):
        photos.check_selection_done("a", "sess-1", wait_s=0.05, poll_s=0.01,
                                    picker=api)


class FlakyItemsHTTP(FakeHTTP):
    """mediaItems.list 404s twice, then returns the picks (backend lag)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = 0

    def get(self, url, params=None, stream=False):
        if "mediaItems" in url and "sessions" in url:
            self.calls += 1
            if self.calls < 3:
                raise Fake404()
        return super().get(url, params=params, stream=stream)


class NeverAttachedHTTP(FakeHTTP):
    """The session reports done, but Google never attaches any items."""

    def get(self, url, params=None, stream=False):
        if "mediaItems" in url and "sessions" in url:
            raise Fake404()
        return super().get(url, params=params, stream=stream)


def test_list_picked_retries_through_transient_404s():
    api = PickerAPI("a", http=FlakyItemsHTTP())
    items = photos.list_picked("a", "sess-1", picker=api,
                               settle_s=30, poll_s=0.01)
    assert [i["id"] for i in items] == ["pic1"]


def test_list_picked_raises_human_error_when_items_never_attach():
    api = PickerAPI("a", http=NeverAttachedHTTP())
    with pytest.raises(photos.PickerItemsMissing, match="Try again"):
        photos.list_picked("a", "sess-1", picker=api,
                           settle_s=0.05, poll_s=0.01)


def test_list_picked_reraises_non_404_errors():
    class BoomHTTP(FakeHTTP):
        def get(self, url, params=None, stream=False):
            if "mediaItems" in url and "sessions" in url:
                raise RuntimeError("connection reset")
            return super().get(url, params=params, stream=stream)

    api = PickerAPI("a", http=BoomHTTP())
    with pytest.raises(RuntimeError, match="connection reset"):
        photos.list_picked("a", "sess-1", picker=api,
                           settle_s=0.05, poll_s=0.01)
