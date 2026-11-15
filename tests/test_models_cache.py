"""GET /aivm_models（実測100MB超）の扱い: 必要項目だけ残す・キャッシュする・同時取得を束ねる。"""
import asyncio
import json

import httpx

from app.aivis_client import AivisClient, _slim_models

HEAVY = {
    "uuid-a": {
        "is_loaded": True,
        "manifest": {
            "name": "モデルA",
            "icon": "data:image/png;base64," + "A" * 5000,  # 巨大な埋め込み画像のつもり
            "speakers": [
                {"uuid": "spk-1", "name": "話者1", "local_id": 0, "icon": "B" * 5000, "styles": [{"voice_samples": ["C" * 5000]}]}
            ],
        },
    },
    "uuid-b": {"is_loaded": False, "manifest": {"name": "モデルB", "speakers": []}},
}


def test_slim_keeps_only_needed_fields():
    slim = _slim_models(json.dumps(HEAVY).encode())
    assert slim == {
        "uuid-a": {"is_loaded": True, "manifest": {"name": "モデルA", "speakers": [{"uuid": "spk-1", "name": "話者1", "local_id": 0}]}},
        "uuid-b": {"is_loaded": False, "manifest": {"name": "モデルB", "speakers": []}},
    }
    assert len(json.dumps(slim)) < len(json.dumps(HEAVY)) / 10


def _client_with_counter():
    calls = {"models": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/aivm_models":
            calls["models"] += 1
            return httpx.Response(200, json=HEAVY)
        calls["post"] += 1
        return httpx.Response(204)

    c = AivisClient("http://engine.test")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c, calls


def test_cache_and_concurrent_dedupe_and_invalidate():
    async def run():
        c, calls = _client_with_counter()
        # 同時に10回呼んでも取得は1回だけ
        await asyncio.gather(*(c.get_models() for _ in range(10)))
        assert calls["models"] == 1
        await c.get_models()  # キャッシュ内
        assert calls["models"] == 1
        # ロードの結果はキャッシュに直接反映し、一覧は取り直さない
        await c.load_model("uuid-b")
        assert (await c.get_models())["uuid-b"]["is_loaded"] is True
        c._mark_loaded("uuid-a", False)  # unload_model が行う反映（別テストが unload_model を汚染するため直接呼ぶ）
        assert (await c.get_models())["uuid-a"]["is_loaded"] is False
        assert calls["models"] == 1
        # インストール/アンインストールは対応関係が変わるので捨てる
        await c.install_model("x.aivmx", b"x")
        await c.get_models()
        assert calls["models"] == 2
        await c.get_models(max_age=0)  # 強制再取得
        assert calls["models"] == 3

    asyncio.run(run())
