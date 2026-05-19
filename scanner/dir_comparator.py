"""
scanner/dir_comparator.py

Compares directories to find exact duplicates and near-duplicates.

Exact duplicates:  same tree_hash  →  content_similarity = 1.0
Near-duplicates:   Jaccard similarity on file-SHA-256 sets and
                   (rel_path, sha256) pairs.

Public API
----------
    from scanner.dir_comparator import DirComparator
    cmp = DirComparator(db_path="dupes.db")
    cmp.compare_all(max_depth=2)
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from itertools import combinations
from typing import Iterator

from tqdm import tqdm

logger = logging.getLogger(__name__)

# Only store pairs above this similarity threshold to avoid flooding the DB
# with completely unrelated directory pairs.
_MIN_SIMILARITY = 0.10

# Minimum files a directory must contain to be included in comparisons.
_MIN_FILE_COUNT = 2


# ------------------------------------------------------------------ #
# Internal schema bootstrap
# ------------------------------------------------------------------ #

_DDL = """
CREATE TABLE IF NOT EXISTS dir_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dir1_id INTEGER,
    dir2_id INTEGER,
    content_similarity REAL,
    structural_similarity REAL,
    is_exact_duplicate INTEGER,
    common_files INTEGER,
    unique_to_dir1 INTEGER,
    unique_to_dir2 INTEGER,
    analyzed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_dir_pairs_dir1 ON dir_pairs(dir1_id);
