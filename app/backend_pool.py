import asyncio
import hashlib
import itertools
import logging
from dataclasses import dataclass, field
from typing import Optional, Protocol, Union

from .aivis_client import AivisClient
from .aivis_cloud_client import AivisCloudClient
from .audio import wav_to_mp3
from .config import settings
from .model_manager import ModelManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 例外（main.py 側で HTTP ステータスへ対応づける）
# ---------------------------------------------------------------------------

class SpeakerNotFound(Exception):
    """指定されたスタイル ID がこのバックエンドに存在しない（→ 404）。"""


class ModelLoadFailed(Exception):
    """モデルの読み込みに失敗した（→ 503）。ローカルバックエンド専用。"""


class SynthesisFailed(Exception):
    """音声合成そのものに失敗した（→ 502）。"""


# ---------------------------------------------------------------------------
# スタイル ID の導出（Cloud 用）
# ---------------------------------------------------------------------------

def derive_style_id(speaker_uuid: str, local_style_id: int) -> int:
    """Cloud のモデル情報から、AivisSpeech 風のグローバルなスタイル ID を作る。

    Aivis Cloud API は話者ごとの `local_id`(0〜31) しか返さず、AivisSpeech Engine が
    使っているグローバルなスタイル ID（855257952 のような値）を公開していない。
    そのためルーター側で「話者 UUID + ローカルスタイル ID」から決定的に導出している。

    **注意**: この値は AivisSpeech Engine が同じモデルに割り当てる ID とは一致しない。
    つまり local ⇄ aivis_cloud を切り替えると、同じ声でもスタイル ID の数値が変わる。
    切り替え後に古い ID を渡されても落ちないよう、CloudBackend 側で
    「未知の ID は既定の話者にフォールバックする」ようにしてある。
    """
    digest = hashlib.sha256(f"{speaker_uuid}:{local_style_id}".encode("utf-8")).digest()
    # AivisSpeech の ID と同じく「正の 32bit 整数」に収める
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


# ---------------------------------------------------------------------------
# バックエンド共通インターフェース
# ---------------------------------------------------------------------------

class SpeechBackend(Protocol):
    """`/speak` から見たバックエンドの最小インターフェース。

    テキスト置換・プリセット解決・発話履歴は main.py 側の共通処理なので、
    バックエンドは「解決済みのテキストとパラメータを受け取って MP3 を返す」だけでよい。
    """

    async def synthesize_mp3(self, text: str, speaker_id: int, params: dict) -> bytes: ...
    async def get_speakers(self) -> list[dict]: ...
    async def refresh(self) -> None: ...
    def style_name(self, speaker_id: int) -> Optional[str]: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# ローカル（AivisSpeech Engine）
# ---------------------------------------------------------------------------

@dataclass
class Backend:
    url: str
    client: AivisClient = field(init=False)
    manager: ModelManager = field(init=False)

    def __post_init__(self):
        self.client = AivisClient(self.url)
        self.manager = ModelManager(self.client)

    async def synthesize_mp3(self, text: str, speaker_id: int, params: dict) -> bytes:
        """モデルをロードしてから `/audio_query` → `/synthesis` を叩き、MP3 に変換して返す。"""
        try:
            await self.manager.ensure_loaded(speaker_id)
        except ValueError as exc:
            raise SpeakerNotFound(str(exc)) from exc
        except Exception as exc:
            raise ModelLoadFailed(str(exc)) from exc

        try:
            query = await self.client.audio_query(text, speaker_id)
        except Exception as exc:
            raise SynthesisFailed(f"audio_query failed: {exc}") from exc

        query["speedScale"] = params["speed"]
        query["pitchScale"] = params["pitch"]
        query["intonationScale"] = params["intonation"]
        query["volumeScale"] = params["volume"]
        query["tempoDynamicsScale"] = params["tempo_dynamics"]
        query["pauseLengthScale"] = params["pause_length_scale"]
        if params.get("pause_length") is not None:
            query["pauseLength"] = params["pause_length"]

        try:
            wav_bytes = await self.client.synthesis(speaker_id, query)
        except Exception as exc:
            raise SynthesisFailed(f"synthesis failed: {exc}") from exc

        return wav_to_mp3(wav_bytes, bitrate=settings.mp3_bitrate)

    async def get_speakers(self) -> list[dict]:
        return await self.client.get_speakers()

    async def refresh(self) -> None:
        await self.manager.refresh()

    def style_name(self, speaker_id: int) -> Optional[str]:
        return self.manager._style_names.get(speaker_id)

    async def close(self) -> None:
        await self.client.close()


# ---------------------------------------------------------------------------
# Aivis Cloud API
# ---------------------------------------------------------------------------

