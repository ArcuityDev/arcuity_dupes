"""
github/repo_matcher.py

Matches local directories to GitHub repos using git blob SHA-1 overlap.

The local file scanner stores a ``git_sha1`` column in the ``files`` table
(git blob format SHA-1: ``sha1("blob <size>\0<content>")``).  The GitHub
Trees API also returns git blob SHA-1s.  This module compares them directly
— no cloning, no downloading.

Public API
----------
    from github.repo_matcher import RepoMatcher
    matcher = RepoMatcher(db_path="db/dupes.db")
    matcher.match_all(min_files=3)
"""

from __future__ import annotations

import difflib
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import NamedTuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema (only what this module creates)
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS local_github_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_dir_id INTEGER,
    github_repo_id INTEGER,
    content_similarity REAL,
    structural_similarity REAL,
    match_type TEXT,
    local_ahead_count INTEGER,
    github_ahead_count INTEGER,
    analyzed_at REAL
);
"""


# ---------------------------------------------------------------------------
# Helpers / data containers
# ---------------------------------------------------------------------------

def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    return conn


def _jaccard(a: set, b: set) -> float:
    """Return Jaccard similarity ∈ [0, 1].  Returns 0.0 if both sets are empty."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _name_similarity(local_name: str, repo_name: str) -> float:
    """
    Fuzzy name similarity between a directory basename and a repo name.
    Uses SequenceMatcher on lowercased, hyphen/underscore-normalised strings.
    """
    def _normalise(s: str) -> str:
        return s.lower().replace("-", "_").replace(" ", "_")

    return difflib.SequenceMatcher(
        None, _normalise(local_name), _normalise(repo_name)
    ).ratio()


@dataclass
class _LocalDir:
    id: int
    abs_path: str
    name: str
    file_count: int
    # git_sha1 set for all files under this directory
    sha1_set: set[str] = field(default_factory=set)
    # set of (relative_path, git_sha1) — structural fingerprint
    structural_set: set[tuple[str, str]] = field(default_factory=set)


@dataclass
class _GithubRepo:
    id: int
    full_name: str
    name: str
    file_count: int
    sha1_set: set[str] = field(default_factory=set)
    structural_set: set[tuple[str, str]] = field(default_factory=set)


class _MatchResult(NamedTuple):
    local_dir_id: int
    github_repo_id: int
    content_similarity: float
    structural_similarity: float
    match_type: str
    local_ahead_count: int
    github_ahead_count: int


# ---------------------------------------------------------------------------
# Match-type logic
# ---------------------------------------------------------------------------

