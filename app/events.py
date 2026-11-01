"""進捗イベントの配信（Zenoh の pub/sub）。

発行側は `publish()` で key expression にイベントを流し、購読側（SSE 用）が
受け取って接続中のブラウザへ配る。Zenoh に繋がらない/無効のときは、同じプロセス内で
直接配る（機能は落とさない）。

key expression の例:
    aivis/install/<job>/start | node | finish
"""
import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

SUBSCRIBE_KEY = "aivis/install/**"


class EventBus:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()
        self._session = None
        self._subscriber = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def zenoh_enabled(self) -> bool:
        return self._session is not None

    async def start(self, endpoint: str) -> None:
        """endpoint 例: tcp/zenoh:7447。空なら Zenoh を使わず進程内配信のみ。"""
        self._loop = asyncio.get_running_loop()
        if not endpoint:
            logger.info("Zenoh 無効（ZENOH_ENDPOINT 未設定）: 進程内でイベントを配信します")
            return
        try:
            import zenoh
            conf = zenoh.Config.from_json5(json.dumps({
                "mode": "client",
                "connect": {"endpoints": [endpoint]},
            }))
            self._session = await asyncio.to_thread(zenoh.open, conf)
            self._subscriber = self._session.declare_subscriber(SUBSCRIBE_KEY, self._on_sample)
            logger.info("Zenoh に接続しました: %s（購読 %s）", endpoint, SUBSCRIBE_KEY)
        except Exception as exc:
            self._session = None
            logger.warning("Zenoh に接続できません（進程内配信にフォールバック）: %s", exc)

    async def close(self) -> None:
        try:
            if self._subscriber is not None:
                self._subscriber.undeclare()
            if self._session is not None:
                await asyncio.to_thread(self._session.close)
        except Exception as exc:
            logger.warning("Zenoh の終了処理に失敗: %s", exc)
        self._session = None
        self._subscriber = None

    # --- 発行 ---------------------------------------------------------------

    def publish(self, key: str, data: dict) -> None:
        data = {**data, "key": key}
        if self._session is not None:
            try:
                self._session.put(key, json.dumps(data, ensure_ascii=False))
                return  # 購読コールバック経由で配られる
            except Exception as exc:
                logger.warning("Zenoh publish に失敗（進程内配信に切替）: %s", exc)
        self._fanout(data)

    # --- 購読 ---------------------------------------------------------------

    def _on_sample(self, sample) -> None:
        # Zenoh のスレッドから呼ばれる。イベントループへ渡す
        try:
            data = json.loads(sample.payload.to_string())
        except Exception:
            return
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._fanout, data)

    def _fanout(self, data: dict) -> None:
        for q in list(self._queues):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass  # 遅い購読者は取りこぼす（状態APIで追いつける）

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)


bus = EventBus()
