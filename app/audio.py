import io

from app.config import settings

# MP3 エンコーダは 2 通り（どちらも WAV → MP3 だけを行う）。
#   pydub     : 既定。サーバー版（Docker）で使う。ffmpeg が必要（PATH か FFMPEG_PATH）。
#   soundfile : デスクトップ版で使う。libsndfile（LGPL）の MP3 書き出しだけで済み、ffmpeg は不要。
#               ffmpeg の配布ビルドは GPL のものが多く、同梱を避けるための選択。
if settings.mp3_encoder == "pydub" and settings.ffmpeg_path:
    from pydub import AudioSegment

    # 同梱の ffmpeg を使う（PATH に無い環境向け）
    AudioSegment.converter = settings.ffmpeg_path


def _kbps(bitrate: str) -> int:
    s = str(bitrate).strip().lower().rstrip("k")
    try:
        return max(32, min(320, int(s)))
    except ValueError:
        return 192


def _wav_to_mp3_soundfile(wav_bytes: bytes, bitrate: str) -> bytes:
    import soundfile as sf

    data, rate = sf.read(io.BytesIO(wav_bytes), dtype="float32")
    # libsndfile の MP3 は compression_level（0.0=最高音質・320kbps 〜 1.0=32kbps）でビットレートを決める
    level = (320 - _kbps(bitrate)) / (320 - 32)
    buf = io.BytesIO()
    sf.write(
        buf, data, rate, format="MP3", subtype="MPEG_LAYER_III",
        bitrate_mode="CONSTANT", compression_level=min(1.0, max(0.0, level)),
    )
    return buf.getvalue()


def wav_to_mp3(wav_bytes: bytes, bitrate: str = "192k") -> bytes:
    if settings.mp3_encoder == "soundfile":
        return _wav_to_mp3_soundfile(wav_bytes, bitrate)
    from pydub import AudioSegment

    segment = AudioSegment.from_wav(io.BytesIO(wav_bytes))
    buf = io.BytesIO()
    segment.export(buf, format="mp3", bitrate=bitrate)
    return buf.getvalue()
