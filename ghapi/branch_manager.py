"""
github/branch_manager.py

Handles creating staging branches on GitHub and generating consolidation
shell scripts for recommended local→GitHub merges.

No local code is pushed by this module.  It creates the remote branch ref
(empty, pointing at the existing HEAD) and emits a shell script the operator
runs to perform the actual ``git push``.

Public API
----------
    from github.branch_manager import BranchManager
    mgr = BranchManager(token="ghp_...", db_path="db/dupes.db")
    mgr.generate_consolidation_scripts(output_dir="/tmp/scripts")
    mgr.create_staging_branch("org/repo", "staging/local-foo-2026-05-19")
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import time
from datetime import date
from pathlib import Path
from typing import NamedTuple

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema (only what this module reads / creates)
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_dir TEXT,
    github_repo TEXT,
    action TEXT,
    rationale TEXT,
    priority INTEGER,
    status TEXT DEFAULT 'pending',
    created_at REAL
);
"""

# Subset of github_repos columns that this module needs.
_GITHUB_REPOS_COLS = "id, full_name, name, ssh_url, clone_url, default_branch"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    return conn


def _safe_filename(name: str) -> str:
    """Strip characters unsafe in file names / shell identifiers."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _make_branch_name(dir_name: str, today: date | None = None) -> str:
    """
    Build a branch name of the form ``staging/local-{dir_name}-{date}``.

    Examples
    --------
    >>> _make_branch_name("baldini_3", date(2026, 5, 19))
    'staging/local-baldini_3-2026-05-19'
    """
    d = today or date.today()
    safe = _safe_filename(dir_name)
    return f"staging/local-{safe}-{d.isoformat()}"


class _Recommendation(NamedTuple):
    id: int
    local_dir: str
    github_repo: str   # full_name, e.g. "org/repo"
    action: str
    rationale: str
    priority: int
    status: str


# ---------------------------------------------------------------------------
# Shell-script template
# ---------------------------------------------------------------------------

_SCRIPT_TEMPLATE = """\
#!/bin/bash
# ============================================================
# Consolidation script — generated {generated_at}
# Local:  {local_dir}
# GitHub: {github_repo}  (branch: {branch_name})
# Action: {action}
# Reason: {rationale}
# ============================================================
set -euo pipefail

LOCAL_DIR="{local_dir}"
REMOTE_URL="{ssh_url}"
BRANCH="{branch_name}"
COMMIT_MSG="staging: import local copy of {dir_name} [{date}]"

echo "[consolidate] Working in: $LOCAL_DIR"
cd "$LOCAL_DIR"

# Initialise a git repo if one does not already exist.
if [ ! -d ".git" ]; then
    git init
    echo "[consolidate] Initialised new git repo."
fi

# Add or update the remote.
if git remote get-url origin &>/dev/null; then
    git remote set-url origin "$REMOTE_URL"
    echo "[consolidate] Updated remote 'origin' → $REMOTE_URL"
else
    git remote add origin "$REMOTE_URL"
    echo "[consolidate] Added remote 'origin' → $REMOTE_URL"
fi

# Create the staging branch and stage all files.
git checkout -b "$BRANCH" 2>/dev/null || git checkout "$BRANCH"
git add -A
git commit -m "$COMMIT_MSG" || echo "[consolidate] Nothing new to commit."

echo "[consolidate] Pushing $BRANCH to origin …"
git push origin "$BRANCH"

