"""Aivis Cloud API (api.aivis-project.com) のクライアント。

ローカルの AivisSpeech Engine（`aivis_client.AivisClient`）の代替として使う。
両者はプロトコルが大きく違うため、インターフェースは揃えず、
`backend_pool.CloudBackend` 側で吸収している。

ローカル版との主な違い:

- 合成が **1 リクエストで完結する**（ローカルは `/audio_query` → `/synthesis` の 2 段）
- **MP3 を直接返せる**ため、ローカル版で必要だった wav→mp3 変換が不要
- VRAM の概念が無いので、モデルのロード / アンロード / インストールは存在しない
- 話者のグローバルなスタイル ID を API が公開しておらず、`local_id`(0〜31) しか返さない
  （スタイル ID の導出については `backend_pool.derive_style_id` を参照）
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class AivisCloudClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    # ------------------------------------------------------------------
    # 音声合成
    # ------------------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        model_uuid: str,
        style_id: Optional[int] = None,
        speaking_rate: float = 1.0,
        emotional_intensity: float = 1.0,
        tempo_dynamics: float = 1.0,
        pitch: float = 0.0,
        volume: float = 1.0,
        line_break_silence_seconds: Optional[float] = None,
        user_dictionary_uuid: Optional[str] = None,
        output_bitrate: Optional[int] = None,
    ) -> bytes:
        """テキストを合成して MP3 のバイト列を返す。

        `output_format="mp3"` を明示しているので、戻り値はそのまま MP3 として使える
        （ローカル版のように wav_to_mp3 を通す必要はない）。
        """
        payload: dict[str, Any] = {
            "model_uuid": model_uuid,
            "text": text,
            "output_format": "mp3",
            "speaking_rate": speaking_rate,
            "emotional_intensity": emotional_intensity,
            "tempo_dynamics": tempo_dynamics,
            "pitch": pitch,
            "volume": volume,
        }
        if style_id is not None:
            payload["style_id"] = style_id
        if line_break_silence_seconds is not None:
            payload["line_break_silence_seconds"] = line_break_silence_seconds
        if user_dictionary_uuid:
            payload["user_dictionary_uuid"] = user_dictionary_uuid
        if output_bitrate:
            payload["output_bitrate"] = output_bitrate

        r = await self._client.post(f"{self.base_url}/v1/tts/synthesize", json=payload)
        # 402(クレジット不足) や 401(キー不正) は本文に理由が入るので、raise 前に拾って残す。
        if r.status_code >= 400:
            detail = r.text[:300]
            logger.error("Aivis Cloud synthesize failed: %s %s", r.status_code, detail)
        r.raise_for_status()

        # 課金状況をログに残す（残高が減っていくのを追えるようにするため）。
        remaining = r.headers.get("x-aivis-credits-remaining")
        used = r.headers.get("x-aivis-credits-used")
        if remaining is not None:
            logger.info(
                "Aivis Cloud: %s文字 / 消費%sクレジット / 残高%sクレジット",
                r.headers.get("x-aivis-character-count", "?"), used, remaining,
            )
        return r.content

    # ------------------------------------------------------------------
    # モデル（話者カタログ）
    # ------------------------------------------------------------------

    async def search_models(self, limit: int = 30, sort: str = "download") -> list[dict]:
        """人気順のモデル一覧を返す。話者・スタイルまで含んだ完全な形で返ってくる。"""
        r = await self._client.get(
            f"{self.base_url}/v1/aivm-models/search",
            params={"sort": sort, "limit": limit},
        )
        r.raise_for_status()
        return r.json().get("aivm_models", [])

    async def get_model(self, identifier: str) -> dict:
        """モデル UUID（または ak_ で始まるアクセスキー）から 1 件取得する。"""
        r = await self._client.get(f"{self.base_url}/v1/aivm-models/{identifier}")
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # ユーザー辞書
    #
    # Cloud 側の単語スキーマ（surface / pronunciation / accent_type / word_type /
    # priority ...）は AivisSpeech と同型なので、hit-aivis-webui の単語登録画面は
    # そのまま使える。ただし更新は「辞書まるごと PUT で置き換え」しか無いため、
    # 1 語の追加・更新・削除も read-modify-write で実装している（CloudBackend 側）。
    # ------------------------------------------------------------------

    async def get_user_dictionary(self, dict_uuid: str) -> Optional[dict]:
        """辞書 1 件を取得する。未作成なら None（404 は「まだ無い」の意味で正常）。"""
        r = await self._client.get(f"{self.base_url}/v1/user-dictionaries/{dict_uuid}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def put_user_dictionary(
        self, dict_uuid: str, name: str, word_properties: list[dict], description: str = ""
    ) -> dict:
        """辞書の内容を完全に置き換える（存在しない UUID なら新規作成される＝upsert）。"""
        r = await self._client.put(
            f"{self.base_url}/v1/user-dictionaries/{dict_uuid}",
            json={"name": name, "description": description, "word_properties": word_properties},
        )
        r.raise_for_status()
        # 204 No Content が返ることがあるため、本文が無い場合は空 dict を返す
        return r.json() if r.content else {}

    async def close(self) -> None:
        await self._client.aclose()
