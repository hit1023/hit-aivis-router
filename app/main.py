import asyncio
import logging
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware


from .audio import wav_to_mp3
from .backend_pool import BackendPool
from .config import settings
from .speaker_preset import SpeakerPresetManager
from .text_replacer import TextReplacer
from . import speech_history

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

pool: BackendPool
replacer: TextReplacer
presets: SpeakerPresetManager

_DESCRIPTION = """
## 概要

AivisSpeech Engine のシンプルなラッパー API です。
少ないパラメータで音声合成を行い、結果を **MP3** 形式で返します。

## 主な機能

- **音声合成** — テキストを指定してMP3音声を取得
- **自動アンロード** — 最後の使用から **10分** 経過したモデルは自動的にVRAMから解放
- **複数バックエンド対応** — 複数のAivisSpeechサーバーをラウンドロビンで使用可能
- **ユーザー辞書** — 固有名詞や読み方を辞書登録して音声合成精度を向上

## 基本的な使い方

1. `/speakers` でスピーカー一覧を取得し、使いたいスタイルの `id` を確認
2. `/speak` にテキストとスタイルIDを渡すとMP3が返ってくる
3. `/models` でVRAMのロード状況を確認できる
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, replacer, presets
    speech_history.init(Path(settings.speech_history_db))
    replacer = TextReplacer(Path(settings.text_replacements_file), seed_file=Path("/srv/name.txt"))
    presets = SpeakerPresetManager(Path(settings.speaker_presets_file))
    pool = BackendPool(settings.backend_urls, idle_timeout=settings.model_idle_timeout)
    try:
        await pool.initialize()
    except Exception as exc:
        logger.warning("Backend initialization failed (will retry on first request): %s", exc)
    cleanup_task = asyncio.create_task(pool.start_cleanup_loop())
    logger.info("Backends: %s", settings.backend_urls)
    yield
    cleanup_task.cancel()
    await pool.close()


app = FastAPI(
    title="AIVIS Router",
    description=_DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

# --- ここから追加 ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# --- ここまで ---

app.mount("/static", StaticFiles(directory="/srv/static"), name="static")


@app.get("/docs", include_in_schema=False)
async def custom_swagger() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="AIVIS Router - API ドキュメント",
        swagger_js_url="/static/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui.css",
    )


@app.get("/redoc", include_in_schema=False)
async def custom_redoc() -> HTMLResponse:
    return get_redoc_html(
        openapi_url="/openapi.json",
        title="AIVIS Router - API ドキュメント",
        redoc_js_url="/static/redoc.standalone.js",
    )


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

_SPEAK_DEFAULTS: dict = {
    "speed": 1.0,
    "pitch": 0.0,
    "intonation": 1.0,
    "volume": 1.0,
    "tempo_dynamics": 1.0,
    "pause_length": None,
    "pause_length_scale": 1.0,
}


class SpeakRequest(BaseModel):
    model_config = {"json_schema_extra": {
        "example": {
            "text": "こんにちは、元気ですか？",
            "speaker_id": 888753760,
        }
    }}

    text: str = Field(..., description="読み上げるテキスト")
    speaker_id: int = Field(..., description="スピーカーのスタイルID（`/speakers` で確認できます）")
    speed: Optional[float] = Field(None, ge=0.5, le=2.0, description="話速。省略時はプリセット→デフォルト(1.0)の順で適用（0.5〜2.0）")
    pitch: Optional[float] = Field(None, ge=-0.15, le=0.15, description="音高。省略時はプリセット→デフォルト(0.0)の順で適用（-0.15〜0.15）")
    intonation: Optional[float] = Field(None, ge=0.0, le=2.0, description="抑揚。省略時はプリセット→デフォルト(1.0)の順で適用（0.0〜2.0）")
    volume: Optional[float] = Field(None, ge=0.0, le=2.0, description="音量。省略時はプリセット→デフォルト(1.0)の順で適用（0.0〜2.0）")
    tempo_dynamics: Optional[float] = Field(None, ge=0.0, le=2.0, description="緩急。省略時はプリセット→デフォルト(1.0)の順で適用（0.0〜2.0）")
    pause_length: Optional[float] = Field(None, ge=0.0, description="句読点などの無音時間（秒）。Noneで自動（デフォルト）")
    pause_length_scale: Optional[float] = Field(None, ge=0.0, description="無音倍率。省略時はプリセット→デフォルト(1.0)の順で適用")


class SpeakerStyle(BaseModel):
    id: int = Field(..., description="スタイルID（`/speak` の `speaker_id` に使用）")
    name: str = Field(..., description="スタイル名")
    type: str = Field(..., description="スタイル種別（talk / singing_teacher など）")


class SpeakerInfo(BaseModel):
    name: str = Field(..., description="スピーカー名")
    speaker_uuid: str = Field(..., description="スピーカーを一意に識別するUUID")
    styles: list[SpeakerStyle] = Field(..., description="利用可能なスタイルの一覧")


class ModelSpeaker(BaseModel):
    name: str = Field(..., description="スピーカー名")
    local_id: Optional[int] = Field(None, description="モデル内でのローカルID")


class ModelStatus(BaseModel):
    backend_url: str = Field(..., description="このモデルが存在するバックエンドサーバーのURL")
    aivm_uuid: str = Field(..., description="モデルを一意に識別するUUID")
    model_name: str = Field(..., description="モデル名")
    is_loaded: bool = Field(..., description="VRAMにロードされているかどうか。`true` なら即座に合成可能")
    speakers: list[ModelSpeaker] = Field(..., description="このモデルに含まれるスピーカーの一覧")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post(
    "/speak",
    response_class=Response,
    responses={
        200: {"content": {"audio/mpeg": {}}, "description": "合成されたMP3音声データ"},
        404: {"description": "指定したスピーカーIDが存在しない"},
        502: {"description": "バックエンドのAivisSpeechサーバーでエラーが発生した"},
        503: {"description": "モデルの読み込みに失敗した"},
    },
    summary="音声合成",
    description="""
