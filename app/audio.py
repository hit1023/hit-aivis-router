import io
from pydub import AudioSegment

from app.config import settings

if settings.ffmpeg_path:
    # 同梱の ffmpeg を使う（PATH に無い環境向け）
    AudioSegment.converter = settings.ffmpeg_path


def wav_to_mp3(wav_bytes: bytes, bitrate: str = "192k") -> bytes:
    segment = AudioSegment.from_wav(io.BytesIO(wav_bytes))
    buf = io.BytesIO()
    segment.export(buf, format="mp3", bitrate=bitrate)
    return buf.getvalue()
