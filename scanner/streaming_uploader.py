"""
streaming_uploader.py

Background thread that rsyncs newly-hashed files to a Lambda host in
real time as the local scanner produces them.  Runs alongside FileHasher
so analysis on Lambda can start before the local scan finishes.

NO originals are ever deleted — this is copy-only.

Architecture
------------
- FileHasher calls ``uploader.enqueue(abs_path, file_id)`` after each hash.
- A daemon thread batches these paths and flushes via rsync every FLUSH_INTERVAL
  seconds OR when BATCH_SIZE files accumulate (whichever comes first).
- Each flushed batch is tracked in the ``uploads`` table so re-runs skip
  already-uploaded files.
- Remote path mirrors local structure under ``remote_base``:
    /mnt/c/Dev/foo/bar.py  →  {remote_base}/mnt/c/Dev/foo/bar.py
  This preserves the full path so Lambda can run its own scan with the
  exact same paths in its own DB.

Usage
-----
    uploader = StreamingUploader(
        db_path="dupes.db",
        ssh_host="ubuntu@170.9.56.76",
        ssh_key="/home/user/.ssh/joe-05012026.pem",
        remote_base="/home/ubuntu/data",
    )
    uploader.start()
    # ... scan files, calling uploader.enqueue() for each ...
    uploader.stop()   # blocks until all queued files are uploaded
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 8       # seconds between forced flushes
BATCH_SIZE = 150         # max files per rsync call
WORKER_THREADS = 3       # parallel rsync workers
_SENTINEL = None         # signals worker threads to exit


@dataclass
class _UploadItem:
    abs_path: str
    file_id: int
    enqueued_at: float = field(default_factory=time.time)


class StreamingUploader:
    """
    Streams local files to a Lambda host via rsync as they are hashed.
    Thread-safe; all public methods can be called from any thread.
    """

    def __init__(
        self,
        db_path: str,
        ssh_host: str,       # e.g. "ubuntu@170.9.56.76"
        ssh_key: str,        # path to .pem key
        remote_base: str,    # e.g. "/home/ubuntu/data"
        flush_interval: int = FLUSH_INTERVAL,
        batch_size: int = BATCH_SIZE,
        dry_run: bool = False,
    ):
        self.db_path = db_path
        self.ssh_host = ssh_host
        self.ssh_key = str(Path(ssh_key).expanduser())
        self.remote_base = remote_base.rstrip("/")
        self.flush_interval = flush_interval
        self.batch_size = batch_size
        self.dry_run = dry_run

        self._queue: queue.Queue[Optional[_UploadItem]] = queue.Queue()
        self._batch: list[_UploadItem] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._flush_thread: Optional[threading.Thread] = None
        self._worker_threads: list[threading.Thread] = []
        self._work_queue: queue.Queue[Optional[list[_UploadItem]]] = queue.Queue()

        # Stats
        self._files_uploaded = 0
        self._bytes_uploaded = 0
        self._errors = 0

        self._already_uploaded: set[int] = set()
        self._load_already_uploaded()

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start background flush thread and rsync worker pool."""
        for i in range(WORKER_THREADS):
            t = threading.Thread(target=self._rsync_worker, daemon=True,
                                 name=f"uploader-worker-{i}")
            t.start()
            self._worker_threads.append(t)

        self._flush_thread = threading.Thread(target=self._flush_loop,
                                              daemon=True, name="uploader-flush")
        self._flush_thread.start()
        log.info("StreamingUploader started → %s:%s", self.ssh_host, self.remote_base)

    def enqueue(self, abs_path: str, file_id: int) -> None:
        """
        Called by FileHasher for every successfully hashed file.
        Non-blocking; returns immediately.
        """
        if file_id in self._already_uploaded:
            return
        self._queue.put(_UploadItem(abs_path=abs_path, file_id=file_id))

    def stop(self, timeout: float = 300.0) -> dict:
        """
        Signal stop and block until all queued uploads finish or timeout.
        Returns stats dict.
        """
        log.info("StreamingUploader stopping — draining queue (%d items)…",
                 self._queue.qsize())
        self._stop_event.set()

        # Drain the input queue into the batch
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    with self._lock:
                        self._batch.append(item)
            except queue.Empty:
                break

        # Flush remaining batch
        self._flush_batch(force=True)

        # Signal workers to stop
        for _ in self._worker_threads:
            self._work_queue.put(_SENTINEL)

        deadline = time.time() + timeout
        for t in self._worker_threads:
            remaining = max(0.0, deadline - time.time())
            t.join(timeout=remaining)

        if self._flush_thread:
            self._flush_thread.join(timeout=5.0)

        stats = {
            "files_uploaded": self._files_uploaded,
            "bytes_uploaded": self._bytes_uploaded,
            "errors": self._errors,
        }
        log.info("StreamingUploader stopped: %s", stats)
        return stats

    @property
    def stats(self) -> dict:
        return {
            "files_uploaded": self._files_uploaded,
            "bytes_uploaded": self._bytes_uploaded,
            "errors": self._errors,
            "queue_depth": self._queue.qsize(),
        }

    # ── internals ─────────────────────────────────────────────────────────────

    def _load_already_uploaded(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute("SELECT file_id FROM uploads").fetchall()
            self._already_uploaded = {r[0] for r in rows}
            conn.close()
            if self._already_uploaded:
                log.info("Skipping %d already-uploaded files.", len(self._already_uploaded))
        except Exception:
            pass

    def _flush_loop(self) -> None:
        """Periodically drains the incoming queue into the batch and flushes."""
        while not self._stop_event.is_set():
            deadline = time.time() + self.flush_interval
            while time.time() < deadline and not self._stop_event.is_set():
                try:
                    item = self._queue.get(timeout=0.5)
                    if item is not None:
                        with self._lock:
                            self._batch.append(item)
                        if len(self._batch) >= self.batch_size:
                            self._flush_batch(force=False)
                except queue.Empty:
                    pass
            self._flush_batch(force=False)

    def _flush_batch(self, force: bool = False) -> None:
        with self._lock:
            if not self._batch:
                return
            if not force and len(self._batch) < self.batch_size:
                # wait for more, unless we're stopping
                if not self._stop_event.is_set():
                    return
            batch = self._batch[:]
            self._batch.clear()

        if batch:
            self._work_queue.put(batch)

    def _rsync_worker(self) -> None:
        while True:
            batch = self._work_queue.get()
            if batch is _SENTINEL:
                self._work_queue.task_done()
                return
            try:
                self._do_rsync(batch)
            except Exception as exc:
                log.error("rsync worker error: %s", exc)
                self._errors += 1
            finally:
                self._work_queue.task_done()

    def _do_rsync(self, batch: list[_UploadItem]) -> None:
        """Write a temp file-list and call rsync --files-from."""
        # Filter out missing files (e.g. deleted between hash and upload)
        valid = [item for item in batch if os.path.exists(item.abs_path)]
        if not valid:
            return

        # Write a temp files-from list (rsync expects paths relative to /)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as fh:
            list_path = fh.name
            for item in valid:
                # rsync --files-from paths are relative to source root "/"
                rel = item.abs_path.lstrip("/")
                fh.write(rel + "\n")

        try:
            cmd = [
                "rsync",
                "-az",                          # archive + compress
                "--relative",                   # preserve relative path from source root
                "--no-implied-dirs",
                "--files-from", list_path,
                "-e", f"ssh -i {self.ssh_key} -o StrictHostKeyChecking=no",
                "/",                            # source root (paths are relative to /)
                f"{self.ssh_host}:{self.remote_base}/",
            ]

            if self.dry_run:
                log.info("[DRY RUN] would rsync %d files: %s…",
                         len(valid), valid[0].abs_path)
                self._record_uploads(valid)
                return

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode == 0:
                total_bytes = sum(
                    os.path.getsize(item.abs_path)
                    for item in valid
                    if os.path.exists(item.abs_path)
                )
                self._files_uploaded += len(valid)
                self._bytes_uploaded += total_bytes
                self._record_uploads(valid)
                log.debug("Uploaded %d files (%.1f KB)",
                          len(valid), total_bytes / 1024)
            else:
                log.warning("rsync exited %d: %s", result.returncode,
                            result.stderr[:300])
                self._errors += len(valid)
        except subprocess.TimeoutExpired:
            log.error("rsync timed out for batch of %d files", len(valid))
            self._errors += len(valid)
        finally:
            try:
                os.unlink(list_path)
            except OSError:
                pass

    def _record_uploads(self, items: list[_UploadItem]) -> None:
        now = time.time()
        rows = [
            (item.file_id, item.abs_path,
             f"{self.remote_base}/{item.abs_path.lstrip('/')}", now)
            for item in items
        ]
        try:
            conn = sqlite3.connect(self.db_path)
            conn.executemany(
                "INSERT OR REPLACE INTO uploads(file_id, abs_path, remote_path, uploaded_at) "
                "VALUES (?,?,?,?)",
                rows,
            )
            conn.commit()
            conn.close()
            for item in items:
                self._already_uploaded.add(item.file_id)
        except Exception as exc:
            log.warning("Failed to record uploads in DB: %s", exc)


def make_uploader_from_env(db_path: str) -> Optional["StreamingUploader"]:
    """
    Construct a StreamingUploader from environment variables if all are set.
    Returns None if streaming is not configured.

    Required env vars:
        LAMBDA_SSH_HOST  e.g. ubuntu@170.9.56.76
        LAMBDA_SSH_KEY   e.g. /home/user/.ssh/joe-05012026.pem
        LAMBDA_DATA_DIR  e.g. /home/ubuntu/data
    """
    host = os.environ.get("LAMBDA_SSH_HOST")
    key = os.environ.get("LAMBDA_SSH_KEY")
    remote = os.environ.get("LAMBDA_DATA_DIR")
    if not all([host, key, remote]):
        return None
    return StreamingUploader(
        db_path=db_path,
        ssh_host=host,
        ssh_key=key,
        remote_base=remote,
    )
