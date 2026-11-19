from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import main


async def test_spa_revalidates_html_on_navigation(monkeypatch, tmp_path):
    index = tmp_path / "index.html"
    index.write_text("<html>Dashboard</html>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-hash.js").write_text("export {};", encoding="utf-8")
    monkeypatch.delenv("NOUS_DISABLE_FRONTEND_MOUNT")
    monkeypatch.setattr(main, "_frontend_dist_dir", lambda: tmp_path)

    app = FastAPI()
    main._mount_frontend(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/", "/dashboard", "/dashboard/", "/index.html"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.text == "<html>Dashboard</html>"
            assert response.headers["cache-control"] == "no-cache"

        asset = await client.get("/assets/index-hash.js")
        assert asset.status_code == 200
        assert "cache-control" not in asset.headers
        assert (await client.get("/api/unknown")).status_code == 404
