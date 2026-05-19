"""
scanner/drive_mapper.py

Detects all mounted drives visible from WSL/Linux. Windows drives appear under
/mnt/<letter>; other block-device or bind mounts are included when readable.

Public API
----------
    from scanner.drive_mapper import list_drives, Drive
    drives = list_drives()
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Pseudo / virtual filesystem types that carry no real files.
_VIRTUAL_FS_TYPES: frozenset[str] = frozenset(
    {
        "proc",
        "sysfs",
        "devtmpfs",
        "devpts",
        "tmpfs",
        "cgroup",
        "cgroup2",
        "pstore",
        "securityfs",
        "debugfs",
        "tracefs",
        "bpf",
        "hugetlbfs",
        "mqueue",
        "fusectl",
        "configfs",
        "autofs",
        "efivarfs",
        "binfmt_misc",
        "overlay",      # Docker layers — not useful for local-file dedup
        "nsfs",
        "rpc_pipefs",
        "nfsd",
    }
)

# Mount-point prefixes that are definitely not user data.
_SKIP_MOUNT_PREFIXES: tuple[str, ...] = (
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/snap",
)


@dataclass
class Drive:
    """Represents a single mounted drive or partition."""

    letter: str          # e.g. "c", "d", or the basename of an arbitrary mount
    mount_path: str      # absolute mount point, e.g. "/mnt/c"
    label: str           # human-readable label; falls back to letter
    total_bytes: int
    free_bytes: int
    is_windows_drive: bool
    fs_type: str = field(default="unknown")

    # ------------------------------------------------------------------ #
    @property
    def used_bytes(self) -> int:
        return self.total_bytes - self.free_bytes

    def __repr__(self) -> str:
        gb = self.total_bytes / (1024**3)
        return (
            f"Drive(letter={self.letter!r}, mount={self.mount_path!r}, "
            f"label={self.label!r}, total={gb:.1f}GB, "
            f"windows={self.is_windows_drive})"
        )


# ------------------------------------------------------------------ #
# Internal helpers
# ------------------------------------------------------------------ #

def _parse_proc_mounts() -> list[dict[str, str]]:
    """
    Parse /proc/mounts and return a list of dicts with keys:
        device, mount_point, fs_type, options
    """
    entries: list[dict[str, str]] = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                entries.append(
                    {
                        "device": parts[0],
                        "mount_point": parts[1],
                        "fs_type": parts[2],
                        "options": parts[3] if len(parts) > 3 else "",
                    }
                )
    except OSError as exc:
        logger.warning("Could not read /proc/mounts: %s", exc)
    return entries


def _is_readable(path: str) -> bool:
    """Return True if *path* is a directory we can listdir."""
    try:
        return os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)
    except OSError:
        return False


def _disk_usage_safe(path: str) -> tuple[int, int]:
    """Return (total_bytes, free_bytes); (0, 0) on any error."""
    try:
        usage = shutil.disk_usage(path)
        return usage.total, usage.free
    except OSError as exc:
        logger.debug("disk_usage failed for %s: %s", path, exc)
        return 0, 0


def _letter_from_mount(mount_point: str) -> str:
    """
    Derive a short identifier from a mount point.

    /mnt/c          -> "c"
    /mnt/d          -> "d"
    /home           -> "home"
    /               -> "root"
    /media/myusb    -> "myusb"
    """
    p = Path(mount_point)
    name = p.name
    if not name:
        return "root"
    return name.lower()


def _build_drive(entry: dict[str, str]) -> Drive | None:
    """
    Try to construct a Drive from a /proc/mounts entry.
    Returns None if the mount should be skipped.
    """
    mount_point = entry["mount_point"]
    fs_type = entry["fs_type"]
    device = entry["device"]

    # Skip virtual filesystems.
    if fs_type in _VIRTUAL_FS_TYPES:
        return None

    # Skip well-known pseudo mount points.
    if any(mount_point.startswith(pfx) for pfx in _SKIP_MOUNT_PREFIXES):
        return None

    # Skip if not actually readable.
    if not _is_readable(mount_point):
        logger.debug("Skipping unreadable mount: %s", mount_point)
        return None

    letter = _letter_from_mount(mount_point)
    is_windows = mount_point.startswith("/mnt/") and len(letter) == 1 and letter.isalpha()

    # Derive a label.  Windows drives: "C:", others: device basename or letter.
    if is_windows:
        label = f"{letter.upper()}:"
    else:
        label = Path(device).name if device not in ("none", "tmpfs", "overlay") else letter

    total, free = _disk_usage_safe(mount_point)
    if total == 0 and not is_windows:
        # Could be a bind-mount sharing space with another mount — still include
        # it if readable, just note zero capacity for now.
        logger.debug("Mount %s reports 0 total bytes (bind-mount?)", mount_point)

    return Drive(
        letter=letter,
        mount_path=mount_point,
        label=label,
        total_bytes=total,
        free_bytes=free,
        is_windows_drive=is_windows,
        fs_type=fs_type,
    )


# ------------------------------------------------------------------ #
# Public API
# ------------------------------------------------------------------ #

def list_drives() -> list[Drive]:
    """
    Return a deduplicated, sorted list of Drive objects for all
    readable mounts on this system.

    Drives are sorted: Windows drive letters first (alphabetical),
    then other mounts alphabetically by mount_path.
    """
    raw_entries = _parse_proc_mounts()
    logger.info("Parsed %d entries from /proc/mounts", len(raw_entries))

    seen_mount_points: set[str] = set()
    drives: list[Drive] = []

    for entry in raw_entries:
        mp = entry["mount_point"]
        if mp in seen_mount_points:
            continue
        seen_mount_points.add(mp)

        drive = _build_drive(entry)
        if drive is None:
            continue

        drives.append(drive)
        logger.debug("Found drive: %r", drive)

    # Stable sort: Windows drives first by letter, then others by mount path.
    drives.sort(
        key=lambda d: (0 if d.is_windows_drive else 1, d.letter, d.mount_path)
    )

    logger.info(
        "Detected %d usable drive(s): %s",
        len(drives),
        [d.mount_path for d in drives],
    )
    return drives


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    for drv in list_drives():
        gb_total = drv.total_bytes / (1024**3) if drv.total_bytes else 0
        gb_free = drv.free_bytes / (1024**3) if drv.free_bytes else 0
        print(
            f"  {drv.label:<8}  {drv.mount_path:<20}  "
            f"{gb_total:7.1f} GB total  {gb_free:7.1f} GB free  "
            f"[{drv.fs_type}]  windows={drv.is_windows_drive}"
        )
