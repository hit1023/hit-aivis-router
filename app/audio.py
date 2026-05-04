import io
from pydub import AudioSegment


def wav_to_mp3(wav_bytes: bytes, bitrate: str = "192k") -> bytes:
    segment = AudioSegment.from_wav(io.BytesIO(wav_bytes))
    buf = io.BytesIO()
    segment.export(buf, format="mp3", bitrate=bitrate)
    return buf.getvalue()
