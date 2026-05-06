from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # カンマ区切りの文字列で受け取りプロパティでリストに変換
    aivis_backend_urls: str = "http://localhost:10101"
    model_idle_timeout: int = 600  # seconds
    mp3_bitrate: str = "192k"
    host: str = "0.0.0.0"
    port: int = 8000
    text_replacements_file: str = "/data/text_replacements.json"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def backend_urls(self) -> list[str]:
        return [u.strip() for u in self.aivis_backend_urls.split(",") if u.strip()]


settings = Settings()