テキストを音声合成し、**MP3形式**で返します。

- モデルがVRAMにロードされていない場合は自動的にロードしてから合成します
- 最後の使用から10分が経過すると、モデルは自動的にVRAMからアンロードされます
- スタイルIDは `/speakers` で確認してください
""",
    tags=["音声合成"],
)
async def speak(req: SpeakRequest):
    backend = pool.next()

    try:
        await backend.manager.ensure_loaded(req.speaker_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Model load failed: %s", exc)
        raise HTTPException(status_code=503, detail="音声モデルの読み込みに失敗しました")

    processed_text = replacer.apply(req.text)
    if processed_text != req.text:
        logger.info("テキスト置換: %r → %r", req.text, processed_text)

    # パラメータ解決: リクエスト値 → プリセット値 → デフォルト値
    preset = presets.get(req.speaker_id) or {}

    def resolve(field_val, key: str):
        if field_val is not None:
            return field_val
        if key in preset:
            return preset[key]
        return _SPEAK_DEFAULTS[key]

    speed            = resolve(req.speed,              "speed")
    pitch            = resolve(req.pitch,              "pitch")
    intonation       = resolve(req.intonation,         "intonation")
    volume           = resolve(req.volume,             "volume")
    tempo_dynamics   = resolve(req.tempo_dynamics,     "tempo_dynamics")
    pause_length_scale = resolve(req.pause_length_scale, "pause_length_scale")
    pause_length     = req.pause_length  # Noneのまま渡す（プリセット対象外）

    if preset:
        logger.info(
            "Preset applied for speaker %d: speed=%.2f pitch=%.2f intonation=%.2f "
            "volume=%.2f tempo_dynamics=%.2f pause_length_scale=%.2f",
            req.speaker_id, speed, pitch, intonation, volume, tempo_dynamics, pause_length_scale,
        )

    try:
        query = await backend.client.audio_query(processed_text, req.speaker_id)
    except Exception as exc:
        logger.error("audio_query failed: %s", exc)
        raise HTTPException(status_code=502, detail="音声クエリの生成に失敗しました")

    query["speedScale"] = speed
    query["pitchScale"] = pitch
    query["intonationScale"] = intonation
    query["volumeScale"] = volume
    query["tempoDynamicsScale"] = tempo_dynamics
    query["pauseLengthScale"] = pause_length_scale
    if pause_length is not None:
        query["pauseLength"] = pause_length

    try:
        wav_bytes = await backend.client.synthesis(req.speaker_id, query)
    except Exception as exc:
        logger.error("synthesis failed: %s", exc)
        raise HTTPException(status_code=502, detail="音声合成に失敗しました")

    mp3_bytes = wav_to_mp3(wav_bytes, bitrate=settings.mp3_bitrate)

    asyncio.create_task(speech_history.record(
        speaker_id=req.speaker_id,
        speaker_name=pool.resolve_speaker_name(req.speaker_id),
        original_text=req.text,
        processed_text=processed_text,
        speed=speed,
        pitch=pitch,
        intonation=intonation,
        volume=volume,
        tempo_dynamics=tempo_dynamics,
        pause_length=pause_length,
        pause_length_scale=pause_length_scale,
    ))

    return Response(content=mp3_bytes, media_type="audio/mpeg")


@app.get(
    "/speakers",
    response_model=list[SpeakerInfo],
    summary="スピーカー一覧の取得",
    description="""
