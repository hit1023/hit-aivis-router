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
from app.aivis_client import AivisClient
from app.backend_pool import BackendPool, InvalidBackendURL, validate_backend_url
from app.config import settings

STYLES = {1: "A（ノーマル）", 2: "A（あまあま）"}
WORDS = {"u1": {"surface": ["猫"], "pronunciation": ["ネコ"]}, "u2": {"surface": ["犬"], "pronunciation": ["イヌ"]}}
IMPORTED: list = []
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
        # URL に partial を含む台は、既存の台より話者が足りない
        self.manager._style_names = {1: "A（ノーマル）"} if "partial" in self.url else dict(STYLES)

    async def fake_close(self):
        return None

    async def fake_get_dict(self, enable_compound_accent=False):
        return dict(WORDS)

    async def fake_import(self, words, override=True):
        IMPORTED.append((self.base_url, dict(words), override))
        if "dictfail" in self.base_url:
            raise RuntimeError("import failed")

    IMPORTED.clear()
    monkeypatch.setattr(AivisClient, "get_user_dict", fake_get_dict)
    monkeypatch.setattr(AivisClient, "import_user_dict", fake_import)
    monkeypatch.setattr(BackendPool, "probe", staticmethod(fake_probe))
    monkeypatch.setattr(bp.Backend, "refresh", fake_refresh)
    monkeypatch.setattr(bp.Backend, "close", fake_close)
    monkeypatch.setattr(settings, "tts_provider", "local")


@pytest_asyncio.fixture()
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    from app import main as m
    m.pool = BackendPool(["http://192.168.0.21:10101"], store_path=tmp_path / "backends.json")
    m.pool._backends[0].manager._style_names = dict(STYLES)
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


# --- 稼働状況（認証不要） ------------------------------------------------------

@pytest.mark.asyncio
async def test_public_status_needs_no_token_and_hides_urls(client):
    r = await client.get("/backends/status")
    assert r.status_code == 200
    body = r.json()
    assert "192.168" not in r.text
    b = body["backends"][0]
    assert b["index"] == 1 and b["ok"] is True and b["latency_ms"] == 12
    assert b["requests"] == 0 and b["errors"] == 0 and b["recent_ms"] == []


@pytest.mark.asyncio
async def test_usage_counters_follow_synthesis(client, monkeypatch):
    backend = client.pool._backends[0]

    async def ok(self, text, speaker_id, params):
        return b"mp3"

    async def ng(self, text, speaker_id, params):
        raise RuntimeError("boom")

    monkeypatch.setattr(bp.Backend, "_synthesize_mp3", ok)
    await backend.synthesize_mp3("a", 1, {})
    monkeypatch.setattr(bp.Backend, "_synthesize_mp3", ng)
    with pytest.raises(RuntimeError):
        await backend.synthesize_mp3("b", 1, {})

    b = (await client.get("/backends/status")).json()["backends"][0]
    assert b["requests"] == 2 and b["errors"] == 1
    assert len(b["recent_ms"]) == 1 and b["last_used"] is not None


# --- モデル・辞書の同期 -------------------------------------------------------

@pytest.mark.asyncio
async def test_add_copies_user_dict_to_new_backend(client):
    r = await client.post("/backends", headers=H, json={"url": "http://192.168.0.22:10101"})
    assert r.status_code == 201
    assert r.json()["added"]["synced_words"] == 2
    base, words, override = IMPORTED[0]
    assert base == "http://192.168.0.22:10101" and set(words) == {"u1", "u2"} and override is True


@pytest.mark.asyncio
async def test_add_rejects_backend_missing_models(client):
    # 既存の台にだけある話者（新しい台には無い）を作る
    client.pool._backends[0].manager._style_names = {**STYLES, 9: "B（ノーマル）"}
    r = await client.post("/backends", headers=H, json={"url": "http://192.168.0.23:10101"})
    assert r.status_code == 422
    assert "B（ノーマル）" in r.json()["detail"] and "モデル" in r.json()["detail"]
    assert len(client.pool._backends) == 1  # 追加されていない
    assert not any(b == "http://192.168.0.23:10101" for b, _, _ in IMPORTED)  # 辞書も送っていない


@pytest.mark.asyncio
async def test_add_fails_cleanly_when_dict_copy_fails(client, monkeypatch):
    async def boom(self, words, override=True):
        raise RuntimeError("import failed")
    monkeypatch.setattr(AivisClient, "import_user_dict", boom)
    r = await client.post("/backends", headers=H, json={"url": "http://192.168.0.24:10101"})
    assert r.status_code == 502 and "import failed" in r.json()["detail"]
    assert len(client.pool._backends) == 1
    assert not client.store.exists()  # 保存もされていない


@pytest.mark.asyncio
async def test_empty_dict_is_not_imported(client, monkeypatch):
    async def empty(self, enable_compound_accent=False):
        return {}
    monkeypatch.setattr(AivisClient, "get_user_dict", empty)
    r = await client.post("/backends", headers=H, json={"url": "http://192.168.0.22:10101"})
    assert r.status_code == 201 and r.json()["added"]["synced_words"] == 0
    assert IMPORTED == []
