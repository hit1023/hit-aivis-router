#!/bin/sh
# AIVIS Router をデスクトップ向けの単体フォルダ（PyInstaller onedir）にまとめる。
# 本体は build.py（Mac / Windows 共通）。これは Mac / Linux 向けの呼び出し口。
# 使い方: desktop/build.sh [出力先(既定: desktop/dist)]
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3.12}" exec "${PYTHON:-python3.12}" "$HERE/build.py" "$@"
