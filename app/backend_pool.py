import asyncio
import itertools
import logging
from dataclasses import dataclass, field

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
            # Bug3修正: バックエンドの実際の状態を同期してからアンロード判定
            await asyncio.gather(
                *(b.manager.refresh() for b in self._backends),
                return_exceptions=True,
            )
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

    async def install_model(self, filename: str, data: bytes) -> list[dict]:
        """aivmxファイルを全バックエンドにインストールする。各バックエンドの結果を返す。"""
        results = []
        for b in self._backends:
            try:
                await b.client.install_model(filename, data)
                results.append({"backend_url": b.url, "success": True})
                logger.info("Model installed on %s", b.url)
            except Exception as exc:
                results.append({"backend_url": b.url, "success": False, "error": str(exc)})
                logger.error("Model install failed on %s: %s", b.url, exc)
        await asyncio.gather(*(b.manager.refresh() for b in self._backends), return_exceptions=True)
        return results

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

    # ------------------------------------------------------------------
    # ユーザー辞書
    # ------------------------------------------------------------------

    async def get_user_dict(self, enable_compound_accent: bool = False) -> dict:
        """最初のバックエンドからユーザー辞書を取得する。"""
        return await self._backends[0].client.get_user_dict(enable_compound_accent)

    async def add_user_dict_word(
        self,
        surface: list[str],
        pronunciation: list[str],
        accent_type: list[int],
        word_type: str = "PROPER_NOUN",
        priority: int = 5,
    ) -> str:
        """全バックエンドに単語を追加し、最初のバックエンドから返された word_uuid を返す。"""
        word_uuid: str | None = None
        for i, b in enumerate(self._backends):
            result = await b.client.add_user_dict_word(
                surface, pronunciation, accent_type, word_type, priority
            )
            if i == 0:
                word_uuid = result
        assert word_uuid is not None
        return word_uuid

    async def update_user_dict_word(
        self,
        word_uuid: str,
        surface: list[str],
        pronunciation: list[str],
        accent_type: list[int],
        word_type: str = "PROPER_NOUN",
        priority: int = 5,
    ) -> None:
        """全バックエンドの単語を更新する。"""
        await asyncio.gather(
            *(
                b.client.update_user_dict_word(
                    word_uuid, surface, pronunciation, accent_type, word_type, priority
                )
                for b in self._backends
            ),
            return_exceptions=False,
        )

    async def delete_user_dict_word(self, word_uuid: str) -> None:
        """全バックエンドから単語を削除する。"""
        await asyncio.gather(
            *(b.client.delete_user_dict_word(word_uuid) for b in self._backends),
            return_exceptions=False,
        )

    async def close(self) -> None:
        await asyncio.gather(*(b.client.close() for b in self._backends))
