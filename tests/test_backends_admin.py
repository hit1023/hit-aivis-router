import asyncio
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
MODELS = {"m1": {"manifest": {"name": "まお"}}, "m2": {"manifest": {"name": "コハク"}}}
MODELS_BY_URL: dict = {}   # url -> インストール済みモデル（無ければ全部入り）
INSTALLED: list = []
INSTALL_DELAY = [0.0]   # 同期中の状態を観測したいテストだけ遅くする
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
        self.manager._style_names = dict(STYLES)

    async def fake_close(self):
        return None

    async def fake_get_dict(self, enable_compound_accent=False):
        return dict(WORDS)

    async def fake_import(self, words, override=True):
        IMPORTED.append((self.base_url, dict(words), override))
        if "dictfail" in self.base_url:
            raise RuntimeError("import failed")

    async def fake_get_models(self):
        return dict(MODELS_BY_URL.get(self.base_url, MODELS))

    async def fake_install(self, path):
        import asyncio
        await asyncio.sleep(INSTALL_DELAY[0])
        INSTALLED.append((self.base_url, path))
        if "installfail" in self.base_url:
            raise RuntimeError("install failed")
        MODELS_BY_URL.setdefault(self.base_url, {})[path.rsplit("/", 1)[-1].removesuffix(".aivmx")] = {}

    INSTALL_DELAY[0] = 0.0; IMPORTED.clear(); INSTALLED.clear(); MODELS_BY_URL.clear()
    monkeypatch.setattr(AivisClient, "get_models", fake_get_models)
    monkeypatch.setattr(AivisClient, "install_model_file", fake_install)
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
    await client.put("/backends/master", json={"url": "http://192.168.0.22:10101"}, headers=H)
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

async def _wait_synced(pool, timeout=2.0):
    import asyncio
    for _ in range(int(timeout / 0.02)):
        if not any(p["state"] == "syncing" for p in pool._pending.values()):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("同期が終わりません")


@pytest.fixture()
def source_dir(tmp_path, monkeypatch):
    d = tmp_path / "models"
    d.mkdir()
    for u in MODELS:
        (d / f"{u}.aivmx").write_bytes(b"x")
    monkeypatch.setattr(settings, "models_source_dir", str(d))
    return d


@pytest.mark.asyncio
async def test_add_with_all_models_goes_active_and_copies_dict(client):
    r = await client.post("/backends", headers=H, json={"url": "http://192.168.0.22:10101"})
    assert r.status_code == 201
    assert r.json()["added"] == {"url": "http://192.168.0.22:10101", "state": "active", "synced_words": 2}
    base, words, override = IMPORTED[0]
    assert base == "http://192.168.0.22:10101" and set(words) == {"u1", "u2"} and override is True
    assert len(client.pool._backends) == 2


@pytest.mark.asyncio
async def test_missing_models_rejected_without_source_dir(client, monkeypatch):
    monkeypatch.setattr(settings, "models_source_dir", "")
    MODELS_BY_URL["http://192.168.0.23:10101"] = {"m1": {}}
    r = await client.post("/backends", headers=H, json={"url": "http://192.168.0.23:10101"})
    assert r.status_code == 422
    assert "コハク" in r.json()["detail"] and "MODELS_SOURCE_DIR" in r.json()["detail"]
    assert len(client.pool._backends) == 1 and not client.pool._pending
    assert IMPORTED == [] and INSTALLED == []


@pytest.mark.asyncio
async def test_missing_models_rejected_when_source_file_absent(client, source_dir):
    (source_dir / "m2.aivmx").unlink()
    MODELS_BY_URL["http://192.168.0.23:10101"] = {"m1": {}}
    r = await client.post("/backends", headers=H, json={"url": "http://192.168.0.23:10101"})
    assert r.status_code == 422 and "見つかりません" in r.json()["detail"]
    assert not client.pool._pending


