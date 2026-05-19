"""
reporting/markdown_report.py

Generates a comprehensive Markdown deduplication report from the SQLite database.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.similarity import format_bytes

logger = logging.getLogger(__name__)


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _pct(value: float) -> str:
    """Format a 0.0–1.0 similarity as '74.3%'."""
    return f"{value * 100:.1f}%"


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a Markdown table. All values are treated as plain strings."""
    if not rows:
        return "_None found._\n"

    # Compute column widths — minimum 3 chars (for the `---` separator).
    widths = [max(3, len(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))

    def fmt_row(cells: list[str]) -> str:
        padded = [str(c).ljust(widths[i]) for i, c in enumerate(cells)]
        return "| " + " | ".join(padded) + " |"

    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    lines = [fmt_row(headers), sep]
    for row in rows:
        lines.append(fmt_row(row))
    return "\n".join(lines) + "\n"


class MarkdownReporter:
    """
    Queries the SQLite database at *db_path* and writes a Markdown report.

    Usage
    -----
        reporter = MarkdownReporter("path/to/dupes.db")
        path = reporter.generate("report.md")
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, output_path: str) -> str:
        """
        Build the full Markdown report and write it to *output_path*.

        Returns *output_path* so callers can chain or log it.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = 1")

            sections: list[str] = [
                self._header(),
                self._executive_summary(conn),
                self._section_exact_local_dupes(conn),
                self._section_near_dupes(conn),
                self._section_github_matches(conn),
                self._section_orphans(conn),
                self._section_actions_by_priority(conn),
                self._section_space_recovery(conn),
            ]

        report = "\n\n".join(s.rstrip() for s in sections) + "\n"

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        logger.info("Markdown report written to %s (%d bytes)", output_path, len(report))
        return output_path

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _header(self) -> str:
        return (
            "# Clear Local Dupes — Deduplication Report\n"
            f"Generated: {_now_str()}"
        )

    # ------------------------------------------------------------------
    # Executive summary
    # ------------------------------------------------------------------

    def _executive_summary(self, conn: sqlite3.Connection) -> str:
        # Files and bytes.
        file_row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(size_bytes), 0) AS total FROM files"
        ).fetchone()
        total_files = file_row["cnt"]
        total_bytes = file_row["total"]

        # Directories.
        dir_count = conn.execute("SELECT COUNT(*) FROM directories").fetchone()[0]

        # Exact duplicate pairs and recoverable space.
        dup_row = conn.execute(
            """
            SELECT COUNT(*) AS pairs,
                   COALESCE(SUM(d2.total_size_bytes), 0) AS recoverable
            FROM dir_pairs dp
            JOIN directories d2 ON d2.id = dp.dir2_id
            WHERE dp.is_exact_duplicate = 1
            """
        ).fetchone()
        exact_pairs = dup_row["pairs"]
        recoverable = dup_row["recoverable"]

        # GitHub repos.
        gh_row = conn.execute(
            "SELECT COUNT(*) AS repos, COUNT(DISTINCT org) AS orgs FROM github_repos"
        ).fetchone()
        gh_repos = gh_row["repos"]
        gh_orgs = gh_row["orgs"]

        # Matched and orphan dirs.
        matched = conn.execute(
            "SELECT COUNT(DISTINCT local_dir_id) FROM local_github_matches"
        ).fetchone()[0]
        orphans = dir_count - matched

        lines = [
            "## Executive Summary",
            "",
            f"- **Total files scanned:** {total_files:,} ({format_bytes(total_bytes)})",
            f"- **Total directories analyzed:** {dir_count:,}",
            f"- **Exact duplicate directory pairs:** {exact_pairs:,} ({format_bytes(recoverable)} recoverable)",
            f"- **GitHub repos enumerated:** {gh_repos:,} (across {gh_orgs:,} org(s))",
            f"- **Local dirs matched to GitHub:** {matched:,}",
            f"- **Unmatched local dirs (orphans):** {max(orphans, 0):,}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Section 1: Exact local duplicates
    # ------------------------------------------------------------------

    def _section_exact_local_dupes(self, conn: sqlite3.Connection) -> str:
        rows = conn.execute(
            """
            SELECT
                d1.abs_path             AS dir1,
                d2.abs_path             AS dir2,
                dp.common_files         AS files,
                d2.total_size_bytes     AS size,
                r.action                AS recommendation
            FROM dir_pairs dp
            JOIN directories d1 ON d1.id = dp.dir1_id
            JOIN directories d2 ON d2.id = dp.dir2_id
            LEFT JOIN recommendations r
                   ON r.local_dir = d2.abs_path AND r.action = 'archive_duplicate'
            WHERE dp.is_exact_duplicate = 1
            ORDER BY d2.total_size_bytes DESC
            """
        ).fetchall()

        table_rows = [
            [
                row["dir1"],
                row["dir2"],
                str(row["files"] or 0),
                format_bytes(row["size"] or 0),
                row["recommendation"] or "archive_duplicate",
            ]
            for row in rows
        ]

        return (
            "## 1. Exact Local Duplicates\n\n"
            + _md_table(
                ["Dir 1 (keep)", "Dir 2 (archive)", "Files", "Size", "Recommendation"],
                table_rows,
            )
        )

    # ------------------------------------------------------------------
    # Section 2: Near-duplicate local dirs
    # ------------------------------------------------------------------

    def _section_near_dupes(self, conn: sqlite3.Connection) -> str:
        rows = conn.execute(
            """
            SELECT
                d1.abs_path             AS dir1,
                d2.abs_path             AS dir2,
                dp.content_similarity   AS content_sim,
                dp.structural_similarity AS struct_sim,
                dp.unique_to_dir1       AS uniq1,
                dp.unique_to_dir2       AS uniq2
            FROM dir_pairs dp
            JOIN directories d1 ON d1.id = dp.dir1_id
            JOIN directories d2 ON d2.id = dp.dir2_id
            WHERE dp.is_exact_duplicate = 0
              AND dp.content_similarity >= 0.5
            ORDER BY dp.content_similarity DESC
            """
        ).fetchall()

        table_rows = [
            [
                row["dir1"],
                row["dir2"],
                _pct(row["content_sim"] or 0),
                _pct(row["struct_sim"] or 0),
                str(row["uniq1"] or 0),
                str(row["uniq2"] or 0),
            ]
            for row in rows
        ]

        return (
            "## 2. Near-Duplicate Local Dirs (>= 50% similar)\n\n"
            + _md_table(
                ["Dir 1", "Dir 2", "Content Sim %", "Structural Sim %",
                 "Unique to Dir1", "Unique to Dir2"],
                table_rows,
            )
        )

    # ------------------------------------------------------------------
    # Section 3: Local → GitHub matches
    # ------------------------------------------------------------------

    def _section_github_matches(self, conn: sqlite3.Connection) -> str:
        match_types: list[tuple[str, str]] = [
            ("exact",           "### Exact Matches"),
            ("local_is_ahead",  "### Local Ahead of GitHub (push needed)"),
            ("github_is_ahead", "### GitHub Ahead of Local (pull needed)"),
            ("diverged",        "### Diverged (merge needed)"),
            ("partial_match",   "### Partial Matches"),
        ]

        all_rows = conn.execute(
            """
            SELECT
                d.abs_path              AS local_dir,
                gr.full_name            AS repo,
                lgm.match_type          AS match_type,
                lgm.content_similarity  AS content_sim,
                lgm.structural_similarity AS struct_sim,
                lgm.local_ahead_count   AS local_ahead,
                lgm.github_ahead_count  AS github_ahead
            FROM local_github_matches lgm
            JOIN directories  d  ON d.id  = lgm.local_dir_id
            JOIN github_repos gr ON gr.id = lgm.github_repo_id
            ORDER BY lgm.content_similarity DESC
            """
        ).fetchall()

        # Group by match_type.
        grouped: dict[str, list[Any]] = {mt: [] for mt, _ in match_types}
        for row in all_rows:
            mt = row["match_type"]
            if mt in grouped:
                grouped[mt].append(row)

        parts = ["## 3. Local → GitHub Matches"]

        for mt, heading in match_types:
            parts.append(heading)
            rows_for_type = grouped[mt]
            if not rows_for_type:
                parts.append("_None._")
                continue

            table_rows = [
                [
                    r["local_dir"],
                    r["repo"],
                    _pct(r["content_sim"] or 0),
                    _pct(r["struct_sim"] or 0),
                    str(r["local_ahead"] or 0),
                    str(r["github_ahead"] or 0),
                ]
                for r in rows_for_type
            ]
            parts.append(
                _md_table(
                    ["Local Dir", "GitHub Repo", "Content Sim %", "Structural Sim %",
                     "Local Ahead", "GitHub Ahead"],
                    table_rows,
                )
            )

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Section 4: Orphan directories
    # ------------------------------------------------------------------

    def _section_orphans(self, conn: sqlite3.Connection) -> str:
        rows = conn.execute(
            """
            SELECT
                d.abs_path          AS dir_path,
                d.file_count        AS file_count,
                d.total_size_bytes  AS size,
                r.action            AS action
            FROM directories d
            LEFT JOIN local_github_matches lgm
                   ON lgm.local_dir_id = d.id AND lgm.content_similarity >= 0.15
            LEFT JOIN recommendations r
                   ON r.local_dir = d.abs_path
                  AND r.action IN ('create_new_repo', 'archive_or_delete')
            WHERE lgm.id IS NULL
            ORDER BY d.total_size_bytes DESC
            """
        ).fetchall()

        table_rows = [
            [
                row["dir_path"],
                str(row["file_count"] or 0),
                format_bytes(row["size"] or 0),
                row["action"] or "review",
            ]
            for row in rows
        ]

        return (
            "## 4. Orphan Directories (no GitHub match)\n\n"
            + _md_table(
                ["Directory", "Files", "Size", "Suggested Action"],
                table_rows,
            )
        )

    # ------------------------------------------------------------------
    # Section 5: Recommended actions by priority
    # ------------------------------------------------------------------

    def _section_actions_by_priority(self, conn: sqlite3.Connection) -> str:
        rows = conn.execute(
            """
            SELECT priority, action, COUNT(*) AS cnt
            FROM recommendations
            GROUP BY priority, action
            ORDER BY priority ASC, cnt DESC
            """
        ).fetchall()

        if not rows:
            return "## 5. Recommended Actions by Priority\n\n_No recommendations generated._"

        # Group by priority.
        by_priority: dict[int, list[Any]] = {}
        for row in rows:
            p = row["priority"]
            by_priority.setdefault(p, []).append(row)

        priority_labels = {
            1: "Priority 1 — Immediate (exact duplicates)",
            2: "Priority 2 — High (push / merge / near-dupes)",
            3: "Priority 3 — Medium (pull / orphans)",
            4: "Priority 4 — Low (partial matches / review)",
            5: "Priority 5 — Informational (already synced)",
        }

        parts = ["## 5. Recommended Actions by Priority"]
        total = 0

        for p in sorted(by_priority):
            label = priority_labels.get(p, f"Priority {p}")
            parts.append(f"### {label}")
            table_rows = [
                [row["action"], str(row["cnt"])]
                for row in by_priority[p]
            ]
            parts.append(_md_table(["Action", "Count"], table_rows))
            total += sum(r["cnt"] for r in by_priority[p])

        parts.append(f"**Total recommendations: {total}**")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Section 6: Space recovery estimate
    # ------------------------------------------------------------------

    def _section_space_recovery(self, conn: sqlite3.Connection) -> str:
        rows = conn.execute(
            """
            SELECT
                r.local_dir         AS dir_path,
                d.total_size_bytes  AS size
            FROM recommendations r
            LEFT JOIN directories d ON d.abs_path = r.local_dir
            WHERE r.action = 'archive_duplicate'
              AND r.status != 'done'
            ORDER BY d.total_size_bytes DESC
            """
        ).fetchall()

        total_recoverable = sum(r["size"] or 0 for r in rows)

        parts = [
            "## 6. Space Recovery Estimate",
            "",
            f"If all `archive_duplicate` recommendations are acted on: "
            f"**{format_bytes(total_recoverable)} recoverable**.",
            "",
        ]

        if rows:
            parts.append("### Directories to Archive")
            table_rows = [
                [row["dir_path"], format_bytes(row["size"] or 0)]
                for row in rows
            ]
            parts.append(_md_table(["Directory", "Size"], table_rows))
        else:
            parts.append("_No archive_duplicate recommendations pending._")

        return "\n".join(parts)
