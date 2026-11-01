import asyncio
import base64
import json
import logging
import uuid
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Optional

import hmac

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware


from .audio import wav_to_mp3
from .backend_pool import (
    BackendPool,
    BackendUnreachable,
    InvalidBackendURL,
    validate_backend_url,
    ModelLoadFailed,
    SpeakerNotFound,
    SynthesisFailed,
)
from .events import bus
from .splitter import split_sentences
from .config import settings
from .speaker_preset import SpeakerPresetManager
from .text_replacer import TextReplacer
from . import speech_history
from . import kana_checker

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


def _client_key(request: Request) -> str:
    """セッションの代わりに使う接続元。NPM 経由なら X-Forwarded-For の先頭。"""
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "")


def _reject_if_cloud(action: str) -> None:
    """Aivis Cloud では意味を持たない操作（VRAM やモデルファイルの管理）を明確に断る。

    Cloud はモデルがサービス側に常設されているため、インストール / アンインストールの
    概念自体が無い。曖昧な 502 ではなく 501 で理由を返す。
    """
    if settings.is_cloud:
        raise HTTPException(
            status_code=501,
            detail=f"{action}は Aivis Cloud バックエンドでは利用できません（TTS_PROVIDER=local のときのみ）",
        )


def _resolve_cloud_user_dict_uuid() -> str:
    """Aivis Cloud のユーザー辞書 UUID を決める（.env → 保存ファイル → 新規生成の順）。

    Cloud 側の辞書はサーバーが UUID を払い出すのではなく、クライアントが指定した UUID で
    upsert される仕様。毎回違う UUID を使うと辞書が増え続けて登録済みの単語を見失うため、
    一度決めた値を /data に残して使い回す。
    """
    if settings.aivis_cloud_user_dict_uuid:
        return settings.aivis_cloud_user_dict_uuid

    path = Path(settings.aivis_cloud_user_dict_uuid_file)
    try:
        if path.exists():
            saved = path.read_text(encoding="utf-8").strip()
            if saved:
                return saved
    except OSError as exc:
        logger.warning("ユーザー辞書 UUID の読み込みに失敗: %s", exc)

    new_uuid = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_uuid, encoding="utf-8")
        logger.info("Aivis Cloud のユーザー辞書 UUID を新規発行しました: %s", new_uuid)
    except OSError as exc:
        # 保存できなくても動作はする（次回起動時にまた別の辞書になるだけ）
        logger.warning("ユーザー辞書 UUID の保存に失敗（次回起動時に再発行されます）: %s", exc)
    return new_uuid


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, replacer, presets
    speech_history.init(Path(settings.speech_history_db))
    replacer = TextReplacer(Path(settings.text_replacements_file), seed_file=Path("/srv/name.txt"))
    presets = SpeakerPresetManager(Path(settings.speaker_presets_file))
    pool = BackendPool(settings.backend_urls, idle_timeout=settings.model_idle_timeout)
    if pool.is_cloud:
        # Cloud のユーザー辞書は「クライアントが UUID を決める」仕様なので、
        # 一度決めた UUID をファイルに残して以後ずっと同じ辞書を使う。
        cloud = pool.cloud_backend()
        if cloud is not None:
            cloud.set_user_dict_uuid(_resolve_cloud_user_dict_uuid())
    try:
        await pool.initialize()
    except Exception as exc:
        logger.warning("Backend initialization failed (will retry on first request): %s", exc)
    await bus.start(settings.zenoh_endpoint)
    cleanup_task = asyncio.create_task(pool.start_cleanup_loop())
    async def _warm():
        await pool.warmup()
        await pool.keep_warm_loop(settings.warmup_interval)
    warmup_task = asyncio.create_task(_warm()) if settings.warmup_on_start and not pool.is_cloud else None
    if pool.is_cloud:
        logger.info("TTS backend: Aivis Cloud API (%s)", settings.aivis_cloud_api_url)
    else:
        logger.info("TTS backend: AivisSpeech Engine %s", settings.backend_urls)
    yield
    cleanup_task.cancel()
    if warmup_task:
        warmup_task.cancel()
    await bus.close()
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
    expose_headers=["X-Backend"],
)

