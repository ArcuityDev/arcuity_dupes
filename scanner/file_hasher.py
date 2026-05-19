"""
scanner/file_hasher.py

Core hashing engine.  Walks a directory tree, computes SHA-256 and git-blob
SHA-1 for every file, persists results to SQLite, and derives per-directory
tree hashes for later duplicate-detection.

Public API
----------
    from scanner.file_hasher import FileHasher
    hasher = FileHasher(db_path="dupes.db", scan_id="scan-001")
    hasher.scan(root_path="/mnt/c/Users/Administrator/Projects")
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Generator

from tqdm import tqdm

logger = logging.getLogger(__name__)

# SQLite schema — only the tables this module writes are created here.
# The full schema lives in db/schema.py; this is a safe local bootstrap.
_DDL = """
CREATE TABLE IF NOT EXISTS scan_meta (
    id INTEGER PRIMARY KEY,
    scan_id TEXT UNIQUE,
    started_at REAL,
    completed_at REAL,
    root_path TEXT,
    total_files INTEGER,
    total_dirs INTEGER,
    total_bytes INTEGER,
    status TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT,
    drive TEXT,
    abs_path TEXT UNIQUE,
    rel_path TEXT,
    filename TEXT,
    extension TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    git_sha1 TEXT,
    mtime REAL,
    scanned_at REAL
);

CREATE INDEX IF NOT EXISTS idx_files_sha256   ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_git_sha1 ON files(git_sha1);
CREATE INDEX IF NOT EXISTS idx_files_drive    ON files(drive);

CREATE TABLE IF NOT EXISTS directories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT,
    drive TEXT,
    abs_path TEXT UNIQUE,
    parent_path TEXT,
    depth INTEGER,
    name TEXT,
    file_count INTEGER,
    total_size_bytes INTEGER,
    tree_hash TEXT,
    scanned_at REAL
);

