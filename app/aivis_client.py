import httpx
from typing import Any


class AivisClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=120.0)

    async def get_speakers(self) -> list[dict]:
        r = await self._client.get(f"{self.base_url}/speakers")
        r.raise_for_status()
        return r.json()

    async def get_models(self) -> dict[str, Any]:
        r = await self._client.get(f"{self.base_url}/aivm_models")
        r.raise_for_status()
        return r.json()

    async def load_model(self, aivm_uuid: str) -> None:
        r = await self._client.post(f"{self.base_url}/aivm_models/{aivm_uuid}/load")
        r.raise_for_status()

    async def unload_model(self, aivm_uuid: str) -> None:
        r = await self._client.post(f"{self.base_url}/aivm_models/{aivm_uuid}/unload")
        r.raise_for_status()

    async def audio_query(self, text: str, speaker_id: int) -> dict:
        r = await self._client.post(
            f"{self.base_url}/audio_query",
            params={"text": text, "speaker": speaker_id},
        )
        r.raise_for_status()
        return r.json()

    async def synthesis(self, speaker_id: int, query: dict) -> bytes:
        r = await self._client.post(
            f"{self.base_url}/synthesis",
            params={"speaker": speaker_id},
            json=query,
        )
        r.raise_for_status()
        return r.content

    async def close(self) -> None:
        await self._client.aclose()