@app.middleware("http")
async def no_cache_middleware(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response

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
async def speak(req: SpeakRequest, request: Request, x_session_id: Optional[str] = Header(default=None)):
    # 同じセッションは、前に成功した台を使い続ける（台が変わると初回だけ遅くなるため）。
    # セッションは X-Session-Id、無ければ接続元 IP（NPM 経由なら X-Forwarded-For の先頭）。
    session = (x_session_id or "").strip()[:128] or _client_key(request)
    mp3_bytes, served_by = await _speak_core(req, session)
    return Response(content=mp3_bytes, media_type="audio/mpeg", headers={"X-Backend": served_by})


async def _speak_core(req: SpeakRequest, session: str) -> tuple[bytes, str]:
    """1 件分の合成（/speak と /speak_stream で共通）。(mp3, 応答した台) を返す。"""
    backend = pool.pick(session, req.speaker_id)
    served_by = getattr(backend, "url", "Aivis Cloud")   # どの台が応答したか

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

    # 実際の合成はバックエンド（ローカルの AivisSpeech Engine / Aivis Cloud API）に委ねる。
    # ここから下はどちらのバックエンドでも共通の処理なので、テキスト置換・プリセット・
    # 発話履歴は TTS_PROVIDER を切り替えても同じように効く。
    synth_params = {
        "speed": speed,
        "pitch": pitch,
        "intonation": intonation,
        "volume": volume,
        "tempo_dynamics": tempo_dynamics,
        "pause_length": pause_length,
        "pause_length_scale": pause_length_scale,
    }
    try:
        try:
            mp3_bytes = await backend.synthesize_mp3(processed_text, req.speaker_id, synth_params)
        except (ModelLoadFailed, SynthesisFailed) as first_exc:
            # 台の不調（接続不可・タイムアウト・5xx）なら、別の台で1回だけやり直す
            alt = pool.pick_other(backend) if getattr(first_exc, "node_fault", False) else None
            if alt is None:
                raise
            logger.warning("%s で失敗したため %s で再試行します: %s", served_by, alt.url, first_exc)
            served_by = alt.url
            backend = alt
            mp3_bytes = await alt.synthesize_mp3(processed_text, req.speaker_id, synth_params)
        pool.remember(session, backend)
    except SpeakerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ModelLoadFailed as exc:
        logger.error("Model load failed: %s", exc)
        raise HTTPException(status_code=503, detail="音声モデルの読み込みに失敗しました")
    except SynthesisFailed as exc:
        logger.error("synthesis failed: %s", exc)
        raise HTTPException(status_code=502, detail="音声合成に失敗しました")

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
        backend=served_by,
    ))

    return mp3_bytes, served_by


class SpeakStreamRequest(SpeakRequest):
    max_len: int = Field(60, ge=10, le=300, description="1 文の最大文字数。超えた分は読点で区切る")


@app.post(
    "/speak_stream",
    summary="音声合成（文ごとに逐次返却）",
    description="""
テキストを文ごとに分けて合成し、**できた文から順に** NDJSON（1 行 1 JSON）で返します。

各行: `{"i": 0, "n": 3, "text": "文", "audio": "<MP3 の base64>", "backend": "台"}`
失敗したときは `{"error": "..."}` の行で終わります。

先頭の文が出来次第再生を始めれば、長文でも体感の待ち時間が最初の 1 文ぶんになります。
合成は同じセッション・同じ台で順番に行うので、「1 モデル 1 ノード」の方針は崩れません。
""",
    tags=["音声合成"],
)
async def speak_stream(req: SpeakStreamRequest, request: Request, x_session_id: Optional[str] = Header(default=None)):
    session = (x_session_id or "").strip()[:128] or _client_key(request)
    sentences = split_sentences(replacer.apply(req.text), max_len=req.max_len) or [req.text]
    base = req.model_dump(exclude={"max_len"})

    async def gen():
        n = len(sentences)
        for i, sent in enumerate(sentences):
            try:
                mp3, served_by = await _speak_core(SpeakRequest(**{**base, "text": sent}), session)
            except SpeakerNotFound as exc:
                yield json.dumps({"error": str(exc)}, ensure_ascii=False) + "\n"
                return
            except (ModelLoadFailed, SynthesisFailed) as exc:
                logger.error("stream synthesis failed: %s", exc)
                yield json.dumps({"error": "音声合成に失敗しました"}, ensure_ascii=False) + "\n"
                return
            yield json.dumps({"i": i, "n": n, "text": sent, "backend": served_by,
                              "audio": base64.b64encode(mp3).decode()}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})


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


