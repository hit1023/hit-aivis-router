import asyncio
import json
import time
import weakref
from typing import Any

import httpx

# GET /aivm_models は、モデルの画像やサンプル音声が入ったマニフェストごと返すので、
# モデルが増えると 100MB を超える（実測 約108MB）。これを台の数×呼び出し回数ぶん
# 解析し続けるとルーターのメモリが数GB単位で膨らむため、
#  1) 解析は別スレッドで（イベントループを止めない）
#  2) 必要な項目だけ残して捨てる
#  3) 短時間キャッシュし、同時取得は1本に束ねる
# という形にしている。
# 話者とモデルの対応はインストール/アンインストールでしか変わらず、ロード状態はルーター自身の
# load/unload でキャッシュを直接更新するので、頻繁に取り直す必要はない（取り直すたびに
# 100MB 超を受信・解析し、ルーターのメモリが数GBまで膨らんだ。実測）。
MODELS_CACHE_TTL = 300.0
# 全台で共有: 巨大JSONの解析を同時に走らせない（イベントループごとに1つ。テストで複数ループを使うため）
_models_fetch_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _fetch_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _models_fetch_locks.get(loop)
    if lock is None:
        lock = _models_fetch_locks[loop] = asyncio.Lock()
    return lock


def _slim_models(raw: bytes) -> dict[str, Any]:
    """aivm_models の巨大なレスポンスから、ルーターが使う項目だけを取り出す。"""
    data = json.loads(raw)
    slim: dict[str, Any] = {}
    for uuid, info in data.items():
        manifest = info.get("manifest") or {}
        slim[uuid] = {
            "is_loaded": bool(info.get("is_loaded", False)),
            "manifest": {
                "name": manifest.get("name", ""),
                "speakers": [
                    {"uuid": sp.get("uuid", ""), "name": sp.get("name", ""), "local_id": sp.get("local_id")}
                    for sp in manifest.get("speakers", [])
                ],
            },
        }
    return slim


class AivisClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=120.0)
        self._models_cache: "tuple[float, dict[str, Any]] | None" = None

    async def get_speakers(self) -> list[dict]:
        r = await self._client.get(f"{self.base_url}/speakers")
        r.raise_for_status()
        return r.json()

    def invalidate_models_cache(self) -> None:
        self._models_cache = None

    def _mark_loaded(self, aivm_uuid: str, loaded: bool) -> None:
        """load/unload の結果をキャッシュに直接反映する（再取得しない）。キャッシュが無ければ何もしない。"""
        cached = self._models_cache
        if cached is not None and aivm_uuid in cached[1]:
            cached[1][aivm_uuid]["is_loaded"] = loaded
        elif cached is not None:
            # 未知のモデルが対象なら一覧自体が古いので捨てる
            self._models_cache = None

    async def get_models(self, max_age: float = MODELS_CACHE_TTL) -> dict[str, Any]:
        """モデル一覧（必要な項目だけに絞ったもの）。max_age 秒以内の取得結果があればそれを返す。"""
        cached = self._models_cache
        if cached is not None and time.monotonic() - cached[0] <= max_age:
            return cached[1]
        async with _fetch_lock():
            cached = self._models_cache  # 待っている間に他の呼び出しが取得済みかもしれない
            if cached is not None and time.monotonic() - cached[0] <= max_age:
                return cached[1]
            r = await self._client.get(f"{self.base_url}/aivm_models")
            r.raise_for_status()
            data = await asyncio.to_thread(_slim_models, r.content)
            del r
            self._models_cache = (time.monotonic(), data)
            return data

    async def load_model(self, aivm_uuid: str) -> None:
        r = await self._client.post(f"{self.base_url}/aivm_models/{aivm_uuid}/load")
        r.raise_for_status()
        self._mark_loaded(aivm_uuid, True)

    async def unload_model(self, aivm_uuid: str) -> None:
        r = await self._client.post(f"{self.base_url}/aivm_models/{aivm_uuid}/unload")
        r.raise_for_status()
        self._mark_loaded(aivm_uuid, False)

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

    async def install_model_file(self, path: str) -> None:
        """`.aivmx` ファイルをこの台にインストールする（ファイルはストリーミングで送る）。"""
        import os
        with open(path, "rb") as f:
            r = await self._client.post(
                f"{self.base_url}/aivm_models/install",
                files={"file": (os.path.basename(path), f, "application/octet-stream")},
                timeout=900.0,
            )
        self.invalidate_models_cache()
        r.raise_for_status()

    async def import_user_dict(self, words: dict, override: bool = True) -> None:
        """`/user_dict` で得た単語一式を、UUID を保ったまま取り込む。"""
        r = await self._client.post(
            f"{self.base_url}/import_user_dict",
            params={"override": str(override).lower()},
            json=words,
            timeout=60.0,
        )
        r.raise_for_status()

    async def uninstall_model(self, aivm_uuid: str) -> None:
        """指定UUIDのモデルをアンインストールする。"""
        r = await self._client.delete(
            f"{self.base_url}/aivm_models/{aivm_uuid}/uninstall",
            timeout=60.0,
        )
        self.invalidate_models_cache()
        r.raise_for_status()

    async def install_model(self, filename: str, data: bytes) -> None:
        """aivmxファイルをAIVISサーバーにアップロードしてインストール。"""
        r = await self._client.post(
            f"{self.base_url}/aivm_models/install",
            files={"file": (filename, data, "application/octet-stream")},
            timeout=600.0,
        )
        self.invalidate_models_cache()
        r.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