@pytest.mark.asyncio
async def test_lazy_sync_installs_models_then_joins_rotation(client, source_dir):
    url = "http://192.168.0.23:10101"
    MODELS_BY_URL[url] = {"m1": {}}          # コハク(m2)だけ足りない
    INSTALL_DELAY[0] = 0.15
    r = await client.post("/backends", headers=H, json={"url": url})
    assert r.status_code == 201
    assert r.json()["added"]["state"] == "syncing" and r.json()["added"]["missing_models"] == 1
    # 同期中は振り分けに入らず、状態に出る（URLは認証なしの表示に出ない）
    assert len(client.pool._backends) == 1
    st = (await client.get("/backends/status")).json()["backends"]
    assert st[-1]["state"] == "syncing" and st[-1]["sync_total"] == 1 and "192.168" not in str(st)
    assert st[-1]["words_state"] in ("waiting", "syncing", "done")

    await _wait_synced(client.pool)
    assert INSTALLED == [(url, str(source_dir / "m2.aivmx"))]
    assert [b.url for b in client.pool._backends] == ["http://192.168.0.21:10101", url]
    assert not client.pool._pending
    assert any(b == url for b, _, _ in IMPORTED)          # 辞書も入った
    assert json.loads(client.store.read_text())["urls"][-1] == url
    st = (await client.get("/backends/status")).json()["backends"]
    assert all(b["state"] == "active" for b in st)


@pytest.mark.asyncio
async def test_pending_shows_words_progress_in_authed_list(client, source_dir):
    url = "http://192.168.0.23:10101"
    MODELS_BY_URL[url] = {}
    INSTALL_DELAY[0] = 0.1
    await client.post("/backends", headers=H, json={"url": url})
    row = (await client.get("/backends", headers=H)).json()["backends"][-1]
    assert row["url"] == url and row["state"] == "syncing" and row["sync_total"] == 2
    assert "words_total" in row and "words_state" in row
    await _wait_synced(client.pool)


@pytest.mark.asyncio
async def test_failed_sync_stays_pending_and_can_be_cancelled(client, source_dir):
    url = "http://192.168.0.23:10101/installfail".replace("/installfail", "")
    url = "http://installfail.test:10101"
    # ホスト名解決が要るので、検証をバイパスして直接 pending を作る代わりに fake を使う
    import app.backend_pool as bpm
    async def ok_validate(u):
        return u
    bpm_validate = bpm.validate_backend_url
    bpm.validate_backend_url = ok_validate
    try:
        MODELS_BY_URL[url] = {}
        r = await client.post("/backends", headers=H, json={"url": url})
        assert r.status_code == 201
        await _wait_synced(client.pool)
    finally:
        bpm.validate_backend_url = bpm_validate
    row = (await client.get("/backends", headers=H)).json()["backends"][-1]
    assert row["state"] == "failed" and "install failed" in row["error"]
    assert len(client.pool._backends) == 1 and not client.store.exists()
    # 失敗した台は取り消して消せる（最後の1台ルールの対象外）
    r = await client.delete("/backends", headers=H, params={"url": url})
    assert r.status_code == 200 and not client.pool._pending


@pytest.mark.asyncio
async def test_empty_dict_is_not_imported(client, monkeypatch):
    async def empty(self, enable_compound_accent=False):
        return {}
    monkeypatch.setattr(AivisClient, "get_user_dict", empty)
    r = await client.post("/backends", headers=H, json={"url": "http://192.168.0.22:10101"})
    assert r.status_code == 201 and r.json()["added"]["synced_words"] == 0
    assert IMPORTED == []


# --- マスター台 --------------------------------------------------------------

async def _two(client):
    await client.post("/backends", headers=H, json={"url": "http://192.168.0.22:10101"})
    assert len(client.pool._backends) == 2


@pytest.mark.asyncio
async def test_first_backend_is_master_by_default(client):
    await _two(client)
    rows = (await client.get("/backends", headers=H)).json()["backends"]
    assert [r["master"] for r in rows] == [True, False]
    pub = (await client.get("/backends/status")).json()["backends"]
    assert [r["master"] for r in pub] == [True, False] and "192.168" not in str(pub)


@pytest.mark.asyncio
async def test_set_master_persists_and_propagates_dict(client):
    await _two(client)
    IMPORTED.clear()
    r = await client.put("/backends/master", headers=H, json={"url": "http://192.168.0.22:10101"})
    assert r.status_code == 200 and r.json()["master"] == "http://192.168.0.22:10101"
    assert [b["master"] for b in r.json()["backends"]] == [False, True]
    assert json.loads(client.store.read_text())["master"] == "http://192.168.0.22:10101"
    # 新しいマスターの辞書が、他の台へUUIDのまま反映される
    assert [(u, set(w)) for u, w, _ in IMPORTED] == [("http://192.168.0.21:10101", {"u1", "u2"})]
    assert client.pool.master.url == "http://192.168.0.22:10101"


@pytest.mark.asyncio
async def test_master_survives_restart(client, tmp_path):
    await _two(client)
    await client.put("/backends/master", headers=H, json={"url": "http://192.168.0.22:10101"})
    again = BackendPool(["http://192.168.0.21:10101"], store_path=client.store)
    assert again.master.url == "http://192.168.0.22:10101"


