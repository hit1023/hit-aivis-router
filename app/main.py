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
from .text_replacer import TextReplacer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

pool: BackendPool
replacer: TextReplacer

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
    global pool, replacer
    replacer = TextReplacer(Path(settings.text_replacements_file))
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

    try:
        query = await backend.client.audio_query(processed_text, req.speaker_id)
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


_ALLOWED_EXTENSIONS = {".aivm", ".aivmx"}


@app.post(
    "/models/install",
    summary="モデルをファイルからインストール",
    description="""
`.aivm` / `.aivmx` ファイルをアップロードして AivisSpeech にモデルをインストールします。

- 複数バックエンドがある場合は全台に同時インストールします
- インストール後、モデルマップを自動更新します
- ファイルサイズが大きい場合はアップロードに時間がかかります（タイムアウトなし）
""",
    tags=["モデル管理"],
)
async def install_model(
    file: UploadFile = File(..., description=".aivm または .aivmx ファイル"),
):
    filename = file.filename or "model.aivm"
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"対応していないファイル形式です。{', '.join(_ALLOWED_EXTENSIONS)} のみ使用できます",
        )

    try:
        file_data = await file.read()
    except Exception as exc:
        logger.error("file read failed: %s", exc)
        raise HTTPException(status_code=400, detail="ファイルの読み込みに失敗しました")

    logger.info("Installing model: %s (%d bytes)", filename, len(file_data))

    try:
        results = await pool.install_model(filename, file_data)
    except Exception as exc:
        logger.error("install_model failed: %s", exc)
        raise HTTPException(status_code=502, detail="モデルのインストールに失敗しました")

    errors = [str(r) for r in results if isinstance(r, Exception)]
    if errors:
        logger.error("install errors: %s", errors)
        raise HTTPException(
            status_code=502,
            detail="一部のバックエンドでインストールに失敗しました: " + "; ".join(errors),
        )

    return {
        "filename": filename,
        "size_bytes": len(file_data),
        "installed_backends": len(results),
        "message": f"{len(results)}台のバックエンドにインストールしました",
    }


