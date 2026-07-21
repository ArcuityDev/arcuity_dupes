"""
github/repo_fetcher.py

Fetches all GitHub repos accessible to the supplied token, then retrieves
their full file trees via the Git Trees API (no cloning required).  Stores
results in `github_repos` and `github_files` tables of the project SQLite DB.

Public API
----------
    from github.repo_fetcher import RepoFetcher
    fetcher = RepoFetcher(token="ghp_...", db_path="db/dupes.db")
    n = fetcher.fetch_all()          # returns count of repos processed
    n = fetcher.fetch_repo_tree("org/repo")  # returns file count for one repo
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Iterator

import requests
from github import Github, GithubException, RateLimitExceededException
from tqdm import tqdm

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extensions of binary / media files that cannot be meaningfully compared
# to local counterparts by content hash alone.
# ---------------------------------------------------------------------------
_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
        ".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm", ".m4v",
        ".mp3", ".wav", ".aac", ".ogg", ".flac", ".m4a", ".wma",
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tgz",
        ".bin", ".exe", ".dll", ".so", ".dylib", ".obj", ".o", ".a",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".pyc", ".pyd", ".pyo",
        ".whl", ".egg",
        ".class", ".jar",
        ".DS_Store", ".db", ".sqlite", ".sqlite3",
        ".lock",  # lock files are generated, not source
        ".woff", ".woff2", ".eot", ".ttf", ".otf",  # fonts
    }
)

# GitHub secondary rate limit guidance: wait at least 1 s between mutating
# requests.  We also apply it between tree fetches to be safe.
_TREE_FETCH_PAUSE_S: float = 1.0

# When fewer than this many core-API requests remain, pause until reset.
_RATELIMIT_PAUSE_THRESHOLD: int = 50


# ---------------------------------------------------------------------------
# Schema (subset — only what this module creates)
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS github_repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org TEXT,
    name TEXT,
    full_name TEXT UNIQUE,
    clone_url TEXT,
    ssh_url TEXT,
    default_branch TEXT,
    last_commit_at TEXT,
    is_private INTEGER,
    topics TEXT,
    description TEXT,
    fetched_at REAL
);

CREATE TABLE IF NOT EXISTS github_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER,
    path TEXT,
    git_sha1 TEXT,
    size_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ghfiles_sha1 ON github_files(git_sha1);
CREATE INDEX IF NOT EXISTS idx_ghfiles_repo ON github_files(repo_id);
"""


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


def _extension(path: str) -> str:
    """Return lower-cased file extension including the dot, e.g. '.py'."""
    _, ext = os.path.splitext(path)
    return ext.lower()


def _is_text_file(path: str) -> bool:
    return _extension(path) not in _BINARY_EXTENSIONS


def _check_rate_limit(response: requests.Response, token: str) -> None:
    """
    Inspect X-RateLimit-* headers from a Trees-API response and sleep if
    we are approaching the limit.
    """
    try:
        remaining = int(response.headers.get("X-RateLimit-Remaining", "9999"))
        reset_ts = int(response.headers.get("X-RateLimit-Reset", "0"))
    except (ValueError, TypeError):
        return

    if remaining < _RATELIMIT_PAUSE_THRESHOLD:
        sleep_s = max(0.0, reset_ts - time.time()) + 5  # +5 s safety buffer
        logger.warning(
            "GitHub rate limit low (%d remaining). Sleeping %.0f s until reset.",
            remaining,
            sleep_s,
        )
        time.sleep(sleep_s)


def _sleep_for_secondary_limit() -> None:
    """One-second pause between tree fetches to respect secondary rate limits."""
    time.sleep(_TREE_FETCH_PAUSE_S)


