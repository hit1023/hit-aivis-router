"""アイドルで自動アンロードしたモデルを、ウォームアップ巡回が読み直さないことの確認。"""
import asyncio

from app.model_manager import ModelManager


class FakeClient:
    def __init__(self):
        self.loaded: set[str] = set()
        self.load_calls = 0

    async def load_model(self, uuid):
        self.loaded.add(uuid)
        self.load_calls += 1

    async def unload_model(self, uuid):
        self.loaded.discard(uuid)


def _manager():
    m = ModelManager(FakeClient(), idle_timeout=0)
    m._style_to_model = {1: "uuid-a"}
    return m


def test_idle_unload_marks_model_and_use_clears_it():
    async def run():
        m = _manager()
        await m.ensure_loaded(1)
        assert m._loaded["uuid-a"]
        await m.unload_idle()  # idle_timeout=0 → 即アンロード
        assert not m._loaded["uuid-a"]
        assert "uuid-a" in m.idle_unloaded  # 巡回はこれを見て読み直さない
        await m.ensure_loaded(1)  # 実際に使われたら通常管理に戻る
        assert "uuid-a" not in m.idle_unloaded
        assert m._loaded["uuid-a"]

    asyncio.run(run())


def test_warmup_default_is_off():
    from app.config import Settings

    assert Settings().warmup_on_start is False
