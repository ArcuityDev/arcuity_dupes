import sqlite3
import time


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scan_meta (
    id INTEGER PRIMARY KEY,
    scan_id TEXT UNIQUE,
    started_at REAL,
    completed_at REAL,
    root_path TEXT,
    total_files INTEGER DEFAULT 0,
    total_dirs INTEGER DEFAULT 0,
    total_bytes INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
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

CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_git_sha1 ON files(git_sha1);
CREATE INDEX IF NOT EXISTS idx_files_drive ON files(drive);
CREATE INDEX IF NOT EXISTS idx_files_abs_path ON files(abs_path);
CREATE INDEX IF NOT EXISTS idx_files_scan_id ON files(scan_id);

CREATE TABLE IF NOT EXISTS directories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT,
    drive TEXT,
    abs_path TEXT UNIQUE,
    parent_path TEXT,
    depth INTEGER,
    name TEXT,
    file_count INTEGER DEFAULT 0,
    total_size_bytes INTEGER DEFAULT 0,
    tree_hash TEXT,
    scanned_at REAL
);

CREATE INDEX IF NOT EXISTS idx_dirs_tree_hash ON directories(tree_hash);
CREATE INDEX IF NOT EXISTS idx_dirs_abs_path ON directories(abs_path);
CREATE INDEX IF NOT EXISTS idx_dirs_depth ON directories(depth);

CREATE TABLE IF NOT EXISTS dir_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dir1_id INTEGER,
    dir2_id INTEGER,
    content_similarity REAL,
    structural_similarity REAL,
    is_exact_duplicate INTEGER DEFAULT 0,
    common_files INTEGER DEFAULT 0,
    unique_to_dir1 INTEGER DEFAULT 0,
    unique_to_dir2 INTEGER DEFAULT 0,
    analyzed_at REAL,
    FOREIGN KEY (dir1_id) REFERENCES directories(id),
    FOREIGN KEY (dir2_id) REFERENCES directories(id)
);

CREATE INDEX IF NOT EXISTS idx_dir_pairs_dir1 ON dir_pairs(dir1_id);
CREATE INDEX IF NOT EXISTS idx_dir_pairs_dir2 ON dir_pairs(dir2_id);
CREATE INDEX IF NOT EXISTS idx_dir_pairs_sim ON dir_pairs(content_similarity);

CREATE TABLE IF NOT EXISTS github_repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org TEXT,
    name TEXT,
    full_name TEXT UNIQUE,
    clone_url TEXT,
    ssh_url TEXT,
    default_branch TEXT,
    last_commit_at TEXT,
    is_private INTEGER DEFAULT 0,
    topics TEXT,
    description TEXT,
    fetched_at REAL
);

CREATE TABLE IF NOT EXISTS github_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER,
    path TEXT,
    git_sha1 TEXT,
    size_bytes INTEGER,
    FOREIGN KEY (repo_id) REFERENCES github_repos(id)
);

CREATE INDEX IF NOT EXISTS idx_ghfiles_sha1 ON github_files(git_sha1);
CREATE INDEX IF NOT EXISTS idx_ghfiles_repo ON github_files(repo_id);

CREATE TABLE IF NOT EXISTS local_github_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_dir_id INTEGER,
    github_repo_id INTEGER,
    content_similarity REAL,
    structural_similarity REAL,
    match_type TEXT,
    local_ahead_count INTEGER DEFAULT 0,
    github_ahead_count INTEGER DEFAULT 0,
    analyzed_at REAL,
    FOREIGN KEY (local_dir_id) REFERENCES directories(id),
    FOREIGN KEY (github_repo_id) REFERENCES github_repos(id)
);

CREATE INDEX IF NOT EXISTS idx_lgm_local ON local_github_matches(local_dir_id);
CREATE INDEX IF NOT EXISTS idx_lgm_repo ON local_github_matches(github_repo_id);
CREATE INDEX IF NOT EXISTS idx_lgm_type ON local_github_matches(match_type);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_dir TEXT,
    github_repo TEXT,
    action TEXT,
    rationale TEXT,
    priority INTEGER DEFAULT 3,
    status TEXT DEFAULT 'pending',
    created_at REAL
);

CREATE INDEX IF NOT EXISTS idx_recs_priority ON recommendations(priority);
CREATE INDEX IF NOT EXISTS idx_recs_action ON recommendations(action);
CREATE INDEX IF NOT EXISTS idx_recs_status ON recommendations(status);

CREATE TABLE IF NOT EXISTS uploads (
    file_id INTEGER PRIMARY KEY,
    abs_path TEXT,
    remote_path TEXT,
    uploaded_at REAL,
    FOREIGN KEY (file_id) REFERENCES files(id)
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA foreign_keys=ON")
    for statement in SCHEMA_SQL.split(";"):
        statement = statement.strip()
        if statement:
            conn.execute(statement)
    conn.commit()
    return conn


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
