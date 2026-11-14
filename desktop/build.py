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

    ffmpeg = subprocess.run(
        [str(py), "-c", "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (out / "work").mkdir(parents=True, exist_ok=True)
    (out / "extra").mkdir(parents=True, exist_ok=True)
    ff_name = "ffmpeg.exe" if WIN else "ffmpeg"
    ff = out / "extra" / ff_name
    shutil.copyfile(ffmpeg, ff)
    ff.chmod(0o755)

    # --add-binary の区切りは Windows が ";"、それ以外が ":"
    sep = ";" if WIN else ":"
    run(
        bindir / "pyinstaller", "--noconfirm", "--clean", "--onedir", "--name", "aivis-router",
        "--distpath", out, "--workpath", out / "work", "--specpath", out / "work",
        "--paths", ROOT,
        "--collect-submodules", "uvicorn",
        "--collect-submodules", "app",
        "--add-binary", f"{ff}{sep}.",
        "--exclude-module", "zenoh",
        "--exclude-module", "tkinter",
        HERE / "entry.py",
        cwd=ROOT,
    )
    exe = out / "aivis-router" / ("aivis-router.exe" if WIN else "aivis-router")
    print(f"OK: {exe}")


if __name__ == "__main__":
    main()