_install_job: dict = {}
_install_tasks: set = set()


@app.post(
    "/models/install",
    summary="カスタムモデルをファイルからインストール",
    description="""
`.aivmx` 形式のモデルファイルをアップロードして全バックエンドサーバーにインストールします。

- 複数バックエンドが設定されている場合は全台へ並列にインストールします
- 応答は即座に `202` で返り、インストールは裏で進みます。結果は `GET /models/install/status` で確認します
- インストール完了後、スピーカーマッピングを自動で更新します
- ファイルサイズによっては完了まで数分かかる場合があります
""",
    tags=["モデル管理"],
)
async def install_model(file: UploadFile = File(...)):
    _reject_if_cloud("モデルのインストール")
    filename = file.filename or "model.aivmx"
    if not filename.split("?")[0].endswith(".aivmx"):
        raise HTTPException(status_code=400, detail="ファイルは .aivmx 形式を指定してください")
    if _install_job.get("state") == "running":
        raise HTTPException(status_code=409, detail="別のインストールが実行中です。完了までお待ちください")
    data = await file.read()
    job_id = uuid.uuid4().hex[:8]
    _install_job.clear()
    _install_job.update({"job_id": job_id, "state": "running", "filename": filename, "results": [], "message": "インストール中..."})
    bus.publish(f"aivis/install/{job_id}/start", {"type": "start", "job_id": job_id, "filename": filename})
    task = asyncio.create_task(_run_install(job_id, filename, data))
    _install_tasks.add(task)
    task.add_done_callback(_install_tasks.discard)
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "filename": filename, "state": "running", "message": "受け付けました。インストールは裏で進みます"},
    )


async def _run_install(job_id: str, filename: str, data: bytes) -> None:
    """アップロード応答を待たせないよう、インストールはバックグラウンドで行う。"""
    def on_event(ev: dict) -> None:
        bus.publish(f"aivis/install/{job_id}/node", {"type": "node", "job_id": job_id, **ev})

    try:
        results = await pool.install_model(filename, data, on_event=on_event)
        failed = [r for r in results if not r["success"]]
        ok = len(results) - len(failed)
        _install_job.update({
            "state": "failed" if ok == 0 else "done",
            "results": results,
            "message": (
                "全バックエンドでインストールに失敗しました" if ok == 0
                else f"{ok}/{len(results)} バックエンドにインストールしました"
            ),
        })
    except Exception as exc:
        logger.error("install job crashed: %s", exc)
        _install_job.update({"state": "failed", "message": f"インストールに失敗しました: {exc}"})
    bus.publish(f"aivis/install/{job_id}/finish", {
        "type": "finish", "job_id": job_id,
        "state": _install_job.get("state"), "message": _install_job.get("message"),
    })


@app.get(
    "/models/install/status",
    summary="モデルインストールの進行状況",
    description="直近の `/models/install` の状態（running / done / failed）を返します。",
    tags=["モデル管理"],
)
async def install_model_status():
    return dict(_install_job) or {"state": "idle"}


