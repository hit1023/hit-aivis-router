# AIVIS Router

AivisSpeech Engine のシンプルなラッパー API です。  
少ないパラメータで音声合成を行い、結果を **MP3** 形式で返します。

## 主な機能

- **音声合成** — テキストを指定してMP3音声を取得
- **自動アンロード** — 最後の使用から10分経過したモデルは自動的にVRAMから解放
- **複数バックエンド対応** — 複数のAivisSpeechサーバーをラウンドロビンで使用可能
- **モデル管理** — URLからのモデルインストール・強制アンロードをAPIで操作可能

---

## 必要な環境

- Docker / Docker Compose
- AivisSpeech Engine（稼働中のサーバー）

---

## セットアップ

### 1. リポジトリをクローン

```bash
git clone git@github.com:hit1023/me-aivis-router.git
cd me-aivis-router
```

### 2. 環境変数を設定

`.env.example` をコピーして編集します。

```bash
cp .env.example .env
```

`.env` の設定項目：

| 変数名 | デフォルト値 | 説明 |
|--------|-------------|------|
| `AIVIS_BACKEND_URLS` | `http://localhost:10101` | AivisSpeech Engine のURL（複数の場合はカンマ区切り） |
| `MODEL_IDLE_TIMEOUT` | `600` | モデルの自動アンロードまでの秒数 |
| `MP3_BITRATE` | `192k` | MP3の出力ビットレート |
| `HOST` | `0.0.0.0` | リッスンするホストアドレス |
| `PORT` | `8000` | リッスンするポート番号 |

複数バックエンドの例：
```env
AIVIS_BACKEND_URLS=http://192.168.123.103:10101,http://192.168.123.104:10101
```

### 3. 起動

```bash
docker compose up -d
```

---

## API エンドポイント

### 音声合成

```
POST /speak
```

**リクエスト例：**

```json
{
  "text": "こんにちは、元気ですか？",
  "speaker_id": 888753760,
  "speed": 1.0,
  "pitch": 0.0,
  "intonation": 1.0,
  "volume": 1.0
}
```

**パラメータ：**

| フィールド | 型 | 範囲 | 説明 |
|-----------|-----|------|------|
| `text` | string | — | 読み上げるテキスト |
| `speaker_id` | int | — | スタイルID（`/speakers` で確認） |
| `speed` | float | 0.5〜2.0 | 話速（デフォルト: 1.0） |
| `pitch` | float | -0.15〜0.15 | 音高（デフォルト: 0.0） |
| `intonation` | float | 0.0〜2.0 | 抑揚（デフォルト: 1.0） |
| `volume` | float | 0.0〜2.0 | 音量（デフォルト: 1.0） |

**レスポンス：** `audio/mpeg`（MP3バイナリ）

---

### スピーカー一覧

```
GET /speakers
```

利用可能なスピーカーとスタイルの一覧を返します。  
各スタイルの `id` を `/speak` の `speaker_id` として使用します。

---

### モデルのVRAMロード状態

```
GET /models
```

インストールされている音声モデルの一覧とVRAMロード状態を返します。

| フィールド | 説明 |
|-----------|------|
| `is_loaded: true` | VRAMにロード済み。即座に合成可能 |
| `is_loaded: false` | アンロード済み。次回リクエスト時に自動ロード |

---

### モデルをURLからインストール

```
POST /models/install
```

```json
{
  "url": "https://example.com/my_model.aivmx"
}
```

- AivisSpeech Engine が直接ダウンロードできない場合はラッパーが代理ダウンロードして転送
- 複数バックエンドの場合は全台に同時インストール

---

### モデルを強制アンロード

```
POST /models/{aivm_uuid}/unload
```

指定したモデルをVRAMから強制アンロードします。  
次回の `/speak` リクエスト時に自動で再ロードされます。

---

### ヘルスチェック

```
GET /health
```

```json
{"status": "ok"}
```

---

## APIドキュメント

起動後、以下のURLでSwagger UIを確認できます。

```
http://localhost:8000/docs
```

ReDocはこちら：

```
http://localhost:8000/redoc
```

---

## アーキテクチャ

```
クライアント
    ↓
AIVIS Router (FastAPI)
    ↓ ラウンドロビン
┌─────────────────────────┐
│  AivisSpeech Engine #1  │
│  AivisSpeech Engine #2  │
│  ...                    │
└─────────────────────────┘
```

- `BackendPool` — 複数バックエンドをラウンドロビンで管理
- `ModelManager` — バックエンドごとのモデルロード状態とアイドルタイムアウトを管理
- `AivisClient` — AivisSpeech Engine の HTTP API クライアント
