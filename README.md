# AIVIS Router

AivisSpeech Engine のシンプルなラッパー API です。  
少ないパラメータで音声合成を行い、結果を **MP3** 形式で返します。

## 主な機能

- **音声合成** — テキストを指定してMP3音声を取得
- **アクセント句取得** — テキストのアクセント句・ピッチ情報を返す（WebUIのピッチ曲線表示に使用）
- **スピーカープリセット** — スピーカーごとにパラメータを保存・自動適用
- **自動アンロード** — 最後の使用から10分経過したモデルは自動的にVRAMから解放
- **複数バックエンド対応** — 複数のAivisSpeechサーバーをラウンドロビンで使用可能
- **モデル管理** — `.aivmx` ファイルのインストール・アンインストール・強制アンロードをAPIで操作可能
- **ユーザー辞書** — 固有名詞や読み方を辞書登録して音声合成精度を向上
- **テキスト前処理** — 音声合成前にテキストを置換するルールを登録（英字固有名詞の誤読対策に有効）
- **発話履歴** — 全TTS合成を自動記録（SQLite）。話者・テキスト・パラメータをページネーション付きで参照可能

---

## 必要な環境

- Docker / Docker Compose
- AivisSpeech Engine（稼働中のサーバー）

---

## セットアップ

### 1. リポジトリをクローン

```bash
git clone git@github.com:hit1023/hit-aivis-router.git
cd hit-aivis-router
```

### 2. 環境変数を設定

`.env.example` をコピーして編集します。

```bash
cp .env.example .env
```

`.env` の設定項目：

| 変数名 | デフォルト値 | 説明 |
|--------|-------------|------|
| `TTS_PROVIDER` | `local` | 合成バックエンドの種別。`local` / `aivis_cloud` |
| `MP3_BITRATE` | `192k` | MP3の出力ビットレート |
| `HOST` | `0.0.0.0` | リッスンするホストアドレス |
| `PORT` | `8000` | リッスンするポート番号 |
| `SPEECH_HISTORY_DB` | `/data/speech_history.db` | 発話履歴SQLiteファイルのパス |

`TTS_PROVIDER=local`（AivisSpeech Engine）のとき：

| 変数名 | デフォルト値 | 説明 |
|--------|-------------|------|
| `AIVIS_BACKEND_URLS` | `http://localhost:10101` | AivisSpeech Engine のURL（複数の場合はカンマ区切り） |
| `MODEL_IDLE_TIMEOUT` | `600` | モデルの自動アンロードまでの秒数 |

複数バックエンドの例：
```env
AIVIS_BACKEND_URLS=http://192.168.123.103:10101,http://192.168.123.104:10101
```

`TTS_PROVIDER=aivis_cloud`（Aivis Cloud API）のとき：

