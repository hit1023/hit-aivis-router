"""台ごとの同時実行数の制限（backend_concurrency）のテスト。"""
import asyncio

import pytest

from app import backend_pool as bp
from app.backend_pool import Backend, QueueTimeout
from app.config import settings


def _backend(monkeypatch, hold: float = 0.05):
    b = Backend("http://192.168.0.21:10101")
    state = {"now": 0, "peak": 0}

    async def fake_synth(self, text, speaker_id, params):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(hold)
        state["now"] -= 1
        return b"mp3"

    monkeypatch.setattr(Backend, "_synthesize_mp3", fake_synth)
    return b, state


@pytest.mark.asyncio
async def test_unlimited_by_default(monkeypatch):
    monkeypatch.setattr(settings, "backend_concurrency", 0)
    b, state = _backend(monkeypatch)
    await asyncio.gather(*(b.synthesize_mp3("a", 1, {}) for _ in range(5)))
    assert state["peak"] == 5


@pytest.mark.asyncio
async def test_limit_caps_concurrency_and_all_finish(monkeypatch):
    monkeypatch.setattr(settings, "backend_concurrency", 2)
    monkeypatch.setattr(settings, "backend_queue_timeout", 5.0)
    b, state = _backend(monkeypatch)
    res = await asyncio.gather(*(b.synthesize_mp3("a", 1, {}) for _ in range(6)))
    assert res == [b"mp3"] * 6
    assert state["peak"] == 2
    assert b.inflight == 0 and b.waiting == 0


@pytest.mark.asyncio
async def test_queue_timeout_not_a_node_fault(monkeypatch):
    monkeypatch.setattr(settings, "backend_concurrency", 1)
    monkeypatch.setattr(settings, "backend_queue_timeout", 0.05)
    b, state = _backend(monkeypatch, hold=0.3)
    first = asyncio.create_task(b.synthesize_mp3("a", 1, {}))
    await asyncio.sleep(0.01)
    with pytest.raises(QueueTimeout):
        await b.synthesize_mp3("b", 1, {})
    assert not b.in_cooldown()          # 台の不調扱いにしない
    assert b.waiting == 0
    assert await first == b"mp3"
    assert b.inflight == 0
    # 待たされた分が詰まりを残さず、その後は通る
    assert await b.synthesize_mp3("c", 1, {}) == b"mp3"


@pytest.mark.asyncio
async def test_speak_returns_503_with_retry_after(monkeypatch, tmp_path):
    from httpx import ASGITransport, AsyncClient
    from app import main as m
    from app.backend_pool import BackendPool

    async def fake_refresh(self):
        self.manager._style_names = {1: "A"}
    monkeypatch.setattr(bp.Backend, "refresh", fake_refresh)
    monkeypatch.setattr(settings, "tts_provider", "local")
    monkeypatch.setattr(settings, "backend_concurrency", 1)
    monkeypatch.setattr(settings, "backend_queue_timeout", 0.05)

    async def slow(self, text, speaker_id, params):
        await asyncio.sleep(0.3)
        return b"mp3"
    monkeypatch.setattr(bp.Backend, "_synthesize_mp3", slow)

    from app.text_replacer import TextReplacer
    from app.speaker_preset import SpeakerPresetManager
    m.replacer = TextReplacer(tmp_path / "r.json", seed_file=tmp_path / "none.json")
    m.presets = SpeakerPresetManager(tmp_path / "p.json")
    async def _noop(**kw): return None
    monkeypatch.setattr(m.speech_history, "record", _noop)
    m.pool = BackendPool(["http://192.168.0.21:10101"], store_path=tmp_path / "backends.json")
    m.pool._backends[0].manager._style_names = {1: "A"}
    async with AsyncClient(transport=ASGITransport(app=m.app), base_url="http://test") as ac:
        t1 = asyncio.create_task(ac.post("/speak", json={"text": "あ", "speaker_id": 1}))
        await asyncio.sleep(0.02)
        r2 = await ac.post("/speak", json={"text": "い", "speaker_id": 1})
        assert r2.status_code == 503
        assert r2.headers.get("retry-after") == "5"
        r1 = await t1
        assert r1.status_code == 200