@app.get(
    "/events/install",
    summary="モデルインストールの進捗（SSE）",
    description="""
Server-Sent Events でインストールの進捗を流します。イベントは Zenoh の pub/sub
(`aivis/install/**`) を購読して中継したものです。

- 接続直後に、現在の状態（`snapshot`）を1回送ります
- `start` / `node`（台ごとの started・done・failed）/ `finish` が順に届きます
- 15秒ごとにコメント行（`: ping`）を送り、プロキシに切られないようにします
""",
    tags=["モデル管理"],
)
async def install_events():
    async def stream():
        q = bus.subscribe()
        try:
            snap = dict(_install_job) or {"state": "idle"}
            yield f"event: snapshot\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    _reject_if_cloud("モデルのアンインストール")
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
    description="""
テキストの音声クエリ（アクセント句・ピッチ情報）を返します。アクセント可視化に使用できます。

**このエンドポイントは `TTS_PROVIDER=local`（AivisSpeech Engine）専用です。**
Aivis Cloud API には合成前のアクセント句を取得する経路が無いため、
`TTS_PROVIDER=aivis_cloud` では 501 を返します（WebUI のピッチ曲線表示のみ使えなくなります）。
""",
    tags=["情報取得"],
)
async def audio_query_endpoint(text: str, speaker_id: int):
    _reject_if_cloud("アクセント句の取得")
    backend = pool.pick(None, speaker_id)   # モデルの定位置の台へ（別の台に重複ロードしない）
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

def _load_compound_splits() -> dict[str, dict]:
    """複合語の分割情報を読み込む。
    形式: {結合表層形: {"surface": [...], "pronunciation": [...], "accent_type": [...]}}
    後方互換: 旧形式 {結合表層形: [...]} も自動変換する。
    """
    path = Path(settings.compound_splits_file)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # 旧形式（値がリスト）を新形式に変換
        migrated = {}
        for k, v in data.items():
            if isinstance(v, list):
                migrated[k] = {"surface": v}
            else:
                migrated[k] = v
        return migrated
    except Exception:
        return {}


def _save_compound_splits(splits: dict[str, dict]) -> None:
    path = Path(settings.compound_splits_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(splits, ensure_ascii=False, indent=2), encoding="utf-8")


def _upsert_compound_split(
    splits: dict[str, dict],
    surface: list[str],
    pronunciation: list[str],
    accent_type: list[int],
) -> None:
    """複合語の分割情報を更新する（インプレース）。"""
    key = "".join(surface)
    splits[key] = {
        "surface": surface,
        "pronunciation": pronunciation,
        "accent_type": accent_type,
    }


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


@app.get(
    "/user_dict/compound_splits",
    summary="複合語の表層形分割情報を取得",
    tags=["ユーザー辞書"],
)
async def get_compound_splits() -> dict:
    """インポート時に保存した複合語の表層形分割情報を返す。
    キー: 結合表層形（例: "新田真剣佑"）、値: 分割リスト（例: ["新田", "真剣佑"]）
    """
    return _load_compound_splits()


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
    # 同じ表層形がすでに登録されている場合は上書き更新（upsert）
    joined_surface = "".join(req.surface)
    try:
        existing = await pool.get_user_dict(enable_compound_accent=True)
    except Exception as exc:
        logger.error("get_user_dict failed in upsert check: %s", exc)
        existing = {}
    existing_uuid = next(
        (uid for uid, e in existing.items()
         if (e.get("surface") == joined_surface
             or (isinstance(e.get("surface"), list) and "".join(e["surface"]) == joined_surface))),
        None,
    )
    try:
        if existing_uuid:
            await pool.update_user_dict_word(
                word_uuid=existing_uuid,
                surface=req.surface,
                pronunciation=req.pronunciation,
                accent_type=req.accent_type,
                word_type=req.word_type.value,
                priority=req.priority,
            )
            word_uuid = existing_uuid
            logger.info("upsert: updated existing entry %s (surface=%r)", existing_uuid, joined_surface)
        else:
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
    if len(req.surface) > 1:
        splits = _load_compound_splits()
        _upsert_compound_split(splits, req.surface, req.pronunciation, req.accent_type)
        _save_compound_splits(splits)
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
    if len(req.surface) > 1:
        splits = _load_compound_splits()
        _upsert_compound_split(splits, req.surface, req.pronunciation, req.accent_type)
        _save_compound_splits(splits)
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
    # 削除前に surface を取得して compound_splits から除去する
    try:
        user_dict = await pool.get_user_dict(enable_compound_accent=False)
        entry = user_dict.get(word_uuid)
        surface_key = "".join(entry.get("surface", [])) if entry else None
    except Exception:
        surface_key = None

    try:
        await pool.delete_user_dict_word(word_uuid)
    except Exception as exc:
        logger.error("delete_user_dict_word failed: %s", exc)
        raise HTTPException(status_code=502, detail="単語の削除に失敗しました")

    if surface_key:
        splits = _load_compound_splits()
        if surface_key in splits:
            del splits[surface_key]
            _save_compound_splits(splits)

    return Response(status_code=204)