_UI_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AIVIS Router - モデル管理</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh;padding:24px}
h1{font-size:1.5rem;font-weight:700;color:#fff;margin-bottom:4px}
.subtitle{color:#64748b;font-size:.875rem;margin-bottom:32px}
h2{font-size:1rem;font-weight:600;color:#cbd5e1;margin-bottom:16px}
.card{background:#1e2130;border:1px solid #2d3148;border-radius:12px;padding:24px;margin-bottom:24px}
.drop-zone{border:2px dashed #3d4566;border-radius:8px;padding:40px;text-align:center;cursor:pointer;transition:all .2s}
.drop-zone:hover,.drop-zone.drag-over{border-color:#6366f1;background:#1a1d2e}
.drop-zone p{color:#64748b;margin-bottom:8px}
.drop-zone .hint{font-size:.75rem;color:#475569}
.file-name{margin-top:12px;font-size:.875rem;color:#818cf8;word-break:break-all}
input[type=file]{display:none}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 20px;border-radius:8px;border:none;cursor:pointer;font-size:.875rem;font-weight:600;transition:all .2s}
.btn-primary{background:#6366f1;color:#fff}
.btn-primary:hover:not(:disabled){background:#4f46e5}
.btn-primary:disabled{background:#3d4566;color:#64748b;cursor:not-allowed}
.btn-sm{padding:5px 12px;font-size:.75rem;border-radius:6px}
.btn-danger{background:transparent;color:#f87171;border:1px solid #7f1d1d}
.btn-danger:hover{background:#7f1d1d22}
.actions{margin-top:16px;display:flex;gap:12px;align-items:center}
.progress{height:4px;background:#2d3148;border-radius:2px;margin-top:16px;overflow:hidden;display:none}
.progress-bar{height:100%;background:linear-gradient(90deg,#6366f1,#818cf8);animation:indeterminate 1.5s infinite}
@keyframes indeterminate{0%{transform:translateX(-100%)}100%{transform:translateX(400%)}}
.alert{padding:12px 16px;border-radius:8px;font-size:.875rem;margin-top:12px;display:none}
.alert-success{background:#14532d22;border:1px solid #166534;color:#86efac}
.alert-error{background:#7f1d1d22;border:1px solid #991b1b;color:#fca5a5}
table{width:100%;border-collapse:collapse;font-size:.875rem}
th{text-align:left;color:#64748b;font-weight:500;padding:8px 12px;border-bottom:1px solid #2d3148}
td{padding:10px 12px;border-bottom:1px solid #1a1d2e;vertical-align:middle}
tr:last-child td{border-bottom:none}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:9999px;font-size:.7rem;font-weight:600}
.badge-loaded{background:#14532d33;color:#86efac}
.badge-unloaded{background:#1e2130;color:#475569;border:1px solid #2d3148}
.dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.text-muted{color:#475569;font-size:.75rem}
.spinner{width:14px;height:14px;border:2px solid #3d4566;border-top-color:#6366f1;border-radius:50%;animation:spin .6s linear infinite;display:none}
@keyframes spin{to{transform:rotate(360deg)}}
.refresh-btn{background:transparent;border:1px solid #2d3148;color:#64748b;padding:5px 10px;border-radius:6px;cursor:pointer;font-size:.75rem}
.refresh-btn:hover{border-color:#6366f1;color:#818cf8}
.header-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
</style>
</head>
<body>
<h1>AIVIS Router</h1>
<p class="subtitle">モデル管理 UI</p>

<div class="card">
  <h2>モデルのインストール</h2>
  <div class="drop-zone" id="dropZone">
    <p>ここに .aivm / .aivmx ファイルをドロップ</p>
    <p class="hint">または クリックしてファイルを選択</p>
    <div class="file-name" id="fileName"></div>
  </div>
  <input type="file" id="fileInput" accept=".aivm,.aivmx">
  <div class="actions">
    <button class="btn btn-primary" id="installBtn" disabled>インストール</button>
    <div class="spinner" id="spinner"></div>
  </div>
  <div class="progress" id="progress"><div class="progress-bar"></div></div>
  <div class="alert alert-success" id="successAlert"></div>
  <div class="alert alert-error" id="errorAlert"></div>
</div>

<div class="card">
  <div class="header-row">
    <h2 style="margin-bottom:0">インストール済みモデル</h2>
    <button class="refresh-btn" id="refreshBtn">更新</button>
  </div>
  <table id="modelsTable">
    <thead><tr><th>モデル名</th><th>UUID</th><th>バックエンド</th><th>状態</th><th></th></tr></thead>
    <tbody id="modelsBody"><tr><td colspan="5" class="text-muted" style="text-align:center;padding:20px">読み込み中...</td></tr></tbody>
  </table>
</div>

<script>
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const fileName = document.getElementById('fileName');
const installBtn = document.getElementById('installBtn');
const spinner = document.getElementById('spinner');
const progress = document.getElementById('progress');
const successAlert = document.getElementById('successAlert');
const errorAlert = document.getElementById('errorAlert');
const modelsBody = document.getElementById('modelsBody');

let selectedFile = null;

dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  const f = e.dataTransfer.files[0];
  if (f) setFile(f);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) setFile(fileInput.files[0]);
});

function setFile(f) {
  selectedFile = f;
  const mb = (f.size / 1024 / 1024).toFixed(1);
  fileName.textContent = `${f.name}  (${mb} MB)`;
  installBtn.disabled = false;
  hideAlerts();
}

function hideAlerts() {
  successAlert.style.display = 'none';
  errorAlert.style.display = 'none';
}

installBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  hideAlerts();
  installBtn.disabled = true;
  spinner.style.display = 'block';
  progress.style.display = 'block';

  const form = new FormData();
  form.append('file', selectedFile, selectedFile.name);

  try {
    const res = await fetch('/models/install', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || `HTTP ${res.status}`);
    }
    successAlert.textContent = `✓ ${data.message}`;
    successAlert.style.display = 'block';
    selectedFile = null;
    fileName.textContent = '';
    fileInput.value = '';
    loadModels();
  } catch(e) {
    errorAlert.textContent = `エラー: ${e.message}`;
    errorAlert.style.display = 'block';
    installBtn.disabled = false;
  } finally {
    spinner.style.display = 'none';
    progress.style.display = 'none';
  }
});

async function unloadModel(uuid) {
  try {
    const res = await fetch(`/models/${encodeURIComponent(uuid)}/unload`, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    loadModels();
  } catch(e) {
    alert(`アンロード失敗: ${e.message}`);
  }
}

async function loadModels() {
  try {
    const res = await fetch('/models');
    const models = await res.json();
    if (models.length === 0) {
      modelsBody.innerHTML = '<tr><td colspan="5" class="text-muted" style="text-align:center;padding:20px">モデルがありません</td></tr>';
      return;
    }
    modelsBody.innerHTML = models.map(m => `
      <tr>
        <td><strong>${escHtml(m.model_name)}</strong>
          ${m.speakers.length ? '<br><span class="text-muted">' + m.speakers.map(s=>escHtml(s.name)).join(', ') + '</span>' : ''}
        </td>
        <td class="text-muted" style="font-family:monospace">${escHtml(m.aivm_uuid.substring(0,8))}…</td>
        <td class="text-muted">${escHtml(m.backend_url)}</td>
        <td>${m.is_loaded
          ? '<span class="badge badge-loaded"><span class="dot"></span>ロード中</span>'
          : '<span class="badge badge-unloaded">未ロード</span>'}</td>
        <td>${m.is_loaded
          ? `<button class="btn btn-sm btn-danger" onclick="unloadModel('${escHtml(m.aivm_uuid)}')">アンロード</button>`
          : ''}</td>
      </tr>`).join('');
  } catch(e) {
    modelsBody.innerHTML = `<tr><td colspan="5" style="color:#f87171;padding:16px">取得失敗: ${e.message}</td></tr>`;
  }
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

document.getElementById('refreshBtn').addEventListener('click', loadModels);
loadModels();
</script>
</body>
</html>"""


@app.get("/ui", include_in_schema=False)
async def webui() -> HTMLResponse:
    return HTMLResponse(_UI_HTML)


@app.get(
    "/health",
    summary="ヘルスチェック",
    description="サーバーが正常に動作しているか確認します。",
    tags=["システム"],
)
async def health():
    return {"status": "ok"}


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
