import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PRESET_KEYS = {"speed", "pitch", "intonation", "volume", "tempo_dynamics", "pause_length", "pause_length_scale"}


class SpeakerPresetManager:
    """スピーカーIDごとの音声パラメータプリセットを管理する。"""

    def __init__(self, file: Path) -> None:
        self._file = file
        self._presets: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._file.exists():
            try:
                self._presets = json.loads(self._file.read_text(encoding="utf-8"))
                logger.info("Speaker presets loaded: %d entries", len(self._presets))
            except Exception as exc:
                logger.warning("Failed to load speaker presets: %s", exc)
                self._presets = {}

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps(self._presets, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_all(self) -> dict[str, dict]:
        return dict(self._presets)

    def get(self, speaker_id: int) -> dict | None:
        return self._presets.get(str(speaker_id))

    def set(self, speaker_id: int, params: dict) -> None:
        # 許可されたキーのみ保存
        filtered = {k: v for k, v in params.items() if k in _PRESET_KEYS}
        self._presets[str(speaker_id)] = filtered
        self._save()
        logger.info("Preset saved for speaker %d", speaker_id)

    def delete(self, speaker_id: int) -> bool:
        key = str(speaker_id)
        if key not in self._presets:
            return False
        del self._presets[key]
        self._save()
        logger.info("Preset deleted for speaker %d", speaker_id)
        return True