_WORD_TYPE_FROM_LABEL: dict[str, str] = {v: k for k, v in _WORD_TYPE_LABELS.items()}


@app.post(
    "/user_dict/import",
    summary="ユーザー辞書をCSVファイルからインポート（UPSERT）",
    description="""
CSVファイルからユーザー辞書に単語を一括登録します。

**CSVフォーマット（UTF-8）**:
```
表層形,読み,アクセント,品詞,優先度
東京スカイツリー,トウキョウスカイツリー,5,固有名詞,5
堀田創,ホッタハジメ,3,人名,7
新田|真剣佑,アラタ|マッケンユウ,1|3,人名,7
```

- `品詞` と `優先度` は省略可能（デフォルト: 固有名詞 / 5）
- 複合語は `|` で各形態素を区切る（表層形・読み・アクセントの要素数を一致させること）
- 既存の同じ表層形を持つ単語は上書き（UPSERT）されます
- `#` で始まる行と空行はスキップされます
""",
    tags=["ユーザー辞書"],
)
async def import_user_dict(file: UploadFile = File(...)) -> dict:
    import csv
    import io

    try:
        content = (await file.read()).decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="ファイルのエンコーディングが UTF-8 ではありません")

    rows: list[dict] = []
    errors: list[dict] = []
    header_skipped = False

    for lineno, row in enumerate(csv.reader(io.StringIO(content)), start=1):
        if not row or not any(c.strip() for c in row):
            continue
        first = row[0].strip()
        if first.startswith("#"):
            continue
        if not header_skipped and first in ("表層形", "surface"):
            header_skipped = True
            continue

        surface_raw = first
        pron_raw    = row[1].strip() if len(row) > 1 else ""
        accent_raw  = row[2].strip() if len(row) > 2 else ""
        wtype_raw   = row[3].strip() if len(row) > 3 else ""
        priority_raw = row[4].strip() if len(row) > 4 else ""

        if not surface_raw or not pron_raw or not accent_raw:
            errors.append({"row": lineno, "reason": "表層形・読み・アクセントは必須です"})
            continue

        surface_list = [s.strip() for s in surface_raw.split("|")]
        pron_list    = [p.strip() for p in pron_raw.split("|")]
        try:
            accent_list = [int(a.strip()) for a in accent_raw.split("|")]
        except ValueError:
            errors.append({"row": lineno, "reason": f"アクセントが数値ではありません: {accent_raw}"})
            continue

        if not (len(surface_list) == len(pron_list) == len(accent_list)):
            errors.append({
                "row": lineno,
                "reason": (
                    f"表層形・読み・アクセントの要素数が一致しません "
                    f"({len(surface_list)}/{len(pron_list)}/{len(accent_list)})"
                ),
            })
            continue

        if wtype_raw:
            word_type = _WORD_TYPE_FROM_LABEL.get(wtype_raw) or (
                wtype_raw if wtype_raw in _WORD_TYPE_LABELS else None
            )
            if word_type is None:
                errors.append({"row": lineno, "reason": f"品詞が不明です: {wtype_raw}"})
                continue
        else:
            word_type = WordType.PROPER_NOUN.value

        try:
            priority = max(0, min(10, int(priority_raw))) if priority_raw else 5
        except ValueError:
            priority = 5

        rows.append({
            "surface": surface_list,
            "pronunciation": pron_list,
            "accent_type": accent_list,
            "word_type": word_type,
            "priority": priority,
        })

    if not rows and not errors:
        raise HTTPException(status_code=422, detail="有効な行が1件も見つかりませんでした")

    try:
        existing = await pool.get_user_dict(enable_compound_accent=False)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"既存辞書の取得に失敗しました: {exc}")

    surface_to_uuid: dict[str, str] = {
        w.get("surface", ""): uuid
        for uuid, w in existing.items()
        if w.get("surface")
    }

    inserted = updated = 0
    for r in rows:
        surface_key = "".join(r["surface"])
        try:
            if surface_key in surface_to_uuid:
                await pool.update_user_dict_word(
                    word_uuid=surface_to_uuid[surface_key],
                    surface=r["surface"],
                    pronunciation=r["pronunciation"],
                    accent_type=r["accent_type"],
                    word_type=r["word_type"],
                    priority=r["priority"],
                )
                updated += 1
            else:
                await pool.add_user_dict_word(
                    surface=r["surface"],
                    pronunciation=r["pronunciation"],
                    accent_type=r["accent_type"],
                    word_type=r["word_type"],
                    priority=r["priority"],
                )
                inserted += 1
        except Exception as exc:
            errors.append({"row": surface_key, "reason": str(exc)})

    # 複合語の分割情報を保存
    splits = _load_compound_splits()
    for r in rows:
        if len(r["surface"]) > 1:
            _upsert_compound_split(splits, r["surface"], r["pronunciation"], r["accent_type"])
    _save_compound_splits(splits)

    return {"inserted": inserted, "updated": updated, "errors": errors}


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


