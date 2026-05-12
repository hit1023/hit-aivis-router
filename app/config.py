from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # カンマ区切りの文字列で受け取りプロパティでリストに変換
    aivis_backend_urls: str = "http://localhost:10101"
    model_idle_timeout: int = 600  # seconds
    mp3_bitrate: str = "192k"
    host: str = "0.0.0.0"
    port: int = 8000
    text_replacements_file: str = "/data/text_replacements.json"
    speaker_presets_file: str = "/data/speaker_presets.json"
    speech_history_db: str = "/data/speech_history.db"
    llm_api_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = ""  # empty = disabled; e.g. "gemma3", "llama3.2"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def backend_urls(self) -> list[str]:
        return [u.strip() for u in self.aivis_backend_urls.split(",") if u.strip()]


settings = Settings()
