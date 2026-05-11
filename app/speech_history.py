import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH: Optional[Path] = None
_lock = asyncio.Lock()


def init(db_path: Path) -> None:
    global _DB_PATH
    _DB_PATH = db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS speech_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT    NOT NULL,
                speaker_id  INTEGER NOT NULL,
                speaker_name TEXT,
                original_text TEXT  NOT NULL,
                processed_text TEXT NOT NULL,
                speed       REAL,
                pitch       REAL,
                intonation  REAL,
                volume      REAL,
                tempo_dynamics REAL,
                pause_length REAL,
                pause_length_scale REAL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON speech_history (created_at DESC)")
        con.commit()
    finally:
        con.close()


def _insert_sync(
    speaker_id: int,
    speaker_name: Optional[str],
    original_text: str,
    processed_text: str,
    speed: float,
    pitch: float,
    intonation: float,
    volume: float,
    tempo_dynamics: float,
    pause_length: Optional[float],
    pause_length_scale: float,
) -> None:
    if _DB_PATH is None:
        return
    created_at = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(_DB_PATH)
    try:
        con.execute(
            """
            INSERT INTO speech_history
              (created_at, speaker_id, speaker_name,
               original_text, processed_text,
               speed, pitch, intonation, volume, tempo_dynamics,
               pause_length, pause_length_scale)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                created_at, speaker_id, speaker_name,
                original_text, processed_text,
                speed, pitch, intonation, volume, tempo_dynamics,
                pause_length, pause_length_scale,
            ),
        )
        con.commit()
    finally:
        con.close()


async def record(
    speaker_id: int,
    speaker_name: Optional[str],
    original_text: str,
    processed_text: str,
    speed: float,
    pitch: float,
    intonation: float,
    volume: float,
    tempo_dynamics: float,
    pause_length: Optional[float],
    pause_length_scale: float,
) -> None:
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            _insert_sync,
            speaker_id, speaker_name, original_text, processed_text,
            speed, pitch, intonation, volume, tempo_dynamics,
            pause_length, pause_length_scale,
        )
    except Exception as exc:
        logger.warning("speech_history: failed to record: %s", exc)


def fetch_page(page: int, per_page: int, speaker_id: Optional[int] = None) -> dict:
    if _DB_PATH is None or not _DB_PATH.exists():
        return {"total": 0, "page": page, "per_page": per_page, "items": []}

    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        if speaker_id is not None:
            total = con.execute(
                "SELECT COUNT(*) FROM speech_history WHERE speaker_id = ?", (speaker_id,)
            ).fetchone()[0]
            rows = con.execute(
                """
                SELECT * FROM speech_history WHERE speaker_id = ?
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (speaker_id, per_page, (page - 1) * per_page),
            ).fetchall()
        else:
            total = con.execute("SELECT COUNT(*) FROM speech_history").fetchone()[0]
            rows = con.execute(
                "SELECT * FROM speech_history ORDER BY id DESC LIMIT ? OFFSET ?",
                (per_page, (page - 1) * per_page),
            ).fetchall()
        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "items": [dict(r) for r in rows],
        }
    finally:
        con.close()