@app.post(
    "/text_replacements/import",
    summary="テキスト置換ルールをファイルからインポート",
    description="""
`src@dst` 形式のテキストファイルを受け取り、置換ルールをインポートします。

- `mode=upsert`（デフォルト）: 既存ルールを保持しつつ追加・上書き
- `mode=replace`: 既存ルールを**全削除**してファイルの内容で置き換え
- `@` を含まない行はスキップされます
""",
    tags=["テキスト置換"],
)
async def import_text_replacements(
    file: UploadFile = File(...),
    mode: str = Form("upsert"),
) -> dict:
    try:
        content = (await file.read()).decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="ファイルはUTF-8エンコーディングである必要があります")

    rules: dict[str, str] = {}
    skipped = 0
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if "@" not in line:
            skipped += 1
            continue
        src, _, dst = line.partition("@")
        src = src.strip()
        dst = dst.strip()
        if not src:
            skipped += 1
            continue
        rules[src] = dst

    if not rules:
        raise HTTPException(status_code=422, detail="有効なルールが1件も見つかりませんでした")

    if mode == "replace":
        inserted, updated = replacer.replace_all(rules)
    else:
        inserted, updated = replacer.upsert_many(rules)
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "message": f"{inserted} 件追加、{updated} 件更新、{skipped} 行スキップしました",
    }


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


# ------------------------------------------------------------------
# 読みチェック LLM判定
# ------------------------------------------------------------------

class KanaVerifyItem(BaseModel):
    text: str
    kana: str


def _llm_cfg() -> dict:
    """LLM接続設定。設定画面の保存値(JSON)があれば環境変数より優先する。"""
    cfg = {
        "api_url": settings.llm_api_url,
        "api_key": settings.llm_api_key,
        "model": settings.llm_model,
        "provider": "openai",
        "source": "env",
    }
    try:
        saved = json.loads(Path(settings.llm_config_file).read_text("utf-8"))
        for k in ("api_url", "api_key", "model", "provider"):
            if isinstance(saved.get(k), str) and saved[k] != "":
                cfg[k] = saved[k]
        cfg["source"] = "settings"
    except (OSError, ValueError):
        pass
    return cfg


@app.get(
    "/kana_verify/status",
    summary="LLM読み判定の設定状態",
    tags=["読みチェック"],
)
async def kana_verify_status() -> dict:
    cfg = _llm_cfg()
    return {
        "enabled": bool(cfg["model"]),
        "model": cfg["model"] or None,
        "api_url": cfg["api_url"],
        "provider": cfg["provider"],
    }


