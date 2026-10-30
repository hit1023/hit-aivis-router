import sqlite3

from app import speech_history


def _rec(**kw):
    base = dict(speaker_id=1, speaker_name="A", original_text="x", processed_text="x",
                speed=1.0, pitch=0.0, intonation=1.0, volume=1.0, tempo_dynamics=1.0,
                pause_length=None, pause_length_scale=1.0)
    base.update(kw)
    speech_history._insert_sync(**base)


def test_backend_is_recorded_and_returned(tmp_path):
    speech_history.init(tmp_path / "h.db")
    _rec(backend="http://192.168.0.22:10102")
    _rec()  # 台が不明な行
    items = speech_history.fetch_page(1, 10)["items"]
    assert [i["backend"] for i in items] == [None, "http://192.168.0.22:10102"]


def test_existing_db_gets_backend_column(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE speech_history (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
        speaker_id INTEGER NOT NULL, speaker_name TEXT, original_text TEXT NOT NULL, processed_text TEXT NOT NULL,
        speed REAL, pitch REAL, intonation REAL, volume REAL, tempo_dynamics REAL, pause_length REAL, pause_length_scale REAL)""")
    con.commit(); con.close()
    speech_history.init(db)
    _rec(backend="b")
    assert speech_history.fetch_page(1, 10)["items"][0]["backend"] == "b"