CREATE INDEX IF NOT EXISTS idx_dir_pairs_dir2 ON dir_pairs(dir2_id);
"""


# ------------------------------------------------------------------ #
# Data structures
# ------------------------------------------------------------------ #

@dataclass(frozen=True)
class DirRecord:
    id: int
    abs_path: str
    depth: int
    file_count: int
    tree_hash: str


# ------------------------------------------------------------------ #
# Main class
# ------------------------------------------------------------------ #

class DirComparator:
    """
    Load directory records from SQLite, find duplicate / near-duplicate pairs,
    and persist results to the `dir_pairs` table.

    Parameters
    ----------
    db_path:  Path to the SQLite database produced by FileHasher.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # ---------------------------------------------------------------- #
    # Connection
    # ---------------------------------------------------------------- #

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA temp_store=MEMORY")
        con.executescript(_DDL)
        con.commit()
        return con

    # ---------------------------------------------------------------- #
    # Directory loading
    # ---------------------------------------------------------------- #

    def _load_dirs(
        self, con: sqlite3.Connection, max_depth: int
    ) -> list[DirRecord]:
        """
        Load directories from the DB with file_count >= _MIN_FILE_COUNT
        and depth <= max_depth.
        """
        rows = con.execute(
            """
            SELECT id, abs_path, depth, file_count, tree_hash
            FROM directories
            WHERE file_count >= ?
              AND depth <= ?
            ORDER BY depth, abs_path
            """,
            (_MIN_FILE_COUNT, max_depth),
        ).fetchall()

        dirs = [
            DirRecord(
                id=r[0],
                abs_path=r[1],
                depth=r[2],
                file_count=r[3],
                tree_hash=r[4],
            )
            for r in rows
        ]
        logger.info(
            "Loaded %d directories (file_count>=%d, depth<=%d)",
            len(dirs), _MIN_FILE_COUNT, max_depth,
        )
        return dirs

    # ---------------------------------------------------------------- #
    # File set retrieval
    # ---------------------------------------------------------------- #

    def get_dir_file_sets(
        self,
        con: sqlite3.Connection,
        dir_id: int,
        dir_path: str,
    ) -> tuple[set[str], set[tuple[str, str]]]:
        """
        Return two sets for the directory identified by *dir_path*:

        sha256_set
            One entry per file under *dir_path*, keyed by SHA-256 digest.
            Captures content identity regardless of file name.

        rel_sha256_set
            One entry per file as a ``(rel_path, sha256)`` tuple.
            Captures both structural position and content identity.

        The query uses a prefix match on abs_path so it naturally
        includes all files in sub-directories.
        """
        # Ensure the prefix ends with '/' so we don't accidentally match
        # /mnt/c/projects/foo-bar when looking for /mnt/c/projects/foo.
        prefix = dir_path.rstrip("/") + "/"

        rows = con.execute(
            """
            SELECT rel_path, sha256
            FROM files
            WHERE abs_path LIKE ? ESCAPE '\\'
            """,
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchall()

        sha256_set: set[str] = set()
        rel_sha256_set: set[tuple[str, str]] = set()
        for rel_path, sha256 in rows:
            if sha256:  # guard against NULL (shouldn't happen, but be safe)
                sha256_set.add(sha256)
                rel_sha256_set.add((rel_path, sha256))

        return sha256_set, rel_sha256_set

    # ---------------------------------------------------------------- #
    # Similarity math
    # ---------------------------------------------------------------- #

    @staticmethod
    def _jaccard(set_a: set, set_b: set) -> float:
        """
        Jaccard similarity: |A ∩ B| / |A ∪ B|.
        Returns 0.0 when both sets are empty (convention: no overlap).
        """
        if not set_a and not set_b:
            return 0.0
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        return intersection / union if union > 0 else 0.0

    # ---------------------------------------------------------------- #
    # Exact duplicate detection
    # ---------------------------------------------------------------- #

    def _find_exact_duplicates(
        self,
        con: sqlite3.Connection,
        dirs: list[DirRecord],
    ) -> set[frozenset[int]]:
        """
        Group directories by tree_hash.  Any group of 2+ members is an
        exact duplicate set.  Insert dir_pairs rows for every unique
        (dir_i, dir_j) combination within each group.

        Returns the set of frozenset({dir1_id, dir2_id}) pairs that were
        marked as exact duplicates (so near-duplicate pass can skip them).
        """
        from collections import defaultdict

        hash_groups: dict[str, list[DirRecord]] = defaultdict(list)
        for d in dirs:
            if d.tree_hash:
                hash_groups[d.tree_hash].append(d)

        exact_pairs: set[frozenset[int]] = set()
        exact_groups = {h: g for h, g in hash_groups.items() if len(g) >= 2}

        if not exact_groups:
            logger.info("No exact duplicate directory groups found.")
            return exact_pairs

        logger.info(
            "Found %d exact-duplicate tree_hash group(s) spanning %d directories.",
            len(exact_groups),
            sum(len(g) for g in exact_groups.values()),
        )

        now = time.time()
        batch: list[tuple] = []

        for group in exact_groups.values():
            for d1, d2 in combinations(sorted(group, key=lambda d: d.id), 2):
                exact_pairs.add(frozenset({d1.id, d2.id}))
                # Get file counts for common / unique tallies.
                # For exact duplicates they are trivially equal.
                common = d1.file_count   # same by definition
                batch.append(
                    (
                        d1.id, d2.id,
                        1.0, 1.0,       # content_similarity, structural_similarity
                        1,              # is_exact_duplicate
                        common,
                        0, 0,           # unique_to_dir1, unique_to_dir2
                        now,
                    )
                )

        con.executemany(
            """
            INSERT OR REPLACE INTO dir_pairs
                (dir1_id, dir2_id, content_similarity, structural_similarity,
                 is_exact_duplicate, common_files, unique_to_dir1, unique_to_dir2,
                 analyzed_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            batch,
        )
        con.commit()
        logger.info("Inserted %d exact-duplicate dir_pairs rows.", len(batch))
        return exact_pairs

    # ---------------------------------------------------------------- #
    # Near-duplicate detection
    # ---------------------------------------------------------------- #

    def _candidate_pairs(
        self,
        dirs: list[DirRecord],
        exact_pairs: set[frozenset[int]],
    ) -> Iterator[tuple[DirRecord, DirRecord]]:
        """
        Yield (dir_a, dir_b) candidate pairs for near-duplicate analysis.

        Rules:
        - Only compare directories at the same depth level.
        - Skip pairs already identified as exact duplicates.
        - Use combinations (unordered, no self-comparisons).
        """
        from collections import defaultdict

        by_depth: dict[int, list[DirRecord]] = defaultdict(list)
        for d in dirs:
            by_depth[d.depth].append(d)

        for depth, group in sorted(by_depth.items()):
            if len(group) < 2:
                continue
            for d1, d2 in combinations(group, 2):
                pair_key = frozenset({d1.id, d2.id})
                if pair_key in exact_pairs:
                    continue
                yield d1, d2

    def _count_near_dup_candidates(
        self,
        dirs: list[DirRecord],
        exact_pairs: set[frozenset[int]],
    ) -> int:
        """Count candidate pairs without materialising them all."""
        from collections import defaultdict

        by_depth: dict[int, list[DirRecord]] = defaultdict(list)
        for d in dirs:
            by_depth[d.depth].append(d)

        total = 0
        for group in by_depth.values():
            n = len(group)
            pairs = n * (n - 1) // 2
            # Subtract exact pairs at this depth (approximation — exact pairs
            # at the same depth have already been removed from consideration).
            total += pairs
        return max(0, total - len(exact_pairs))

    def _find_near_duplicates(
        self,
        con: sqlite3.Connection,
        dirs: list[DirRecord],
        exact_pairs: set[frozenset[int]],
    ) -> None:
        """
        Iterate over candidate pairs, compute Jaccard similarities, and
        insert rows for pairs meeting the _MIN_SIMILARITY threshold.
        """
        estimated = self._count_near_dup_candidates(dirs, exact_pairs)
        logger.info(
            "Near-duplicate analysis: ~%d candidate pairs to evaluate.", estimated
        )

        # Pre-load all file sets into memory (keyed by dir.id) to avoid
        # repeated DB queries for large comparison matrices.  For very large
        # scans this could be memory-intensive; we still lazy-load per-pair
        # to be safe.
        file_set_cache: dict[int, tuple[set[str], set[tuple[str, str]]]] = {}

        def _get_sets(d: DirRecord) -> tuple[set[str], set[tuple[str, str]]]:
            if d.id not in file_set_cache:
                file_set_cache[d.id] = self.get_dir_file_sets(con, d.id, d.abs_path)
            return file_set_cache[d.id]

        now = time.time()
        batch: list[tuple] = []
        inserted = 0

        with tqdm(
            total=estimated or None,
            desc="Near-dup comparison",
            unit="pair",
            dynamic_ncols=True,
        ) as pbar:
            for d1, d2 in self._candidate_pairs(dirs, exact_pairs):
                pbar.update(1)

                sha256_a, rel_sha256_a = _get_sets(d1)
                sha256_b, rel_sha256_b = _get_sets(d2)

                content_sim = self._jaccard(sha256_a, sha256_b)
                if content_sim < _MIN_SIMILARITY:
                    continue

                structural_sim = self._jaccard(rel_sha256_a, rel_sha256_b)

                common_files = len(sha256_a & sha256_b)
                unique_to_1 = len(sha256_a - sha256_b)
                unique_to_2 = len(sha256_b - sha256_a)

                batch.append(
                    (
                        d1.id, d2.id,
                        round(content_sim, 6),
                        round(structural_sim, 6),
                        0,                  # is_exact_duplicate
                        common_files,
                        unique_to_1,
                        unique_to_2,
                        now,
                    )
                )

                if len(batch) >= 500:
                    con.executemany(
                        """
                        INSERT OR REPLACE INTO dir_pairs
                            (dir1_id, dir2_id, content_similarity,
                             structural_similarity, is_exact_duplicate,
                             common_files, unique_to_dir1, unique_to_dir2,
                             analyzed_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        batch,
                    )
                    con.commit()
                    inserted += len(batch)
                    batch.clear()

        if batch:
            con.executemany(
                """
                INSERT OR REPLACE INTO dir_pairs
                    (dir1_id, dir2_id, content_similarity,
                     structural_similarity, is_exact_duplicate,
                     common_files, unique_to_dir1, unique_to_dir2,
                     analyzed_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                batch,
            )
            con.commit()
            inserted += len(batch)

        logger.info(
            "Near-duplicate analysis complete: %d pairs stored "
            "(threshold >= %.0f%%).",
            inserted,
            _MIN_SIMILARITY * 100,
        )

    # ---------------------------------------------------------------- #
    # Public entry point
    # ---------------------------------------------------------------- #

    def compare_all(self, max_depth: int = 2) -> None:
        """
        Run the full comparison pipeline:

        1. Load directories (file_count >= 2, depth <= max_depth).
        2. Exact-duplicate pass: group by tree_hash.
        3. Near-duplicate pass: pairwise Jaccard at same depth level.

        Results are written to the `dir_pairs` table.

        Parameters
        ----------
        max_depth:
            Maximum directory depth to include in comparisons.
            Depth 0 = the root itself, 1 = its immediate children, etc.
            Keeping this small (default 2) prevents O(n²) explosion on
            deep trees with thousands of directories.
        """
        con = self._connect()
        started = time.time()

        logger.info("DirComparator.compare_all  max_depth=%d", max_depth)

        dirs = self._load_dirs(con, max_depth)
        if len(dirs) < 2:
            logger.info("Fewer than 2 qualifying directories — nothing to compare.")
            con.close()
            return

        # Pass 1: exact duplicates (tree_hash identity).
        exact_pairs = self._find_exact_duplicates(con, dirs)

        # Pass 2: near-duplicates (Jaccard on file sets).
        self._find_near_duplicates(con, dirs, exact_pairs)

        elapsed = time.time() - started
        total_pairs = con.execute("SELECT COUNT(*) FROM dir_pairs").fetchone()[0]
        exact_count = con.execute(
            "SELECT COUNT(*) FROM dir_pairs WHERE is_exact_duplicate=1"
        ).fetchone()[0]

        logger.info(
            "compare_all finished in %.1f s  "
            "total_pairs=%d  exact=%d  near=%d",
            elapsed,
            total_pairs,
            exact_count,
            total_pairs - exact_count,
        )

        con.close()


# ------------------------------------------------------------------ #
# CLI entry point
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Compare directories for duplicates.")
    parser.add_argument("--db", default="dupes.db", help="SQLite DB path")
    parser.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help="Maximum directory depth to compare (default: 2)",
    )
    args = parser.parse_args()

    cmp = DirComparator(db_path=args.db)
    cmp.compare_all(max_depth=args.max_depth)

    # Summary report.
    con = sqlite3.connect(args.db)
    rows = con.execute(
        """
        SELECT
            d1.abs_path, d2.abs_path,
            p.content_similarity, p.structural_similarity,
            p.is_exact_duplicate, p.common_files
        FROM dir_pairs p
        JOIN directories d1 ON d1.id = p.dir1_id
        JOIN directories d2 ON d2.id = p.dir2_id
        ORDER BY p.content_similarity DESC
        LIMIT 20
        """
    ).fetchall()
    con.close()

    if rows:
        print("\nTop duplicate pairs:")
        print(f"{'Dir 1':<40}  {'Dir 2':<40}  {'Content':>8}  {'Struct':>8}  Exact")
        print("-" * 105)
        for d1p, d2p, cs, ss, ex, cf in rows:
            print(
                f"{d1p[-39:]:<40}  {d2p[-39:]:<40}  "
                f"{cs:8.3f}  {ss:8.3f}  {'YES' if ex else 'no'}"
            )
    else:
        print("No duplicate pairs found.")