利用可能なスピーカーとスタイルの一覧を返します。

各スタイルの `id` を `/speak` の `speaker_id` として使用します。
""",
    tags=["情報取得"],
)
async def list_speakers():
    try:
        raw = await pool.get_speakers()
    except Exception as exc:
        logger.error("speakers fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="スピーカー情報の取得に失敗しました")

    return [
        SpeakerInfo(
            name=s["name"],
            speaker_uuid=s["speaker_uuid"],
            styles=[
                SpeakerStyle(id=st["id"], name=st["name"], type=st.get("type", "talk"))
                for st in s.get("styles", [])
            ],
        )
        for s in raw
    ]


@app.get(
    "/models",
    response_model=list[ModelStatus],
    summary="モデルのVRAMロード状態一覧",
    description="""
インストールされている音声モデルの一覧と、各モデルのVRAMロード状態を返します。

- `is_loaded: true` — VRAMにロード済み。リクエストがあれば即座に合成できます
- `is_loaded: false` — アンロード済み。次回のリクエスト時に自動でロードされます

複数のバックエンドサーバーが設定されている場合は、全サーバー分をまとめて返します。
""",
    tags=["情報取得"],
)
async def list_models():
    try:
        return await pool.get_models_status()
    except Exception as exc:
        logger.error("models fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="モデル情報の取得に失敗しました")


@app.post(
    "/models/install",
    summary="カスタムモデルをファイルからインストール",
    description="""
`.aivmx` 形式のモデルファイルをアップロードして全バックエンドサーバーにインストールします。

- 複数バックエンドが設定されている場合は全台に順次インストールします
- インストール完了後、スピーカーマッピングを自動で更新します
- ファイルサイズによっては完了まで数分かかる場合があります
""",
    tags=["モデル管理"],
)
async def install_model(file: UploadFile = File(...)):
    filename = file.filename or "model.aivmx"
    if not filename.split("?")[0].endswith(".aivmx"):
        raise HTTPException(status_code=400, detail="ファイルは .aivmx 形式を指定してください")
    data = await file.read()
    results = await pool.install_model(filename, data)
    failed = [r for r in results if not r["success"]]
    if len(failed) == len(results):
        raise HTTPException(status_code=502, detail="全バックエンドでインストールに失敗しました")
    return {
        "filename": filename,
        "results": results,
        "message": f"{len(results) - len(failed)}/{len(results)} バックエンドにインストールしました",
    }


@app.post(
    "/models/{aivm_uuid}/unload",
    summary="モデルを強制アンロード",
    description="""
