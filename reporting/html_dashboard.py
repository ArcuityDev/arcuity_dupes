"""
reporting/html_dashboard.py

Generates a single self-contained HTML dashboard from the SQLite database.
All data is embedded as JSON in <script> tags.  Bootstrap 5, DataTables, and
Chart.js are loaded from CDN.  Status updates are persisted in localStorage
so no server is required.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.similarity import format_bytes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CDN URLs — pinned to stable versions
# ---------------------------------------------------------------------------
_CDN = {
    "bootstrap_css":      "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css",
    "bootstrap_js":       "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js",
    "datatables_css":     "https://cdn.datatables.net/2.0.8/css/dataTables.bootstrap5.min.css",
    "datatables_js":      "https://cdn.datatables.net/2.0.8/js/dataTables.min.js",
    "datatables_bs5_js":  "https://cdn.datatables.net/2.0.8/js/dataTables.bootstrap5.min.js",
    "chartjs":            "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js",
    "jquery":             "https://cdn.jsdelivr.net/npm/jquery@3.7.1/dist/jquery.min.js",
}


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _js(obj: Any) -> str:
    """Serialize Python object to a JSON string safe to embed in <script>."""
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Code-extension check (mirrors recommendations.py logic, pure Python)
# ---------------------------------------------------------------------------
_CODE_EXT: frozenset[str] = frozenset(
    {
        ".py", ".js", ".ts", ".jsx", ".tsx",
        ".go", ".java", ".kt", ".scala",
        ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".hpp",
        ".rs", ".swift", ".sh", ".bash", ".zsh", ".fish",
        ".html", ".css", ".scss", ".sql", ".tf", ".r", ".jl",
    }
)


class HTMLReporter:
    """
    Generates a single self-contained HTML dashboard.

    Usage
    -----
        reporter = HTMLReporter("path/to/dupes.db")
        path = reporter.generate("dashboard.html")
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, output_path: str) -> str:
        """
        Build the full HTML dashboard and write it to *output_path*.

        Returns *output_path*.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = 1")

            data = self._collect_data(conn)

        html = self._render(data)

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        logger.info(
            "HTML dashboard written to %s (%d bytes)", output_path, len(html)
        )
        return output_path

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    def _collect_data(self, conn: sqlite3.Connection) -> dict[str, Any]:
        return {
            "generated_at": _now_str(),
            "summary":       self._q_summary(conn),
            "local_dupes":   self._q_local_dupes(conn),
            "github_matches":self._q_github_matches(conn),
            "orphans":       self._q_orphans(conn),
            "recommendations": self._q_recommendations(conn),
            "space_recovery":  self._q_space_recovery(conn),
            "match_type_dist": self._q_match_type_distribution(conn),
            "top_dup_dirs":    self._q_top_dup_dirs(conn),
        }

    def _q_summary(self, conn: sqlite3.Connection) -> dict[str, Any]:
        file_row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(size_bytes),0) AS total FROM files"
        ).fetchone()
        dir_count = conn.execute("SELECT COUNT(*) FROM directories").fetchone()[0]
        exact_pairs = conn.execute(
            "SELECT COUNT(*) FROM dir_pairs WHERE is_exact_duplicate = 1"
        ).fetchone()[0]
        gh_repos = conn.execute("SELECT COUNT(*) FROM github_repos").fetchone()[0]
        matched = conn.execute(
            "SELECT COUNT(DISTINCT local_dir_id) FROM local_github_matches"
        ).fetchone()[0]
        orphans = max(dir_count - matched, 0)
        rec_count = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]

        return {
            "total_files":    file_row["cnt"],
            "total_bytes":    file_row["total"],
            "total_bytes_hr": format_bytes(file_row["total"]),
            "dir_count":      dir_count,
            "exact_pairs":    exact_pairs,
            "gh_repos":       gh_repos,
            "matched_dirs":   matched,
            "orphan_dirs":    orphans,
            "rec_count":      rec_count,
        }

    def _q_local_dupes(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            """
            SELECT
                d1.abs_path             AS dir1,
                d2.abs_path             AS dir2,
                dp.common_files         AS files,
                d2.total_size_bytes     AS size,
                dp.content_similarity   AS content_sim,
                dp.is_exact_duplicate   AS is_exact
            FROM dir_pairs dp
            JOIN directories d1 ON d1.id = dp.dir1_id
            JOIN directories d2 ON d2.id = dp.dir2_id
            WHERE dp.content_similarity >= 0.5
               OR dp.is_exact_duplicate = 1
            ORDER BY dp.is_exact_duplicate DESC, dp.content_similarity DESC
            """
        ).fetchall()

        return [
            {
                "dir1":        r["dir1"],
                "dir2":        r["dir2"],
                "files":       r["files"] or 0,
                "size":        r["size"] or 0,
                "size_hr":     format_bytes(r["size"] or 0),
                "sim_pct":     round((r["content_sim"] or 0) * 100, 1),
                "is_exact":    bool(r["is_exact"]),
                "action":      "archive_duplicate" if r["is_exact"] else "merge_local_dirs",
            }
            for r in rows
        ]

    def _q_github_matches(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            """
            SELECT
                d.abs_path              AS local_dir,
                gr.full_name            AS repo,
                gr.org                  AS org,
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

        action_map = {
            "exact":           "already_synced",
            "local_is_ahead":  "push_to_branch",
            "github_is_ahead": "pull_from_github",
            "diverged":        "create_branch",
            "partial_match":   "review_similarity",
            "name_only":       "review_similarity",
        }
        return [
            {
                "local_dir":    r["local_dir"],
                "repo":         r["repo"],
                "org":          r["org"] or "",
                "match_type":   r["match_type"],
                "content_sim":  round((r["content_sim"] or 0) * 100, 1),
                "struct_sim":   round((r["struct_sim"] or 0) * 100, 1),
                "local_ahead":  r["local_ahead"] or 0,
                "github_ahead": r["github_ahead"] or 0,
                "action":       action_map.get(r["match_type"], "review"),
            }
            for r in rows
        ]

    def _q_orphans(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            """
            SELECT
                d.abs_path          AS dir_path,
                d.drive             AS drive,
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

        # Determine "looks like code" via the files table.
        result = []
        for r in rows:
            has_code = self._has_code_files(conn, r["dir_path"])
            result.append(
                {
                    "dir_path":    r["dir_path"],
                    "drive":       r["drive"] or "",
                    "file_count":  r["file_count"] or 0,
                    "size":        r["size"] or 0,
                    "size_hr":     format_bytes(r["size"] or 0),
                    "has_code":    has_code,
                    "action":      r["action"] or ("create_new_repo" if has_code else "archive_or_delete"),
                }
            )
        return result

    def _has_code_files(self, conn: sqlite3.Connection, dir_path: str) -> bool:
        ext_list = ",".join(f"'{e}'" for e in _CODE_EXT)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt
            FROM files
            WHERE LOWER(extension) IN ({ext_list})
              AND abs_path LIKE ?
            """,
            (dir_path.rstrip("/") + "/%",),
        ).fetchone()
        return (row["cnt"] or 0) > 0

    def _q_recommendations(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            """
            SELECT id, local_dir, github_repo, action, rationale, priority, status, created_at
            FROM recommendations
            ORDER BY priority ASC, created_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def _q_space_recovery(self, conn: sqlite3.Connection) -> dict[str, Any]:
        rows = conn.execute(
            """
            SELECT
                r.local_dir         AS dir_path,
                COALESCE(d.total_size_bytes, 0) AS size
            FROM recommendations r
            LEFT JOIN directories d ON d.abs_path = r.local_dir
            WHERE r.action = 'archive_duplicate'
              AND r.status != 'done'
            ORDER BY d.total_size_bytes DESC
            """
        ).fetchall()

        items = [
            {"dir_path": r["dir_path"], "size": r["size"], "size_hr": format_bytes(r["size"])}
            for r in rows
        ]
        total = sum(r["size"] for r in items)
        return {"total": total, "total_hr": format_bytes(total), "items": items}

    def _q_match_type_distribution(self, conn: sqlite3.Connection) -> dict[str, int]:
        rows = conn.execute(
            """
            SELECT match_type, COUNT(*) AS cnt
            FROM local_github_matches
            GROUP BY match_type
            """
        ).fetchall()
        dist: dict[str, int] = {r["match_type"]: r["cnt"] for r in rows}

        # Add orphan count.
        dir_count = conn.execute("SELECT COUNT(*) FROM directories").fetchone()[0]
        matched = conn.execute(
            "SELECT COUNT(DISTINCT local_dir_id) FROM local_github_matches"
        ).fetchone()[0]
        orphan_count = max(dir_count - matched, 0)
        if orphan_count:
            dist["orphan"] = orphan_count

        return dist

    def _q_top_dup_dirs(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            """
            SELECT d2.abs_path AS dir_path, d2.total_size_bytes AS size
            FROM dir_pairs dp
            JOIN directories d2 ON d2.id = dp.dir2_id
            WHERE dp.is_exact_duplicate = 1
            ORDER BY d2.total_size_bytes DESC
            LIMIT 10
            """
        ).fetchall()
        return [
            {"dir_path": r["dir_path"], "size": r["size"] or 0, "size_hr": format_bytes(r["size"] or 0)}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self, data: dict[str, Any]) -> str:
        s = data["summary"]
        generated_at = data["generated_at"]
        js_data = _js(data)

        return f"""<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Clear Local Dupes — Dashboard</title>
  <link rel="stylesheet" href="{_CDN['bootstrap_css']}">
  <link rel="stylesheet" href="{_CDN['datatables_css']}">
  <style>
    body {{ font-size: 0.9rem; }}
    .stat-card {{ min-width: 160px; }}
    .nav-tabs .nav-link {{ cursor: pointer; }}
    .badge-exact {{ background-color: #dc3545; }}
    .badge-ahead {{ background-color: #fd7e14; }}
    .badge-behind {{ background-color: #0d6efd; }}
    .badge-diverged {{ background-color: #6f42c1; }}
    .badge-partial {{ background-color: #20c997; }}
    .badge-orphan {{ background-color: #6c757d; }}
    .row-exact td {{ background-color: rgba(220,53,69,.15) !important; }}
    .row-near td  {{ background-color: rgba(253,126,20,.12) !important; }}
    tr.status-done td {{ opacity: 0.45; text-decoration: line-through; }}
    .dt-search input {{ background-color: #2b3035; color: #dee2e6; border-color: #495057; }}
    .dataTables_wrapper select {{ background-color: #2b3035; color: #dee2e6; border-color: #495057; }}
    pre {{ white-space: pre-wrap; word-break: break-all; }}
  </style>
</head>
<body>

<nav class="navbar navbar-expand-lg navbar-dark bg-dark px-3">
  <span class="navbar-brand fw-bold">&#x1F4C1; Clear Local Dupes</span>
  <span class="navbar-text text-secondary ms-3">Generated: {generated_at}</span>
</nav>

<div class="container-fluid mt-3">

  <!-- Tab navigation -->
  <ul class="nav nav-tabs mb-3" id="mainTabs" role="tablist">
    <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-overview" type="button">Overview</button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-dupes" type="button">Local Duplicates</button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-github" type="button">GitHub Matches</button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-orphans" type="button">Orphans</button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-recs" type="button">Recommendations</button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-space" type="button">Space Recovery</button></li>
  </ul>

  <div class="tab-content">

    <!-- ============================================================ -->
    <!-- TAB 1: OVERVIEW                                              -->
    <!-- ============================================================ -->
    <div class="tab-pane fade show active" id="tab-overview">
      <div class="row g-3 mb-4">
        <div class="col-auto">
          <div class="card stat-card bg-secondary text-white text-center p-3">
            <div class="fs-2 fw-bold" id="stat-files">{s['total_files']:,}</div>
            <div>Files Scanned</div>
            <div class="small text-light">{s['total_bytes_hr']}</div>
          </div>
        </div>
        <div class="col-auto">
          <div class="card stat-card bg-primary text-white text-center p-3">
            <div class="fs-2 fw-bold">{s['dir_count']:,}</div>
            <div>Dirs Analyzed</div>
          </div>
        </div>
        <div class="col-auto">
          <div class="card stat-card bg-danger text-white text-center p-3">
            <div class="fs-2 fw-bold">{s['exact_pairs']:,}</div>
            <div>Exact Dup Pairs</div>
          </div>
        </div>
        <div class="col-auto">
          <div class="card stat-card bg-success text-white text-center p-3">
            <div class="fs-2 fw-bold">{s['gh_repos']:,}</div>
            <div>GitHub Repos</div>
          </div>
        </div>
        <div class="col-auto">
          <div class="card stat-card bg-info text-dark text-center p-3">
            <div class="fs-2 fw-bold">{s['matched_dirs']:,}</div>
            <div>Matched Dirs</div>
          </div>
        </div>
        <div class="col-auto">
          <div class="card stat-card bg-warning text-dark text-center p-3">
            <div class="fs-2 fw-bold">{s['orphan_dirs']:,}</div>
            <div>Orphan Dirs</div>
          </div>
        </div>
      </div>

      <div class="row g-4">
        <div class="col-md-5">
          <div class="card">
            <div class="card-header">Local Dirs by Match Type</div>
            <div class="card-body" style="max-height:340px;">
              <canvas id="matchTypePie"></canvas>
            </div>
          </div>
        </div>
        <div class="col-md-7">
          <div class="card">
            <div class="card-header">Top 10 Largest Duplicate Dirs (Recoverable Space)</div>
            <div class="card-body" style="max-height:340px;">
              <canvas id="topDupBar"></canvas>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ============================================================ -->
    <!-- TAB 2: LOCAL DUPLICATES                                      -->
    <!-- ============================================================ -->
    <div class="tab-pane fade" id="tab-dupes">
      <h5 class="mb-3">Local Duplicate Directory Pairs</h5>
      <p class="text-secondary small">
        <span class="badge bg-danger me-1">&#9632;</span> Exact duplicates &nbsp;
        <span class="badge bg-warning text-dark me-1">&#9632;</span> Near-duplicates (&ge;50%)
      </p>
      <table id="dt-dupes" class="table table-sm table-hover table-striped" style="width:100%">
        <thead>
          <tr>
            <th>Dir 1</th>
            <th>Dir 2</th>
            <th>Files</th>
            <th>Size</th>
            <th>Sim %</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="body-dupes"></tbody>
      </table>
    </div>

    <!-- ============================================================ -->
    <!-- TAB 3: GITHUB MATCHES                                        -->
    <!-- ============================================================ -->
    <div class="tab-pane fade" id="tab-github">
      <div class="row mb-3 align-items-center">
        <div class="col-auto">
          <h5 class="mb-0">Local → GitHub Matches</h5>
        </div>
        <div class="col-auto">
          <label class="me-2 text-secondary small">Filter by type:</label>
          <select id="ghMatchFilter" class="form-select form-select-sm d-inline-block w-auto bg-dark text-light border-secondary">
            <option value="">All</option>
            <option value="exact">Exact</option>
            <option value="local_is_ahead">Local Ahead</option>
            <option value="github_is_ahead">GitHub Ahead</option>
            <option value="diverged">Diverged</option>
            <option value="partial_match">Partial Match</option>
            <option value="name_only">Name Only</option>
          </select>
        </div>
      </div>
      <table id="dt-github" class="table table-sm table-hover table-striped" style="width:100%">
        <thead>
          <tr>
            <th>Local Dir</th>
            <th>GitHub Repo</th>
            <th>Org</th>
            <th>Match Type</th>
            <th>Content Sim %</th>
            <th>Local Ahead</th>
            <th>GitHub Ahead</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="body-github"></tbody>
      </table>
    </div>

    <!-- ============================================================ -->
    <!-- TAB 4: ORPHANS                                               -->
    <!-- ============================================================ -->
    <div class="tab-pane fade" id="tab-orphans">
      <h5 class="mb-3">Orphan Directories (No GitHub Match)</h5>
      <table id="dt-orphans" class="table table-sm table-hover table-striped" style="width:100%">
        <thead>
          <tr>
            <th>Directory</th>
            <th>Drive</th>
            <th>Files</th>
            <th>Size</th>
            <th>Looks Like Code?</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="body-orphans"></tbody>
      </table>
    </div>

    <!-- ============================================================ -->
    <!-- TAB 5: RECOMMENDATIONS                                       -->
    <!-- ============================================================ -->
    <div class="tab-pane fade" id="tab-recs">
      <h5 class="mb-3">All Recommendations</h5>
      <p class="text-secondary small">Status is stored in your browser (localStorage). Marking an item done here does not modify the database.</p>
      <table id="dt-recs" class="table table-sm table-hover table-striped" style="width:100%">
        <thead>
          <tr>
            <th>Priority</th>
            <th>Local Dir</th>
            <th>GitHub Repo</th>
            <th>Action</th>
            <th>Rationale</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody id="body-recs"></tbody>
      </table>
    </div>

    <!-- ============================================================ -->
    <!-- TAB 6: SPACE RECOVERY                                        -->
    <!-- ============================================================ -->
    <div class="tab-pane fade" id="tab-space">
      <h5 class="mb-3">Space Recovery Estimate</h5>
      <div class="alert alert-info">
        If all <code>archive_duplicate</code> recommendations are acted on:
        <strong id="totalRecoverable"></strong> recoverable.
      </div>
      <table id="dt-space" class="table table-sm table-hover table-striped" style="width:100%">
        <thead>
          <tr>
            <th>Directory to Archive</th>
            <th>Size</th>
          </tr>
        </thead>
        <tbody id="body-space"></tbody>
      </table>
    </div>

  </div><!-- /tab-content -->
</div><!-- /container-fluid -->

<script src="{_CDN['jquery']}"></script>
<script src="{_CDN['bootstrap_js']}"></script>
<script src="{_CDN['datatables_js']}"></script>
<script src="{_CDN['datatables_bs5_js']}"></script>
<script src="{_CDN['chartjs']}"></script>

<script>
// ============================================================
// Embedded data
// ============================================================
const APP_DATA = {js_data};

// ============================================================
// Helpers
// ============================================================
function esc(s) {{
  if (!s) return '';
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function matchBadge(mt) {{
  const map = {{
    'exact':           ['badge-exact',   'Exact'],
    'local_is_ahead':  ['badge-ahead',   'Local Ahead'],
    'github_is_ahead': ['badge-behind',  'GitHub Ahead'],
    'diverged':        ['badge-diverged','Diverged'],
    'partial_match':   ['badge-partial', 'Partial'],
    'name_only':       ['badge-orphan',  'Name Only'],
    'orphan':          ['badge-orphan',  'Orphan'],
  }};
  const [cls, label] = map[mt] || ['bg-secondary','?'];
  return `<span class="badge ${{cls}}">${{label}}</span>`;
}}

function actionBadge(action) {{
  const map = {{
    'archive_duplicate':  'bg-danger',
    'merge_local_dirs':   'bg-warning text-dark',
    'push_to_branch':     'bg-primary',
    'pull_from_github':   'bg-info text-dark',
    'create_branch':      'bg-purple',
    'review_similarity':  'bg-secondary',
    'already_synced':     'bg-success',
    'create_new_repo':    'bg-primary',
    'archive_or_delete':  'bg-danger',
  }};
  const cls = map[action] || 'bg-secondary';
  return `<span class="badge ${{cls}}">${{esc(action)}}</span>`;
}}

// localStorage-based status persistence.
const LS_KEY = 'cld_rec_status';
function loadStatuses() {{
  try {{ return JSON.parse(localStorage.getItem(LS_KEY) || '{{}}'); }}
  catch(e) {{ return {{}}; }}
}}
function saveStatus(id, status) {{
  const s = loadStatuses(); s[id] = status;
  localStorage.setItem(LS_KEY, JSON.stringify(s));
}}

// ============================================================
// Tab 2: Local Duplicates
// ============================================================
(function buildDupes() {{
  const tbody = document.getElementById('body-dupes');
  APP_DATA.local_dupes.forEach(r => {{
    const rowClass = r.is_exact ? 'row-exact' : 'row-near';
    const tr = `<tr class="${{rowClass}}">
      <td><code>${{esc(r.dir1)}}</code></td>
      <td><code>${{esc(r.dir2)}}</code></td>
      <td>${{r.files}}</td>
      <td>${{esc(r.size_hr)}}</td>
      <td>${{r.sim_pct}}%</td>
      <td>${{actionBadge(r.action)}}</td>
    </tr>`;
    tbody.insertAdjacentHTML('beforeend', tr);
  }});
}})();

// ============================================================
// Tab 3: GitHub Matches
// ============================================================
(function buildGithub() {{
  const tbody = document.getElementById('body-github');
  APP_DATA.github_matches.forEach(r => {{
    const tr = `<tr data-match-type="${{esc(r.match_type)}}">
      <td><code>${{esc(r.local_dir)}}</code></td>
      <td><a href="https://github.com/${{esc(r.repo)}}" target="_blank" rel="noopener">${{esc(r.repo)}}</a></td>
      <td>${{esc(r.org)}}</td>
      <td>${{matchBadge(r.match_type)}}</td>
      <td>${{r.content_sim}}%</td>
      <td>${{r.local_ahead}}</td>
      <td>${{r.github_ahead}}</td>
      <td>${{actionBadge(r.action)}}</td>
    </tr>`;
    tbody.insertAdjacentHTML('beforeend', tr);
  }});
}})();

// ============================================================
// Tab 4: Orphans
// ============================================================
(function buildOrphans() {{
  const tbody = document.getElementById('body-orphans');
  APP_DATA.orphans.forEach(r => {{
    const codeYN = r.has_code
      ? '<span class="badge bg-success">Yes</span>'
      : '<span class="badge bg-secondary">No</span>';
    const tr = `<tr>
      <td><code>${{esc(r.dir_path)}}</code></td>
      <td>${{esc(r.drive)}}</td>
      <td>${{r.file_count}}</td>
      <td>${{esc(r.size_hr)}}</td>
      <td>${{codeYN}}</td>
      <td>${{actionBadge(r.action)}}</td>
    </tr>`;
    tbody.insertAdjacentHTML('beforeend', tr);
  }});
}})();

// ============================================================
// Tab 5: Recommendations
// ============================================================
(function buildRecs() {{
  const tbody = document.getElementById('body-recs');
  const statuses = loadStatuses();
  const priorityLabels = {{1:'1 — Immediate',2:'2 — High',3:'3 — Medium',4:'4 — Low',5:'5 — Info'}};

  APP_DATA.recommendations.forEach(r => {{
    const currentStatus = statuses[r.id] || r.status || 'pending';
    const rowClass = currentStatus === 'done' ? 'status-done' : '';
    const statusSelect = `
      <select class="form-select form-select-sm bg-dark text-light border-secondary rec-status-sel"
              data-rec-id="${{r.id}}"
              style="min-width:100px">
        <option value="pending" ${{currentStatus==='pending'?'selected':''}}>Pending</option>
        <option value="in_progress" ${{currentStatus==='in_progress'?'selected':''}}>In Progress</option>
        <option value="done" ${{currentStatus==='done'?'selected':''}}>Done</option>
        <option value="skipped" ${{currentStatus==='skipped'?'selected':''}}>Skipped</option>
      </select>`;
    const tr = `<tr class="${{rowClass}}" id="rec-row-${{r.id}}">
      <td>${{priorityLabels[r.priority] || r.priority}}</td>
      <td><code class="small">${{esc(r.local_dir)}}</code></td>
      <td>${{r.github_repo ? `<a href="https://github.com/${{esc(r.github_repo)}}" target="_blank" rel="noopener">${{esc(r.github_repo)}}</a>` : '—'}}</td>
      <td>${{actionBadge(r.action)}}</td>
      <td class="small">${{esc(r.rationale)}}</td>
      <td>${{statusSelect}}</td>
    </tr>`;
    tbody.insertAdjacentHTML('beforeend', tr);
  }});

  // Event delegation for status dropdowns.
  tbody.addEventListener('change', function(e) {{
    const sel = e.target.closest('.rec-status-sel');
    if (!sel) return;
    const id = sel.dataset.recId;
    const newStatus = sel.value;
    saveStatus(id, newStatus);
    const row = document.getElementById('rec-row-' + id);
    if (row) {{
      row.className = newStatus === 'done' ? 'status-done' : '';
    }}
  }});
}})();

// ============================================================
// Tab 6: Space Recovery
// ============================================================
(function buildSpace() {{
  document.getElementById('totalRecoverable').textContent = APP_DATA.space_recovery.total_hr;
  const tbody = document.getElementById('body-space');
  APP_DATA.space_recovery.items.forEach(r => {{
    tbody.insertAdjacentHTML('beforeend',
      `<tr><td><code>${{esc(r.dir_path)}}</code></td><td>${{esc(r.size_hr)}}</td></tr>`);
  }});
}})();

// ============================================================
// DataTables init (wait for DOM to be ready)
// ============================================================
$(document).ready(function() {{
  $('#dt-dupes').DataTable({{
    pageLength: 25,
    order: [[4,'desc']],
    columnDefs: [{{targets:[0,1,5], orderable:false}}]
  }});
  const ghTable = $('#dt-github').DataTable({{
    pageLength: 25,
    order: [[4,'desc']],
    columnDefs: [{{targets:[0,1,7], orderable:false}}]
  }});
  $('#dt-orphans').DataTable({{
    pageLength: 25,
    order: [[3,'desc']],
    columnDefs: [{{targets:[0,5], orderable:false}}]
  }});
  $('#dt-recs').DataTable({{
    pageLength: 25,
    order: [[0,'asc']],
    columnDefs: [{{targets:[1,2,4,5], orderable:false}}]
  }});
  $('#dt-space').DataTable({{
    pageLength: 25,
    order: [[1,'desc']],
    columnDefs: [{{targets:[0], orderable:false}}]
  }});

  // GitHub match-type filter.
  $('#ghMatchFilter').on('change', function() {{
    const val = this.value;
    ghTable.column(3).search(val).draw();
  }});
}});

// ============================================================
// Charts (Chart.js)
// ============================================================
(function buildCharts() {{
  // -- Pie: match type distribution --
  const dist = APP_DATA.match_type_dist;
  const pieLabels = Object.keys(dist);
  const pieData   = Object.values(dist);
  const pieColors = [
    '#dc3545','#fd7e14','#0d6efd','#6f42c1',
    '#20c997','#6c757d','#ffc107','#198754','#0dcaf0'
  ];

  const pieCtx = document.getElementById('matchTypePie');
  if (pieCtx && pieLabels.length > 0) {{
    new Chart(pieCtx, {{
      type: 'pie',
      data: {{
        labels: pieLabels,
        datasets: [{{
          data: pieData,
          backgroundColor: pieColors.slice(0, pieLabels.length),
          borderColor: '#212529',
          borderWidth: 2,
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: true,
        plugins: {{
          legend: {{ position: 'right', labels: {{ color: '#dee2e6' }} }},
          tooltip: {{
            callbacks: {{
              label: ctx => ` ${{ctx.label}}: ${{ctx.parsed}}`
            }}
          }}
        }}
      }}
    }});
  }} else if (pieCtx) {{
    pieCtx.parentElement.innerHTML = '<p class="text-secondary p-3">No match data available.</p>';
  }}

  // -- Bar: top 10 duplicate dirs --
  const topDirs = APP_DATA.top_dup_dirs;
  const barCtx  = document.getElementById('topDupBar');
  if (barCtx && topDirs.length > 0) {{
    const barLabels = topDirs.map(d => {{
      const parts = d.dir_path.replace(/\\\\/g,'/').split('/');
      return parts.slice(-2).join('/');
    }});
    const barData = topDirs.map(d => +(d.size / (1024*1024*1024)).toFixed(2));

    new Chart(barCtx, {{
      type: 'bar',
      data: {{
        labels: barLabels,
        datasets: [{{
          label: 'Size (GB)',
          data: barData,
          backgroundColor: 'rgba(220,53,69,0.7)',
          borderColor: '#dc3545',
          borderWidth: 1,
        }}]
      }},
      options: {{
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: true,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            callbacks: {{
              label: ctx => ` ${{topDirs[ctx.dataIndex].size_hr}}`
            }}
          }}
        }},
        scales: {{
          x: {{
            ticks: {{ color: '#adb5bd' }},
            grid:  {{ color: '#343a40' }},
            title: {{ display: true, text: 'GB', color: '#adb5bd' }}
          }},
          y: {{
            ticks: {{ color: '#dee2e6', font: {{ size: 11 }} }},
            grid:  {{ color: '#343a40' }}
          }}
        }}
      }}
    }});
  }} else if (barCtx) {{
    barCtx.parentElement.innerHTML = '<p class="text-secondary p-3">No duplicate directories found.</p>';
  }}
}})();
</script>
</body>
</html>"""
