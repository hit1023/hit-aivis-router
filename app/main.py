import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .audio import wav_to_mp3
from .backend_pool import BackendPool
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

pool: BackendPool

_DESCRIPTION = """
## 概要

AivisSpeech Engine のシンプルなラッパー API です。
少ないパラメータで音声合成を行い、結果を **MP3** 形式で返します。

## 主な機能

- **音声合成** — テキストを指定してMP3音声を取得
- **自動アンロード** — 最後の使用から **10分** 経過したモデルは自動的にVRAMから解放
- **複数バックエンド対応** — 複数のAivisSpeechサーバーをラウンドロビンで使用可能

## 基本的な使い方

1. `/speakers` でスピーカー一覧を取得し、使いたいスタイルの `id` を確認
2. `/speak` にテキストとスタイルIDを渡すとMP3が返ってくる
3. `/models` でVRAMのロード状況を確認できる
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
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

class SpeakRequest(BaseModel):
    model_config = {"json_schema_extra": {
        "example": {
            "text": "こんにちは、元気ですか？",
            "speaker_id": 888753760,
            "speed": 1.0,
            "pitch": 0.0,
            "intonation": 1.0,
            "volume": 1.0,
        }
    }}

    text: str = Field(..., description="読み上げるテキスト")
    speaker_id: int = Field(..., description="スピーカーのスタイルID（`/speakers` で確認できます）")
    speed: float = Field(1.0, ge=0.5, le=2.0, description="話速。1.0が標準。大きいほど速くなります（0.5〜2.0）")
    pitch: float = Field(0.0, ge=-0.15, le=0.15, description="音高。0.0が標準。正の値で高く、負の値で低くなります（-0.15〜0.15）")
    intonation: float = Field(1.0, ge=0.0, le=2.0, description="抑揚。1.0が標準。大きいほど抑揚が強くなります（0.0〜2.0）")
    volume: float = Field(1.0, ge=0.0, le=2.0, description="音量。1.0が標準（0.0〜2.0）")


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
    local_id: int | None = Field(None, description="モデル内でのローカルID")


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

    try:
        query = await backend.client.audio_query(req.text, req.speaker_id)
    except Exception as exc:
        logger.error("audio_query failed: %s", exc)
        raise HTTPException(status_code=502, detail="音声クエリの生成に失敗しました")

    query["speedScale"] = req.speed
    query["pitchScale"] = req.pitch
    query["intonationScale"] = req.intonation
    query["volumeScale"] = req.volume

    try:
        wav_bytes = await backend.client.synthesis(req.speaker_id, query)
    except Exception as exc:
        logger.error("synthesis failed: %s", exc)
        raise HTTPException(status_code=502, detail="音声合成に失敗しました")

    mp3_bytes = wav_to_mp3(wav_bytes, bitrate=settings.mp3_bitrate)
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


@app.get(
    "/health",
    summary="ヘルスチェック",
    description="サーバーが正常に動作しているか確認します。",
    tags=["システム"],
)
async def health():
    return {"status": "ok"}