指定したモデルをVRAMから強制的にアンロードします。

- ロードされていないモデルを指定した場合は何もせず正常終了します
- 次回の `/speak` リクエスト時に自動で再ロードされます
- 複数バックエンドがある場合は全サーバーでアンロードします
""",
    tags=["モデル管理"],
)
async def force_unload_model(aivm_uuid: str):
    try:
        unloaded = await pool.force_unload(aivm_uuid)
    except Exception as exc:
        logger.error("force unload failed: %s", exc)
        raise HTTPException(status_code=502, detail="アンロードに失敗しました")
    return {
        "aivm_uuid": aivm_uuid,
        "unloaded_from": unloaded,
        "message": f"{len(unloaded)}台のサーバーからアンロードしました" if unloaded else "対象モデルはロードされていませんでした",
    }


@app.delete(
    "/models/{aivm_uuid}/uninstall",
    summary="モデルをアンインストール",
    description="""
指定したモデルを全バックエンドサーバーからアンインストールします。

- アンインストール前に自動でVRAMからアンロードされます
- 複数バックエンドがある場合は全台から削除します
- この操作は取り消せません
""",
    tags=["モデル管理"],
)
async def uninstall_model(aivm_uuid: str):
    try:
        results = await pool.uninstall_model(aivm_uuid)
    except Exception as exc:
        logger.error("uninstall failed: %s", exc)
        raise HTTPException(status_code=502, detail="アンインストールに失敗しました")
    failed = [r for r in results if not r["success"]]
    if len(failed) == len(results):
        raise HTTPException(status_code=502, detail="全バックエンドでアンインストールに失敗しました")
    return {
        "aivm_uuid": aivm_uuid,
        "results": results,
        "message": f"{len(results) - len(failed)}/{len(results)} バックエンドからアンインストールしました",
    }


@app.get(
    "/health",
    summary="ヘルスチェック",
    description="サーバーが正常に動作しているか確認します。",
    tags=["システム"],
)
async def health():
    return {"status": "ok"}


@app.post(
    "/audio_query",
    summary="アクセント句の取得",
    description="テキストの音声クエリ（アクセント句・ピッチ情報）を返します。アクセント可視化に使用できます。",
    tags=["情報取得"],
)
async def audio_query_endpoint(text: str, speaker_id: int):
    backend = pool.next()
    try:
        await backend.manager.ensure_loaded(speaker_id)
    except Exception:
        pass
    processed_text = replacer.apply(text)
    try:
        query = await backend.client.audio_query(processed_text, speaker_id)
        return query
    except Exception as exc:
        logger.error("audio_query failed: %s", exc)
        raise HTTPException(status_code=502, detail="音声クエリの生成に失敗しました")


# ---------------------------------------------------------------------------
# スピーカープリセット
# ---------------------------------------------------------------------------

class SpeakerPresetParams(BaseModel):
    """スピーカーに紐づく音声パラメータのプリセット。"""
    model_config = {"json_schema_extra": {
        "example": {
            "speed": 1.0,
            "pitch": 0.0,
            "intonation": 1.0,
            "volume": 1.0,
            "tempo_dynamics": 1.0,
            "pause_length": None,
            "pause_length_scale": 1.0,
        }
    }}

    speed: float = Field(1.0, ge=0.5, le=2.0, description="話速（0.5〜2.0）")
    pitch: float = Field(0.0, ge=-0.15, le=0.15, description="音高（-0.15〜0.15）")
    intonation: float = Field(1.0, ge=0.0, le=2.0, description="抑揚（0.0〜2.0）")
    volume: float = Field(1.0, ge=0.0, le=2.0, description="音量（0.0〜2.0）")
    tempo_dynamics: float = Field(1.0, ge=0.0, le=2.0, description="緩急（0.0〜2.0）")
    pause_length: Optional[float] = Field(None, ge=0.0, description="無音秒数（Noneで自動）")
    pause_length_scale: float = Field(1.0, ge=0.0, description="無音倍率")


@app.get(
    "/speaker_presets",
    summary="スピーカープリセット一覧の取得",
    description="""
