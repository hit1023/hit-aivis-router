"""AIVIS Router をデスクトップ向けの単体フォルダ（PyInstaller onedir）にまとめる。Mac / Windows / Linux 共通。

使い方: python desktop/build.py [出力先(既定: desktop/dist)]
  環境変数 PYTHON: venv を作るときの Python（既定: 今動かしている Python）
  環境変数 VENV  : venv の場所（既定: desktop/.venv）
PyInstaller は「動かしている OS 用」のものしか作れない（Mac で Windows 用は作れない）。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WIN = sys.platform == "win32"


def run(*cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def main() -> None:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "dist"
    venv = Path(os.environ.get("VENV", HERE / ".venv"))
    py_boot = os.environ.get("PYTHON", sys.executable)
    bindir = venv / ("Scripts" if WIN else "bin")
    py = bindir / ("python.exe" if WIN else "python")

    if not venv.is_dir():
        run(py_boot, "-m", "venv", venv)
    run(py, "-m", "pip", "install", "-q", "--upgrade", "pip")
    run(py, "-m", "pip", "install", "-q", "-r", HERE / "requirements-desktop.txt")

    (out / "work").mkdir(parents=True, exist_ok=True)

    # MP3 変換に使う libsndfile（soundfile 同梱、LGPL）が、この OS で MP3 を書けるか先に確かめる
    run(py, "-c", "import soundfile as sf; assert 'MP3' in sf.available_formats(), 'libsndfile に MP3 が無い'; print('libsndfile', sf.__libsndfile_version__, 'MP3 OK')")

    run(
        bindir / "pyinstaller", "--noconfirm", "--clean", "--onedir", "--name", "aivis-router",
        "--distpath", out, "--workpath", out / "work", "--specpath", out / "work",
        "--paths", ROOT,
        "--collect-submodules", "uvicorn",
        "--collect-submodules", "app",
        "--exclude-module", "zenoh",
        "--exclude-module", "tkinter",
        HERE / "entry.py",
        cwd=ROOT,
    )
    exe = out / "aivis-router" / ("aivis-router.exe" if WIN else "aivis-router")
    # 固めた実行ファイルだけで MP3 変換が動くか確認（libsndfile の取り込み漏れを検出）
    run(exe, "--selftest")
    print(f"OK: {exe}")


if __name__ == "__main__":
    main()
