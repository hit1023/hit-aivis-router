import asyncio
import logging
import time
from typing import Optional

from .aivis_client import AivisClient

logger = logging.getLogger(__name__)


class ModelManager:
    """
    Tracks which AIVIS models are loaded for one backend server.
    Maps speaker style IDs to model UUIDs and auto-unloads idle models.
    """

    def __init__(self, client: AivisClient, idle_timeout: int = 600):
        self.client = client
        self.idle_timeout = idle_timeout
        self._lock = asyncio.Lock()
        # style_id (int) -> aivm_uuid
        self._style_to_model: dict[int, str] = {}
        # aivm_uuid -> is_loaded
        self._loaded: dict[str, bool] = {}
        # aivm_uuid -> last used timestamp
        self._last_used: dict[str, float] = {}

    async def refresh(self) -> None:
        """Rebuild style_id → aivm_uuid mapping from AIVIS."""
        speakers = await self.client.get_speakers()
        models = await self.client.get_models()

        # speaker_uuid → aivm_uuid  (manifest uses "uuid", not "speaker_uuid")
        uuid_to_model: dict[str, str] = {}
        for aivm_uuid, info in models.items():
            was_loaded = self._loaded.get(aivm_uuid, False)  # Bug1修正: already_known → was_loaded
            is_loaded = info.get("is_loaded", False)
            self._loaded[aivm_uuid] = is_loaded
            # ロード済みへの遷移時のみタイマーを開始（外部でのロードも検知）
            if is_loaded and not was_loaded and aivm_uuid not in self._last_used:
                self._last_used[aivm_uuid] = time.monotonic()
            for spk in info.get("manifest", {}).get("speakers", []):
                s_uuid = spk.get("uuid", "")
                if s_uuid:
                    uuid_to_model[s_uuid] = aivm_uuid

        # style_id → aivm_uuid  (/speakers uses "speaker_uuid")
        for speaker in speakers:
            s_uuid = speaker.get("speaker_uuid", "")
            aivm_uuid = uuid_to_model.get(s_uuid)
            if aivm_uuid is None:
                continue
            for style in speaker.get("styles", []):
                sid = style.get("id")
                if sid is not None:
                    self._style_to_model[sid] = aivm_uuid

    def get_model_uuid(self, speaker_id: int) -> Optional[str]:
        return self._style_to_model.get(speaker_id)

    async def ensure_loaded(self, speaker_id: int) -> str:
        """Load model for speaker_id if not already loaded. Returns aivm_uuid."""
        async with self._lock:
            aivm_uuid = self._style_to_model.get(speaker_id)
            if aivm_uuid is None:
                await self.refresh()
                aivm_uuid = self._style_to_model.get(speaker_id)
                if aivm_uuid is None:
                    raise ValueError(f"Speaker ID {speaker_id} not found on this backend")

            if not self._loaded.get(aivm_uuid, False):
                logger.info("Loading model %s for speaker %d", aivm_uuid, speaker_id)
                await self.client.load_model(aivm_uuid)
                self._loaded[aivm_uuid] = True

            self._last_used[aivm_uuid] = time.monotonic()
            return aivm_uuid

    def touch(self, aivm_uuid: str) -> None:
        self._last_used[aivm_uuid] = time.monotonic()

    async def unload_idle(self) -> None:
        """Unload models that have exceeded the idle timeout."""
        now = time.monotonic()
        async with self._lock:
            for uuid, last in list(self._last_used.items()):
                if self._loaded.get(uuid) and (now - last) >= self.idle_timeout:
                    logger.info("Unloading idle model %s (idle %.0fs)", uuid, now - last)
                    try:
                        await self.client.unload_model(uuid)
                        self._loaded[uuid] = False
                        self._last_used.pop(uuid)  # Bug2修正: アンロード後にタイムスタンプを削除
                    except Exception as exc:
                        logger.warning("Failed to unload %s: %s", uuid, exc)