スピーカーIDをキーとして登録されているパラメータプリセットの一覧を返します。

スピーカーIDは `/speakers` で確認できます。
""",
    tags=["スピーカープリセット"],
)
async def get_speaker_presets() -> dict:
    return presets.get_all()


@app.get(
    "/speaker_presets/{speaker_id}",
    summary="スピーカープリセットの取得",
    description="指定したスピーカーIDのプリセットを返します。登録がない場合は404を返します。",
    tags=["スピーカープリセット"],
)
async def get_speaker_preset(speaker_id: int) -> SpeakerPresetParams:
    p = presets.get(speaker_id)
    if p is None:
        raise HTTPException(status_code=404, detail="プリセットが登録されていません")
    return SpeakerPresetParams(**p)


@app.put(
    "/speaker_presets/{speaker_id}",
    summary="スピーカープリセットの保存",
    description="""
指定したスピーカーIDにパラメータプリセットを保存します。

既にプリセットが存在する場合は上書きします。
プリセットは `/data/speaker_presets.json` に永続化されます。
""",
    tags=["スピーカープリセット"],
)
async def save_speaker_preset(speaker_id: int, req: SpeakerPresetParams) -> SpeakerPresetParams:
    presets.set(speaker_id, req.model_dump())
    return req


@app.delete(
    "/speaker_presets/{speaker_id}",
    status_code=204,
    summary="スピーカープリセットの削除",
    description="指定したスピーカーIDのプリセットを削除します。",
    tags=["スピーカープリセット"],
)
async def delete_speaker_preset(speaker_id: int) -> Response:
    found = presets.delete(speaker_id)
    if not found:
        raise HTTPException(status_code=404, detail="プリセットが登録されていません")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# ユーザー辞書
# ---------------------------------------------------------------------------

class WordType(str, Enum):
    """品詞の種別。"""
    PROPER_NOUN = "PROPER_NOUN"
    LOCATION_NAME = "LOCATION_NAME"
    ORGANIZATION_NAME = "ORGANIZATION_NAME"
    PERSON_NAME = "PERSON_NAME"
    PERSON_FAMILY_NAME = "PERSON_FAMILY_NAME"
    PERSON_GIVEN_NAME = "PERSON_GIVEN_NAME"
    COMMON_NOUN = "COMMON_NOUN"
    VERB = "VERB"
    ADJECTIVE = "ADJECTIVE"
    SUFFIX = "SUFFIX"


_WORD_TYPE_LABELS: dict[str, str] = {
    "PROPER_NOUN": "固有名詞",
    "LOCATION_NAME": "地名",
    "ORGANIZATION_NAME": "組織・施設名",
    "PERSON_NAME": "人名",
    "PERSON_FAMILY_NAME": "人名（姓）",
    "PERSON_GIVEN_NAME": "人名（名）",
    "COMMON_NOUN": "普通名詞",
    "VERB": "動詞",
    "ADJECTIVE": "形容詞",
    "SUFFIX": "語尾",
}


class UserDictWordRequest(BaseModel):
    """単語の追加・更新リクエスト。

    単純な1語の場合はリストに要素を1つだけ入れてください。
    複合語（例: 「新田真剣佑」）は各モーフィームを別々の要素として指定します。
    """
    model_config = {"json_schema_extra": {
        "examples": [
            {
                "summary": "単純な1語の例",
                "value": {
                    "surface": ["東京スカイツリー"],
                    "pronunciation": ["トウキョウスカイツリー"],
                    "accent_type": [5],
                    "word_type": "PROPER_NOUN",
                    "priority": 5,
                }
            },
            {
                "summary": "複合語の例（新田真剣佑）",
                "value": {
                    "surface": ["新田", "真剣佑"],
                    "pronunciation": ["アラタ", "マッケンユウ"],
                    "accent_type": [1, 3],
                    "word_type": "PERSON_NAME",
                    "priority": 7,
                }
            },
        ]
    }}

    surface: list[str] = Field(
        ...,
        min_length=1,
        description="単語の表層形。複合語の場合は要素を分けて指定します（例: `['新田', '真剣佑']`）",
    )
    pronunciation: list[str] = Field(
        ...,
        min_length=1,
        description="カタカナ読み。`surface` と同じ長さのリストで指定します（例: `['アラタ', 'マッケンユウ']`）",
    )
    accent_type: list[int] = Field(
        ...,
        min_length=1,
        description="アクセント型。0=平板型、1以上=下がり目の位置（1-indexed）。`surface` と同じ長さで指定します",
    )
    word_type: WordType = Field(
        WordType.PROPER_NOUN,
        description="品詞種別。" + " / ".join(_WORD_TYPE_LABELS.values()),
    )
    priority: int = Field(
        5,
        ge=0,
        le=10,
        description="優先度（0〜10、推奨: 1〜9）。数値が大きいほど優先されます",
    )


class UserDictWordAdded(BaseModel):
    word_uuid: str = Field(..., description="追加された単語のUUID。更新・削除に使用します")


@app.get(
    "/user_dict",
    summary="ユーザー辞書の単語一覧を取得",
    description="""
