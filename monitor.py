#!/usr/bin/env python3
"""
monitor.py  —  pipeline status dashboard.

python3 monitor.py

Clears the terminal and redraws every 6 seconds.
Rings the bell 3x and shows a full-screen alert on any failure.
Ctrl-C to quit.
"""

import os, re, subprocess, sys, time
from datetime import datetime
from pathlib import Path

LAMBDA_HOST = "ubuntu@170.9.56.76"
LAMBDA_KEY  = str(Path("~/.ssh/joe-05012026.pem").expanduser())
SSH_CTL     = "/tmp/cld-mon-ssh"
POLL        = 6

# ── ANSI helpers ──────────────────────────────────────────────────────────────
R  = "\033[0m"
BOLD = "\033[1m"
RED  = "\033[31m"
GRN  = "\033[32m"
YEL  = "\033[33m"
CYN  = "\033[36m"
DIM  = "\033[2m"
REDBG = "\033[41m"

def clr():   print("\033[2J\033[H", end="")
def bell():  sys.stdout.write("\a\a\a"); sys.stdout.flush()

# ── SSH (multiplexed so it stays fast) ───────────────────────────────────────
def ssh(cmd, timeout=8):
    try:
        r = subprocess.run(
            ["ssh", "-i", LAMBDA_KEY,
             "-o", "StrictHostKeyChecking=no",
             "-o", f"ControlPath={SSH_CTL}",
             "-o", "ControlMaster=auto",
             "-o", "ControlPersist=120s",
             "-o", f"ConnectTimeout={timeout}",
             LAMBDA_HOST, cmd],
            capture_output=True, text=True, timeout=timeout+2)
        return r.stdout.strip()
    except Exception:
        return ""

def tail_local(path, n=8):
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
        return lines[-n:]
    except FileNotFoundError:
        return []

def tail_lambda(path, n=8):
    out = ssh(f"tail -n {n} {path} 2>/dev/null || true")
    return out.splitlines() if out else []

# ── Failure detection ─────────────────────────────────────────────────────────
ERR = re.compile(
    r"(Traceback \(most recent|^\s*File \".*\", line \d|^(ERROR|FATAL|CRITICAL)|"
    r"SystemExit|subprocess.*[Ee]rror|FAILED|[Kk]illed by signal|command not found)",
    re.M)

NONFATAL = re.compile(
    r"(database is locked|NotADirectoryError|integer expression|WARNING|"
    r"Hashing|file/s|cached=|\d+%)",
    re.I)

def find_error(lines):
    for l in reversed(lines):
        stripped = l.strip()
        if not stripped or NONFATAL.search(stripped):
            continue
        if ERR.search(stripped):
            return stripped
    return None

# ── DB sync ───────────────────────────────────────────────────────────────────
def sync_db_to_lambda():
    db  = "/mnt/c/Users/Administrator/CascadeProjects/clear_dupes_on_my_machines/clear_local_dupes.db"
    key = str(Path("~/.ssh/joe-05012026.pem").expanduser())
    r = subprocess.run(
        ["rsync", "-az", "-e", f"ssh -i {key} -o StrictHostKeyChecking=no",
         db, "ubuntu@170.9.56.76:/home/ubuntu/clear_local_dupes/"],
        capture_output=True)
    return r.returncode == 0

def pull_reports_from_lambda():
    key = str(Path("~/.ssh/joe-05012026.pem").expanduser())
    subprocess.run(
        ["rsync", "-az", "-e", f"ssh -i {key} -o StrictHostKeyChecking=no",
         "ubuntu@170.9.56.76:/home/ubuntu/clear_local_dupes/reports/",
         "/mnt/c/Users/Administrator/CascadeProjects/clear_dupes_on_my_machines/reports/"],
        capture_output=True)

# ── Probes ────────────────────────────────────────────────────────────────────
def probe_scan():
    # Aggregate across all 3 parallel workers
    # A worker is "done" if its DONE marker is present — even if earlier errors exist.
    # Only flag an error if a worker has errors AND no DONE marker (crashed before finishing).
    all_lines, counts, rates = [], [], []
    workers_done = 0
    worker_errors = []
    for i in (1, 2, 3):
        lines = tail_local(f"/tmp/scan_w{i}.log", 10)
        all_lines += lines
        worker_done = any(f"WORKER{i}_DONE" in l for l in lines)
        if worker_done:
            workers_done += 1
        elif find_error(lines):  # only an error if NOT done
            worker_errors.append(i)
        for l in reversed(lines):
            m = re.search(r"(\d+)file \[.*?,\s*([\d.]+)file/s", l)
            if m:
                counts.append(int(m.group(1)))
                rates.append(float(m.group(2)))
                break

    done  = workers_done == 3
    err   = find_error([l for i in worker_errors
                        for l in tail_local(f"/tmp/scan_w{i}.log", 10)]) if worker_errors else None
    total = sum(counts)
    rate  = sum(rates)
    prog  = f"W1+W2+W3: {total:,} files  {rate:.0f} files/s  ({workers_done}/3 workers done)"
    return dict(label="1. Local Scan (3 workers)", done=done, err=err,
                running=not done and not err, prog=prog, lines=all_lines[-6:])

