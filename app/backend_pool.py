import asyncio
import itertools
import logging
from dataclasses import dataclass, field

import httpx

from .aivis_client import AivisClient
from .model_manager import ModelManager

logger = logging.getLogger(__name__)


@dataclass
class Backend:
    url: str
    client: AivisClient = field(init=False)
    manager: ModelManager = field(init=False)

    def __post_init__(self):
        self.client = AivisClient(self.url)
        self.manager = ModelManager(self.client)


class BackendPool:
    """Round-robin pool of AIVIS backend servers."""

    def __init__(self, urls: list[str], idle_timeout: int = 600):
        self._backends = [Backend(url) for url in urls]
        for b in self._backends:
            b.manager.idle_timeout = idle_timeout
        self._cycle = itertools.cycle(self._backends)
        self._idle_timeout = idle_timeout

    def next(self) -> Backend:
        return next(self._cycle)

    async def initialize(self) -> None:
        await asyncio.gather(*(b.manager.refresh() for b in self._backends))

    async def start_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await asyncio.gather(
                *(b.manager.unload_idle() for b in self._backends),
                return_exceptions=True,
            )

    async def get_speakers(self) -> list[dict]:
        """Proxy /speakers from the first backend."""
        return await self._backends[0].client.get_speakers()

    async def get_models_status(self) -> list[dict]:
        """各バックエンドのモデルVRAMロード状態を返す。"""
        results = []
        for b in self._backends:
            try:
                raw = await b.client.get_models()
            except Exception as exc:
                logger.warning("Failed to fetch models from %s: %s", b.url, exc)
                raw = {}
            for uuid, info in raw.items():
                manifest = info.get("manifest", {})
                speakers = manifest.get("speakers", [])
                results.append({
                    "backend_url": b.url,
                    "aivm_uuid": uuid,
                    "model_name": manifest.get("name", uuid),
                    "is_loaded": info.get("is_loaded", False),
                    "speakers": [
                        {"name": s.get("name", ""), "local_id": s.get("local_id")}
                        for s in speakers
                    ],
                })
        return results

    async def install_model_from_url(self, url: str) -> None:
        """全バックエンドに同じモデルをURLからインストールする。
        AIVISサーバーがURLに直接アクセスできる場合はそちらに任せ、
        できない場合はラッパーがダウンロードして転送する。
        """
        try:
            await asyncio.gather(
                *(b.client.install_model_from_url(url) for b in self._backends)
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 500:
                # AIVISサーバーがURLに直接アクセスできない → ラッパーが代理ダウンロード
                logger.info("Direct URL install failed, falling back to proxy download")
                await self._proxy_install(url)
            else:
                raise
        await asyncio.gather(*(b.manager.refresh() for b in self._backends))

    async def _proxy_install(self, url: str) -> None:
        """ラッパーがURLからダウンロードし、全バックエンドにアップロードする。"""
        filename = url.split("?")[0].rstrip("/").split("/")[-1]
        async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as dl:
            r = await dl.get(url)
            r.raise_for_status()
            data = r.content
        logger.info("Proxy downloaded %s (%d bytes), uploading to backends", filename, len(data))
        await asyncio.gather(
            *(b.client.install_model_from_bytes(filename, data) for b in self._backends)
        )

    async def force_unload(self, aivm_uuid: str) -> list[str]:
        """指定UUIDのモデルを全バックエンドから強制アンロード。アンロードしたバックエンドURLのリストを返す。"""
        unloaded = []
        for b in self._backends:
            async with b.manager._lock:
                if b.manager._loaded.get(aivm_uuid):
                    await b.client.unload_model(aivm_uuid)
                    b.manager._loaded[aivm_uuid] = False
                    b.manager._last_used.pop(aivm_uuid, None)
                    unloaded.append(b.url)
        return unloaded

    async def close(self) -> None:
        await asyncio.gather(*(b.client.close() for b in self._backends))