ユーザー辞書に登録されている単語の一覧を返します。

- `enable_compound_accent=false`（デフォルト）: 後方互換モード。各単語は単一のアクセント情報で返されます
- `enable_compound_accent=true`: 複合語アクセント対応モード（AivisSpeech Engine 1.1.0以降）

辞書は最初のバックエンドサーバーから取得します。
""",
    tags=["ユーザー辞書"],
)
async def get_user_dict(enable_compound_accent: bool = False) -> dict:
    try:
        return await pool.get_user_dict(enable_compound_accent)
    except Exception as exc:
        logger.error("get_user_dict failed: %s", exc)
        raise HTTPException(status_code=502, detail="ユーザー辞書の取得に失敗しました")


@app.post(
    "/user_dict",
    response_model=UserDictWordAdded,
    status_code=200,
    summary="ユーザー辞書に単語を追加",
    description="""
ユーザー辞書に新しい単語を追加します。

追加された単語の UUID を返します。この UUID は後で単語の更新・削除に使用します。

複数のバックエンドサーバーがある場合は、全サーバーに同時に追加します。
""",
    tags=["ユーザー辞書"],
)
async def add_user_dict_word(req: UserDictWordRequest) -> UserDictWordAdded:
    if len(req.surface) != len(req.pronunciation) or len(req.surface) != len(req.accent_type):
        raise HTTPException(
            status_code=422,
            detail="surface / pronunciation / accent_type のリスト長が一致していません",
        )
    try:
        word_uuid = await pool.add_user_dict_word(
            surface=req.surface,
            pronunciation=req.pronunciation,
            accent_type=req.accent_type,
            word_type=req.word_type.value,
            priority=req.priority,
        )
    except Exception as exc:
        logger.error("add_user_dict_word failed: %s", exc)
        raise HTTPException(status_code=502, detail="単語の追加に失敗しました")
    return UserDictWordAdded(word_uuid=word_uuid)


@app.put(
    "/user_dict/{word_uuid}",
    status_code=204,
    summary="ユーザー辞書の単語を更新",
    description="""
指定した UUID の単語を更新します。

`word_uuid` は `/user_dict` の GET レスポンスのキー、または単語追加時に返された値です。

