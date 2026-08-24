"""US-013 library API tests — seeded rows + tmp files only, no downloads."""

import asyncio
import importlib

import pytest


def _seed_item(db_mod, tmp_path, name="a.mp4", **kw):
    """Real file on disk + matching LibraryItem row. Returns item id."""
    p = tmp_path / "media" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    content = kw.pop("content", b"x" * 1000)
    p.write_bytes(content)
    fields = dict(
        file_path=str(p),
        thumbnail_path=None,
        platform=kw.pop("platform", "bilibili"),
        creator=kw.pop("creator", "alice"),
        title=kw.pop("title", "Clip A"),
        media_type=kw.pop("media_type", "video"),
        size_bytes=len(content),
    )
    fields.update(kw)

    async def _add():
        async with db_mod.async_session() as s:
            item = db_mod.models.LibraryItem(**fields)
            s.add(item)
            await s.commit()
            await s.refresh(item)
            return item.id

    return asyncio.run(_add())


@pytest.fixture
def client(authed_client, tmp_path):
    from tests.conftest import current_db

    c, stub = authed_client
    yield c, current_db(), tmp_path


# 1. list + filters + pagination + no path leakage
def test_library_list_filters_and_hides_paths(client):
    c, db, tmp = client
    _seed_item(db, tmp, name="one.mp4", platform="bilibili")
    _seed_item(
        db, tmp, name="two.mp4",
        platform="tiktok", creator="carol", content=b"y" * 10,
    )
    r = c.get("/api/library")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    assert all("file_path" not in i and "thumbnail_path" not in i for i in items)
    assert items[0]["id"] != items[1]["id"]

    r = c.get("/api/library?platform=tiktok")
    data = r.json()
    assert len(data) == 1 and data[0]["creator"] == "carol"

    r = c.get("/api/library?limit=1&offset=1")
    assert len(r.json()) == 1


# 2 + 3. streaming: full file, ranges, invalid ranges
def test_stream_full_and_ranges(client):
    c, db, tmp = client
    payload = bytes(range(256)) * 4  # 1024 bytes, known content
    iid = _seed_item(db, tmp, name="v.mp4", content=payload)

    r = c.get(f"/api/library/{iid}/stream")
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == len(payload)
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == payload

    r = c.get(f"/api/library/{iid}/stream", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-99/{len(payload)}"
    assert len(r.content) == 100

    r = c.get(f"/api/library/{iid}/stream", headers={"Range": "bytes=-100"})
    assert r.status_code == 206
    assert r.content == payload[-100:]

    r = c.get(
        f"/api/library/{iid}/stream",
        headers={"Range": f"bytes={len(payload)}-2000"},
    )
    assert r.status_code == 416

    r = c.get(f"/api/library/{iid}/stream", headers={"Range": "bytes=bogus"})
    assert r.status_code == 416


# 4. thumbnail missing -> 404, present -> served
def test_thumbnail_serving(client):
    c, db, tmp = client
    thumb = tmp / "media" / "a.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)

    iid = _seed_item(db, tmp, name="a.mp4")
    assert c.get(f"/api/library/{iid}/thumbnail").status_code == 404

    thumb.write_bytes(b"\xff\xd8fakejpeg")

    async def _attach():
        async with db.async_session() as s:
            row = await s.get(db.models.LibraryItem, iid)
            row.thumbnail_path = str(thumb)
            await s.commit()

    asyncio.run(_attach())
    r = c.get(f"/api/library/{iid}/thumbnail")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8fakejpeg"
    assert r.headers["content-type"].startswith("image/")
    assert c.get("/api/library/99999/thumbnail").status_code == 404


# 5. delete removes disk files + row
def test_delete_removes_files_and_row(client):
    c, db, tmp = client
    thumb = tmp / "media" / "a.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"t")
    iid = _seed_item(db, tmp, name="a.mp4")

    async def _attach():
        async with db.async_session() as s:
            row = await s.get(db.models.LibraryItem, iid)
            row.thumbnail_path = str(thumb)
            await s.commit()

    asyncio.run(_attach())
    target = tmp / "media" / "a.mp4"

    r = c.delete(f"/api/library/{iid}")
    assert r.status_code in (200, 204)
    assert not target.exists()
    assert not thumb.exists()
    assert c.get("/api/library").json() == []
    assert c.delete(f"/api/library/{iid}").status_code == 404


# 6. downloader-done helper: media_type mapping video/images/story/audio
async def test_write_library_item_for_job_mapping(tmp_path, monkeypatch):
    from app.services.library_writer import write_library_item_for_job

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import app.config as config

    importlib.reload(config)
    import app.db as db_mod

    importlib.reload(db_mod)
    await db_mod.init_db()

    class FakeJob:
        def __init__(self, kind, out):
            self.kind = kind
            self.output_path = str(out)
            self.platform = "tiktok"
            self.creator = "bob"
            self.title = f"Job {kind}"

    cases = []
    for kind, fname, expected in [
        ("video", "v.mp4", "video"),
        ("images", "i.jpg", "image_set"),
        ("story", "s.mp4", "video"),
        ("audio_only", "a.mp3", "audio"),
    ]:
        f = tmp_path / fname
        f.write_bytes(b"d" * 7)
        cases.append((FakeJob(kind, f), expected))

    try:
        async with db_mod.async_session() as s:
            for job, expected in cases:
                item = await write_library_item_for_job(s, job)
                assert item is not None
                assert item.media_type == expected
                assert item.size_bytes == 7
                assert item.title.startswith("Job ")
                # no duplicate write for same path
                again = await write_library_item_for_job(s, job)
                assert again is None
    finally:
        await db_mod.engine.dispose()


# downloader.run_job calls the hook after commit — smoke-check the wiring exists
def test_run_job_calls_library_writer(tmp_path, monkeypatch):
    import inspect

    import app.services.downloader as dl

    src = inspect.getsource(dl.DownloadManager.run_job)
    assert "write_library_item_for_job" in src
