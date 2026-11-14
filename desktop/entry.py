"""AIVIS Router デスクトップ版の起動エントリ（PyInstaller 用）。

Electron などから子プロセスとして起動する想定。
  aivis-router [--port 8000] [--data-dir <dir>]

- 127.0.0.1 だけで待ち受ける（外部から見えない）
- 設定・履歴などは data-dir（既定: OS のアプリデータ領域）へ置く
- MP3 への変換は libsndfile（soundfile、LGPL）で行う。GPL になりやすい ffmpeg は同梱しない
"""
import argparse
import os
import sys
from pathlib import Path


def _app_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AivisRouter"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "AivisRouter"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "aivis-router"


def _bundled(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def _selftest() -> int:
    """無音の WAV を MP3 に変換できるか。同梱部品（libsndfile）が欠けていると失敗する。"""
    import io
    import wave

    from app.audio import wav_to_mp3

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 44100)
    mp3 = wav_to_mp3(buf.getvalue(), "192k")
    ok = len(mp3) > 1000 and (mp3[:3] == b"ID3" or mp3[0] == 0xFF)
    print(f"selftest: mp3 {len(mp3)} bytes ->", "OK" if ok else "NG")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data-dir", default=os.environ.get("AIVIS_DATA_DIR", ""))
    ap.add_argument("--selftest", action="store_true", help="MP3 変換が同梱の部品だけで動くか確かめて終了する（ビルド検証用）")
    args = ap.parse_args()

    if args.selftest:
        os.environ.setdefault("MP3_ENCODER", "soundfile")
        sys.exit(_selftest())

    data = Path(args.data_dir) if args.data_dir else _app_data_dir()
    data.mkdir(parents=True, exist_ok=True)

    # 保存先（Docker では /data）。ユーザーが環境変数で指定済みならそちらを優先する
    for env, fname in [
        ("BACKENDS_FILE", "backends.json"),
        ("TEXT_REPLACEMENTS_FILE", "text_replacements.json"),
        ("COMPOUND_SPLITS_FILE", "compound_splits.json"),
        ("SPEAKER_PRESETS_FILE", "speaker_presets.json"),
        ("SPEECH_HISTORY_DB", "speech_history.db"),
        ("LLM_CONFIG_FILE", "llm_config.json"),
        ("AIVIS_CLOUD_USER_DICT_UUID_FILE", "aivis_cloud_user_dict_uuid.txt"),
    ]:
        os.environ.setdefault(env, str(data / fname))

    os.environ.setdefault("MP3_ENCODER", "soundfile")   # ffmpeg 不要の MP3 変換

    os.environ.setdefault("STATIC_DIR", str(_bundled("static")))
    os.environ.setdefault("SEED_FILE", str(data / "no-seed.txt"))   # 個人の初期ルールは持ち込まない
    os.environ.setdefault("WARMUP_ON_START", "false")
    os.environ["HOST"] = args.host
    os.environ["PORT"] = str(args.port)

    import uvicorn
    from app.main import app
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