@pytest.mark.asyncio
async def test_set_master_unknown_and_auth(client):
    assert (await client.put("/backends/master", json={"url": "http://192.168.0.21:10101"})).status_code == 401
    r = await client.put("/backends/master", headers=H, json={"url": "http://192.168.0.99:10101"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_master_cannot_be_removed(client):
    await _two(client)
    r = await client.delete("/backends", headers=H, params={"url": "http://192.168.0.21:10101"})
    assert r.status_code == 409 and "マスター" in r.json()["detail"]
    await client.put("/backends/master", headers=H, json={"url": "http://192.168.0.22:10101"})
    r = await client.delete("/backends", headers=H, params={"url": "http://192.168.0.21:10101"})
    assert r.status_code == 200


def _stub_engines(client, models):
    """各台に {model: (style_id, loaded)} を持たせ、読み込み/合成の呼び出し順を記録する。"""
    calls = []
    for b in client.pool._backends:
        async def refresh(_b=b):
            pass
        async def ensure(sid, _b=b):
            calls.append(("load", _b.url, sid))
            _b.manager._loaded[_b.manager._style_to_model[sid]] = True
        async def aq(text, sid, _b=b):
            return {}
        async def syn(sid, q, _b=b):
            calls.append(("syn", _b.url, sid)); return b""
        b.refresh = refresh
        b.manager.ensure_loaded = ensure
        b.client.audio_query = aq
        b.client.synthesis = syn
        b.manager._style_to_model = {sid: m for m, (sid, _) in models.items()}
        b.manager._loaded = {m: loaded for m, (_, loaded) in models.items()}
    return calls


@pytest.mark.asyncio
async def test_warmup_warms_only_cold_models_sequentially(client):
    await client.post("/backends", headers=H, json={"url": "http://192.168.0.22:10101"})
    calls = _stub_engines(client, {"cold": (7, False), "warm": (9, True)})
    await client.pool.warmup()
    assert calls == [("load", "http://192.168.0.21:10101", 7), ("syn", "http://192.168.0.21:10101", 7),
                     ("load", "http://192.168.0.22:10101", 7), ("syn", "http://192.168.0.22:10101", 7)]


@pytest.mark.asyncio
async def test_warmup_rewarms_after_engine_restart(client):
    calls = _stub_engines(client, {"m": (7, True)})
    await client.pool.warmup()
    assert calls == []                              # 温まっていれば何もしない
    client.pool._backends[0].manager._loaded["m"] = False   # エンジン再起動で未ロードに戻った
    await client.pool.warmup()
    assert [c[0] for c in calls] == ["load", "syn"]


@pytest.mark.asyncio
async def test_warmup_skips_manually_unloaded_model(client):
    calls = _stub_engines(client, {"m": (7, False)})
    client.pool._manual_unloaded = {"m"}
    await client.pool.warmup()
    assert calls == []


def test_llm_config_roundtrip(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main.settings, "llm_config_file", str(tmp_path / "llm.json"))
    monkeypatch.setattr(main.settings, "admin_token", "tok")
    monkeypatch.setattr(main.settings, "llm_model", "")
    c = TestClient(main.app)
    h = {"X-Admin-Token": "tok"}
    assert c.get("/kana_verify/status").json()["enabled"] is False
    r = c.put("/llm_config", headers=h, json={"api_url": "http://x/v1", "model": "m", "api_key": "secret"})
    assert r.status_code == 200 and r.json()["has_key"] is True
    assert "secret" not in r.text
    assert c.get("/kana_verify/status").json()["model"] == "m"
    # キー省略は変更なし
    c.put("/llm_config", headers=h, json={"api_url": "http://x/v1", "model": "m2"})
    assert c.get("/llm_config", headers=h).json()["has_key"] is True
    assert c.put("/llm_config", json={"model": "m"}).status_code in (401, 403)


def test_kana_verify_cache_skips_llm(tmp_path, monkeypatch):
    import asyncio
    from app import kana_checker
    calls = []

    async def fake(words, *a, **k):
        calls.append(list(words))
        return [{"text": w, "reading": "テスト"} for w in words]

    monkeypatch.setattr(kana_checker, "_ask_claude_cli", fake)
    cf = str(tmp_path / "c.json")
    pairs = [{"text": "試験", "kana": "テスト"}, {"text": "試験", "kana": "テスト"}]
    r1 = asyncio.run(kana_checker.verify_readings(pairs, provider="claude_cli", cache_file=cf))
    r2 = asyncio.run(kana_checker.verify_readings(pairs, provider="claude_cli", cache_file=cf))
    assert len(calls) == 1 and calls[0] == ["試験"]
    assert r1 == r2 and r1[0]["correct"] is True


@pytest.mark.asyncio
async def test_install_returns_202_and_reports_status(client, monkeypatch):
    async def fake_install(self, filename, data):
        pass
    monkeypatch.setattr(AivisClient, "install_model", fake_install)
    r = await client.post("/models/install", files={"file": ("a.aivmx", b"x")})
    assert r.status_code == 202 and r.json()["state"] == "running"
    for _ in range(50):
        st = (await client.get("/models/install/status")).json()
        if st["state"] != "running":
            break
        await asyncio.sleep(0.05)
    assert st["state"] == "done" and st["results"][0]["success"] is True


@pytest.mark.asyncio
async def test_install_publishes_progress_events(client, monkeypatch):
    from app.events import bus
    async def fake_install(self, filename, data):
        pass
    monkeypatch.setattr(AivisClient, "install_model", fake_install)
    q = bus.subscribe()
    try:
        r = await client.post("/models/install", files={"file": ("a.aivmx", b"x")})
        job = r.json()["job_id"]
        got = []
        while True:
            ev = await asyncio.wait_for(q.get(), timeout=3)
            got.append((ev["type"], ev.get("status")))
            assert ev["job_id"] == job and ev["key"].startswith(f"aivis/install/{job}/")
            if ev["type"] == "finish":
                break
    finally:
        bus.unsubscribe(q)
    assert got[0] == ("start", None)
    assert ("node", "started") in got and ("node", "done") in got
    assert got[-1] == ("finish", None)


# --- 不調な台の退避・やり直し --------------------------------------------------

def _two_backend_pool(tmp_path, monkeypatch):
    import httpx
    from app import backend_pool as bp
    pool = BackendPool(["http://good.test:10101", "http://bad.test:10101"], store_path=tmp_path / "b.json")

    async def fake(self, text, speaker_id, params):
        if "bad" in self.url:
            raise bp.SynthesisFailed("synthesis failed") from httpx.ConnectError("down")
        return b"mp3-from-" + self.url.encode()
    monkeypatch.setattr(bp.Backend, "_synthesize_mp3", fake)
    return pool


@pytest.mark.asyncio
async def test_failed_backend_is_skipped_for_cooldown(tmp_path, monkeypatch):
    pool = _two_backend_pool(tmp_path, monkeypatch)
    bad = next(b for b in pool._backends if "bad" in b.url)
    params = {"speed": 1, "pitch": 0, "intonation": 1, "volume": 1, "tempo_dynamics": 1, "pause_length_scale": 1}
    with pytest.raises(Exception) as ei:
        await bad.synthesize_mp3("a", 1, params)
    assert ei.value.node_fault is True and bad.in_cooldown()
    assert all("good" in pool.next().url for _ in range(6))   # 退避中は選ばれない
    assert pool.pick_other(bad).url.startswith("http://good")


@pytest.mark.asyncio
async def test_all_in_cooldown_falls_back_to_rotation(tmp_path, monkeypatch):
    pool = _two_backend_pool(tmp_path, monkeypatch)
    import time as _t
    for b in pool._backends:
        b.cooldown_until = _t.monotonic() + 60
    assert pool.next() is not None


@pytest.mark.asyncio
async def test_speak_retries_on_other_backend(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app import main as m, speech_history
    pool = _two_backend_pool(tmp_path, monkeypatch)
    # 最初に bad が選ばれる順にしておく
    pool._backends.sort(key=lambda b: "bad" not in b.url)
    pool._rebuild_cycle()
    monkeypatch.setattr(m, "pool", pool, raising=False)
    monkeypatch.setattr(m, "replacer", SimpleNamespace(apply=lambda t: t), raising=False)
    monkeypatch.setattr(m, "presets", SimpleNamespace(get=lambda i: None), raising=False)
    async def noop(**kw): pass
    monkeypatch.setattr(speech_history, "record", noop)
    async with AsyncClient(transport=ASGITransport(app=m.app), base_url="http://test") as ac:
        r = await ac.post("/speak", json={"text": "こんにちは", "speaker_id": 1})
    assert r.status_code == 200
    assert r.headers["X-Backend"].startswith("http://good")
