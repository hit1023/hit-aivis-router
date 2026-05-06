"""
テキスト置換ルール API のテスト。
"""
import json
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# TextReplacer 単体テスト
# ---------------------------------------------------------------------------

class TestTextReplacer:
    def test_apply_empty_rules(self, tmp_path):
        from app.text_replacer import TextReplacer
        r = TextReplacer(tmp_path / "rules.json")
        assert r.apply("Mumon") == "Mumon"

    def test_add_and_apply(self, tmp_path):
        from app.text_replacer import TextReplacer
        r = TextReplacer(tmp_path / "rules.json")
        r.add("Mumon", "ミューモン")
        assert r.apply("Mumon です") == "ミューモン です"

    def test_longest_match_first(self, tmp_path):
        from app.text_replacer import TextReplacer
        r = TextReplacer(tmp_path / "rules.json")
        r.add("Mu", "ム")
        r.add("Mumon", "ミューモン")
        # 長い "Mumon" が優先されるべき
        assert r.apply("Mumon") == "ミューモン"

    def test_remove_existing(self, tmp_path):
        from app.text_replacer import TextReplacer
        r = TextReplacer(tmp_path / "rules.json")
        r.add("Mumon", "ミューモン")
        assert r.remove("Mumon") is True
        assert r.apply("Mumon") == "Mumon"

    def test_remove_nonexistent_returns_false(self, tmp_path):
        from app.text_replacer import TextReplacer
        r = TextReplacer(tmp_path / "rules.json")
        assert r.remove("存在しない") is False

    def test_persistence(self, tmp_path):
        from app.text_replacer import TextReplacer
        path = tmp_path / "rules.json"
        r1 = TextReplacer(path)
        r1.add("Mumon", "ミューモン")

        # 新しいインスタンスで読み込み
        r2 = TextReplacer(path)
        assert r2.apply("Mumon") == "ミューモン"

    def test_add_empty_src_raises(self, tmp_path):
        from app.text_replacer import TextReplacer
        r = TextReplacer(tmp_path / "rules.json")
        with pytest.raises(ValueError):
            r.add("", "dst")

    def test_get_all_returns_copy(self, tmp_path):
        from app.text_replacer import TextReplacer
        r = TextReplacer(tmp_path / "rules.json")
        r.add("A", "B")
        d = r.get_all()
        d["X"] = "Y"
        assert "X" not in r.get_all()

    def test_dst_can_be_empty_string(self, tmp_path):
        """置換後テキストが空文字列でも登録可能（削除用途）。"""
        from app.text_replacer import TextReplacer
        r = TextReplacer(tmp_path / "rules.json")
        r.add("Mumon", "")
        assert r.apply("Mumon です") == " です"


# ---------------------------------------------------------------------------
# API エンドポイントテスト
# ---------------------------------------------------------------------------

def _make_app(tmp_path):
    """テスト用 FastAPI アプリをビルド（TextReplacer を tmp_path に向ける）。"""
    import app.main as main_module
    from app.text_replacer import TextReplacer

    replacer = TextReplacer(tmp_path / "rules.json")

    mock_pool = MagicMock()
    mock_pool.next.return_value = MagicMock(
        manager=AsyncMock(ensure_loaded=AsyncMock()),
        client=AsyncMock(
            audio_query=AsyncMock(return_value={}),
            synthesis=AsyncMock(return_value=b"RIFF...."),
        ),
    )
    mock_pool.get_user_dict = AsyncMock(return_value={})

    original_pool = getattr(main_module, "pool", None)
    original_replacer = getattr(main_module, "replacer", None)

    main_module.pool = mock_pool
    main_module.replacer = replacer

    yield main_module.app

    main_module.pool = original_pool
    main_module.replacer = original_replacer


class TestTextReplacementsApi:
    @pytest_asyncio.fixture
    async def client(self, tmp_path):
        import app.main as main_module
        from app.text_replacer import TextReplacer

        replacer = TextReplacer(tmp_path / "rules.json")
        main_module.replacer = replacer

        async with AsyncClient(
            transport=ASGITransport(app=main_module.app),
            base_url="http://test",
        ) as c:
            yield c

    async def test_get_empty(self, client):
        res = await client.get("/text_replacements")
        assert res.status_code == 200
        assert res.json() == {}

    async def test_add_and_get(self, client):
        res = await client.post("/text_replacements", json={"src": "Mumon", "dst": "ミューモン"})
        assert res.status_code == 200
        assert res.json() == {"src": "Mumon", "dst": "ミューモン"}

        res = await client.get("/text_replacements")
        assert res.json() == {"Mumon": "ミューモン"}

    async def test_add_empty_src_returns_422(self, client):
        res = await client.post("/text_replacements", json={"src": "", "dst": "x"})
        assert res.status_code == 422

    async def test_delete_existing(self, client):
        await client.post("/text_replacements", json={"src": "Mumon", "dst": "ミューモン"})
        res = await client.delete("/text_replacements", params={"src": "Mumon"})
        assert res.status_code == 204

        res = await client.get("/text_replacements")
        assert res.json() == {}

    async def test_delete_nonexistent_returns_404(self, client):
        res = await client.delete("/text_replacements", params={"src": "存在しない"})
        assert res.status_code == 404

    async def test_overwrite_existing_rule(self, client):
        await client.post("/text_replacements", json={"src": "Mumon", "dst": "ミューモン"})
        await client.post("/text_replacements", json={"src": "Mumon", "dst": "むもん"})
        res = await client.get("/text_replacements")
        assert res.json()["Mumon"] == "むもん"