def _wait_for_primary_ratelimit(gh: Github) -> None:
    """Block until the PyGithub client has enough core-API quota."""
    try:
        limits = gh.get_rate_limit()
        core = limits.core
        if core.remaining < _RATELIMIT_PAUSE_THRESHOLD:
            reset_ts = core.reset.timestamp()
            sleep_s = max(0.0, reset_ts - time.time()) + 5
            logger.warning(
                "PyGithub core rate limit low (%d remaining). Sleeping %.0f s.",
                core.remaining,
                sleep_s,
            )
            time.sleep(sleep_s)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not query rate limit: %s", exc)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class RepoFetcher:
    """
    Fetches GitHub repos and their file trees into the project database.

    Parameters
    ----------
    token:
        Personal access token (classic or fine-grained) with at minimum
        ``repo`` (read) scope.  Falls back to the ``GITHUB_TOKEN`` environment
        variable if not supplied or empty.
    db_path:
        Path to the SQLite database file.  Created if it does not exist.
    """

    def __init__(self, token: str = "", db_path: str = "db/dupes.db") -> None:
        self._token: str = token or os.environ.get("GITHUB_TOKEN", "")
        if not self._token:
            raise ValueError(
                "GitHub token required.  Pass token= or set GITHUB_TOKEN env var."
            )
        self._db_path = db_path
        self._gh = Github(self._token, per_page=100, retry=3)
        self._session = self._build_requests_session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_requests_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        return session

    def _get_already_fetched(self, conn: sqlite3.Connection) -> set[str]:
        """Return set of full_names that already have a non-NULL fetched_at."""
        rows = conn.execute(
            "SELECT full_name FROM github_repos WHERE fetched_at IS NOT NULL"
        ).fetchall()
        return {r["full_name"] for r in rows}

    def _iter_accessible_repos(self) -> Iterator:
        """
        Yield every repo the token can see:
        1. All repos for the authenticated user.
        2. All repos for every org the user belongs to.

        Deduplicates by full_name to avoid double-processing forks that
        appear under both user and org namespaces.
        """
        seen: set[str] = set()

        _wait_for_primary_ratelimit(self._gh)
        user = self._gh.get_user()

        # --- user repos (includes personal, collaborator, and org repos) ---
        try:
            for repo in user.get_repos(type="all"):
                if repo.full_name not in seen:
                    seen.add(repo.full_name)
                    yield repo
        except (GithubException, RateLimitExceededException) as exc:
            logger.error("Error listing user repos: %s", exc)

        # --- org repos not already covered ---
        try:
            _wait_for_primary_ratelimit(self._gh)
            for org in user.get_orgs():
                try:
                    _wait_for_primary_ratelimit(self._gh)
                    for repo in org.get_repos(type="all"):
                        if repo.full_name not in seen:
                            seen.add(repo.full_name)
                            yield repo
                except (GithubException, RateLimitExceededException) as exc:
                    logger.error("Error listing repos for org %s: %s", org.login, exc)
        except (GithubException, RateLimitExceededException) as exc:
            logger.error("Error listing orgs: %s", exc)

    def _upsert_repo(
        self, conn: sqlite3.Connection, repo
    ) -> int:
        """
        Insert or update a repo row.  Returns the rowid of the upserted row.
        ``fetched_at`` is intentionally left NULL here; it is set to a
        non-NULL value only after the file tree has been successfully stored.
        """
        # Collect topics (list[str] → comma-separated)
        try:
            topics = ",".join(repo.get_topics())
        except Exception:  # noqa: BLE001
            topics = ""

        try:
            last_commit_at = (
                repo.pushed_at.isoformat() if repo.pushed_at else None
            )
        except Exception:  # noqa: BLE001
            last_commit_at = None

        # Determine org: if the owner is an Organisation, use its login;
        # otherwise leave it as the user login.
        try:
            org = repo.organization.login if repo.organization else repo.owner.login
        except Exception:  # noqa: BLE001
            org = repo.owner.login if repo.owner else ""

        conn.execute(
            """
            INSERT INTO github_repos
                (org, name, full_name, clone_url, ssh_url, default_branch,
                 last_commit_at, is_private, topics, description, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(full_name) DO UPDATE SET
                org            = excluded.org,
                name           = excluded.name,
                clone_url      = excluded.clone_url,
                ssh_url        = excluded.ssh_url,
                default_branch = excluded.default_branch,
                last_commit_at = excluded.last_commit_at,
                is_private     = excluded.is_private,
                topics         = excluded.topics,
                description    = excluded.description
            """,
            (
                org,
                repo.name,
                repo.full_name,
                repo.clone_url,
                repo.ssh_url,
                repo.default_branch,
                last_commit_at,
                1 if repo.private else 0,
                topics,
                repo.description or "",
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM github_repos WHERE full_name = ?", (repo.full_name,)
        ).fetchone()
        return row["id"]

    def _fetch_tree_via_api(
        self,
        owner: str,
        repo_name: str,
        branch_sha: str,
    ) -> list[dict]:
        """
        Call GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1.

        Returns list of blob entries (dicts with 'path', 'sha', 'size').
        Handles truncated responses with a warning.
        """
        url = (
            f"https://api.github.com/repos/{owner}/{repo_name}"
            f"/git/trees/{branch_sha}?recursive=1"
        )
        try:
            resp = self._session.get(url, timeout=60)
        except requests.RequestException as exc:
            logger.error("Network error fetching tree for %s/%s: %s", owner, repo_name, exc)
            return []

        _check_rate_limit(resp, self._token)

        if resp.status_code == 409:
            # 409 = empty repository
            logger.info("Repo %s/%s is empty (409). Skipping tree.", owner, repo_name)
            return []

        if resp.status_code == 422:
            logger.warning(
                "Tree API returned 422 for %s/%s (possibly too large). Skipping.",
                owner, repo_name,
            )
            return []

        if resp.status_code == 403:
            # Could be secondary rate limit
            retry_after = int(resp.headers.get("Retry-After", "60"))
            logger.warning(
                "403 from tree API for %s/%s. Sleeping %d s (secondary rate limit).",
                owner, repo_name, retry_after,
            )
            time.sleep(retry_after)
            # Retry once
            try:
                resp = self._session.get(url, timeout=60)
                _check_rate_limit(resp, self._token)
            except requests.RequestException as exc:
                logger.error("Retry failed for %s/%s: %s", owner, repo_name, exc)
                return []

        if not resp.ok:
            logger.error(
                "Tree API error %d for %s/%s: %s",
                resp.status_code, owner, repo_name, resp.text[:200],
            )
            return []

        data = resp.json()

        if data.get("truncated"):
            logger.warning(
                "Tree for %s/%s was truncated by GitHub (> 100k items). "
                "Only the first 100k files will be indexed.",
                owner, repo_name,
            )

        # Only blobs (actual files), not trees (directories)
        return [
            item
            for item in data.get("tree", [])
            if item.get("type") == "blob"
        ]

    def _get_branch_sha(self, owner: str, repo_name: str, branch: str) -> str | None:
        """
        Resolve the branch name to its HEAD commit SHA via the Refs API.
        Returns None on failure.
        """
        url = (
            f"https://api.github.com/repos/{owner}/{repo_name}"
            f"/git/refs/heads/{branch}"
        )
        try:
            resp = self._session.get(url, timeout=30)
        except requests.RequestException as exc:
            logger.error("Network error resolving branch SHA for %s/%s@%s: %s",
                         owner, repo_name, branch, exc)
            return None

        _check_rate_limit(resp, self._token)

        if resp.status_code == 404:
            logger.debug("Branch '%s' not found in %s/%s.", branch, owner, repo_name)
            return None
        if not resp.ok:
            logger.error("Could not resolve branch SHA for %s/%s@%s: HTTP %d",
                         owner, repo_name, branch, resp.status_code)
            return None

        payload = resp.json()
        # The API can return a list when the ref prefix matches multiple refs.
        if isinstance(payload, list):
            if not payload:
                return None
            payload = payload[0]

        try:
            return payload["object"]["sha"]
        except (KeyError, TypeError) as exc:
            logger.error("Unexpected Refs API response for %s/%s: %s", owner, repo_name, exc)
            return None

    def _store_files(
        self,
        conn: sqlite3.Connection,
        repo_id: int,
        blobs: list[dict],
    ) -> int:
        """
        Delete previous file records for this repo and insert fresh ones.
        Returns count of text-file rows inserted.
        """
        conn.execute("DELETE FROM github_files WHERE repo_id = ?", (repo_id,))

        rows: list[tuple] = []
        for blob in blobs:
            path: str = blob.get("path", "")
            if not path:
                continue
            if not _is_text_file(path):
                continue
            sha1: str = blob.get("sha", "")
            size: int = blob.get("size") or 0
            rows.append((repo_id, path, sha1, size))

        if rows:
            conn.executemany(
                "INSERT INTO github_files (repo_id, path, git_sha1, size_bytes) VALUES (?, ?, ?, ?)",
                rows,
            )
        conn.commit()
        return len(rows)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_repo_tree(self, repo_full_name: str) -> int:
        """
        Fetch and store the file tree for a single repo (by full name).

        Does NOT skip if already fetched — always refreshes.

        Parameters
        ----------
        repo_full_name:
            e.g. ``"myorg/myrepo"``

        Returns
        -------
        int
            Number of (text) files stored.
        """
        conn = _open_db(self._db_path)
        try:
            parts = repo_full_name.split("/", 1)
            if len(parts) != 2:
                raise ValueError(f"Invalid repo full name: {repo_full_name!r}")
            owner, repo_name = parts

            _wait_for_primary_ratelimit(self._gh)
            try:
                gh_repo = self._gh.get_repo(repo_full_name)
            except GithubException as exc:
                logger.error("Cannot access repo %s: %s", repo_full_name, exc)
                return 0

            repo_id = self._upsert_repo(conn, gh_repo)

            branch = gh_repo.default_branch or "main"
            branch_sha = self._get_branch_sha(owner, repo_name, branch)
            if not branch_sha:
                logger.warning("No SHA for default branch '%s' in %s. Skipping tree.", branch, repo_full_name)
                return 0

            _sleep_for_secondary_limit()
            blobs = self._fetch_tree_via_api(owner, repo_name, branch_sha)
            count = self._store_files(conn, repo_id, blobs)

            import time as _time
            conn.execute(
                "UPDATE github_repos SET fetched_at = ? WHERE id = ?",
                (_time.time(), repo_id),
            )
            conn.commit()
            logger.info("Stored %d text files for %s", count, repo_full_name)
            return count
        finally:
            conn.close()

    def fetch_all(self) -> int:
        """
        Discover all repos the token can access and fetch their file trees.

        Skips repos whose ``fetched_at`` is already set in the DB (i.e. they
        were fully indexed on a previous run).

        Returns
        -------
        int
            Total number of repos whose trees were fetched (skipped ones not
            counted).
        """
        conn = _open_db(self._db_path)
        already_fetched = self._get_already_fetched(conn)
        conn.close()

        logger.info(
            "%d repos already in DB with fetched_at set — will skip them.",
            len(already_fetched),
        )

        # Collect repo list first so tqdm can show a total.
        logger.info("Enumerating accessible repos from GitHub API …")
        all_repos = list(self._iter_accessible_repos())
        to_fetch = [r for r in all_repos if r.full_name not in already_fetched]
        logger.info(
            "Found %d total repos; %d need tree fetching.",
            len(all_repos),
            len(to_fetch),
        )

        fetched_count = 0
        for repo in tqdm(to_fetch, desc="Fetching repo trees", unit="repo"):
            try:
                conn = _open_db(self._db_path)
                owner = repo.owner.login
                repo_name = repo.name
                repo_full_name = repo.full_name

                repo_id = self._upsert_repo(conn, repo)

                branch = repo.default_branch or "main"
                branch_sha = self._get_branch_sha(owner, repo_name, branch)

                if not branch_sha:
                    logger.warning(
                        "Skipping %s — could not resolve branch SHA for '%s'.",
                        repo_full_name, branch,
                    )
                    conn.close()
                    continue

                _sleep_for_secondary_limit()
                blobs = self._fetch_tree_via_api(owner, repo_name, branch_sha)
                file_count = self._store_files(conn, repo_id, blobs)

                conn.execute(
                    "UPDATE github_repos SET fetched_at = ? WHERE id = ?",
                    (time.time(), repo_id),
                )
                conn.commit()
                conn.close()

                logger.debug("%s → %d text files indexed.", repo_full_name, file_count)
                fetched_count += 1

            except Exception as exc:  # noqa: BLE001
                logger.error("Unexpected error processing %s: %s", repo.full_name, exc, exc_info=True)
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

        logger.info("fetch_all complete: %d repos fetched.", fetched_count)
        return fetched_count
