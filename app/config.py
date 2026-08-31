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