class CloudBackend:
    """Aivis Cloud API を、ローカルの AivisSpeech Engine と同じ顔で見せるバックエンド。

    VRAM が無いのでモデルのロード / アンロードは何もしない（no-op）。
    話者カタログは起動時に一度取得し、`derive_style_id` でスタイル ID を振る。
    """

    def __init__(self, api_url: str, api_key: str, model_uuids: list[str], catalog_limit: int):
        self.url = api_url
        self.client = AivisCloudClient(api_url, api_key)
        self._model_uuids = model_uuids
        self._catalog_limit = catalog_limit
        # style_id -> (model_uuid, local_style_id)
        self._style_to_model: dict[int, tuple[str, int]] = {}
        self._style_names: dict[int, str] = {}
        self._speakers: list[dict] = []
        # 未知のスタイル ID を渡された時に使う既定（カタログの先頭）
        self._default_style_id: Optional[int] = None
        self._user_dict_uuid: Optional[str] = None
        # Cloud 側に辞書の実体が「まだ無い」状態で user_dictionary_uuid を送ると
        # 422 (User dictionary not found) で合成そのものが失敗する。実体を確認/作成
        # できるまでは辞書を指定しない（辞書が無いだけで喋れなくなるのを防ぐ）。
        self._user_dict_ready = False

    # -- カタログ ------------------------------------------------------

    async def refresh(self) -> None:
        """Cloud のモデル一覧から、AivisSpeech 互換の話者カタログを組み立てる。"""
        if self._model_uuids:
            models = []
            for uuid in self._model_uuids:
                try:
                    models.append(await self.client.get_model(uuid))
                except Exception as exc:
                    logger.warning("Cloud model %s の取得に失敗: %s", uuid, exc)
        else:
            models = await self.client.search_models(limit=self._catalog_limit)

        speakers: list[dict] = []
        style_to_model: dict[int, tuple[str, int]] = {}
        style_names: dict[int, str] = {}

        for model in models:
            model_uuid = model.get("aivm_model_uuid")
            if not model_uuid:
                continue
            model_name = model.get("name", "")
            for spk in model.get("speakers", []):
                spk_uuid = spk.get("aivm_speaker_uuid", "")
                spk_name = spk.get("name", "") or model_name
                if not spk_uuid:
                    continue
                styles = []
                for style in spk.get("styles", []):
                    local_id = style.get("local_id")
                    if local_id is None:
                        continue
                    style_name = style.get("name", "ノーマル")
                    sid = derive_style_id(spk_uuid, local_id)
                    styles.append({"id": sid, "name": style_name, "type": "talk"})
                    style_to_model[sid] = (model_uuid, local_id)
                    style_names[sid] = f"{spk_name}（{style_name}）" if style_name else spk_name
                if styles:
                    speakers.append(
                        {"name": spk_name, "speaker_uuid": spk_uuid, "styles": styles}
                    )

        self._speakers = speakers
        self._style_to_model = style_to_model
        self._style_names = style_names
        if speakers and speakers[0]["styles"]:
            self._default_style_id = speakers[0]["styles"][0]["id"]
        logger.info(
            "Aivis Cloud カタログ: モデル%d件 / 話者%d件 / スタイル%d件",
            len(models), len(speakers), len(style_to_model),
        )

    async def get_speakers(self) -> list[dict]:
        if not self._speakers:
            await self.refresh()
        return self._speakers

    def style_name(self, speaker_id: int) -> Optional[str]:
        return self._style_names.get(speaker_id)

    # -- 合成 ----------------------------------------------------------

    async def synthesize_mp3(self, text: str, speaker_id: int, params: dict) -> bytes:
        if not self._style_to_model:
            await self.refresh()

        entry = self._style_to_model.get(speaker_id)
        if entry is None:
            # local モードで保存されたスタイル ID を渡された場合など。
            # 声は変わってしまうが、無音や 404 で会話が止まるよりはマシなので既定話者で喋る。
            if self._default_style_id is None:
                raise SpeakerNotFound(f"Speaker ID {speaker_id} not found (カタログが空です)")
            logger.warning(
                "未知のスタイル ID %d のため既定の話者 %d で合成します"
                "（local ⇄ aivis_cloud の切り替えでは ID が変わります）",
                speaker_id, self._default_style_id,
            )
            entry = self._style_to_model[self._default_style_id]

        model_uuid, local_style_id = entry

        # pause_length_scale は Cloud に直接の対応が無いため、改行間の無音（既定 0.4 秒）
        # を倍率として解釈する。pause_length（絶対秒）は Cloud に相当機能が無く無視される。
        line_break_silence = None
        scale = params.get("pause_length_scale")
        if scale is not None and scale != 1.0:
            line_break_silence = round(0.4 * scale, 3)

        try:
            return await self.client.synthesize(
                text=text,
                model_uuid=model_uuid,
                style_id=local_style_id,
                speaking_rate=params["speed"],
                emotional_intensity=params["intonation"],
                tempo_dynamics=params["tempo_dynamics"],
                # ローカルの pitchScale は概ね -0.15〜0.15、Cloud の pitch は -1.0〜1.0。
                # 範囲内に収まるのでそのまま渡す（0.0 同士が既定なので通常は差が出ない）。
                pitch=params["pitch"],
                volume=params["volume"],
                line_break_silence_seconds=line_break_silence,
                user_dictionary_uuid=self._user_dict_uuid if self._user_dict_ready else None,
                output_bitrate=settings.mp3_bitrate_kbps,
            )
        except Exception as exc:
            raise SynthesisFailed(str(exc)) from exc

    # -- ユーザー辞書 --------------------------------------------------
    #
    # Cloud 側は「辞書まるごと PUT で置き換え」しか無いので、1 語の追加・更新・削除も
    # read-modify-write で行う。単語スキーマは AivisSpeech と同型なので、
    # hit-aivis-webui の単語登録画面はそのまま使える。

    def set_user_dict_uuid(self, dict_uuid: str) -> None:
        self._user_dict_uuid = dict_uuid

    async def ensure_user_dictionary(self) -> None:
        """辞書の実体が Cloud 側にあることを保証する（無ければ空で作る）。

        Cloud の辞書はクライアントが決めた UUID で PUT した時に初めて実体ができる。
        存在しない UUID を合成時に渡すと 422 になるため、起動時にここで作っておく。
        失敗しても `_user_dict_ready` が False のままになるだけで、合成は
        「辞書なし」で通常どおり動く。
        """
        if not self._user_dict_uuid:
            return
        try:
            doc = await self.client.get_user_dictionary(self._user_dict_uuid)
            if doc is None:
                await self.client.put_user_dictionary(
                    self._user_dict_uuid, settings.aivis_cloud_user_dict_name, []
                )
                logger.info("Aivis Cloud のユーザー辞書を新規作成しました: %s", self._user_dict_uuid)
            self._user_dict_ready = True
        except Exception as exc:
            logger.warning(
                "ユーザー辞書の準備に失敗しました（辞書なしで合成を続行します）: %s", exc
            )

    async def _load_words(self) -> list[dict]:
        if not self._user_dict_uuid:
            return []
        doc = await self.client.get_user_dictionary(self._user_dict_uuid)
        return list(doc.get("word_properties", [])) if doc else []

    async def _save_words(self, words: list[dict]) -> None:
        await self.client.put_user_dictionary(
            self._user_dict_uuid, settings.aivis_cloud_user_dict_name, words
        )
        # 書き込めた時点で辞書の実体は確実に存在する
        self._user_dict_ready = True

    async def get_user_dict(self, enable_compound_accent: bool = False) -> dict:
        """AivisSpeech の `/user_dict` と同じ「UUID をキーにした dict」の形で返す。

        Cloud も surface / pronunciation / accent_type を**リスト**で持つ（複合語対応）ため、
        `enable_compound_accent=True` 相当の形がそのまま得られる。False が渡された場合は
        後方互換のため結合した文字列にして返す。
        """
        words = await self._load_words()
        result: dict[str, dict] = {}
        for w in words:
            uid = w.get("uuid")
            if not uid:
                continue
            surface = w.get("surface", [])
            pronunciation = w.get("pronunciation", [])
            accent_type = w.get("accent_type", [])
            if not enable_compound_accent:
                surface = "".join(surface)
                pronunciation = "".join(pronunciation)
                accent_type = accent_type[0] if accent_type else 0
            result[uid] = {
                "surface": surface,
                "pronunciation": pronunciation,
                "accent_type": accent_type,
                "word_type": w.get("word_type", "PROPER_NOUN"),
                "priority": w.get("priority", 5),
            }
        return result

    async def add_user_dict_word(
        self,
        surface: list[str],
        pronunciation: list[str],
        accent_type: list[int],
        word_type: str = "PROPER_NOUN",
        priority: int = 5,
    ) -> str:
        import uuid as _uuid

        word_uuid = str(_uuid.uuid4())
        words = await self._load_words()
        # Cloud も surface / pronunciation / accent_type をリストで受け取る（複合語対応）ので、
        # ローカル版と同じくそのまま渡す。
        words.append(
            {
                "uuid": word_uuid,
                "surface": surface,
                "pronunciation": pronunciation,
                "accent_type": accent_type,
                "word_type": word_type,
                "priority": priority,
            }
        )
        await self._save_words(words)
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
        words = await self._load_words()
        for w in words:
            if w.get("uuid") == word_uuid:
                w.update(
                    {
                        "surface": surface,
                        "pronunciation": pronunciation,
                        "accent_type": accent_type,
                        "word_type": word_type,
                        "priority": priority,
                    }
                )
                break
        await self._save_words(words)

    async def delete_user_dict_word(self, word_uuid: str) -> None:
        words = [w for w in await self._load_words() if w.get("uuid") != word_uuid]
        await self._save_words(words)

    async def close(self) -> None:
        await self.client.close()


