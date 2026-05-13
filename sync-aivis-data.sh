#!/bin/bash
set -e

SRC_DIR=""
DST_DIR=""
DST_PROJECT=""

case "$1" in
  "1to2")
    SRC_DIR="/home/hit/docker/mm-aivis-router/data"
    DST_DIR="/home/hit/docker/mm-aivis-router2/data"
    DST_PROJECT="mm-aivis-router2"
    ;;
  "2to1")
    SRC_DIR="/home/hit/docker/mm-aivis-router2/data"
    DST_DIR="/home/hit/docker/mm-aivis-router/data"
    DST_PROJECT="mm-aivis-router"
    ;;
  *)
    echo "Usage: $0 {1to2|2to1}"
    exit 1
    ;;
esac

echo "Stopping $DST_PROJECT..."
docker compose -p "$DST_PROJECT" -f "/home/hit/docker/$DST_PROJECT/docker-compose.yml" stop

echo "Copying data: $SRC_DIR -> $DST_DIR"
cp -f "$SRC_DIR/compound_splits.json"   "$DST_DIR/compound_splits.json"
cp -f "$SRC_DIR/speaker_presets.json"   "$DST_DIR/speaker_presets.json"
cp -f "$SRC_DIR/text_replacements.json" "$DST_DIR/text_replacements.json"
cp -f "$SRC_DIR/speech_history.db"      "$DST_DIR/speech_history.db"

echo "Starting $DST_PROJECT..."
docker compose -p "$DST_PROJECT" -f "/home/hit/docker/$DST_PROJECT/docker-compose.yml" start

echo "Done."
