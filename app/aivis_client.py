import httpx
from typing import Any

# AIVIS Engine のインストール API で使うフィールド名
_INSTALL_FILE_FIELD = "file"


class AivisClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=120.0)
        # 大容量ファイルのアップロード用（読み書きタイムアウトなし）
        self._install_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=None, write=None, pool=30.0)
        )

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

    # ------------------------------------------------------------------
    # ユーザー辞書
    # ------------------------------------------------------------------

    async def get_user_dict(self, enable_compound_accent: bool = False) -> dict:
        """ユーザー辞書の単語一覧を返す。"""
        r = await self._client.get(
            f"{self.base_url}/user_dict",
            params={"enable_compound_accent": str(enable_compound_accent).lower()},
        )
        r.raise_for_status()
        return r.json()

    async def add_user_dict_word(
        self,
        surface: list[str],
        pronunciation: list[str],
        accent_type: list[int],
        word_type: str = "PROPER_NOUN",
        priority: int = 5,
    ) -> str:
        """単語を追加し、word_uuid を返す。"""
        params: list[tuple[str, str | int]] = []
        for s in surface:
            params.append(("surface", s))
        for p in pronunciation:
            params.append(("pronunciation", p))
        for a in accent_type:
            params.append(("accent_type", a))
        params.append(("word_type", word_type))
        params.append(("priority", priority))
        r = await self._client.post(f"{self.base_url}/user_dict_word", params=params)
        r.raise_for_status()
        return r.json()

    async def update_user_dict_word(
        self,
        word_uuid: str,
        surface: list[str],
        pronunciation: list[str],
        accent_type: list[int],
        word_type: str = "PROPER_NOUN",
        priority: int = 5,
    ) -> None:
        """指定 UUID の単語を更新する。"""
        params: list[tuple[str, str | int]] = []
        for s in surface:
            params.append(("surface", s))
        for p in pronunciation:
            params.append(("pronunciation", p))
        for a in accent_type:
            params.append(("accent_type", a))
        params.append(("word_type", word_type))
        params.append(("priority", priority))
        r = await self._client.put(
            f"{self.base_url}/user_dict_word/{word_uuid}", params=params
        )
        r.raise_for_status()

    async def delete_user_dict_word(self, word_uuid: str) -> None:
        """指定 UUID の単語を削除する。"""
        r = await self._client.delete(
            f"{self.base_url}/user_dict_word/{word_uuid}"
        )
        r.raise_for_status()

    async def install_model(self, filename: str, file_data: bytes) -> dict | None:
        """AIVM ファイルをバックエンドにアップロードしてモデルをインストールする。"""
        r = await self._install_client.post(
            f"{self.base_url}/aivm_models/install",
            files={_INSTALL_FILE_FIELD: (filename, file_data, "application/octet-stream")},
        )
        r.raise_for_status()
        return r.json() if r.content else None

    async def close(self) -> None:
        await self._client.aclose()
        await self._install_client.aclose()