| 変数名 | デフォルト値 | 説明 |
|--------|-------------|------|
| `AIVIS_CLOUD_API_KEY` | （必須） | [Aivis Hub](https://hub.aivis-project.com/) で発行するAPIキー |
| `AIVIS_CLOUD_API_URL` | `https://api.aivis-project.com` | 通常は変更不要 |
| `AIVIS_CLOUD_MODEL_UUIDS` | （空） | `/speakers` に載せるモデルUUID（カンマ区切り）。空なら人気順で自動取得 |
| `AIVIS_CLOUD_CATALOG_LIMIT` | `30` | 自動取得するモデル件数 |
| `AIVIS_CLOUD_USER_DICT_UUID` | （空） | ユーザー辞書UUID。空なら初回起動時に自動生成して `/data` に保存 |

---

## バックエンドの切り替えについて

`TTS_PROVIDER` を変えるだけで、AivisSpeech Engine（GPUが必要）と
Aivis Cloud API（従量課金・GPU不要）を行き来できます。
**`/speak` の入出力は変わらないため、このルーターを叩いている側のアプリは無変更で動きます。**

テキスト置換・話者プリセット・発話履歴・ユーザー辞書は、
どちらのバックエンドでも共通に効きます。

`aivis_cloud` を選んだ場合の制約：

| 機能 | 挙動 |
|------|------|
| モデルのインストール / アンインストール | 501（Cloud はモデルがサービス側に常設のため概念が無い） |
| モデルの強制アンロード / 自動アンロード | 何もしない（VRAM の概念が無いため） |
| `/audio_query`（アクセント句・ピッチ情報） | 501（Cloud に合成前のクエリ取得APIが無い。WebUIのピッチ曲線表示のみ使えなくなる） |
| スタイルIDの数値 | **`local` とは一致しない**（下記） |

### スタイルIDについての注意

Aivis Cloud API は話者ごとの `local_id`（0〜31）しか返さず、AivisSpeech Engine が
使っているグローバルなスタイルID（`855257952` のような値）を公開していません。
そのため `aivis_cloud` では、ルーターが「話者UUID + ローカルスタイルID」から
決定的にIDを導出しています。

**結果として、同じ声でも `local` と `aivis_cloud` ではスタイルIDの数値が変わります。**
切り替え後に古いIDで `/speak` を呼ばれた場合は、エラーにせずカタログ先頭の話者で
合成して会話が止まらないようにしています（警告ログは出ます）。
切り替えたら `/speakers` を見て、呼び出し側の話者設定を選び直してください。

### 3. 起動

```bash
docker compose up -d
```

または `run.sh` の対話メニューを使うと、起動・更新・ログ確認などをまとめて操作できます。

```bash
./run.sh
```

詳細は [運用ガイド](docs/operations.md) を参照してください。

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
| `tempo_dynamics` | float | 0.0〜2.0 | 話す速さの緩急。大きいほど早口で生っぽい抑揚（デフォルト: 1.0） |
| `pause_length` | float\|null | 0.0〜 | 句読点などの無音時間（秒）。省略で自動 |
| `pause_length_scale` | float | 0.0〜 | 無音時間の倍率（デフォルト: 1.0） |

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

### モデルをファイルからインストール

```
POST /models/install
```

`.aivmx` ファイルを `multipart/form-data` でアップロードします。

```bash
curl -X POST http://localhost:8000/models/install \
  -F "file=@my_model.aivmx"
```

- 複数バックエンドの場合は全台に順次インストール
- ファイルサイズによっては完了まで数分かかる場合があります

---

### モデルをアンインストール

```
DELETE /models/{aivm_uuid}/uninstall
```

指定したモデルを全バックエンドサーバーから完全に削除します。  
**この操作は取り消せません。** `aivm_uuid` は `GET /models` で確認できます。

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
GET    /user_dict                  # 登録単語の一覧取得
POST   /user_dict                  # 単語を追加
PUT    /user_dict/{word_uuid}      # 単語を更新
DELETE /user_dict/{word_uuid}      # 単語を削除
POST   /user_dict/import           # CSVファイルから一括インポート（UPSERT）
GET    /user_dict/compound_splits  # 複合語の表層形分割情報を取得
```

日本語の読み方・アクセントをAivisSpeechのMeCab辞書に登録します。  
**英字の単語はカタカナに変換されて保存されるため、英字固有名詞には後述のテキスト置換ルールを使用してください。**

#### CSVファイルからの一括インポート

`POST /user_dict/import` に UTF-8 CSV をアップロードして単語を一括登録できます。

**CSVフォーマット：**
```
表層形,読み,アクセント,品詞,優先度
東京スカイツリー,トウキョウスカイツリー,5,固有名詞,5
堀田創,ホッタハジメ,3,人名,7
新田|真剣佑,アラタ|マッケンユウ,1|3,人名,7
```

- `品詞` / `優先度` は省略可（デフォルト: 固有名詞 / 5）
- 複合語は `|` で各形態素を区切る（表層形・読み・アクセントの要素数を一致させること）
- 既存の同じ表層形の単語は上書き（UPSERT）

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

### テキスト置換ルールの一括インポート

```
POST /text_replacements/import
```

`src@dst` 形式のテキストファイルを `multipart/form-data` でアップロードし、ルールを一括 UPSERT します。

```bash
curl -X POST http://localhost:8000/text_replacements/import \
  -F "file=@name.txt"
```

**ファイル形式：** 1行1ルール、`置換前@置換後`

```
Mumon@ミューモン
Claude@クロード
```

**レスポンス例：**
```json
{"inserted": 2, "updated": 1}
```

---

### 発話履歴

```
GET /history?page=1&per_page=20&speaker_id=888753760
```

記録されたTTS発話の一覧を返します。新しい順にページネーション。

| パラメータ | 型 | 説明 |
|-----------|-----|------|
| `page` | int | ページ番号（1始まり、デフォルト: 1） |
| `per_page` | int | 1ページあたりの件数（デフォルト: 20） |
| `speaker_id` | int\|null | 話者IDでフィルタリング（省略時は全話者） |

**レスポンス例：**
```json
{
  "total": 42,
  "page": 1,
  "per_page": 20,
  "items": [
    {
      "id": 42,
      "created_at": "2026-05-11T12:34:56",
      "speaker_id": 888753760,
      "speaker_name": "Anneli（ノーマル）",
      "original_text": "こんにちは",
      "processed_text": "こんにちは",
      "speed": 1.0,
      "pitch": 0.0,
      "intonation": 1.0,
      "volume": 1.0,
      "tempo_dynamics": 1.0,
      "pause_length": null,
      "pause_length_scale": 1.0
    }
  ]
}
```

履歴は `/speak` 実行後に非同期で記録され（`asyncio.create_task`）、TTS レスポンス速度に影響しません。

---

### アクセント句の取得

```
POST /audio_query?text=〇〇&speaker_id=〇〇
```

指定テキストのアクセント句・ピッチ情報を返します。WebUIのピッチ曲線表示に使用されます。

---

### スピーカープリセット

```
GET    /speaker_presets              # 全プリセット一覧
GET    /speaker_presets/{id}         # 特定スピーカーのプリセット取得
PUT    /speaker_presets/{id}         # プリセット保存
DELETE /speaker_presets/{id}         # プリセット削除
```

スピーカーごとに話速・音高・抑揚・音量・緩急・無音倍率を保存します。  
`/speak` でパラメータを省略した場合、自動的にプリセット値が適用されます。

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
- `SpeechHistory` — 発話履歴のSQLite非同期書き込み・ページネーション検索

---

## 更新履歴

### 2026-05 発話履歴・テキスト置換一括インポートの追加
- `GET /history` エンドポイントを追加（ページネーション・話者IDフィルタ対応）
- `/speak` 実行後に非同期でSQLiteへ記録（`asyncio.create_task` + `run_in_executor`）
- `POST /text_replacements/import` エンドポイントを追加（`src@dst` 形式ファイルを一括UPSERT）
- `SpeechHistory` モジュールを追加（`app/speech_history.py`）
- `SPEECH_HISTORY_DB` 環境変数でDBパスを設定可能

### 2026-05 アクセント句取得エンドポイントの追加・運用改善
- `POST /audio_query` エンドポイントを追加（WebUIのピッチ曲線表示用）
- `run.sh` に「更新 & 起動」（`git pull` → ビルド → 起動）をメニュー項目1に追加

### 2026-05 スピーカープリセット機能の追加
- `GET/PUT/DELETE /speaker_presets/{id}` エンドポイントを追加
- `/speak` でパラメータ省略時にプリセット値を自動適用
- プリセットは `/data/speaker_presets.json` に永続化

### 2026-05 モデルアンインストール機能の追加
- `DELETE /models/{aivm_uuid}/uninstall` エンドポイントを追加
- 全バックエンドから一括アンインストール・モデル状態を自動再同期
- WebUI の VRAMモニターに「🗑 削除」ボタンを追加（確認ダイアログ付き）

### 2026-05 音声パラメータの拡充（sbv2互換）
- `/speak` に `tempo_dynamics`（速さの緩急）、`pause_length`（無音秒数）、`pause_length_scale`（無音倍率）を追加
- WebUI にスライダーを追加し、リアルタイムcurlサンプルにも反映

### 2026-05 `.aivmx` モデルファイルのアップロード機能
- `POST /models/install` エンドポイントを追加（multipart/form-data）
- 複数バックエンドへの全台同時インストール
- WebUI の VRAMモニターにドラッグ＆ドロップ対応のアップロードゾーンを追加

### 2026-05 APIエンドポイントURLの環境変数化（WebUI）
- `AIVIS_API_URL` 環境変数でWebUIの接続先を設定可能に
- `docker-entrypoint.sh` でビルド済みHTMLにURLを注入する方式を採用

### 2026-05 スピーカーIDの表示（WebUI）
- VRAMモニターのモデル一覧にスタイルIDを表示
- `/speakers` と `/models` をクロス参照して表示

### 2026-05 テキスト置換ルールの自動シード機能
- コンテナ初回起動時に `name.txt` から置換ルールを自動インポート
- ボリュームが空の場合のみ動作（既存ルールは上書きしない）
- `docker-compose.yml` にボリューム名を明示し、ディレクトリ変更による再作成を防止

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
