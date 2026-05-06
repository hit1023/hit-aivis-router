# AIVIS Router

AivisSpeech Engine のシンプルなラッパー API です。  
少ないパラメータで音声合成を行い、結果を **MP3** 形式で返します。

## 主な機能

- **音声合成** — テキストを指定してMP3音声を取得
- **自動アンロード** — 最後の使用から10分経過したモデルは自動的にVRAMから解放
- **複数バックエンド対応** — 複数のAivisSpeechサーバーをラウンドロビンで使用可能
- **モデル管理** — 強制アンロードをAPIで操作可能
- **ユーザー辞書** — 固有名詞や読み方を辞書登録して音声合成精度を向上
- **テキスト前処理** — 音声合成前にテキストを置換するルールを登録（英字固有名詞の誤読対策に有効）

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

### ユーザー辞書

```
GET  /user_dict                  # 登録単語の一覧取得
POST /user_dict                  # 単語を追加
PUT  /user_dict/{word_uuid}      # 単語を更新
DELETE /user_dict/{word_uuid}    # 単語を削除
```

日本語の読み方・アクセントをAivisSpeechのMeCab辞書に登録します。  
**英字の単語はカタカナに変換されて保存されるため、英字固有名詞には後述のテキスト置換ルールを使用してください。**

---

### テキスト置換ルール

```
GET    /text_replacements        # ルール一覧取得
POST   /text_replacements        # ルールを追加・更新
DELETE /text_replacements?src=〇〇  # ルールを削除
```

`/speak` に渡されたテキストを、AivisSpeechへ送る**前**に文字列置換します。  
英字の固有名詞をカタカナに変換するなど、MeCabの誤読を確実に防ぐのに有効です。

**追加例：**
```json
POST /text_replacements
{"src": "Mumon", "dst": "ミューモン"}
```

**単語辞書との違い：**

| | 単語辞書 | テキスト置換 |
|---|---|---|
| 対象 | 日本語の読み・アクセント調整 | 英字・記号など何でも |
| 英字の登録 | カタカナに変換されてしまう | そのまま登録できる |
| 確実性 | コンテキストにより効かない場合あり | 必ず効く |

ルールは `/data/text_replacements.json` に永続化されます。

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
- `TextReplacer` — 音声合成前テキスト前処理（最長マッチ優先の文字列置換）

---

## 更新履歴

### 2025-05 テキスト前処理レイヤーの追加（MeCab誤読対策）
- `TextReplacer` クラスを新規追加（JSON永続化・最長マッチ優先）
- `/speak` 実行前にテキスト置換を適用するプリプロセスレイヤーを実装
- `GET/POST/DELETE /text_replacements` エンドポイントを追加
- `/data` ボリュームで置換ルールを永続化（`docker-compose.yml` にボリューム追加）
- **背景**: 英字の固有名詞（例: `Mumon`）はユーザー辞書が効かないケースがあるため、
  ルーター側で確実に変換する仕組みを追加

### 2025-05 ユーザー辞書機能の追加
- `GET/POST/PUT/DELETE /user_dict` エンドポイントを追加
- 複数バックエンドへの辞書同期（GET は1台目のみ、書き込みは全台）
- WebUI に「📖 単語辞書」タブを追加（アクセントビジュアライザー付き）

### 2025-05 WebUI の追加
- AivisWebUI（Nginx + 単一HTMLファイル）を新規追加
- 音声合成フォーム・スピーカー選択・パラメータ調整・VRAMロード状態表示

### 初期リリース
- FastAPI ベースのラッパー API
- MP3変換・複数バックエンド対応・モデル自動アンロード