@app.post(
    "/kana_verify",
    summary="LLMで読みの正誤を判定",
    description="""
単語とカナ読みのペアをLLMに渡し、読みが正しいか一括判定します。

モデルが未設定（設定画面 or `LLM_MODEL` 環境変数）の場合は 503 を返します。
""",
    tags=["読みチェック"],
)
async def kana_verify_endpoint(items: list[KanaVerifyItem]) -> list[dict]:
    cfg = _llm_cfg()
    if not cfg["model"]:
        raise HTTPException(
            503,
            detail="LLMが設定されていません。設定タブの「LLM読み判定」でモデル名を設定してください",
        )
    try:
        results = await kana_checker.verify_readings(
            [{"text": it.text, "kana": it.kana} for it in items],
            api_url=cfg["api_url"],
            api_key=cfg["api_key"],
            model=cfg["model"],
            provider=cfg["provider"],
            cache_file=str(Path(settings.llm_config_file).with_name("kana_llm_cache.json")),
        )
        return results
    except Exception as exc:
        logger.error("kana_verify failed: %s", exc)
        raise HTTPException(500, detail=str(exc))


# ---------------------------------------------------------------------------
# バックエンド（AivisSpeech Engine の台）の管理
# ---------------------------------------------------------------------------

def require_admin(x_admin_token: Optional[str] = Header(None)) -> None:
    """管理系 API 用の認証。ADMIN_TOKEN が未設定なら機能ごと無効にする。"""
    if not settings.admin_token:
        raise HTTPException(status_code=403, detail="管理機能は無効です（ADMIN_TOKEN が未設定）")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=401, detail="管理トークンが正しくありません")


class LLMConfigRequest(BaseModel):
    provider: str = Field("openai", description="openai（OpenAI互換API）/ claude_cli（Claude Code ヘッドレス）")
    api_url: str = Field("", description="OpenAI互換APIのベースURL（空=未変更）")
    model: str = Field("", description="モデル名（空にして保存すると無効化）")
    api_key: Optional[str] = Field(None, description="APIキー。省略=変更しない / 空文字=削除")


@app.get(
    "/llm_config",
    summary="LLM読み判定の設定（キーは返さない）",
    tags=["読みチェック"],
    dependencies=[Depends(require_admin)],
)
async def llm_config_get() -> dict:
    cfg = _llm_cfg()
    return {
        "api_url": cfg["api_url"],
        "model": cfg["model"],
        "provider": cfg["provider"],
        "has_key": bool(cfg["api_key"]) and cfg["api_key"] != "ollama",
        "source": cfg["source"],
    }