def _classify_match(
    local_dir: _LocalDir,
    repo: _GithubRepo,
    content_sim: float,
    structural_sim: float,
) -> tuple[str, int, int]:
    """
    Determine match_type, local_ahead_count, github_ahead_count.

    Returns (match_type, local_ahead_count, github_ahead_count).
    """
    # Files in local but not in GitHub (by SHA-1)
    local_ahead = local_dir.sha1_set - repo.sha1_set
    # Files in GitHub but not in local
    github_ahead = repo.sha1_set - local_dir.sha1_set

    local_ahead_count = len(local_ahead)
    github_ahead_count = len(github_ahead)

    if content_sim < 0.15:
        # Check for name-only match
        if _name_similarity(local_dir.name, repo.name) >= 0.8:
            return "name_only", local_ahead_count, github_ahead_count
        # Should not reach here (caller filters < 0.15), but be defensive.
        return "no_match", local_ahead_count, github_ahead_count

    if structural_sim == 1.0 and local_dir.file_count == repo.file_count:
        return "exact", local_ahead_count, github_ahead_count

    if local_ahead_count > 0 and github_ahead_count == 0:
        return "local_is_ahead", local_ahead_count, github_ahead_count

    if github_ahead_count > 0 and local_ahead_count == 0:
        return "github_is_ahead", local_ahead_count, github_ahead_count

    if local_ahead_count > 0 and github_ahead_count > 0:
        return "diverged", local_ahead_count, github_ahead_count

    # content_sim >= 0.15 but structural_similarity < 0.5 (or other edge cases)
    return "partial_match", local_ahead_count, github_ahead_count


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class RepoMatcher:
    """
    Computes content and structural similarity between every local directory
    (with enough files) and every GitHub repo, then writes matches to
    ``local_github_matches``.

    Strategy
    --------
    All data is loaded into memory in two bulk queries:
      1. All ``files`` rows — grouped by their containing directory.
      2. All ``github_files`` rows — grouped by repo_id.

    Matching then runs entirely in Python set operations, which is O(D × R)
    in-memory comparisons — no per-file SQL queries.
    """

    def __init__(self, db_path: str = "db/dupes.db") -> None:
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_local_dirs(
        self, conn: sqlite3.Connection, min_files: int
    ) -> list[_LocalDir]:
        """
        Load directories that have at least *min_files* files, together with
        the git_sha1 and relative path of every file under them.

        We use the ``directories`` table for the directory list, then join
        ``files`` on abs_path prefix.
        """
        # Load qualifying directories
        dir_rows = conn.execute(
            """
            SELECT id, abs_path, name, file_count
            FROM directories
            WHERE file_count >= ?
            ORDER BY id
            """,
            (min_files,),
        ).fetchall()

        if not dir_rows:
            logger.info("No directories with >= %d files found.", min_files)
            return []

        # Load ALL files in one query.  We'll assign them to directories in
        # Python to avoid N directory × file-count queries.
        # Only files with a non-NULL git_sha1 are useful for matching.
        all_files = conn.execute(
            "SELECT abs_path, git_sha1 FROM files WHERE git_sha1 IS NOT NULL AND git_sha1 != ''"
        ).fetchall()

        # Build a sorted list of (abs_path, git_sha1) for prefix matching.
        file_list: list[tuple[str, str]] = [
            (row["abs_path"], row["git_sha1"]) for row in all_files
        ]

        result: list[_LocalDir] = []
        for dr in dir_rows:
            dir_path: str = dr["abs_path"]
            # Normalise: ensure trailing slash so prefix match doesn't confuse
            # /foo/bar with /foo/barbaz
            prefix = dir_path if dir_path.endswith("/") else dir_path + "/"

            sha1_set: set[str] = set()
            structural_set: set[tuple[str, str]] = set()

            for abs_path, sha1 in file_list:
                if abs_path == dir_path or abs_path.startswith(prefix):
                    sha1_set.add(sha1)
                    # Relative path = strip the directory prefix
                    rel = abs_path[len(prefix):] if abs_path.startswith(prefix) else ""
                    structural_set.add((rel, sha1))

            if len(sha1_set) < min_files:
                # Fewer qualifying files than threshold — skip
                continue

            result.append(
                _LocalDir(
                    id=dr["id"],
                    abs_path=dir_path,
                    name=dr["name"],
                    file_count=dr["file_count"],
                    sha1_set=sha1_set,
                    structural_set=structural_set,
                )
            )

        logger.info("Loaded %d qualifying local directories.", len(result))
        return result

    def _load_github_repos(self, conn: sqlite3.Connection) -> list[_GithubRepo]:
        """
        Load all GitHub repos and their file SHA-1 sets from ``github_files``.
        """
        repo_rows = conn.execute(
            "SELECT id, full_name, name FROM github_repos WHERE fetched_at IS NOT NULL"
        ).fetchall()

        if not repo_rows:
            logger.info("No fetched GitHub repos in DB.")
            return []

        # Build a dict: repo_id → _GithubRepo
        repos: dict[int, _GithubRepo] = {
            r["id"]: _GithubRepo(
                id=r["id"],
                full_name=r["full_name"],
                name=r["name"],
                file_count=0,
            )
            for r in repo_rows
        }

        # Load all github_files in one shot
        file_rows = conn.execute(
            "SELECT repo_id, path, git_sha1 FROM github_files WHERE git_sha1 IS NOT NULL AND git_sha1 != ''"
        ).fetchall()

        for fr in file_rows:
            repo = repos.get(fr["repo_id"])
            if repo is None:
                continue
            sha1 = fr["git_sha1"]
            path = fr["path"]
            repo.sha1_set.add(sha1)
            repo.structural_set.add((path, sha1))

        # Populate file_count from the sets (more accurate than a separate query)
        for repo in repos.values():
            repo.file_count = len(repo.sha1_set)

        result = [r for r in repos.values() if r.file_count > 0]
        logger.info("Loaded %d GitHub repos with files.", len(result))
        return result

    # ------------------------------------------------------------------
    # Matching logic
    # ------------------------------------------------------------------

    def _compute_matches(
        self,
        local_dirs: list[_LocalDir],
        github_repos: list[_GithubRepo],
        min_files: int,
    ) -> list[_MatchResult]:
        """
        Compute all matches above the 0.15 content-similarity threshold, plus
        any name-only matches even below that threshold.
        """
        results: list[_MatchResult] = []

        for local_dir in local_dirs:
            if not local_dir.sha1_set:
                continue

            for repo in github_repos:
                if not repo.sha1_set:
                    continue

                content_sim = _jaccard(local_dir.sha1_set, repo.sha1_set)

                # Name-only check for low-similarity pairs
                if content_sim < 0.15:
                    name_sim = _name_similarity(local_dir.name, repo.name)
                    if name_sim >= 0.8:
                        structural_sim = _jaccard(
                            local_dir.structural_set, repo.structural_set
                        )
                        match_type, local_ahead, github_ahead = _classify_match(
                            local_dir, repo, content_sim, structural_sim
                        )
                        results.append(
                            _MatchResult(
                                local_dir_id=local_dir.id,
                                github_repo_id=repo.id,
                                content_similarity=content_sim,
                                structural_similarity=structural_sim,
                                match_type="name_only",
                                local_ahead_count=local_ahead,
                                github_ahead_count=github_ahead,
                            )
                        )
                    continue

                structural_sim = _jaccard(
                    local_dir.structural_set, repo.structural_set
                )

                match_type, local_ahead, github_ahead = _classify_match(
                    local_dir, repo, content_sim, structural_sim
                )

                results.append(
                    _MatchResult(
                        local_dir_id=local_dir.id,
                        github_repo_id=repo.id,
                        content_similarity=content_sim,
                        structural_similarity=structural_sim,
                        match_type=match_type,
                        local_ahead_count=local_ahead,
                        github_ahead_count=github_ahead,
                    )
                )

        return results

    def _store_matches(
        self,
        conn: sqlite3.Connection,
        matches: list[_MatchResult],
    ) -> None:
        """
        Replace all existing matches and insert the new ones.
        Runs in a single transaction for atomicity.
        """
        analyzed_at = time.time()
        rows = [
            (
                m.local_dir_id,
                m.github_repo_id,
                m.content_similarity,
                m.structural_similarity,
                m.match_type,
                m.local_ahead_count,
                m.github_ahead_count,
                analyzed_at,
            )
            for m in matches
        ]

        with conn:
            conn.execute("DELETE FROM local_github_matches")
            conn.executemany(
                """
                INSERT INTO local_github_matches
                    (local_dir_id, github_repo_id, content_similarity,
                     structural_similarity, match_type, local_ahead_count,
                     github_ahead_count, analyzed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        logger.info("Stored %d local↔GitHub matches.", len(rows))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match_all(self, min_files: int = 3) -> int:
        """
        Load all local directories with >= *min_files* files and all GitHub
        repos, compute pairwise similarity, and write matches to
        ``local_github_matches``.

        Clears all previous match rows before inserting fresh results.

        Parameters
        ----------
        min_files:
            Minimum number of files a local directory must contain to be
            considered for matching.  Default: 3.

        Returns
        -------
        int
            Number of match rows written.
        """
        conn = _open_db(self._db_path)
        try:
            local_dirs = self._load_local_dirs(conn, min_files)
            github_repos = self._load_github_repos(conn)

            if not local_dirs:
                logger.warning("No local directories to match.")
                return 0
            if not github_repos:
                logger.warning("No GitHub repos to match against.")
                return 0

            logger.info(
                "Computing matches: %d local dirs × %d GitHub repos …",
                len(local_dirs),
                len(github_repos),
            )
            matches = self._compute_matches(local_dirs, github_repos, min_files)
            logger.info("Found %d candidate matches.", len(matches))

            self._store_matches(conn, matches)
            return len(matches)

        finally:
            conn.close()
