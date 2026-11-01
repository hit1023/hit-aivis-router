import asyncio
import httpx
import hashlib
import ipaddress
import itertools
import json
import logging
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, Union
from urllib.parse import urlsplit

from .aivis_client import AivisClient
from .aivis_cloud_client import AivisCloudClient
from .audio import wav_to_mp3
from .config import settings
from .model_manager import ModelManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 例外（main.py 側で HTTP ステータスへ対応づける）
# ---------------------------------------------------------------------------

# 台が不調になったとき、振り分けから外しておく秒数
BACKEND_COOLDOWN_SEC = 30.0


def _is_node_fault(exc: BaseException) -> bool:
    """原因が台の不調（接続できない・タイムアウト・5xx）か。リクエスト自体の不備（4xx等）は除く。"""
    cause = exc.__cause__ or exc
    if isinstance(cause, httpx.TransportError):
        return True
    return isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code >= 500


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

class InvalidBackendURL(ValueError):
    """登録できないバックエンド URL（形式不正・社内ネットワーク外など）。"""


class BackendIncompatible(InvalidBackendURL):
    """新しい台に、既存の台にあるモデル（話者）が揃っていない。"""


class BackendUnreachable(Exception):
    """バックエンドに接続できない。"""


async def validate_backend_url(url: str) -> str:
    """バックエンド URL を検証して正規化した URL を返す。

    管理 API から任意の URL を登録できると、ルーターが内部ネットワークや
    クラウドのメタデータ (169.254.x.x) へリクエストを送る踏み台になりうる。
    そのため http(s) かつ、名前解決した全アドレスがプライベート/ループバックのものだけを許す。
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https"):
        raise InvalidBackendURL("URL は http:// または https:// で始めてください")
    if not parts.hostname:
        raise InvalidBackendURL("ホスト名がありません")
    if parts.username or parts.password:
        raise InvalidBackendURL("URL に認証情報を含めることはできません")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise InvalidBackendURL("パス・クエリを含まない http://ホスト:ポート の形で指定してください")
    try:
        port = parts.port
    except ValueError:
        raise InvalidBackendURL("ポート番号が不正です") from None
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, parts.hostname, port or (443 if parts.scheme == "https" else 80),
            0, socket.SOCK_STREAM,
        )
    except socket.gaierror:
        raise InvalidBackendURL(f"ホスト名を解決できません: {parts.hostname}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise InvalidBackendURL(f"登録できないアドレスです: {ip}")
        if not (ip.is_private or ip.is_loopback):
            raise InvalidBackendURL(f"社内ネットワーク（プライベートIP）のサーバーのみ登録できます: {ip}")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


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
    # 画面表示用の統計（メモリ上のみ。再起動でリセットされる）
    requests: int = 0
    errors: int = 0
    last_used: Optional[float] = None
    recent_ms: deque = field(default_factory=lambda: deque(maxlen=20))
    # この時刻(monotonic)まで振り分けから外す（不調だった台の一時退避）
    cooldown_until: float = 0.0
    # False なら振り分けに使わない（登録は残す。設定画面の「無効化」）
    enabled: bool = True

    def in_cooldown(self) -> bool:
        return self.cooldown_until > time.monotonic()

    def __post_init__(self):
        self.client = AivisClient(self.url)
        self.manager = ModelManager(self.client)

    async def synthesize_mp3(self, text: str, speaker_id: int, params: dict) -> bytes:
        started = time.monotonic()
        self.requests += 1
        self.last_used = time.time()
        try:
            result = await self._synthesize_mp3(text, speaker_id, params)
        except Exception as exc:
            self.errors += 1
            if not isinstance(exc, SpeakerNotFound) and _is_node_fault(exc):
                exc.node_fault = True
                self.cooldown_until = time.monotonic() + BACKEND_COOLDOWN_SEC
                logger.warning("%s を %d 秒間 振り分けから外します: %s", self.url, BACKEND_COOLDOWN_SEC, exc)
            raise
        self.cooldown_until = 0.0
        self.recent_ms.append(round((time.monotonic() - started) * 1000))
        return result

    async def _synthesize_mp3(self, text: str, speaker_id: int, params: dict) -> bytes:
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

    def __init__(self, urls: list[str], idle_timeout: int = 600, store_path: Optional[Path] = None):
        self.is_cloud = settings.is_cloud
        self._store_path = Path(store_path if store_path is not None else settings.backends_file)
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
            self._backends = [Backend(url) for url in (self._load_saved_urls() or urls)]
            self._master_url: Optional[str] = self._load_saved_master()
            disabled = {u.rstrip("/") for u in self._load_saved_disabled()}
            for b in self._backends:
                b.enabled = b.url.rstrip("/") not in disabled
            for b in self._backends:
                b.manager.idle_timeout = idle_timeout
        self._cycle = itertools.cycle(self._backends)
        self._idle_timeout = idle_timeout
        # モデル同期中（まだ振り分けに入れていない）の台。url -> 状態
        self._pending: dict[str, dict] = {}
        # セッション → (台, 最後に使った時刻 monotonic)。成功した台を使い続けるため
        self._sticky: dict[str, tuple] = {}

    # ------------------------------------------------------------------
    # 台の管理（設定画面から）
    # ------------------------------------------------------------------

    def _load_saved_urls(self) -> list[str]:
        """設定画面で保存した台の一覧。無い・壊れている場合は空（= .env の値を使う）。"""
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
            urls = [u for u in data.get("urls", []) if isinstance(u, str) and u]
            if urls:
                logger.info("保存済みのバックエンド一覧を使用: %s", urls)
            return urls
        except FileNotFoundError:
            return []
        except (OSError, ValueError, AttributeError) as exc:
            logger.warning("バックエンド一覧の読み込みに失敗（.env の値を使います）: %s", exc)
            return []

    def _load_saved_master(self) -> Optional[str]:
        try:
            m = json.loads(self._store_path.read_text(encoding="utf-8")).get("master")
            return m if isinstance(m, str) and m else None
        except (OSError, ValueError, AttributeError):
            return None

    def _load_saved_disabled(self) -> list[str]:
        try:
            d = json.loads(self._store_path.read_text(encoding="utf-8")).get("disabled", [])
            return [u for u in d if isinstance(u, str) and u]
        except (OSError, ValueError, AttributeError):
            return []

    def _save_urls(self) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._store_path.with_suffix(".tmp")
        data = {"urls": [b.url for b in self._backends]}
        if getattr(self, "_master_url", None):
            data["master"] = self._master_url
        disabled = [b.url for b in self._backends if not b.enabled]
        if disabled:
            data["disabled"] = disabled
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._store_path)

    # ------------------------------------------------------------------
    # マスター台
    #
    # 「基準になる台」。モデルの有無（話者）・ユーザー辞書の正はマスターで、
    # 単語の登録/更新/削除とモデルのインストールはマスターに先に入れ、
    # 他の台はそれに追従させる。未指定なら先頭の台がマスター。
    # ------------------------------------------------------------------

    @property
    def master(self) -> "Backend":
        m = getattr(self, "_master_url", None)
        if m:
            found = next((b for b in self._backends if b.url.rstrip("/") == m.rstrip("/")), None)
            if found is not None:
                return found
        return self._backends[0]

    def _followers(self) -> list:
        m = self.master
        return [b for b in self._backends if b is not m]

    async def set_master(self, url: str) -> dict:
        """マスター台を変える。新しいマスターの辞書を他の台へ反映する。"""
        if self.is_cloud:
            raise NotImplementedError("Aivis Cloud ではマスターは使えません")
        key = url.rstrip("/")
        target = next((b for b in self._backends if b.url.rstrip("/") == key), None)
        if target is None:
            raise KeyError(url)
        if not target.enabled:
            raise InvalidBackendURL("無効化中の台はマスターにできません。先に有効化してください")
        self._master_url = target.url
        self._save_urls()
        logger.info("マスター台を変更: %s", target.url)
        return {"master": target.url, "dict": await self._propagate_dict()}

    async def _propagate_dict(self) -> dict:
        """マスターの辞書を、他の台へ UUID を保ったまま取り込ませる（失敗した台は結果に残す）。"""
        words = await self.master.client.get_user_dict(enable_compound_accent=True)
        results = {"words": len(words), "failed": []}
        if not words:
            return results
        for b in self._followers():
            try:
                await b.client.import_user_dict(words, override=True)
            except Exception as exc:
                logger.error("辞書の反映に失敗 %s: %s", b.url, exc)
                results["failed"].append(b.url)
        return results

    def _rebuild_cycle(self) -> None:
        self._cycle = itertools.cycle(self._backends)

    @staticmethod
    async def probe(url: str, timeout: float = 5.0) -> dict:
        """台に接続できるか調べる（登録はしない）。応答時間とスピーカー数を返す。"""
        client = AivisClient(url)
        started = time.monotonic()
        try:
            speakers = await asyncio.wait_for(client.get_speakers(), timeout)
            return {"url": url, "ok": True, "latency_ms": round((time.monotonic() - started) * 1000), "speakers": len(speakers)}
        except Exception as exc:
            return {"url": url, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
        finally:
            await client.close()

    def _pending_view(self, url_visible: bool, first_index: int) -> list[dict]:
        out = []
        for i, (url, p) in enumerate(self._pending.items(), start=first_index):
            row = {
                "index": i,
                "ok": False,
                "state": p["state"],
                "sync_done": p["done"],
                "sync_total": p["total"],
                "sync_current": p["current"],
                "words_state": p["words_state"],
                "words_total": p["words_total"],
                "error": p["error"],
            }
            if url_visible:
                row["url"] = url
            out.append(row)
        return out

    async def backend_status(self) -> list[dict]:
        """登録済みの台の状態（死活・応答時間・ロード済みモデル数）。同期中の台も末尾に付く。"""
        if self.is_cloud:
            return [{"url": self._backends[0].url, "ok": True, "cloud": True}]
        probes = await asyncio.gather(*(self.probe(b.url) for b in self._backends))
        out = []
        for b, pr in zip(self._backends, probes):
            pr["loaded_models"] = sum(1 for v in b.manager._loaded.values() if v)
            if pr.get("ok"):
                b.cooldown_until = 0.0
            pr["state"] = "active" if b.enabled else "disabled"
            pr["enabled"] = b.enabled
            pr["master"] = b is self.master
            pr.update(self._usage(b))
            out.append(pr)
        return out + self._pending_view(True, len(out) + 1)

    @staticmethod
    def _usage(b: "Backend") -> dict:
        recent = list(b.recent_ms)
        return {
            "requests": b.requests,
            "errors": b.errors,
            "last_used": b.last_used,
            "avg_ms": round(sum(recent) / len(recent)) if recent else None,
            "recent_ms": recent,
        }

    async def public_status(self) -> list[dict]:
        """認証不要の状態表示用。URL（社内IP）は含めず、台の番号だけで返す。"""
        if self.is_cloud:
            return [{"index": 1, "ok": True, "cloud": True, "state": "active"}]
        probes = await asyncio.gather(*(self.probe(b.url) for b in self._backends))
        out = []
        for i, (b, pr) in enumerate(zip(self._backends, probes), start=1):
            out.append({
                "index": i,
                "state": "active" if b.enabled else "disabled",
                "enabled": b.enabled,
                "master": b is self.master,
                "ok": pr["ok"],
                "latency_ms": pr.get("latency_ms"),
                "speakers": pr.get("speakers"),
                "loaded_models": sum(1 for v in b.manager._loaded.values() if v),
                **self._usage(b),
            })
        return out + self._pending_view(False, len(out) + 1)

    async def add_backend(self, url: str, position: Optional[int] = None) -> dict:
        """台を追加する。

        - 話者が揃っている台は、辞書をコピーしてすぐ振り分けに加える（state=active）。
        - モデルが足りない台は、`models_source_dir` があれば裏でモデルを送り込み、
          完了してから振り分けに加える（state=syncing、遅延同期）。無ければ拒否する。
        """
        if self.is_cloud:
            raise NotImplementedError("Aivis Cloud では台の追加はできません")
        url = await validate_backend_url(url)
        norm = url.rstrip("/")
        if any(b.url.rstrip("/") == norm for b in self._backends) or norm in self._pending:
            raise InvalidBackendURL("すでに登録されています")
        result = await self.probe(url)
        if not result["ok"]:
            raise BackendUnreachable(result["error"])
        backend = Backend(url)
        backend.manager.idle_timeout = self._idle_timeout
        try:
            await backend.refresh()
            missing = await self._missing_models(backend)
            if not missing:
                self._ensure_same_speakers(backend)
                synced = await self._copy_user_dict(backend)
        except Exception as exc:
            await backend.close()
            raise BackendUnreachable(str(exc)) from exc
        if not missing:
            self._activate(backend, position)
            logger.info("バックエンドを追加: %s（計 %d 台、辞書 %d 語をコピー）", url, len(self._backends), synced)
            return {"url": url, "state": "active", "synced_words": synced}

        # モデルが足りない
        names = [m["name"] for m in missing]
        shown = "、".join(names[:5]) + (f" ほか{len(names) - 5}件" if len(names) > 5 else "")
        src = settings.models_source_dir
        files_ok = bool(src) and all(Path(src, f"{m['uuid']}.aivmx").is_file() for m in missing)
        if not files_ok:
            await backend.close()
            why = ("モデルの自動同期が設定されていません（MODELS_SOURCE_DIR）" if not src
                   else "同期元にモデルファイルが見つかりません")
            raise BackendIncompatible(
                f"新しい台に、既存の台にあるモデルがありません（{shown}）。{why}。"
                "エンジンのデータフォルダを既存の台からコピーしてから追加してください"
            )
        self._pending[norm] = {
            "backend": backend, "state": "syncing", "done": 0, "total": len(missing),
            "current": names[0], "error": None,
            "words_state": "waiting", "words_total": None, "position": position,
            "task": asyncio.create_task(self._sync_job(norm, backend, missing)),
        }
        logger.info("バックエンドの同期を開始: %s（モデル %d 件）", url, len(missing))
        return {"url": url, "state": "syncing", "missing_models": len(missing)}

    def _activate(self, backend: "Backend", position: Optional[int] = None) -> None:
        if position is None or position >= len(self._backends):
            self._backends = [*self._backends, backend]
        else:
            self._backends = [*self._backends[:position], backend, *self._backends[position:]]
        self._rebuild_cycle()
        self._save_urls()
        # 追加直後の最初のリクエストが冷えた台に当たらないよう、裏で温める
        try:
            asyncio.get_running_loop().create_task(self._warm_backend(backend))
        except RuntimeError:
            pass

    async def _missing_models(self, new: "Backend") -> list[dict]:
        """最初の台にあって新しい台に無いモデル（uuid と名前）。"""
        ref = await self._backends[0].client.get_models()
        have = await new.client.get_models()
        out = []
        for uuid, info in ref.items():
            if uuid not in have:
                name = (info.get("manifest") or {}).get("name") or uuid
                out.append({"uuid": uuid, "name": name})
        return out

    async def _sync_job(self, key: str, backend: "Backend", missing: list[dict]) -> None:
        """モデルを1つずつ送り込み、辞書をコピーしてから振り分けに加える（裏で動く）。"""
        entry = self._pending[key]
        try:
            words = await self._backends[0].client.get_user_dict(enable_compound_accent=True)
            entry["words_total"] = len(words)
            for m in missing:
                entry["current"] = m["name"]
                await backend.client.install_model_file(str(Path(settings.models_source_dir, f"{m['uuid']}.aivmx")))
                entry["done"] += 1
                logger.info("モデルを同期: %s → %s（%d/%d）", m["name"], backend.url, entry["done"], entry["total"])
            entry["current"] = None
            await backend.refresh()
            self._ensure_same_speakers(backend)
            entry["words_state"] = "syncing"
            synced = await self._copy_user_dict(backend)
            entry["words_state"] = "done"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("バックエンドの同期に失敗: %s: %s", backend.url, exc)
            entry["state"] = "failed"
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            return
        self._pending.pop(key, None)
        self._activate(backend, entry.get("position"))
        logger.info("同期が完了し振り分けに追加: %s（計 %d 台、辞書 %d 語）", backend.url, len(self._backends), synced)

    def _ensure_same_speakers(self, new: "Backend") -> None:
        """最初の台にある話者（スタイル）が、新しい台にもすべてあることを確認する。"""
        ref = self.master.manager._style_names
        missing = [name for sid, name in ref.items() if sid not in new.manager._style_names]
        if missing:
            shown = "、".join(missing[:5]) + (f" ほか{len(missing) - 5}件" if len(missing) > 5 else "")
            raise BackendIncompatible(f"新しい台に、既存の台にある話者がありません（{shown}）")

    async def _copy_user_dict(self, new: "Backend") -> int:
        words = await self.master.client.get_user_dict(enable_compound_accent=True)
        if words:
            await new.client.import_user_dict(words, override=True)
        return len(words)

    def set_enabled(self, url: str, enabled: bool) -> None:
        """台を振り分けに使うか切り替える（登録は残す）。マスターと最後の有効な1台は無効化できない。"""
        if self.is_cloud:
            raise NotImplementedError("Aivis Cloud では台の無効化はできません")
        key = url.rstrip("/")
        target = next((b for b in self._backends if b.url.rstrip("/") == key), None)
        if target is None:
            raise KeyError(url)
        if enabled:
            raise ValueError("有効化は enable_backend() を使う（モデル・辞書の同期が要る）")
        if not enabled:
            if target is self.master:
                raise InvalidBackendURL("マスターの台は無効化できません。先に別の台をマスターにしてください")
            if sum(1 for b in self._backends if b.enabled and b is not target) < 1:
                raise InvalidBackendURL("最後の有効な1台は無効化できません")
        target.enabled = False
        self._save_urls()
        logger.info("バックエンドを無効化: %s", target.url)

    async def enable_backend(self, url: str) -> dict:
        """無効化中の台を振り分けに戻す。無効の間に入ったモデル・辞書の変更を、戻す前に同期する。

        台の追加と同じ手順（接続確認 → 不足モデルの送り込み → 辞書コピー）を通し、
        完了してから元の位置に戻す。接続できない・モデルが足りず同期もできない場合は、
        無効のまま残してエラーにする。
        """
        if self.is_cloud:
            raise NotImplementedError("Aivis Cloud では台の有効化はできません")
        key = url.rstrip("/")
        target = next((b for b in self._backends if b.url.rstrip("/") == key), None)
        if target is None:
            raise KeyError(url)
        if target.enabled:
            return {"url": target.url, "state": "active", "synced_words": 0}
        idx = self._backends.index(target)
        self._backends = [b for b in self._backends if b is not target]
        self._rebuild_cycle()
        try:
            result = await self.add_backend(target.url, position=idx)
        except BaseException:
            self._backends = [*self._backends[:idx], target, *self._backends[idx:]]
            self._rebuild_cycle()
            raise
        await target.close()
        return result

    async def remove_backend(self, url: str) -> None:
        if self.is_cloud:
            raise NotImplementedError("Aivis Cloud では台の削除はできません")
        key = url.rstrip("/")
        if key in self._pending:  # 同期中・同期失敗の台は取り消せる
            entry = self._pending.pop(key)
            entry["task"].cancel()
            await entry["backend"].close()
            logger.info("同期中のバックエンドを取り消し: %s", url)
            return
        target = next((b for b in self._backends if b.url.rstrip("/") == key), None)
        if target is None:
            raise KeyError(url)
        if len(self._backends) <= 1:
            raise InvalidBackendURL("最後の1台は削除できません")
        if target is self.master:
            raise InvalidBackendURL("マスターの台は削除できません。先に別の台をマスターにしてください")
        self._backends = [b for b in self._backends if b is not target]
        self._rebuild_cycle()
        self._save_urls()
        await target.close()
        logger.info("バックエンドを削除: %s（計 %d 台）", url, len(self._backends))

    def next(self) -> AnyBackend:
        """順番に台を返す。不調で退避中の台は飛ばす（全台が退避中なら通常の順番）。"""
        if len(self._backends) <= 1:
            return next(self._cycle)
        first = None
        for _ in range(len(self._backends)):
            b = next(self._cycle)
            if not getattr(b, "enabled", True):
                continue
            if not getattr(b, "in_cooldown", lambda: False)():
                return b
            first = first or b
        return first or self.master

    @staticmethod
    def _has_loaded(b: AnyBackend, speaker_id: Optional[int]) -> bool:
        """その台で、この話者のモデルが今メモリ（RAM）に載っているか。"""
        if speaker_id is None:
            return False
        mgr = getattr(b, "manager", None)
        model = getattr(mgr, "_style_to_model", {}).get(speaker_id)
        return bool(model and getattr(mgr, "_loaded", {}).get(model))

    @staticmethod
    def _model_of(b: AnyBackend, speaker_id: Optional[int]) -> Optional[str]:
        mgr = getattr(b, "manager", None)
        return getattr(mgr, "_style_to_model", {}).get(speaker_id) if speaker_id is not None else None

    @staticmethod
    def _loaded_count(b: AnyBackend) -> int:
        return sum(1 for v in getattr(getattr(b, "manager", None), "_loaded", {}).values() if v)

    def _usable(self, b: AnyBackend) -> bool:
        return (b in self._backends and getattr(b, "enabled", True)
                and not getattr(b, "in_cooldown", lambda: False)())

    def home_for(self, speaker_id: Optional[int]) -> Optional[AnyBackend]:
        """このモデルの「定位置」の台。同じモデルを複数の台に載せないための判断。

        ①すでにロード済みの台（複数あれば最初の台）②どこにも無ければ、ロード済みモデルが
        最も少ない台（同数なら登録順）。使える台がなければ None。
        """
        if speaker_id is None:
            return None
        usable = [b for b in self._backends if self._usable(b)]
        if len(usable) < 1:
            return None
        for b in usable:
            if self._has_loaded(b, speaker_id):
                return b
        return min(usable, key=self._loaded_count)

    def pick(self, session: Optional[str], speaker_id: Optional[int] = None) -> AnyBackend:
        """使う台を選ぶ。1つのモデルを2台以上に載せない（RAMの無駄を避ける）ことを最優先にする。

        ①話者のモデルの定位置の台（ロード済みの台、無ければ最も空いている台）
        ②話者が分からない場合のみ、このセッションが前に成功した台 ③通常の順番。
        定位置の台が使えない（無効・不調で退避中）ときは、他の台が代わりに受ける。
        """
        if speaker_id is not None and len(self._backends) > 1:
            home = self.home_for(speaker_id)
            if home is not None:
                return home
        ttl = settings.session_sticky_ttl
        if session and ttl > 0:
            entry = self._sticky.get(session)
            if entry:
                b, last = entry
                if self._usable(b) and time.monotonic() - last <= ttl:
                    return b
                self._sticky.pop(session, None)
        return self.next()

    def remember(self, session: Optional[str], backend: AnyBackend) -> None:
        """合成に成功した台を、このセッションの定位置にする（使うたびに期限を延ばす）。"""
        ttl = settings.session_sticky_ttl
        if not session or ttl <= 0 or len(self._backends) <= 1:
            return
        now = time.monotonic()
        if len(self._sticky) >= 1000:   # 溜まりすぎないよう、期限切れを掃除
            self._sticky = {k: v for k, v in self._sticky.items() if now - v[1] <= ttl}
        self._sticky[session] = (backend, now)

    def pick_other(self, failed: AnyBackend) -> Optional[AnyBackend]:
        """失敗した台以外から、やり直し先を1台選ぶ。元気な台を優先し、いなければ None。"""
        others = [b for b in self._backends if b is not failed and getattr(b, "enabled", True)]
        healthy = [b for b in others if not getattr(b, "in_cooldown", lambda: False)()]
        pool = healthy or []
        return pool[0] if pool else None

    def resolve_speaker_name(self, speaker_id: int) -> Optional[str]:
        return self.master.style_name(speaker_id) if self._backends else None

    async def initialize(self) -> None:
        await asyncio.gather(*(b.refresh() for b in self._backends))
        if self.is_cloud:
            await self._backends[0].ensure_user_dictionary()

    async def _warm_backend(self, b: "Backend") -> None:
        """1台ぶん: エンジンの実際のロード状態を取り直し、未ロードのモデルを1本ずつ温める。

        ルーターの記憶（ロード済み）がエンジン再起動で古くなっていても、ここで実態に合わせる。
        台の追加直後・エンジン再起動直後に最初のリクエストが冷えた状態に当たるのを防ぐ。
        """
        lock = getattr(self, "_warm_lock", None)
        if lock is None:
            lock = self._warm_lock = asyncio.Lock()
        async with lock:  # 4コアの台に同時に読み込ませると過負荷になるので全体で直列
            try:
                await b.refresh()
            except Exception as exc:
                logger.warning("ウォームアップ: 状態の取得に失敗 %s: %s", b.url, exc)
                return
            first_style: dict[str, int] = {}
            for sid, model in sorted(b.manager._style_to_model.items()):
                first_style.setdefault(model, sid)
            skip = getattr(self, "_manual_unloaded", set())
            for model, sid in first_style.items():
                if b.manager._loaded.get(model) or model in skip:
                    continue
                # 同じモデルを複数の台に載せない: 他の台に載っている / 定位置が別の台なら温めない
                home = self.home_for(sid)
                if home is not None and home is not b:
                    continue
                started = time.monotonic()
                try:
                    await b.manager.ensure_loaded(sid)
                    query = await b.client.audio_query("あ", sid)
                    await b.client.synthesis(sid, query)
                    logger.info("ウォームアップ完了 %s %s (%.1f 秒)", b.url, b.style_name(sid), time.monotonic() - started)
                except Exception as exc:
                    logger.warning("ウォームアップ失敗 %s %s: %s", b.url, b.style_name(sid), exc)

    async def warmup(self) -> None:
        """全台を順番に温める（起動時）。"""
        if self.is_cloud:
            return
        for b in list(self._backends):
            if b.enabled:
                await self._warm_backend(b)

    async def keep_warm_loop(self, interval: float = 60.0) -> None:
        """定期的に各台の状態を見て、冷えている（エンジン再起動など）モデルを温め直す。"""
        if self.is_cloud:
            return
        while True:
            await asyncio.sleep(interval)
            for b in list(self._backends):
                if not b.enabled:
                    continue
                try:
                    await self._warm_backend(b)
                except Exception as exc:  # ループは止めない
                    logger.warning("ウォームアップ巡回で例外 %s: %s", b.url, exc)

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
            try:
                await self.dedupe_loaded()
            except Exception as exc:  # ループは止めない
                logger.warning("重複ロードの解消で例外: %s", exc)

    async def dedupe_loaded(self) -> list[tuple[str, str]]:
        """同じモデルが2台以上に載っていたら、1台だけ残して降ろす。

        残すのは、載っている台のうちロード済みモデルが最も少ない台（同数なら登録順）。
        使える台が1台だけのときは何もしない（降ろす先の代わりが無い）。
        """
        if self.is_cloud:
            return []
        usable = [b for b in self._backends if self._usable(b)]
        if len(usable) < 2:
            return []
        dropped: list[tuple[str, str]] = []
        models = {m for b in usable for m, on in b.manager._loaded.items() if on}
        for model in models:
            holders = [b for b in usable if b.manager._loaded.get(model)]
            if len(holders) < 2:
                continue
            keep = min(holders, key=self._loaded_count)
            for b in holders:
                if b is keep:
                    continue
                async with b.manager._lock:
                    await b.client.unload_model(model)
                    b.manager._loaded[model] = False
                    b.manager._last_used.pop(model, None)
                dropped.append((b.url, model))
        if dropped:
            logger.info("重複ロードを解消: %d 本を降ろしました", len(dropped))
        return dropped

    async def rebalance(self) -> list[dict]:
        """ロード済みモデルの数が偏っているとき、多い台から少ない台へモデルを移す。

        多い台で降ろし、少ない台に載せて温める。差が1以下になるまで繰り返す。
        使える台が2台未満なら何もしない。移した内容（model / from / to）を返す。
        """
        if self.is_cloud:
            return []
        moved: list[dict] = []
        for _ in range(64):  # 念のための上限
            usable = [b for b in self._backends if self._usable(b)]
            if len(usable) < 2:
                break
            src = max(usable, key=self._loaded_count)
            dst = min(usable, key=self._loaded_count)
            if self._loaded_count(src) - self._loaded_count(dst) < 2:
                break
            # 移し先に入っている（インストール済みの）モデルだけが候補。一番使われていないものから
            cands = [m for m, on in src.manager._loaded.items()
                     if on and any(v == m for v in dst.manager._style_to_model.values())
                     and not dst.manager._loaded.get(m)]
            if not cands:
                break
            model = min(cands, key=lambda m: src.manager._last_used.get(m, 0.0))
            sid = next(k for k, v in sorted(dst.manager._style_to_model.items()) if v == model)
            try:
                await dst.manager.ensure_loaded(sid)
                await dst.client.synthesis(sid, await dst.client.audio_query("あ", sid))
            except Exception as exc:
                logger.warning("再配置: 移し先への読み込みに失敗 %s: %s", dst.url, exc)
                break
            async with src.manager._lock:
                await src.client.unload_model(model)
                src.manager._loaded[model] = False
                src.manager._last_used.pop(model, None)
            moved.append({"model": model, "from": src.url, "to": dst.url})
        if moved:
            logger.info("再配置: %d 本を移しました", len(moved))
        return moved

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

    async def install_model(self, filename: str, data: bytes, on_event=None) -> list[dict]:
        """aivmxファイルを全バックエンドにインストールする。各バックエンドの結果を返す。"""
        if self.is_cloud:
            raise NotImplementedError("Aivis Cloud ではモデルのインストールはできません")
        targets = [self.master, *self._followers()]

        def _emit(**kw):
            if on_event:
                on_event(kw)

        async def _one(b):
            _emit(backend_url=b.url, status="started")
            try:
                await b.client.install_model(filename, data)
                logger.info("Model installed on %s", b.url)
                _emit(backend_url=b.url, status="done")
                return {"backend_url": b.url, "success": True}
            except Exception as exc:
                logger.error("Model install failed on %s: %s", b.url, exc)
                _emit(backend_url=b.url, status="failed", error=str(exc))
                return {"backend_url": b.url, "success": False, "error": str(exc)}

        # 全台へ並列に送る（順次だと台数×転送時間かかる）
        results = list(await asyncio.gather(*(_one(b) for b in targets)))
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
        if unloaded:
            # 手動で解放したモデルを、巡回ウォームアップが勝手に読み直さないようにする
            if not hasattr(self, "_manual_unloaded"):
                self._manual_unloaded = set()
            self._manual_unloaded.add(aivm_uuid)
        return unloaded

    # ------------------------------------------------------------------
    # ユーザー辞書
    # ------------------------------------------------------------------


    async def get_user_dict(self, enable_compound_accent: bool = False) -> dict:
        """最初のバックエンドからユーザー辞書を取得する。"""
        if self.is_cloud:
            return await self._backends[0].get_user_dict(enable_compound_accent)
        return await self.master.client.get_user_dict(enable_compound_accent)

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
        # マスターに先に入れ、発行された UUID のまま他の台へ取り込ませる（台ごとに UUID が
        # 食い違うと、あとの更新・削除が他の台で失敗するため）
        word_uuid = await self.master.client.add_user_dict_word(
            surface, pronunciation, accent_type, word_type, priority
        )
        await self._follow_words({word_uuid})
        return word_uuid

    async def _follow_words(self, uuids: set[str]) -> None:
        """マスターにある指定の単語を、他の台へ UUID を保ったまま反映する（失敗は記録のみ）。"""
        followers = self._followers()
        if not followers:
            return
        words = await self.master.client.get_user_dict(enable_compound_accent=True)
        part = {u: w for u, w in words.items() if u in uuids}
        for b in followers:
            try:
                await b.client.import_user_dict(part, override=True)
            except Exception as exc:
                logger.error("単語の反映に失敗 %s: %s", b.url, exc)

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
        await self.master.client.update_user_dict_word(
            word_uuid, surface, pronunciation, accent_type, word_type, priority
        )
        await self._follow_words({word_uuid})

    async def delete_user_dict_word(self, word_uuid: str) -> None:
        """全バックエンドから単語を削除する。"""
        if self.is_cloud:
            await self._backends[0].delete_user_dict_word(word_uuid)
            return
        await self.master.client.delete_user_dict_word(word_uuid)
        for b in self._followers():
            try:
                await b.client.delete_user_dict_word(word_uuid)
            except Exception as exc:
                logger.error("単語の削除の反映に失敗 %s: %s", b.url, exc)

    def cloud_backend(self) -> Optional[CloudBackend]:
        """Cloud モードのときだけ CloudBackend を返す（辞書 UUID の注入用）。"""
        return self._backends[0] if self.is_cloud else None

    async def close(self) -> None:
        await asyncio.gather(*(b.close() for b in self._backends))