複数のバックエンドサーバーがある場合は、全サーバーを同時に更新します。
""",
    tags=["ユーザー辞書"],
)
async def update_user_dict_word(word_uuid: str, req: UserDictWordRequest) -> Response:
    if len(req.surface) != len(req.pronunciation) or len(req.surface) != len(req.accent_type):
        raise HTTPException(
            status_code=422,
            detail="surface / pronunciation / accent_type のリスト長が一致していません",
        )
    try:
        await pool.update_user_dict_word(
            word_uuid=word_uuid,
            surface=req.surface,
            pronunciation=req.pronunciation,
            accent_type=req.accent_type,
            word_type=req.word_type.value,
            priority=req.priority,
        )
    except Exception as exc:
        logger.error("update_user_dict_word failed: %s", exc)
        raise HTTPException(status_code=502, detail="単語の更新に失敗しました")
    return Response(status_code=204)


@app.delete(
    "/user_dict/{word_uuid}",
    status_code=204,
    summary="ユーザー辞書の単語を削除",
    description="""
指定した UUID の単語を辞書から削除します。

複数のバックエンドサーバーがある場合は、全サーバーから同時に削除します。
""",
    tags=["ユーザー辞書"],
)
async def delete_user_dict_word(word_uuid: str) -> Response:
    try:
        await pool.delete_user_dict_word(word_uuid)
    except Exception as exc:
        logger.error("delete_user_dict_word failed: %s", exc)
        raise HTTPException(status_code=502, detail="単語の削除に失敗しました")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# テキスト置換ルール
# ---------------------------------------------------------------------------

class TextReplacementRequest(BaseModel):
    """テキスト置換ルールの追加リクエスト。"""
    model_config = {"json_schema_extra": {
        "example": {
            "src": "Mumon",
            "dst": "ミューモン",
        }
    }}

    src: str = Field(..., description="置換前テキスト（空不可）")
    dst: str = Field("", description="置換後テキスト（空文字列も可）")


@app.get(
    "/text_replacements",
    summary="テキスト置換ルール一覧の取得",
    description="""
音声合成前にテキストへ適用される置換ルールの一覧を返します。

ルールは `/speak` 実行時に **長い置換前テキストを優先** して適用されます。
これにより MeCab が英単語を誤読するケースなどを事前に修正できます。
""",
    tags=["テキスト置換"],
)
async def get_text_replacements() -> dict:
    return replacer.get_all()


@app.post(
    "/text_replacements",
    summary="テキスト置換ルールを追加・更新",
    description="""
テキスト置換ルールを追加します。既存の `src` を指定した場合は上書き更新されます。

例: `src="Mumon"`, `dst="ミューモン"` を登録すると、
`/speak` へ送られるテキスト中の `Mumon` が `ミューモン` に変換されてから
AivisSpeech へ渡されます。
""",
    tags=["テキスト置換"],
)
async def add_text_replacement(req: TextReplacementRequest) -> dict:
    try:
        replacer.add(req.src, req.dst)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"src": req.src, "dst": req.dst}


@app.delete(
    "/text_replacements",
    status_code=204,
    summary="テキスト置換ルールを削除",
    description="""
指定した置換前テキスト（`src`）に対応するルールを削除します。

`src` はクエリパラメータとして渡してください。
""",
    tags=["テキスト置換"],
)
async def delete_text_replacement(
    src: str = Query(..., description="削除する置換前テキスト"),
) -> Response:
    found = replacer.remove(src)
    if not found:
        raise HTTPException(status_code=404, detail="指定された置換ルールが見つかりません")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 音声合成履歴
# ---------------------------------------------------------------------------

@app.get(
    "/history",
    summary="音声合成履歴の取得",
    description="""
音声合成リクエストの履歴を最新順で返します。

- `page`: ページ番号（1始まり）
- `per_page`: 1ページあたりの件数（最大100）
- `speaker_id`: スピーカーIDでフィルタリング（省略時は全件）
""",
    tags=["履歴"],
)
async def get_history(
    page: int = Query(1, ge=1, description="ページ番号"),
    per_page: int = Query(20, ge=1, le=100, description="1ページの件数"),
    speaker_id: Optional[int] = Query(None, description="スピーカーIDでフィルタ"),
) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        speech_history.fetch_page,
        page,
        per_page,
        speaker_id,
    )
