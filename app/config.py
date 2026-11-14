from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 音声合成のバックエンド種別。
    #   "local"       … AivisSpeech Engine（aivis_backend_urls、GPU が要る）
    #   "aivis_cloud" … Aivis Cloud API（api.aivis-project.com、従量課金・GPU 不要）
    # どちらを選んでも /speak の入出力は同じなので、呼び出し側（KIOKUPETA 等）は
    # この設定を知らなくてよい。GPU を落としている間だけ aivis_cloud にしておき、
    # 復活したら local に戻せるよう、両方の設定を .env に残しておいて構わない。
    tts_provider: str = "local"

    # --- local 用 ---
    # カンマ区切りの文字列で受け取りプロパティでリストに変換
    aivis_backend_urls: str = "http://localhost:10101"
    model_idle_timeout: int = 600  # seconds
    # 一度合成に成功した台を、同じセッション（X-Session-Id、無ければ接続元IP）では
    # この秒数のあいだ使い続ける（台ごとにモデルのロード状態が違い、台が変わると
    # 初回だけ遅くなるため）。最後の合成から無通信がこの秒数続くとセッション切れ。0で無効。
    session_sticky_ttl: int = 600
    # 起動後に各台のモデルを1本ずつ先にロードしておく（初回の応答が数十秒かかるのを防ぐ）。
    warmup_on_start: bool = True
    # 起動後も、この秒数ごとに各台を見回り、冷えているモデル（エンジン再起動など）を温め直す。
    warmup_interval: float = 60.0
    # 設定画面から編集した台の一覧の保存先。ファイルがあれば AIVIS_BACKEND_URLS より優先する。
    backends_file: str = "/data/backends.json"

    # 台を追加するとき、足りないモデル(.aivmx)をここから新しい台へ送り込む。
    # 既存の台のモデルフォルダを読み取り専用でマウントして指定する。空なら自動同期はしない。
    models_source_dir: str = ""

    # 進捗イベント配信用 Zenoh ルーターのエンドポイント（例: tcp/zenoh:7447）。空なら進程内配信のみ。
    zenoh_endpoint: str = ""

    # モデルのインストールを途中で止まっても続きから再開できるよう、受け取ったファイルと状態を置く場所。
    install_jobs_dir: str = "/data/install_jobs"
    # 台へのインストールが一時的に失敗したとき（接続できない・タイムアウト・5xx）の再試行回数と、最初の待ち秒数（3倍ずつ延ばす）。
    install_max_attempts: int = 3
    install_retry_base: float = 5.0

    # 1台が同時に受け持つ合成の数。0 なら制限しない（従来どおり、来た分がそのまま台へ届く）。
    # 1以上にすると、超えた分はルーターの中で順番待ち（先着順）になり、台が詰まって
    # 全部がタイムアウトする事態を防げる。CPU版など非力な台向け。詳しくは docs/queue-design.md。
    backend_concurrency: int = 0
    # 順番待ちの最大秒数。これを超えたら 503 + Retry-After で返す（台の不調とは扱わない）。
    backend_queue_timeout: float = 60.0

    # 管理系 API（/backends）を使うためのトークン。空なら管理系 API は無効（403）。
    admin_token: str = ""

    # --- aivis_cloud 用 ---
    aivis_cloud_api_url: str = "https://api.aivis-project.com"
    aivis_cloud_api_key: str = ""
    # 話者一覧に載せるモデル。カンマ区切りの UUID。空なら人気順の上位を自動で拾う。
    aivis_cloud_model_uuids: str = ""
    aivis_cloud_catalog_limit: int = 30
    # ユーザー辞書の UUID（Cloud 側はクライアントが UUID を決める仕様）。
    # 空なら初回起動時に自動生成してこのファイルへ保存し、以後それを使い続ける。
    aivis_cloud_user_dict_uuid: str = ""
    aivis_cloud_user_dict_uuid_file: str = "/data/aivis_cloud_user_dict_uuid.txt"
    aivis_cloud_user_dict_name: str = "AIVIS Router 辞書"
    mp3_bitrate: str = "192k"
    host: str = "0.0.0.0"
    port: int = 8000
    text_replacements_file: str = "/data/text_replacements.json"
    compound_splits_file: str = "/data/compound_splits.json"
    speaker_presets_file: str = "/data/speaker_presets.json"
    speech_history_db: str = "/data/speech_history.db"
    llm_api_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = ""  # empty = disabled; e.g. "gemma3", "llama3.2"
    llm_config_file: str = "/data/llm_config.json"  # 設定画面からの上書き(環境変数より優先)

    # --- 配置先（Docker の既定値。デスクトップ版などは環境変数で上書きする） ---
    static_dir: str = "/srv/static"      # /docs・/redoc 用の JS/CSS。無ければ /static は載せない
    seed_file: str = "/srv/name.txt"     # 置換ルールの初期インポート元。無ければ何もしない
    ffmpeg_path: str = ""                # 空なら PATH の ffmpeg。同梱版を使うときに指定する
    mp3_encoder: str = "pydub"           # "pydub"（ffmpeg を使う。サーバー版の既定）/ "soundfile"（ffmpeg 不要。デスクトップ版）

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def backend_urls(self) -> list[str]:
        return [u.strip() for u in self.aivis_backend_urls.split(",") if u.strip()]

    @property
    def is_cloud(self) -> bool:
        return self.tts_provider.strip().lower() == "aivis_cloud"

    @property
    def cloud_model_uuids(self) -> list[str]:
        return [u.strip() for u in self.aivis_cloud_model_uuids.split(",") if u.strip()]

    @property
    def mp3_bitrate_kbps(self) -> int | None:
        """"192k" のような表記を Cloud API の output_bitrate 用の整数 kbps に変換する。"""
        raw = self.mp3_bitrate.strip().lower().rstrip("k")
        try:
            return int(raw)
        except ValueError:
            return None


settings = Settings()
