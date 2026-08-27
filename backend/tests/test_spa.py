"""SPA static serving.

vite copies public/ into dist's ROOT, while only dist/assets is mounted as
StaticFiles -- so favicon.svg and icons.svg reach the browser through the
fallback route or not at all.
"""

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod


@pytest.fixture
def spa_client(tmp_path, monkeypatch):
    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Cormorant</title>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("export default 1;\n", encoding="utf-8")
    monkeypatch.setattr(main_mod, "REPO_ROOT", tmp_path)
    # Deliberately not a context manager: entering one runs the lifespan and
    # starts the workers, and AuthMiddleware returns early for any path outside
    # /api/, so nothing here needs a database.
    return TestClient(main_mod.create_app())


def test_public_dir_file_keeps_its_own_content_type(spa_client):
    r = spa_client.get("/favicon.svg")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in r.text


def test_deep_link_still_falls_back_to_index(spa_client):
    r = spa_client.get("/settings")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Cormorant" in r.text


def test_missing_file_falls_back_rather_than_404(spa_client):
    """A client-side route and a typo'd asset are indistinguishable here."""
    r = spa_client.get("/nope.svg")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_api_path_is_not_swallowed_by_the_fallback(spa_client):
    """401, not 404: AuthMiddleware answers before routing reaches the
    fallback. Either way the shell must never stand in for an API route --
    a 200 page where JSON was expected is the failure worth pinning."""
    r = spa_client.get("/api/definitely-not-a-route")

    assert r.status_code == 401
    assert not r.headers["content-type"].startswith("text/html")


def test_traversal_cannot_escape_dist(spa_client, tmp_path):
    (tmp_path / "secret.txt").write_text("SHOULD-NOT-LEAK", encoding="utf-8")

    r = spa_client.get("/%2e%2e%2f%2e%2e%2fsecret.txt")

    assert "SHOULD-NOT-LEAK" not in r.text
