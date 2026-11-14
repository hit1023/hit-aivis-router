"""AIVIS Router デスクトップ版の起動エントリ（PyInstaller 用）。

Electron などから子プロセスとして起動する想定。
  aivis-router [--port 8000] [--data-dir <dir>]

- 127.0.0.1 だけで待ち受ける（外部から見えない）
- 設定・履歴などは data-dir（既定: OS のアプリデータ領域）へ置く
- ffmpeg は実行ファイルと同梱のものを使う
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data-dir", default=os.environ.get("AIVIS_DATA_DIR", ""))
    args = ap.parse_args()

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

    # 同梱 ffmpeg（無ければ PATH の ffmpeg にフォールバック）
    ff = _bundled("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if ff.exists():
        os.environ.setdefault("FFMPEG_PATH", str(ff))

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