echo "[consolidate] Done.  Open a PR at:"
echo "  https://github.com/{github_repo}/compare/{branch_name}?expand=1"
"""


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class BranchManager:
    """
    Generates consolidation scripts and creates staging branch refs on GitHub.

    Parameters
    ----------
    token:
        GitHub personal access token with ``repo`` (write) scope.
        Falls back to the ``GITHUB_TOKEN`` environment variable.
    db_path:
        Path to the SQLite database file.
    """

    def __init__(self, token: str = "", db_path: str = "db/dupes.db") -> None:
        self._token: str = token or os.environ.get("GITHUB_TOKEN", "")
        if not self._token:
            raise ValueError(
                "GitHub token required.  Pass token= or set GITHUB_TOKEN env var."
            )
        self._db_path = db_path
        self._session = self._build_session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        return session

    def _get_repo_info(
        self, conn: sqlite3.Connection, github_repo_full_name: str
    ) -> sqlite3.Row | None:
        """Return the github_repos row for the given full_name, or None."""
        return conn.execute(
            f"SELECT {_GITHUB_REPOS_COLS} FROM github_repos WHERE full_name = ?",
            (github_repo_full_name,),
        ).fetchone()

    def _load_recommendations(
        self, conn: sqlite3.Connection
    ) -> list[_Recommendation]:
        """
        Load recommendations where action is ``push_to_branch`` or
        ``create_branch`` and status is ``pending``.
        """
        rows = conn.execute(
            """
            SELECT id, local_dir, github_repo, action, rationale, priority, status
            FROM recommendations
            WHERE action IN ('push_to_branch', 'create_branch')
              AND status = 'pending'
            ORDER BY priority ASC, id ASC
            """,
        ).fetchall()
        return [
            _Recommendation(
                id=r["id"],
                local_dir=r["local_dir"],
                github_repo=r["github_repo"],
                action=r["action"],
                rationale=r["rationale"] or "",
                priority=r["priority"] or 0,
                status=r["status"],
            )
            for r in rows
        ]

    def _get_default_branch_sha(
        self, owner: str, repo_name: str, branch: str
    ) -> str | None:
        """
        Resolve the current HEAD SHA of *branch* via the GitHub Refs API.
        Returns None on any error.
        """
        url = (
            f"https://api.github.com/repos/{owner}/{repo_name}"
            f"/git/refs/heads/{branch}"
        )
        try:
            resp = self._session.get(url, timeout=30)
        except requests.RequestException as exc:
            logger.error("Network error resolving branch SHA for %s/%s: %s",
                         owner, repo_name, exc)
            return None

        if resp.status_code == 404:
            logger.debug("Branch '%s' not found in %s/%s.", branch, owner, repo_name)
            return None
        if not resp.ok:
            logger.error(
                "GitHub API error %d resolving branch %s in %s/%s: %s",
                resp.status_code, branch, owner, repo_name, resp.text[:200],
            )
            return None

        payload = resp.json()
        if isinstance(payload, list):
            if not payload:
                return None
            payload = payload[0]

        try:
            return payload["object"]["sha"]
        except (KeyError, TypeError) as exc:
            logger.error(
                "Unexpected Refs API response for %s/%s@%s: %s",
                owner, repo_name, branch, exc,
            )
            return None

    def _resolve_base_branch(
        self, owner: str, repo_name: str, declared_default: str | None
    ) -> tuple[str, str] | None:
        """
        Return (branch_name, sha) for the base branch to create staging refs
        from.  Tries ``declared_default`` first, then ``main``, then
        ``master``.  Returns None if none can be resolved.
        """
        candidates: list[str] = []
        if declared_default:
            candidates.append(declared_default)
        for fallback in ("main", "master"):
            if fallback not in candidates:
                candidates.append(fallback)

        for branch in candidates:
            sha = self._get_default_branch_sha(owner, repo_name, branch)
            if sha:
                return branch, sha

        logger.error(
            "Could not resolve any base branch for %s/%s (tried: %s).",
            owner, repo_name, candidates,
        )
        return None

    def _mark_recommendation_done(
        self, conn: sqlite3.Connection, rec_id: int
    ) -> None:
        conn.execute(
            "UPDATE recommendations SET status = 'script_generated' WHERE id = ?",
            (rec_id,),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_staging_branch(
        self,
        repo_full_name: str,
        branch_name: str,
        token: str = "",
    ) -> bool:
        """
        Create a new branch ref on GitHub pointing at the current HEAD of
        the repo's default branch.

        No code is pushed.  The branch is simply a named ref from which the
        operator can push their local commits.

        Parameters
        ----------
        repo_full_name:
            e.g. ``"myorg/myrepo"``
        branch_name:
            e.g. ``"staging/local-mydir-2026-05-19"``
        token:
            Override token for this call.  Defaults to the instance token.

        Returns
        -------
        bool
            True if the branch was created (or already exists), False on error.
        """
        effective_token = token or self._token
        # Build a per-call session if token differs from instance token
        if effective_token != self._token:
            session = requests.Session()
            session.headers.update(
                {
                    "Authorization": f"Bearer {effective_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
            )
        else:
            session = self._session

        parts = repo_full_name.split("/", 1)
        if len(parts) != 2:
            logger.error("Invalid repo full name: %s", repo_full_name)
            return False
        owner, repo_name = parts

        # We need to know the default branch to look up its SHA from the DB.
        conn = _open_db(self._db_path)
        repo_row = self._get_repo_info(conn, repo_full_name)
        conn.close()

        declared_default = repo_row["default_branch"] if repo_row else None

        resolved = self._resolve_base_branch(owner, repo_name, declared_default)
        if resolved is None:
            return False
        _base_branch, base_sha = resolved

        url = f"https://api.github.com/repos/{owner}/{repo_name}/git/refs"
        payload = {
            "ref": f"refs/heads/{branch_name}",
            "sha": base_sha,
        }

        try:
            resp = session.post(url, json=payload, timeout=30)
        except requests.RequestException as exc:
            logger.error("Network error creating branch %s in %s: %s",
                         branch_name, repo_full_name, exc)
            return False

        if resp.status_code == 201:
            logger.info(
                "Created staging branch '%s' in %s (SHA %s).",
                branch_name, repo_full_name, base_sha[:8],
            )
            return True

        if resp.status_code == 422:
            # 422 Unprocessable Entity is GitHub's response for "ref already exists"
            error_body = resp.json()
            if "already exists" in str(error_body.get("message", "")).lower():
                logger.info(
                    "Branch '%s' already exists in %s — treating as success.",
                    branch_name, repo_full_name,
                )
                return True
            logger.error(
                "422 creating branch '%s' in %s: %s",
                branch_name, repo_full_name, resp.text[:400],
            )
            return False

        if resp.status_code == 403:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            logger.warning(
                "403 creating branch — secondary rate limit hit. Sleeping %d s.",
                retry_after,
            )
            time.sleep(retry_after)
            # Retry once
            try:
                resp = session.post(url, json=payload, timeout=30)
                if resp.status_code == 201:
                    logger.info(
                        "Created staging branch '%s' in %s after retry.",
                        branch_name, repo_full_name,
                    )
                    return True
            except requests.RequestException as exc:
                logger.error("Retry failed: %s", exc)
            return False

        logger.error(
            "GitHub API returned %d creating branch '%s' in %s: %s",
            resp.status_code, branch_name, repo_full_name, resp.text[:400],
        )
        return False

    def generate_consolidation_scripts(self, output_dir: str) -> int:
        """
        Write one ``.sh`` script per pending recommendation whose action is
        ``push_to_branch`` or ``create_branch``.

        Scripts are written to ``{output_dir}/consolidation_scripts/``.
        Each script is made executable (``chmod +x``).

        The corresponding recommendation row is updated to
        ``status='script_generated'`` after the script is written.

        Parameters
        ----------
        output_dir:
            Parent directory under which ``consolidation_scripts/`` will be
            created.  Created if it does not exist.

        Returns
        -------
        int
            Number of scripts written.
        """
        scripts_dir = Path(output_dir) / "consolidation_scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        conn = _open_db(self._db_path)
        try:
            recommendations = self._load_recommendations(conn)
            if not recommendations:
                logger.info("No pending recommendations found for script generation.")
                return 0

            logger.info(
                "Generating %d consolidation script(s) in %s …",
                len(recommendations),
                scripts_dir,
            )

            today = date.today()
            written = 0

            for rec in recommendations:
                repo_row = self._get_repo_info(conn, rec.github_repo)
                if repo_row is None:
                    logger.warning(
                        "Repo '%s' not found in github_repos table — skipping recommendation %d.",
                        rec.github_repo, rec.id,
                    )
                    continue

                local_dir_path = rec.local_dir
                dir_name = Path(local_dir_path).name
                branch_name = _make_branch_name(dir_name, today)

                ssh_url: str = repo_row["ssh_url"] or repo_row["clone_url"] or ""
                if not ssh_url:
                    logger.warning(
                        "No SSH/clone URL for repo '%s' — skipping recommendation %d.",
                        rec.github_repo, rec.id,
                    )
                    continue

                script_content = _SCRIPT_TEMPLATE.format(
                    generated_at=today.isoformat(),
                    local_dir=local_dir_path,
                    github_repo=rec.github_repo,
                    branch_name=branch_name,
                    action=rec.action,
                    rationale=rec.rationale or "no rationale provided",
                    ssh_url=ssh_url,
                    dir_name=dir_name,
                    date=today.isoformat(),
                )

                # Build a unique filename: priority_index_repo-name_dir-name.sh
                safe_repo = _safe_filename(rec.github_repo.replace("/", "__"))
                safe_dir = _safe_filename(dir_name)
                script_filename = f"{rec.priority:03d}_{rec.id}_{safe_repo}__{safe_dir}.sh"
                script_path = scripts_dir / script_filename

                script_path.write_text(script_content, encoding="utf-8")

                # chmod +x
                current_mode = script_path.stat().st_mode
                script_path.chmod(
                    current_mode
                    | stat.S_IXUSR
                    | stat.S_IXGRP
                    | stat.S_IXOTH
                )

                self._mark_recommendation_done(conn, rec.id)
                logger.info("Wrote script: %s", script_path)
                written += 1

            logger.info(
                "generate_consolidation_scripts: wrote %d script(s) to %s.",
                written, scripts_dir,
            )
            return written

        finally:
            conn.close()
