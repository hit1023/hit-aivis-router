"""モデルインストールのジョブを /data に残し、ルーターの再起動後に続きから再開できるようにする。

置き場所: <root>/<job_id>/job.json（状態）と model.aivmx（受け取ったファイル）。
ジョブは常に1つだけ残す（新しいインストールを始めたら古いものは消す）。
全ての台が済んだら、ファイル（数百MB）はすぐ消す。失敗した台があるときだけ、再試行のために残す。
"""
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MODEL_FILE = "model.aivmx"
JOB_FILE = "job.json"
# 失敗したまま残っているファイルを、この秒数を過ぎたら起動時に片づける
KEEP_SECONDS = 24 * 3600


class InstallJobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _dir(self, job_id: str) -> Path:
        return self.root / job_id

    def _write_atomic(self, path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def create(self, job: dict, data: bytes) -> None:
        """新しいジョブを保存する。古いジョブは消す。"""
        self.root.mkdir(parents=True, exist_ok=True)
        for old in self.root.iterdir():
            if old.is_dir() and old.name != job["job_id"]:
                shutil.rmtree(old, ignore_errors=True)
        d = self._dir(job["job_id"])
        d.mkdir(parents=True, exist_ok=True)
        self._write_atomic(d / MODEL_FILE, data)
        self.save(job)

    def save(self, job: dict) -> None:
        d = self._dir(job["job_id"])
        if not d.is_dir():
            return  # 保存に失敗していたジョブ（ディスク不調など）は、メモリ上だけで動かす
        self._write_atomic(d / JOB_FILE, json.dumps(job, ensure_ascii=False).encode("utf-8"))

    def read_data(self, job_id: str) -> Optional[bytes]:
        p = self._dir(job_id) / MODEL_FILE
        try:
            return p.read_bytes()
        except OSError:
            return None

    def has_data(self, job_id: str) -> bool:
        return (self._dir(job_id) / MODEL_FILE).is_file()

    def discard_data(self, job_id: str) -> None:
        """ファイルだけ消す（状態 job.json は残す）。"""
        try:
            (self._dir(job_id) / MODEL_FILE).unlink()
        except OSError:
            pass

    def load_all(self) -> list[dict]:
        jobs = []
        if not self.root.is_dir():
            return jobs
        for d in self.root.iterdir():
            try:
                jobs.append(json.loads((d / JOB_FILE).read_text("utf-8")))
            except (OSError, ValueError):
                shutil.rmtree(d, ignore_errors=True)  # 壊れた・空のジョブは片づける
        jobs.sort(key=lambda j: j.get("created", 0), reverse=True)
        return jobs

    def prune(self) -> None:
        """古い失敗ジョブのファイルを片づける。"""
        now = time.time()
        for j in self.load_all():
            if j.get("state") != "running" and now - j.get("created", now) > KEEP_SECONDS:
                shutil.rmtree(self._dir(j["job_id"]), ignore_errors=True)