def probe_sync(db_synced: bool):
    # Sync is now handled by monitor.py itself; no external watcher needed.
    if db_synced:
        prog = "DB synced to Lambda by monitor"
        return dict(label="2. DB → Lambda", done=True, err=None,
                    running=False, prog=prog, lines=[prog])
    else:
        prog = "Handled by monitor — waiting for scan to finish..."
        return dict(label="2. DB → Lambda", done=False, err=None,
                    running=False, prog=prog, lines=[prog])

def probe_github():
    lines = tail_lambda("/home/ubuntu/clear_local_dupes/github_fetch.log", 6)
    done  = any(k in " ".join(lines[-3:]) for k in ("DONE","repos fetched","Fetched"))
    err   = find_error(lines)
    last  = lines[-1].strip()[:90] if lines else "Checking Lambda..."
    return dict(label="3. GitHub Fetch", done=done, err=err,
                running=not done and not err, prog=last, lines=lines)

def probe_watcher():
    lines = tail_lambda("/home/ubuntu/clear_local_dupes/watch_and_run.log", 8)
    done  = any("ALL DONE" in l for l in lines)
    err   = find_error(lines)
    stages = ("Running analyze","Running recommend","Generating reports","Merge complete")
    active = any(s in " ".join(lines) for s in stages)
    waiting = any("Waiting" in l for l in lines)
    stage = "Done" if done else ("Analyzing" if active else ("Waiting for DB" if waiting else "Starting"))
    last = lines[-1].strip()[:90] if lines else "Checking Lambda..."
    return dict(label="4. Lambda Analysis", done=done, err=err,
                running=not done and not err,
                prog=f"[{stage}]  {last}", lines=lines)

# ── Rendering ─────────────────────────────────────────────────────────────────
def status_icon(p):
    if p["err"]:     return f"{REDBG}{BOLD} ✗ ERROR  {R}"
    if p["done"]:    return f"{GRN}{BOLD} ✓ Done   {R}"
    if p["running"]: return f"{YEL}{BOLD} ⟳ Running{R}"
    return f"{DIM} ◌ Pending{R}"

def render(probes, alerted):
    clr()
    now = datetime.now().strftime("%H:%M:%S")
    all_done = all(p["done"] for p in probes)
    errors   = [(p["label"], p["err"]) for p in probes if p["err"]]

    # Header
    if errors:
        print(f"{REDBG}{BOLD}{'  ⚠  PIPELINE FAILURE  ⚠':^78}{R}")
    elif all_done:
        print(f"{GRN}{BOLD}{'  ✓  ALL STAGES COMPLETE  ':^78}{R}")
    else:
        print(f"{CYN}{BOLD}  clear_local_dupes — Pipeline Monitor{R}  {DIM}{now}{R}")

    print("─" * 78)

    # Status rows
    for p in probes:
        label = f"{BOLD}{p['label']}{R}"
        icon  = status_icon(p)
        prog  = p["prog"] or ""
        # Truncate progress to fit terminal
        prog_display = (prog[:50] + "…") if len(prog) > 51 else prog
        print(f"  {icon}  {label:<30}  {DIM}{prog_display}{R}")

    print("─" * 78)

    # Error detail
    if errors:
        print(f"\n{RED}{BOLD}  ERRORS DETECTED:{R}")
        for label, err in errors:
            print(f"  {RED}[{label}]{R}  {err[:90]}")
        if errors and not alerted:
            bell()

    # Log tail of most active process
    active = next((p for p in probes if p["running"]), None) or probes[-1]
    print(f"\n{CYN}  {active['label']} — recent output:{R}")
    for line in active["lines"][-6:]:
        color = RED if ERR.search(line) else (GRN if "Done" in line or "done" in line else DIM)
        print(f"  {color}{line[:90]}{R}")

    print(f"\n{DIM}  Refreshes every {POLL}s · Ctrl-C to quit{R}")

    if all_done:
        print(f"\n{GRN}{BOLD}  Reports on Lambda: /home/ubuntu/clear_local_dupes/reports/{R}")
        print(f"{GRN}  Pull locally:  rsync -az -e 'ssh -i {LAMBDA_KEY}' {LAMBDA_HOST}:/home/ubuntu/clear_local_dupes/reports/ ./reports/{R}")

    return errors

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    alerted   = False
    db_synced = False
    reports_pulled = False
    while True:
        scan_probe    = probe_scan()
        sync_probe    = probe_sync(db_synced)
        github_probe  = probe_github()
        watcher_probe = probe_watcher()
        probes = [scan_probe, sync_probe, github_probe, watcher_probe]

        # One-time DB sync: trigger as soon as all 3 scan workers are done
        if scan_probe["done"] and not db_synced:
            print(f"\n{YEL}  → All scan workers done. Syncing DB to Lambda…{R}")
            sys.stdout.flush()
            ok = sync_db_to_lambda()
            db_synced = True
            status = f"{GRN}DB sync OK{R}" if ok else f"{RED}DB sync FAILED (check rsync){R}"
            print(f"  {status}")
            sys.stdout.flush()
            # Rebuild sync_probe with updated flag so it renders correctly
            sync_probe = probe_sync(db_synced)
            probes[1]  = sync_probe

        # One-time report pull: trigger when Lambda analysis is all done
        if all(p["done"] for p in probes) and not reports_pulled:
            pull_reports_from_lambda()
            reports_pulled = True

        errors = render(probes, alerted)
        if errors and not alerted:
            alerted = True
        if all(p["done"] for p in probes):
            break
        time.sleep(POLL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