AnyBackend = Union[Backend, CloudBackend]


# ---------------------------------------------------------------------------
# プール
# ---------------------------------------------------------------------------

class BackendPool:
    """バックエンドのプール。

    `tts_provider` が "local" なら AivisSpeech Engine をラウンドロビンで、
    "aivis_cloud" なら Aivis Cloud API を 1 つだけ持つ。
    どちらの場合も `/speak` から見た振る舞いは同じ。
    """

    def __init__(self, urls: list[str], idle_timeout: int = 600):
        self.is_cloud = settings.is_cloud
        if self.is_cloud:
            if not settings.aivis_cloud_api_key:
                logger.error(
                    "TTS_PROVIDER=aivis_cloud ですが AIVIS_CLOUD_API_KEY が未設定です"
                )
            self._backends: list[AnyBackend] = [
                CloudBackend(
                    settings.aivis_cloud_api_url,
                    settings.aivis_cloud_api_key,
                    settings.cloud_model_uuids,
                    settings.aivis_cloud_catalog_limit,
                )
            ]
        else:
            self._backends = [Backend(url) for url in urls]
            for b in self._backends:
                b.manager.idle_timeout = idle_timeout
        self._cycle = itertools.cycle(self._backends)
        self._idle_timeout = idle_timeout

    def next(self) -> AnyBackend:
        return next(self._cycle)

    def resolve_speaker_name(self, speaker_id: int) -> Optional[str]:
        return self._backends[0].style_name(speaker_id) if self._backends else None

    async def initialize(self) -> None:
        await asyncio.gather(*(b.refresh() for b in self._backends))
        if self.is_cloud:
            await self._backends[0].ensure_user_dictionary()

    async def start_cleanup_loop(self) -> None:
        # Cloud には VRAM の概念が無いのでアンロードのループ自体が不要
        if self.is_cloud:
            return
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
        return await self._backends[0].get_speakers()

    async def get_models_status(self) -> list[dict]:
        """各バックエンドのモデルVRAMロード状態を返す。"""
        if self.is_cloud:
            # Cloud はモデルが常時利用可能なので、カタログを「常にロード済み」として見せる
            results = []
            for spk in await self._backends[0].get_speakers():
                results.append({
                    "backend_url": self._backends[0].url,
                    "aivm_uuid": spk.get("speaker_uuid", ""),
                    "model_name": spk.get("name", ""),
                    "is_loaded": True,
                    "speakers": [{"name": spk.get("name", ""), "local_id": None}],
                })
            return results
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

    async def uninstall_model(self, aivm_uuid: str) -> list[dict]:
        """指定UUIDのモデルを全バックエンドからアンインストールする。"""
        if self.is_cloud:
            raise NotImplementedError("Aivis Cloud ではモデルのアンインストールはできません")
        results = []
        for b in self._backends:
            try:
                await b.client.uninstall_model(aivm_uuid)
                results.append({"backend_url": b.url, "success": True})
                logger.info("Model %s uninstalled from %s", aivm_uuid, b.url)
            except Exception as exc:
                results.append({"backend_url": b.url, "success": False, "error": str(exc)})
                logger.error("Model uninstall failed on %s: %s", b.url, exc)
        await asyncio.gather(*(b.manager.refresh() for b in self._backends), return_exceptions=True)
        return results

    async def install_model(self, filename: str, data: bytes) -> list[dict]:
        """aivmxファイルを全バックエンドにインストールする。各バックエンドの結果を返す。"""
        if self.is_cloud:
            raise NotImplementedError("Aivis Cloud ではモデルのインストールはできません")
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
        if self.is_cloud:
            return []
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
        if self.is_cloud:
            return await self._backends[0].get_user_dict(enable_compound_accent)
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
        if self.is_cloud:
            return await self._backends[0].add_user_dict_word(
                surface, pronunciation, accent_type, word_type, priority
            )
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
        if self.is_cloud:
            await self._backends[0].update_user_dict_word(
                word_uuid, surface, pronunciation, accent_type, word_type, priority
            )
            return
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
        if self.is_cloud:
            await self._backends[0].delete_user_dict_word(word_uuid)
            return
        await asyncio.gather(
            *(b.client.delete_user_dict_word(word_uuid) for b in self._backends),
            return_exceptions=False,
        )

    def cloud_backend(self) -> Optional[CloudBackend]:
        """Cloud モードのときだけ CloudBackend を返す（辞書 UUID の注入用）。"""
        return self._backends[0] if self.is_cloud else None

    async def close(self) -> None:
        await asyncio.gather(*(b.close() for b in self._backends))
