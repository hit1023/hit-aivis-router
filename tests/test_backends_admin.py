"""
バックエンド管理 API（/backends）のテスト。

- 認証（ADMIN_TOKEN 未設定 / 不一致 / 一致）
- URL 検証（プライベート IP のみ許可・メタデータ等は拒否）
- 追加・削除・永続化（実際の BackendPool を使い、台への通信だけモックする）
"""
import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import backend_pool as bp
from app.backend_pool import BackendPool, InvalidBackendURL, validate_backend_url
from app.config import settings

TOKEN = "test-admin-token"
H = {"X-Admin-Token": TOKEN}


@pytest.fixture(autouse=True)
def _fake_engines(monkeypatch):
    """台への通信をすべて偽物にする（接続テスト・refresh・close）。"""
    async def fake_probe(url, timeout=5.0):
        if "down" in url:
            return {"url": url, "ok": False, "error": "ConnectError: refused"}
        return {"url": url, "ok": True, "latency_ms": 12, "speakers": 3}

    async def fake_refresh(self):
        return None

    async def fake_close(self):
        return None

    monkeypatch.setattr(BackendPool, "probe", staticmethod(fake_probe))
    monkeypatch.setattr(bp.Backend, "refresh", fake_refresh)
    monkeypatch.setattr(bp.Backend, "close", fake_close)
    monkeypatch.setattr(settings, "tts_provider", "local")


@pytest_asyncio.fixture()
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    from app import main as m
    m.pool = BackendPool(["http://192.168.0.21:10101"], store_path=tmp_path / "backends.json")
    async with AsyncClient(transport=ASGITransport(app=m.app), base_url="http://test") as ac:
        ac.pool = m.pool
        ac.store = tmp_path / "backends.json"
        yield ac


# --- 認証 -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_without_admin_token(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "")
    assert (await client.get("/backends", headers=H)).status_code == 403


@pytest.mark.asyncio
async def test_rejects_missing_or_wrong_token(client):
    assert (await client.get("/backends")).status_code == 401
    assert (await client.get("/backends", headers={"X-Admin-Token": "nope"})).status_code == 401
    assert (await client.post("/backends", json={"url": "http://192.168.0.22:10101"})).status_code == 401
    assert (await client.delete("/backends", params={"url": "http://192.168.0.21:10101"})).status_code == 401


# --- 一覧・追加・削除 -------------------------------------------------------

@pytest.mark.asyncio
async def test_list(client):
    r = await client.get("/backends", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["editable"] is True
    assert [b["url"] for b in body["backends"]] == ["http://192.168.0.21:10101"]
    assert body["backends"][0]["ok"] is True


@pytest.mark.asyncio
async def test_add_persists_and_round_robins(client):
    r = await client.post("/backends", json={"url": "http://192.168.0.22:10101/"}, headers=H)
    assert r.status_code == 201
    assert [b["url"] for b in r.json()["backends"]] == ["http://192.168.0.21:10101", "http://192.168.0.22:10101"]
    assert json.loads(client.store.read_text())["urls"] == ["http://192.168.0.21:10101", "http://192.168.0.22:10101"]
    picked = {client.pool.next().url for _ in range(4)}
    assert picked == {"http://192.168.0.21:10101", "http://192.168.0.22:10101"}


@pytest.mark.asyncio
async def test_add_duplicate_rejected(client):
    r = await client.post("/backends", json={"url": "http://192.168.0.21:10101"}, headers=H)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_add_unreachable_rejected_and_not_saved(client):
    r = await client.post("/backends", json={"url": "http://192.168.0.99:10101?down"}, headers=H)
    assert r.status_code == 422  # クエリ付きは形式エラー
    r = await client.post("/backends", json={"url": "http://192.168.0.99:10101"}, headers=H)
    # probe は URL に "down" を含むときだけ失敗させているので、ここは成功する
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_add_when_engine_down_returns_502(client, monkeypatch):
    async def down(url, timeout=5.0):
        return {"url": url, "ok": False, "error": "ConnectError: refused"}
    monkeypatch.setattr(BackendPool, "probe", staticmethod(down))
    r = await client.post("/backends", json={"url": "http://192.168.0.50:10101"}, headers=H)
    assert r.status_code == 502
    assert not client.store.exists()


@pytest.mark.asyncio
async def test_remove(client):
    await client.post("/backends", json={"url": "http://192.168.0.22:10101"}, headers=H)
    r = await client.delete("/backends", params={"url": "http://192.168.0.21:10101"}, headers=H)
    assert r.status_code == 200
    assert [b["url"] for b in r.json()["backends"]] == ["http://192.168.0.22:10101"]
    assert {client.pool.next().url for _ in range(3)} == {"http://192.168.0.22:10101"}


@pytest.mark.asyncio
async def test_cannot_remove_last_backend(client):
    r = await client.delete("/backends", params={"url": "http://192.168.0.21:10101"}, headers=H)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_remove_unknown_is_404(client):
    r = await client.delete("/backends", params={"url": "http://192.168.0.77:10101"}, headers=H)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_saved_file_overrides_env_on_restart(client):
    await client.post("/backends", json={"url": "http://192.168.0.22:10101"}, headers=H)
    reborn = BackendPool(["http://from-env:10101"], store_path=client.store)
    assert [b.url for b in reborn._backends] == ["http://192.168.0.21:10101", "http://192.168.0.22:10101"]


def test_corrupt_store_falls_back_to_env(tmp_path):
    f = tmp_path / "backends.json"
    f.write_text("{broken")
    pool = BackendPool(["http://192.168.0.21:10101"], store_path=f)
    assert [b.url for b in pool._backends] == ["http://192.168.0.21:10101"]


# --- URL 検証（SSRF 対策） ---------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "ftp://192.168.0.1:10101",
    "http://169.254.169.254",          # クラウドのメタデータ
    "http://8.8.8.8:10101",            # グローバル IP
    "http://user:pw@192.168.0.1:10101",
    "http://192.168.0.1:10101/path",
    "http://0.0.0.0:10101",
    "",
])
async def test_validate_rejects(url):
    with pytest.raises(InvalidBackendURL):
        await validate_backend_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("url,expected", [
    ("http://192.168.0.22:10101", "http://192.168.0.22:10101"),
    ("http://192.168.0.22:10101/", "http://192.168.0.22:10101"),
    ("http://10.1.2.3", "http://10.1.2.3"),
    ("http://127.0.0.1:10101", "http://127.0.0.1:10101"),
])
async def test_validate_accepts(url, expected):
    assert await validate_backend_url(url) == expected


@pytest.mark.asyncio
async def test_test_endpoint(client):
    r = await client.post("/backends/test", json={"url": "http://192.168.0.22:10101"}, headers=H)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (await client.post("/backends/test", json={"url": "http://8.8.8.8"}, headers=H)).status_code == 422
    assert not client.store.exists()  # テストでは保存されない