@app.put(
    "/llm_config",
    summary="LLM読み判定の設定を保存",
    tags=["読みチェック"],
    dependencies=[Depends(require_admin)],
)
async def llm_config_put(req: LLMConfigRequest) -> dict:
    cur = _llm_cfg()
    if req.provider not in ("openai", "claude_cli"):
        raise HTTPException(422, detail="provider は openai か claude_cli です")
    url = req.api_url.strip() or cur["api_url"]
    if not url.startswith(("http://", "https://")):
        raise HTTPException(422, detail="API URL は http:// か https:// で始めてください")
    key = cur["api_key"] if req.api_key is None else (req.api_key.strip() or "ollama")
    data = {"api_url": url.rstrip("/"), "api_key": key, "model": req.model.strip(), "provider": req.provider}
    path = Path(settings.llm_config_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    except OSError as exc:
        raise HTTPException(500, detail=f"保存に失敗しました: {exc}")
    return await llm_config_get()


class BackendURLRequest(BaseModel):
    url: str = Field(..., description="台の URL（例: http://192.168.0.22:10101）")


async def _backends_response() -> dict:
    return {
        "provider": "aivis_cloud" if settings.is_cloud else "local",
        "editable": not settings.is_cloud,
        "backends": await pool.backend_status(),
    }


@app.get(
    "/backends/status",
    summary="台の稼働状況（認証不要・URLなし）",
    description="台の番号・死活・応答時間・ロード済みモデル数・処理件数を返します。URL は含みません（再起動でカウンタはリセット）。",
    tags=["バックエンド管理"],
)
async def backends_status() -> dict:
    return {
        "provider": "aivis_cloud" if settings.is_cloud else "local",
        "backends": await pool.public_status(),
    }


@app.get(
    "/backends",
    summary="バックエンド一覧と状態",
    description="登録されている台の URL・死活・応答時間・ロード済みモデル数を返します。`X-Admin-Token` が必要です。",
    tags=["バックエンド管理"],
    dependencies=[Depends(require_admin)],
)
async def list_backends() -> dict:
    return await _backends_response()


@app.post(
    "/backends/test",
    summary="台への接続テスト（登録はしない）",
    tags=["バックエンド管理"],
    dependencies=[Depends(require_admin)],
)
async def test_backend(req: BackendURLRequest) -> dict:
    _reject_if_cloud("バックエンドの接続テスト")
    try:
        url = await validate_backend_url(req.url)
    except InvalidBackendURL as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return await pool.probe(url)


@app.post(
    "/backends",
    status_code=201,
    summary="台を追加",
    description="接続・モデル（話者）の揃い具合を確認し、最初の台のユーザー辞書をコピーしてから追加します。`/data/backends.json` に保存され、再起動は不要です。",
    tags=["バックエンド管理"],
    dependencies=[Depends(require_admin)],
)
async def add_backend(req: BackendURLRequest) -> dict:
    _reject_if_cloud("バックエンドの追加")
    try:
        added = await pool.add_backend(req.url)
    except InvalidBackendURL as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except BackendUnreachable as exc:
        raise HTTPException(status_code=502, detail=f"台に接続できませんでした: {exc}")
    return {**await _backends_response(), "added": added}


class MasterRequest(BaseModel):
    url: str


@app.put(
    "/backends/master",
    summary="マスター台を指定",
    description="モデルと辞書の基準にする台を指定します。単語の登録・更新・削除とモデルのインストールはマスターに先に行い、他の台が追従します。切り替え時は新しいマスターの辞書を他の台へ反映します。",
    tags=["バックエンド管理"],
    dependencies=[Depends(require_admin)],
)
async def set_master(req: MasterRequest) -> dict:
    _reject_if_cloud("マスターの指定")
    try:
        result = await pool.set_master(req.url)
    except KeyError:
        raise HTTPException(status_code=404, detail="その台は登録されていません")
    except InvalidBackendURL as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {**await _backends_response(), "master": result["master"], "dict": result["dict"]}


class EnabledRequest(BaseModel):
    url: str
    enabled: bool = Field(..., description="true で有効化、false で無効化（振り分けから外す・登録は残す）")


@app.put(
    "/backends/enabled",
    summary="台の有効/無効を切り替え",
    description="無効にした台は /speak の振り分けとウォームアップの対象外になります（登録は残ります）。有効に戻すときは、無効の間に変わったモデル・辞書を同期してから戻します（台の追加と同じ手順で、モデルが多いと state=syncing になり完了後に自動で戻ります）。マスターと最後の有効な1台は無効化できません。",
    tags=["バックエンド管理"],
    dependencies=[Depends(require_admin)],
)
async def set_backend_enabled(req: EnabledRequest) -> dict:
    _reject_if_cloud("台の無効化")
    added = None
    try:
        if req.enabled:
            added = await pool.enable_backend(req.url)
        else:
            pool.set_enabled(req.url, False)
    except KeyError:
        raise HTTPException(status_code=404, detail="その台は登録されていません")
    except InvalidBackendURL as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except BackendUnreachable as exc:
        raise HTTPException(status_code=502, detail=f"台に接続できませんでした（無効のままです）: {exc}")
    return {**await _backends_response(), "added": added}


@app.post(
    "/backends/rebalance",
    summary="ロード済みモデルを台の間で均す",
    description="ロード済みモデルの数が偏っているとき、多い台から少ない台へモデルを移します（少ない台に載せて温めてから、多い台で降ろします）。同じモデルが2台に載ることはありません。",
    tags=["バックエンド管理"],
    dependencies=[Depends(require_admin)],
)
async def rebalance_backends() -> dict:
    _reject_if_cloud("再配置")
    moved = await pool.rebalance()
    return {"moved": moved}


@app.delete(
    "/backends",
    summary="台を削除",
    description="最後の1台は削除できません。",
    tags=["バックエンド管理"],
    dependencies=[Depends(require_admin)],
)
async def remove_backend(url: str = Query(..., description="削除する台の URL")) -> dict:
    _reject_if_cloud("バックエンドの削除")
    try:
        await pool.remove_backend(url)
    except KeyError:
        raise HTTPException(status_code=404, detail="その台は登録されていません")
    except InvalidBackendURL as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return await _backends_response()
