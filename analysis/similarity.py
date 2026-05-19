"""
analysis/similarity.py

Utility functions for computing file and directory similarity.
All functions operate on Python sets/dicts — callers load data from the DB
and pass the appropriate structures in.  Nothing here touches SQLite directly.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# Common suffixes that should be stripped before name comparison so that
# "myproject_backup" is still recognised as similar to "myproject".
# ---------------------------------------------------------------------------
_STRIP_SUFFIXES: list[str] = [
    "_demo", "-demo",
    "_fresh", "-fresh",
    "_backup", "-backup",
    "_bak", "-bak",
    "_old", "-old",
    "_new", "-new",
    "_copy", "-copy",
    "_2", "-2",
    "_v2", "-v2",
    "_final", "-final",
    "_test", "-test",
    "_tmp", "-tmp",
    "_temp", "-temp",
]

# Pre-compile: longest first so greedier suffixes are matched first.
_STRIP_SUFFIXES.sort(key=len, reverse=True)


# ---------------------------------------------------------------------------
# Core similarity primitives
# ---------------------------------------------------------------------------

def jaccard(set_a: set, set_b: set) -> float:
    """
    Return the Jaccard similarity coefficient of two sets.

    Returns 0.0 when both sets are empty (avoids ZeroDivisionError and is the
    sensible convention — two empty sets share nothing meaningful).
    """
    if not set_a and not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def dir_content_similarity(sha256_set_a: set[str], sha256_set_b: set[str]) -> float:
    """
    Jaccard similarity on the *content* of two directories.

    Each set contains the SHA-256 digests of files in the directory.
    Path and filename are ignored — two directories that contain the same bytes
    (even under different names) score 1.0.

    Returns 0.0 if both sets are empty.
    """
    return jaccard(sha256_set_a, sha256_set_b)


def dir_structural_similarity(
    file_set_a: set[tuple[str, str]],  # (rel_path, sha256)
    file_set_b: set[tuple[str, str]],
) -> float:
    """
    Jaccard similarity on (relative_path, sha256) pairs.

    A pair matches only when the same content appears at the same relative path
    in both directories.  This is stricter than content-only similarity: two
    directories with identical files but different layouts score lower here.

    Returns 0.0 if both sets are empty.
    """
    return jaccard(file_set_a, file_set_b)


# ---------------------------------------------------------------------------
# Name similarity
# ---------------------------------------------------------------------------

def _normalise_name(name: str) -> str:
    """
    Normalise a directory / repo name for fuzzy comparison:

    1. Lowercase.
    2. Collapse hyphens, underscores, dots, and spaces into a single space.
    3. Strip common trailing suffixes (e.g. _backup, _old).
    4. Strip any trailing digits that remain after the above.
    """
    s = name.lower()
    # Unify separators to a single space.
    s = re.sub(r"[-_.\s]+", " ", s).strip()

    # Strip known suffixes (already lowercased and separator-normalised).
    changed = True
    while changed:
        changed = False
        for suffix in _STRIP_SUFFIXES:
            # Normalise the suffix the same way.
            norm_suffix = re.sub(r"[-_.\s]+", " ", suffix.lower()).strip()
            if s.endswith(norm_suffix):
                s = s[: -len(norm_suffix)].rstrip()
                changed = True
                break

    # Strip trailing lone digits (e.g. "myproject 2").
    s = re.sub(r"\s+\d+$", "", s).strip()

    return s


def name_similarity(name_a: str, name_b: str) -> float:
    """
    Return a similarity score in [0.0, 1.0] between two names.

    Computes SequenceMatcher ratios at three levels of normalisation and
    returns the maximum:

    1. Raw names (case-insensitive).
    2. Separator-unified names (hyphens/underscores/spaces → single space).
    3. Suffix-stripped, separator-unified names.

    Returning the maximum means "give the names the benefit of the doubt" —
    if they are similar *under any reasonable normalisation*, reflect that.
    """
    if not name_a or not name_b:
        return 0.0

    def ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()

    # Level 1: lowercase only.
    r1 = ratio(name_a.lower(), name_b.lower())

    # Level 2: separator-unified but not suffix-stripped.
    sep_a = re.sub(r"[-_.\s]+", " ", name_a.lower()).strip()
    sep_b = re.sub(r"[-_.\s]+", " ", name_b.lower()).strip()
    r2 = ratio(sep_a, sep_b)

    # Level 3: fully normalised (suffix-stripped).
    norm_a = _normalise_name(name_a)
    norm_b = _normalise_name(name_b)
    r3 = ratio(norm_a, norm_b) if (norm_a and norm_b) else 0.0

    return max(r1, r2, r3)


# ---------------------------------------------------------------------------
# Match classification
# ---------------------------------------------------------------------------

def classify_match(
    content_sim: float,
    structural_sim: float,
    local_file_count: int,
    github_file_count: int,
    local_ahead: int,
    github_ahead: int,
) -> str:
    """
    Classify the relationship between a local directory and a GitHub repo.

    Parameters
    ----------
    content_sim:
        Jaccard similarity on SHA-256 hashes (0.0–1.0).
    structural_sim:
        Jaccard similarity on (rel_path, sha256) pairs (0.0–1.0).
    local_file_count:
        Total files in the local directory.
    github_file_count:
        Total files in the GitHub repo.
    local_ahead:
        Number of files present locally but not in GitHub.
    github_ahead:
        Number of files present in GitHub but not locally.

    Returns
    -------
    One of:
        'exact'            – byte-for-byte identical content at identical paths,
                             same file count.
        'local_is_ahead'   – local has extra files, GitHub is a strict subset.
        'github_is_ahead'  – GitHub has extra files, local is a strict subset.
        'diverged'         – both sides have files the other lacks.
        'partial_match'    – meaningful overlap but below the 0.8 threshold.
        'name_only'        – negligible content overlap; similarity is name-based.
    """
    # Exact: structurally identical and same number of files.
    if structural_sim == 1.0 and local_file_count == github_file_count:
        return "exact"

    # High content similarity branch — determine directionality.
    if content_sim >= 0.8:
        if local_ahead > 0 and github_ahead == 0:
            return "local_is_ahead"
        if github_ahead > 0 and local_ahead == 0:
            return "github_is_ahead"
        if local_ahead > 0 and github_ahead > 0:
            return "diverged"
        # content_sim >= 0.8 but both ahead-counts are 0:
        # Files are mostly the same bytes but paths differ — treat as diverged
        # rather than exact (structural_sim != 1.0 got us here).
        return "diverged"

    # Moderate content similarity — both sides diverged below 0.8 threshold.
    if content_sim >= 0.5 and local_ahead > 0 and github_ahead > 0:
        return "diverged"

    # Some overlap.
    if content_sim >= 0.15:
        return "partial_match"

    # Negligible content match — only name similarity (if any) triggered this.
    return "name_only"


# ---------------------------------------------------------------------------
# Byte formatting
# ---------------------------------------------------------------------------

def format_bytes(n: int) -> str:
    """
    Convert *n* bytes to a human-readable string with one decimal place.

    Examples
    --------
    >>> format_bytes(1_234_567_890)
    '1.1 GB'
    >>> format_bytes(345_000_000)
    '329.1 MB'
    >>> format_bytes(12_345)
    '12.1 KB'
    >>> format_bytes(512)
    '512 B'
    """
    if n < 0:
        raise ValueError(f"Byte count must be non-negative, got {n!r}")

    units = [
        (1 << 30, "GB"),
        (1 << 20, "MB"),
        (1 << 10, "KB"),
    ]
    for threshold, label in units:
        if n >= threshold:
            return f"{n / threshold:.1f} {label}"
    return f"{n} B"