CREATE INDEX IF NOT EXISTS idx_dirs_tree_hash ON directories(tree_hash);
CREATE INDEX IF NOT EXISTS idx_dirs_abs_path  ON directories(abs_path);
"""

_CHUNK = 65_536          # 64 KB read chunks
_PROGRESS_INTERVAL = 100  # flush scan_meta every N files


# ------------------------------------------------------------------ #
# Low-level hash helpers
# ------------------------------------------------------------------ #

def _sha256_of_file(path: str) -> tuple[str, int]:
    """
    Stream *path* through SHA-256.
    Returns (hex_digest, size_in_bytes).
    Zero-byte files return the SHA-256 of the empty string.
    """
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _git_sha1_of_file(path: str, size: int) -> str:
    """
    Compute the git blob object SHA-1 for an already-known file.

    Git blob SHA-1 = SHA1("blob {size}\\0{file-content}")

    We reuse the already-known *size* so we only read the file once
    total (caller has already read it for SHA-256).  We stream again
    here to avoid holding the full content in memory for large files.
    """
    h = hashlib.sha1(usedforsecurity=False)
    header = f"blob {size}\0".encode()
    h.update(header)
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _drive_from_path(abs_path: str) -> str:
    """
    Extract drive letter/name from an absolute path.

    /mnt/c/foo  -> "c"
    /mnt/d/bar  -> "d"
    /home/user  -> "home"
    /           -> "root"
    """
    parts = Path(abs_path).parts
    # parts = ('/', 'mnt', 'c', ...)
    if len(parts) >= 3 and parts[1] == "mnt":
        return parts[2].lower()
    if len(parts) >= 2:
        return parts[1].lower()
    return "root"


def _rel_path(abs_path: str, root_path: str) -> str:
    """Return a POSIX-style relative path, never starting with '/'."""
    try:
        return str(Path(abs_path).relative_to(root_path))
    except ValueError:
        return abs_path


def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


# ------------------------------------------------------------------ #
# Directory tree estimation
# ------------------------------------------------------------------ #

def _estimate_file_count(root_path: str, exclude_patterns: list[str]) -> int:
    """
    Fast estimate of total file count without hashing.
    Caps at 5 million to avoid hanging on enormous trees.
    """
    count = 0
    cap = 5_000_000
    try:
        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            if _matches_any(os.path.basename(dirpath), exclude_patterns):
                dirnames[:] = []
                continue
            # Prune excluded sub-dirs in-place.
            dirnames[:] = [
                d for d in dirnames
                if not _matches_any(d, exclude_patterns)
                and not os.path.islink(os.path.join(dirpath, d))
            ]
            count += len(filenames)
            if count >= cap:
                return cap
    except OSError:
        pass
    return count


# ------------------------------------------------------------------ #
# Main class
# ------------------------------------------------------------------ #

class FileHasher:
    """
    Walk *root_path*, hash every file, persist to SQLite.

    Parameters
    ----------
    db_path:  Path to (or filename of) the SQLite database.
    scan_id:  Unique identifier for this scan run, written into every row.
    """

    def __init__(self, db_path: str, scan_id: str, uploader=None) -> None:
        self.db_path = db_path
        self.scan_id = scan_id
        self._con: sqlite3.Connection | None = None
        self._uploader = uploader  # optional StreamingUploader instance

    # ---------------------------------------------------------------- #
    # Connection management
    # ---------------------------------------------------------------- #

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA temp_store=MEMORY")
        con.executescript(_DDL)
        con.commit()
        return con

    # ---------------------------------------------------------------- #
    # Resume helpers
    # ---------------------------------------------------------------- #

    def _already_hashed(
        self, con: sqlite3.Connection, abs_path: str, mtime: float
    ) -> bool:
        """
        Return True if the DB already has a fresh record for this path.
        A record is "fresh" when its stored mtime matches the current mtime
        (meaning the file has not been modified since the last scan).
        """
        row = con.execute(
            "SELECT mtime FROM files WHERE abs_path = ?", (abs_path,)
        ).fetchone()
        if row is None:
            return False
        return abs(row[0] - mtime) < 0.01   # 10 ms tolerance

    # ---------------------------------------------------------------- #
    # scan_meta helpers
    # ---------------------------------------------------------------- #

    def _upsert_meta(self, con: sqlite3.Connection, **kwargs) -> None:
        fields = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" * len(kwargs))
        values = list(kwargs.values())
        con.execute(
            f"INSERT INTO scan_meta ({fields}) VALUES ({placeholders}) "
            f"ON CONFLICT(scan_id) DO UPDATE SET "
            + ", ".join(f"{k}=excluded.{k}" for k in kwargs.keys()),
            values,
        )
        con.commit()

    # ---------------------------------------------------------------- #
    # Core walk
    # ---------------------------------------------------------------- #

    def _walk(
        self,
        root_path: str,
        exclude_patterns: list[str],
    ) -> Generator[tuple[str, list[str], list[str]], None, None]:
        """
        os.walk wrapper that:
        - skips symlink directories
        - skips excluded patterns
        - logs permission errors instead of raising
        """
        def _onerror(exc: OSError) -> None:
            logger.warning("Permission denied (skipping): %s", exc.filename)

        for dirpath, dirnames, filenames in os.walk(
            root_path, onerror=_onerror, followlinks=False
        ):
            basename = os.path.basename(dirpath)
            if _matches_any(basename, exclude_patterns):
                dirnames[:] = []
                continue

            # Prune symlink sub-dirs and excluded sub-dirs in-place.
            dirnames[:] = sorted(
                d for d in dirnames
                if not _matches_any(d, exclude_patterns)
                and not os.path.islink(os.path.join(dirpath, d))
            )

            yield dirpath, dirnames, filenames

    # ---------------------------------------------------------------- #
    # Per-file hashing
    # ---------------------------------------------------------------- #

    def _hash_file(
        self,
        con: sqlite3.Connection,
        abs_path: str,
        root_path: str,
        mtime: float,
    ) -> tuple[str, str, int, int] | None:
        """
        Hash one file.  Returns (sha256, git_sha1, size_bytes, file_id) or None on error.
        Writes to the `files` table.
        """
        drive = _drive_from_path(abs_path)
        rel = _rel_path(abs_path, root_path)
        p = Path(abs_path)
        filename = p.name
        extension = p.suffix.lower()

        try:
            sha256, size = _sha256_of_file(abs_path)
            git_sha1 = _git_sha1_of_file(abs_path, size)
        except OSError as exc:
            logger.warning("Cannot read file (skipping): %s — %s", abs_path, exc)
            return None

        now = time.time()
        cur = con.execute(
            """
            INSERT INTO files
                (scan_id, drive, abs_path, rel_path, filename, extension,
                 size_bytes, sha256, git_sha1, mtime, scanned_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(abs_path) DO UPDATE SET
                scan_id=excluded.scan_id,
                sha256=excluded.sha256,
                git_sha1=excluded.git_sha1,
                size_bytes=excluded.size_bytes,
                mtime=excluded.mtime,
                scanned_at=excluded.scanned_at
            """,
            (
                self.scan_id, drive, abs_path, rel, filename, extension,
                size, sha256, git_sha1, mtime, now,
            ),
        )
        file_id = cur.lastrowid
        return sha256, git_sha1, size, file_id

    # ---------------------------------------------------------------- #
    # Directory tree-hash computation
    # ---------------------------------------------------------------- #

    def _compute_directory_records(
        self,
        con: sqlite3.Connection,
        root_path: str,
        all_dirs: list[str],
    ) -> None:
        """
        For each directory in *all_dirs*, query all files below it from the
        `files` table, compute a tree_hash, and upsert into `directories`.
        """
        logger.info("Computing directory tree hashes for %d directories …", len(all_dirs))

        root = Path(root_path)
        drive = _drive_from_path(root_path)
        now = time.time()

        # Build a mapping: dir_abs_path -> list of (rel_path, sha256, size)
        # from the DB so we avoid re-scanning disk.
        rows = con.execute(
            "SELECT abs_path, rel_path, sha256, size_bytes "
            "FROM files WHERE scan_id = ?",
            (self.scan_id,),
        ).fetchall()

        # Index: abs_path -> (rel_path, sha256, size)
        file_index: dict[str, tuple[str, str, int]] = {
            r[0]: (r[1], r[2], r[3]) for r in rows
        }

        # For each directory, collect all files whose abs_path starts with it.
        # We compute depth and parent from the path itself.
        dir_files: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
        for abs_fp, (rel_fp, sha256, size) in file_index.items():
            fp = Path(abs_fp)
            for parent in fp.parents:
                parent_s = str(parent)
                if parent_s.startswith(root_path):
                    dir_files[parent_s].append((rel_fp, sha256, size))

        with tqdm(
            total=len(all_dirs),
            desc="Directory hashes",
            unit="dir",
            leave=False,
        ) as pbar:
            for dir_abs in all_dirs:
                entries = dir_files.get(dir_abs, [])
                file_count = len(entries)
                total_size = sum(e[2] for e in entries)

                # tree_hash = SHA-256 of sorted "rel_path|sha256\n" lines.
                sorted_lines = sorted(
                    f"{e[0]}|{e[1]}\n" for e in entries
                )
                tree_hash = hashlib.sha256(
                    "".join(sorted_lines).encode()
                ).hexdigest()

                dir_path = Path(dir_abs)
                try:
                    depth = len(dir_path.relative_to(root).parts)
                except ValueError:
                    depth = 0

                parent_path = str(dir_path.parent) if dir_path != root else ""
                name = dir_path.name or root_path

                con.execute(
                    """
                    INSERT INTO directories
                        (scan_id, drive, abs_path, parent_path, depth, name,
                         file_count, total_size_bytes, tree_hash, scanned_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(abs_path) DO UPDATE SET
                        scan_id=excluded.scan_id,
                        file_count=excluded.file_count,
                        total_size_bytes=excluded.total_size_bytes,
                        tree_hash=excluded.tree_hash,
                        scanned_at=excluded.scanned_at
                    """,
                    (
                        self.scan_id, drive, dir_abs, parent_path, depth, name,
                        file_count, total_size, tree_hash, now,
                    ),
                )
                pbar.update(1)

        con.commit()
        logger.info("Directory records written.")

    # ---------------------------------------------------------------- #
    # Public entry point
    # ---------------------------------------------------------------- #

    def scan(
        self,
        root_path: str,
        exclude_patterns: list[str] | None = None,
        resume: bool = True,
    ) -> None:
        """
        Walk *root_path*, hash every non-symlink file, persist to DB.

        Parameters
        ----------
        root_path:         Absolute path to the directory to scan.
        exclude_patterns:  List of fnmatch glob patterns.
        resume:            If True, skip files already in DB with matching mtime.
        """
        if exclude_patterns is None:
            exclude_patterns = []

        root_path = str(Path(root_path).resolve())
        if not os.path.isdir(root_path):
            raise NotADirectoryError(f"root_path is not a directory: {root_path!r}")

        con = self._connect()
        started_at = time.time()

        logger.info(
            "Starting scan  scan_id=%s  root=%s", self.scan_id, root_path
        )

        # Initialise scan_meta row.
        self._upsert_meta(
            con,
            scan_id=self.scan_id,
            started_at=started_at,
            root_path=root_path,
            status="running",
            total_files=0,
            total_dirs=0,
            total_bytes=0,
        )

        # Estimate total for the progress bar (fast pass, no hashing).
        logger.info("Estimating file count …")
        estimated_total = _estimate_file_count(root_path, exclude_patterns)
        logger.info("Estimated %d files to scan.", estimated_total)

        files_done = 0
        bytes_done = 0
        skipped_cached = 0
        all_dirs: list[str] = []

        with tqdm(
            total=estimated_total or None,
            desc="Hashing files",
            unit="file",
            dynamic_ncols=True,
        ) as pbar:
            for dirpath, dirnames, filenames in self._walk(root_path, exclude_patterns):
                all_dirs.append(dirpath)

                for fname in filenames:
                    abs_path = os.path.join(dirpath, fname)

                    # Skip symlinks.
                    if os.path.islink(abs_path):
                        continue

                    try:
                        mtime = os.path.getmtime(abs_path)
                    except OSError as exc:
                        logger.warning("stat failed (skipping): %s — %s", abs_path, exc)
                        continue

                    # Resume: skip files we already hashed with the same mtime.
                    if self._already_hashed(con, abs_path, mtime):
                        skipped_cached += 1
                        pbar.set_postfix_str(f"cached={skipped_cached}", refresh=False)
                        pbar.update(1)
                        files_done += 1
                        continue

                    pbar.set_description_str(
                        f"Hashing  {os.path.basename(abs_path)[:40]}", refresh=False
                    )

                    result = self._hash_file(con, abs_path, root_path, mtime)
                    if result is not None:
                        _, _, size, file_id = result
                        bytes_done += size
                        if self._uploader is not None:
                            self._uploader.enqueue(abs_path, file_id)

                    files_done += 1
                    pbar.update(1)

                    # Periodic commit + meta update.
                    if files_done % _PROGRESS_INTERVAL == 0:
                        con.commit()
                        self._upsert_meta(
                            con,
                            scan_id=self.scan_id,
                            total_files=files_done,
                            total_bytes=bytes_done,
                            total_dirs=len(all_dirs),
                        )

        # Final commit of any buffered file rows.
        con.commit()
        logger.info(
            "File hashing complete: %d files hashed, %d skipped (cached), "
            "%d bytes, %d dirs",
            files_done - skipped_cached,
            skipped_cached,
            bytes_done,
            len(all_dirs),
        )

        # Compute and store directory tree hashes.
        self._compute_directory_records(con, root_path, all_dirs)

        completed_at = time.time()
        self._upsert_meta(
            con,
            scan_id=self.scan_id,
            completed_at=completed_at,
            total_files=files_done,
            total_dirs=len(all_dirs),
            total_bytes=bytes_done,
            status="complete",
        )

        elapsed = completed_at - started_at
        logger.info(
            "Scan complete in %.1f s  scan_id=%s  files=%d  dirs=%d  bytes=%d",
            elapsed,
            self.scan_id,
            files_done,
            len(all_dirs),
            bytes_done,
        )

        con.close()


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Hash a directory tree.")
    parser.add_argument("root", help="Directory to scan")
    parser.add_argument("--db", default="dupes.db", help="SQLite DB path")
    parser.add_argument("--scan-id", default=f"manual-{int(time.time())}")
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[".git", "__pycache__", "node_modules", "*.pyc"],
        help="fnmatch patterns to exclude",
    )
    args = parser.parse_args()

    hasher = FileHasher(db_path=args.db, scan_id=args.scan_id)
    hasher.scan(root_path=args.root, exclude_patterns=args.exclude)
