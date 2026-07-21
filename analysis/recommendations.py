"""
analysis/recommendations.py

Generates actionable recommendations from analysis results and persists them
to the `recommendations` table.  All DB queries are read-only except for the
final upsert into `recommendations`.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from analysis.similarity import format_bytes

logger = logging.getLogger(__name__)

# File extensions that suggest a directory is a real code project.
_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py", ".js", ".ts", ".jsx", ".tsx",
        ".go", ".java", ".kt", ".scala",
        ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".hpp",
        ".rs", ".swift", ".m", ".r", ".jl",
        ".sh", ".bash", ".zsh", ".fish",
        ".html", ".css", ".scss", ".sass",
        ".sql", ".tf", ".yaml", ".yml", ".toml",
        ".dockerfile",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RecommendationEngine:
    """
    Reads analysis tables from *db_path* and writes structured recommendations
    to the `recommendations` table.

    Usage
    -----
        engine = RecommendationEngine("path/to/dupes.db")
        recs = engine.generate()
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self) -> list[dict[str, Any]]:
        """
        Run all recommendation rules and persist results.

        Returns the full list of recommendation dicts (including any that
        already existed in the table from a prior run — keyed by
        (local_dir, github_repo, action) so re-running is idempotent).
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")

            self._ensure_table(conn)

            recs: list[dict[str, Any]] = []
            recs.extend(self._exact_local_duplicates(conn))
            recs.extend(self._exact_github_matches(conn))
            recs.extend(self._local_ahead(conn))
            recs.extend(self._github_ahead(conn))
            recs.extend(self._diverged(conn))
            recs.extend(self._partial_matches(conn))
            recs.extend(self._near_duplicate_local_dirs(conn))
            recs.extend(self._orphan_dirs(conn))

            self._upsert_recommendations(conn, recs)
            conn.commit()

            return self._load_all(conn)

    # ------------------------------------------------------------------
    # Table bootstrap
    # ------------------------------------------------------------------

    def _ensure_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recommendations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                local_dir   TEXT,
                github_repo TEXT,
                action      TEXT NOT NULL,
                rationale   TEXT NOT NULL,
                priority    INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL,
                UNIQUE(local_dir, github_repo, action)
            )
            """
        )

    # ------------------------------------------------------------------
    # Rule implementations
    # ------------------------------------------------------------------

    def _exact_local_duplicates(self, conn: sqlite3.Connection) -> list[dict]:
        """Rule 1: byte-for-byte identical local directory pairs."""
        rows = conn.execute(
            """
            SELECT
                dp.id,
                d1.abs_path  AS dir1,
                d2.abs_path  AS dir2,
                d1.mtime     AS mtime1,
                d2.mtime     AS mtime2
            FROM dir_pairs dp
            JOIN directories d1 ON d1.id = dp.dir1_id
            JOIN directories d2 ON d2.id = dp.dir2_id
            WHERE dp.is_exact_duplicate = 1
            """
        ).fetchall()

        recs = []
        for row in rows:
            # Keep the more recently modified copy; archive the other.
            if (row["mtime1"] or 0) >= (row["mtime2"] or 0):
                keep, archive = row["dir1"], row["dir2"]
            else:
                keep, archive = row["dir2"], row["dir1"]

            recs.append(
                self._rec(
                    local_dir=archive,
                    github_repo=None,
                    action="archive_duplicate",
                    rationale=(
                        f"Byte-for-byte identical to {keep}. "
                        "Safe to archive — keep the more recently modified copy."
                    ),
                    priority=1,
                )
            )
        return recs

    def _exact_github_matches(self, conn: sqlite3.Connection) -> list[dict]:
        """Rule 2: local directory is already in sync with GitHub."""
        rows = conn.execute(
            """
            SELECT
                d.abs_path   AS local_dir,
                gr.full_name AS repo
            FROM local_github_matches lgm
            JOIN directories   d  ON d.id  = lgm.local_dir_id
            JOIN github_repos  gr ON gr.id = lgm.github_repo_id
            WHERE lgm.match_type = 'exact'
            """
        ).fetchall()

        return [
            self._rec(
                local_dir=row["local_dir"],
                github_repo=row["repo"],
                action="already_synced",
                rationale=(
                    f"Local copy is identical to {row['repo']}. No action needed."
                ),
                priority=5,
            )
            for row in rows
        ]

    def _local_ahead(self, conn: sqlite3.Connection) -> list[dict]:
        """Rule 3: local has commits/files GitHub doesn't."""
        rows = conn.execute(
            """
            SELECT
                d.abs_path            AS local_dir,
                gr.full_name          AS repo,
                lgm.local_ahead_count AS ahead
            FROM local_github_matches lgm
            JOIN directories  d  ON d.id  = lgm.local_dir_id
            JOIN github_repos gr ON gr.id = lgm.github_repo_id
            WHERE lgm.match_type = 'local_is_ahead'
            """
        ).fetchall()

        return [
            self._rec(
                local_dir=row["local_dir"],
                github_repo=row["repo"],
                action="push_to_branch",
                rationale=(
                    f"Local has {row['ahead']} file(s) not in {row['repo']}. "
                    "Push to a staging branch for review."
                ),
                priority=2,
            )
            for row in rows
        ]

    def _github_ahead(self, conn: sqlite3.Connection) -> list[dict]:
        """Rule 4: GitHub has commits/files the local copy lacks."""
        rows = conn.execute(
            """
            SELECT
                d.abs_path              AS local_dir,
                gr.full_name            AS repo,
                lgm.github_ahead_count  AS ahead
            FROM local_github_matches lgm
            JOIN directories  d  ON d.id  = lgm.local_dir_id
            JOIN github_repos gr ON gr.id = lgm.github_repo_id
            WHERE lgm.match_type = 'github_is_ahead'
            """
        ).fetchall()

        return [
            self._rec(
                local_dir=row["local_dir"],
                github_repo=row["repo"],
                action="pull_from_github",
                rationale=(
                    f"GitHub {row['repo']} has {row['ahead']} file(s) not present locally. "
                    "Pull to sync."
                ),
                priority=3,
            )
            for row in rows
        ]

    def _diverged(self, conn: sqlite3.Connection) -> list[dict]:
        """Rule 5: both sides have unique content — needs careful merge."""
        rows = conn.execute(
            """
            SELECT
                d.abs_path              AS local_dir,
                gr.full_name            AS repo,
                lgm.local_ahead_count   AS local_ahead,
                lgm.github_ahead_count  AS github_ahead
            FROM local_github_matches lgm
            JOIN directories  d  ON d.id  = lgm.local_dir_id
            JOIN github_repos gr ON gr.id = lgm.github_repo_id
            WHERE lgm.match_type = 'diverged'
            """
        ).fetchall()

        return [
            self._rec(
                local_dir=row["local_dir"],
                github_repo=row["repo"],
                action="create_branch",
                rationale=(
                    f"Local and {row['repo']} have diverged. "
                    f"{row['local_ahead']} unique local file(s), "
                    f"{row['github_ahead']} unique in GitHub. "
                    "Requires careful merge."
                ),
                priority=2,
            )
            for row in rows
        ]

    def _partial_matches(self, conn: sqlite3.Connection) -> list[dict]:
        """Rule 6: meaningful overlap but below the push/pull threshold."""
        rows = conn.execute(
            """
            SELECT
                d.abs_path              AS local_dir,
                gr.full_name            AS repo,
                lgm.content_similarity  AS sim
            FROM local_github_matches lgm
            JOIN directories  d  ON d.id  = lgm.local_dir_id
            JOIN github_repos gr ON gr.id = lgm.github_repo_id
            WHERE lgm.match_type = 'partial_match'
              AND lgm.content_similarity >= 0.15
            """
        ).fetchall()

        recs = []
        for row in rows:
            pct = round(row["sim"] * 100, 1)
            recs.append(
                self._rec(
                    local_dir=row["local_dir"],
                    github_repo=row["repo"],
                    action="review_similarity",
                    rationale=(
                        f"Local dir shares {pct}% content with {row['repo']}. "
                        "May be a fork or variant — human review needed."
                    ),
                    priority=4,
                )
            )
        return recs

    def _near_duplicate_local_dirs(self, conn: sqlite3.Connection) -> list[dict]:
        """
        Rule 7: local dirs that are highly similar but not byte-for-byte identical.
        Threshold: content_similarity >= 0.8 but is_exact_duplicate = 0.
        """
        rows = conn.execute(
            """
            SELECT
                d1.abs_path             AS dir1,
                d2.abs_path             AS dir2,
                dp.content_similarity   AS sim
            FROM dir_pairs dp
            JOIN directories d1 ON d1.id = dp.dir1_id
            JOIN directories d2 ON d2.id = dp.dir2_id
            WHERE dp.is_exact_duplicate = 0
              AND dp.content_similarity >= 0.8
            """
        ).fetchall()

        recs = []
        for row in rows:
            pct = round(row["sim"] * 100, 1)
            recs.append(
                self._rec(
                    local_dir=row["dir1"],
                    github_repo=None,
                    action="merge_local_dirs",
                    rationale=(
                        f"{pct}% content overlap with {row['dir2']}. "
                        "Consider merging before pushing to GitHub."
                    ),
                    priority=2,
                )
            )
        return recs

    def _orphan_dirs(self, conn: sqlite3.Connection) -> list[dict]:
        """
        Rule 8: directories with no GitHub match (or all matches below 0.15).

        Classifies each orphan as a likely code project or a data/archive dir
        based on whether it contains code-like file extensions.
        """
        # All directories that appear in no match row OR whose best match is < 0.15.
        rows = conn.execute(
            """
            SELECT
                d.id                AS dir_id,
                d.abs_path          AS dir_path,
                d.file_count        AS file_count,
                d.total_size_bytes  AS total_size
            FROM directories d
            WHERE d.id NOT IN (
                SELECT local_dir_id
                FROM local_github_matches
                WHERE content_similarity >= 0.15
            )
            """
        ).fetchall()

        recs = []
        for row in rows:
            file_count = row["file_count"] or 0
            size_str = format_bytes(row["total_size"] or 0)
            looks_like_code = self._dir_has_code_files(conn, row["dir_id"])

            if looks_like_code:
                action = "create_new_repo"
                rationale = (
                    f"No GitHub equivalent found. {file_count} file(s), {size_str}. "
                    "Directory contains source code — consider creating a new GitHub repo."
                )
            else:
                action = "archive_or_delete"
                rationale = (
                    f"No GitHub equivalent found. {file_count} file(s), {size_str}. "
                    "No source code detected — candidate for archival or deletion."
                )

            recs.append(
                self._rec(
                    local_dir=row["dir_path"],
                    github_repo=None,
                    action=action,
                    rationale=rationale,
                    priority=3,
                )
            )
        return recs

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _dir_has_code_files(self, conn: sqlite3.Connection, dir_id: int) -> bool:
        """Return True if the directory contains at least one code-like file."""
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM files f
            WHERE f.scan_id IN (
                SELECT scan_id FROM directories WHERE id = ?
            )
            AND (
                -- Compare lowercase extension against code extension list.
                -- SQLite doesn't support IN with a subquery of Python values,
                -- so we check the most common ones explicitly and fall back
                -- to a LIKE pattern for the rest.
                LOWER(f.extension) IN (
                    '.py','.js','.ts','.jsx','.tsx',
                    '.go','.java','.kt','.scala',
                    '.rb','.php','.cs','.cpp','.c','.h','.hpp',
                    '.rs','.swift','.sh','.bash','.zsh',
                    '.html','.css','.scss','.sql','.tf',
                    '.r','.jl'
                )
            )
            AND f.abs_path LIKE (
                (SELECT abs_path FROM directories WHERE id = ?) || '%'
            )
            """,
            (dir_id, dir_id),
        ).fetchone()
        return (row["cnt"] or 0) > 0

    @staticmethod
    def _rec(
        *,
        local_dir: str | None,
        github_repo: str | None,
        action: str,
        rationale: str,
        priority: int,
    ) -> dict[str, Any]:
        return {
            "local_dir": local_dir or "",
            "github_repo": github_repo or "",
            "action": action,
            "rationale": rationale,
            "priority": priority,
            "status": "pending",
            "created_at": _now_iso(),
        }

    def _upsert_recommendations(
        self, conn: sqlite3.Connection, recs: list[dict[str, Any]]
    ) -> None:
        """
        Insert or ignore recommendations keyed on (local_dir, github_repo, action).

        We use INSERT OR IGNORE so that manually updated `status` values (e.g.
        'done') are not overwritten by re-runs.
        """
        conn.executemany(
            """
            INSERT OR IGNORE INTO recommendations
                (local_dir, github_repo, action, rationale, priority, status, created_at)
            VALUES
                (:local_dir, :github_repo, :action, :rationale, :priority, :status, :created_at)
            """,
            recs,
        )
        logger.info("Upserted %d recommendation(s).", len(recs))

    def _load_all(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT id, local_dir, github_repo, action, rationale, priority, status, created_at
            FROM recommendations
            ORDER BY priority ASC, created_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]
