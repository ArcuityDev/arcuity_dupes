#!/usr/bin/env python3
"""
clear_local_dupes — find, compare, and consolidate duplicate files and directories.

Phases:
  scan         Hash files locally → SQLite DB
  fetch-github Enumerate all GitHub repos + file trees
  analyze      Compare local dirs to each other and to GitHub repos
  recommend    Generate prioritized consolidation recommendations
  report       Produce HTML dashboard + Markdown report
  full         Run all phases end-to-end
"""

import os
import sys
import time
import uuid
import logging
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import print as rprint

load_dotenv()

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clear_local_dupes")


def _db_path(db: str) -> str:
    p = db or os.environ.get("DB_PATH", "./clear_local_dupes.db")
    return str(Path(p).expanduser().resolve())


# ── CLI root ──────────────────────────────────────────────────────────────────

@click.group()
@click.version_option("0.1.0")
def cli():
    """Clear Local Dupes — deduplicate and consolidate your codebases."""


# ── scan ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--path", "-p", multiple=True, default=None,
              help="Root path(s) to scan. Defaults to /mnt/c/Dev.")
@click.option("--db", default=None, help="SQLite DB path (default: ./clear_local_dupes.db)")
@click.option("--resume/--no-resume", default=True, show_default=True,
              help="Skip already-hashed files by path+mtime.")
@click.option("--exclude", multiple=True, default=None,
              help="Glob patterns to exclude (e.g. '**/node_modules/**')")
@click.option("--stream/--no-stream", default=True, show_default=True,
              help="Stream files to Lambda as they are hashed (reads LAMBDA_SSH_HOST/KEY/DATA_DIR from .env).")
@click.option("--lambda-host", default=None, envvar="LAMBDA_SSH_HOST",
              help="Lambda SSH target, e.g. ubuntu@170.9.56.76")
@click.option("--lambda-key", default=None, envvar="LAMBDA_SSH_KEY",
              help="Path to SSH private key for Lambda.")
@click.option("--lambda-data-dir", default=None, envvar="LAMBDA_DATA_DIR",
              help="Remote base directory on Lambda for uploaded files.")
def scan(path, db, resume, exclude, stream, lambda_host, lambda_key, lambda_data_dir):
    """Phase 1: walk drives and hash every file into SQLite, streaming copies to Lambda."""
    from db.schema import init_db
    from scanner.file_hasher import FileHasher
    from scanner.streaming_uploader import StreamingUploader

    db_path = _db_path(db)
    conn = init_db(db_path)
    conn.close()

    paths = list(path) if path else ["/mnt/c/Dev"]
    scan_id = f"scan-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    console.rule(f"[bold cyan]Scan {scan_id}")
    console.print(f"  DB      : {db_path}")
    console.print(f"  Paths   : {paths}")
    console.print(f"  Resume  : {resume}")

    uploader = None
    if stream and lambda_host and lambda_key and lambda_data_dir:
        uploader = StreamingUploader(
            db_path=db_path,
            ssh_host=lambda_host,
            ssh_key=lambda_key,
            remote_base=lambda_data_dir,
        )
        uploader.start()
        console.print(f"  Streaming → [bold]{lambda_host}:{lambda_data_dir}[/bold]")
    elif stream:
        console.print("  [yellow]Streaming disabled: set LAMBDA_SSH_HOST, LAMBDA_SSH_KEY, LAMBDA_DATA_DIR[/yellow]")

    hasher = FileHasher(db_path=db_path, scan_id=scan_id, uploader=uploader)
    exclude_patterns = list(exclude) if exclude else None

    try:
        for root_path in paths:
            if not Path(root_path).exists():
                console.print(f"[yellow]WARNING: {root_path} does not exist, skipping.[/yellow]")
                continue
            console.print(f"\n[bold]Scanning:[/bold] {root_path}")
            hasher.scan(root_path=root_path, exclude_patterns=exclude_patterns, resume=resume)
    finally:
        if uploader:
            stats = uploader.stop()
            console.print(
                f"\n  Upload stats: "
                f"[green]{stats['files_uploaded']} files[/green], "
                f"{stats['bytes_uploaded'] // 1024 // 1024} MB, "
                f"[{'red' if stats['errors'] else 'green'}]{stats['errors']} errors[/]"
            )

    console.print("\n[bold green]Scan complete.[/bold green]")


# ── fetch-github ──────────────────────────────────────────────────────────────

@cli.command("fetch-github")
@click.option("--token", default=None, envvar="GITHUB_TOKEN",
              help="GitHub PAT. Falls back to GITHUB_TOKEN env var.")
@click.option("--db", default=None, help="SQLite DB path")
@click.option("--skip-forks/--include-forks", default=False, show_default=True)
def fetch_github(token, db, skip_forks):
    """Phase 2: enumerate all GitHub repos and fetch their file trees."""
    from db.schema import init_db
    from ghapi.repo_fetcher import RepoFetcher

    if not token:
        console.print("[red]ERROR: no GitHub token. Set GITHUB_TOKEN or pass --token.[/red]")
        sys.exit(1)

    db_path = _db_path(db)
    init_db(db_path).close()

    console.rule("[bold cyan]GitHub Repo Fetch")
    fetcher = RepoFetcher(token=token, db_path=db_path)
    count = fetcher.fetch_all()
    console.print(f"\n[bold green]Fetched {count} repos.[/bold green]")


# ── analyze ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--db", default=None, help="SQLite DB path")
@click.option("--max-depth", default=2, show_default=True,
              help="Max directory depth for local-local comparisons.")
def analyze(db, max_depth):
    """Phase 3: compare local dirs (local-local + local-GitHub)."""
    from scanner.dir_comparator import DirComparator
    from ghapi.repo_matcher import RepoMatcher

    db_path = _db_path(db)

    console.rule("[bold cyan]Local Directory Comparison")
    comparator = DirComparator(db_path=db_path)
    pairs = comparator.compare_all(max_depth=max_depth)
    console.print(f"  Found {pairs} directory pairs.")

    console.rule("[bold cyan]Local ↔ GitHub Matching")
    matcher = RepoMatcher(db_path=db_path)
    matches = matcher.match_all()
    console.print(f"  Found {matches} local↔GitHub matches.")


# ── recommend ─────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--db", default=None, help="SQLite DB path")
def recommend(db):
    """Phase 4: generate prioritized consolidation recommendations."""
    from analysis.recommendations import RecommendationEngine

    db_path = _db_path(db)

    console.rule("[bold cyan]Generating Recommendations")
    engine = RecommendationEngine(db_path=db_path)
    recs = engine.generate()

    table = Table(title=f"{len(recs)} Recommendations", show_lines=True)
    table.add_column("P", style="bold", width=3)
    table.add_column("Action", style="cyan", width=22)
    table.add_column("Local Dir", width=40)
    table.add_column("GitHub Repo", width=30)

    colors = {1: "red", 2: "orange1", 3: "yellow", 4: "blue", 5: "green"}
    for r in sorted(recs, key=lambda x: x.get("priority", 3)):
        p = r.get("priority", 3)
        color = colors.get(p, "white")
        table.add_row(
            f"[{color}]{p}[/{color}]",
            r.get("action", ""),
            r.get("local_dir", "")[-40:],
            r.get("github_repo", "") or "—",
        )

    console.print(table)


# ── report ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--db", default=None, help="SQLite DB path")
@click.option("--output-dir", "-o", default="./reports", show_default=True)
@click.option("--format", "fmt", type=click.Choice(["html", "md", "both"]),
              default="both", show_default=True)
def report(db, output_dir, fmt):
    """Phase 5: generate HTML dashboard and/or Markdown report."""
    from reporting.markdown_report import MarkdownReporter
    from reporting.html_dashboard import HTMLReporter

    db_path = _db_path(db)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")

    if fmt in ("md", "both"):
        console.rule("[bold cyan]Markdown Report")
        md_path = str(out / f"report_{timestamp}.md")
        reporter = MarkdownReporter(db_path=db_path)
        reporter.generate(output_path=md_path)
        console.print(f"  Written: [bold]{md_path}[/bold]")

    if fmt in ("html", "both"):
        console.rule("[bold cyan]HTML Dashboard")
        html_path = str(out / f"dashboard_{timestamp}.html")
        reporter = HTMLReporter(db_path=db_path)
        reporter.generate(output_path=html_path)
        console.print(f"  Written: [bold]{html_path}[/bold]")


# ── scripts ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--token", default=None, envvar="GITHUB_TOKEN")
@click.option("--db", default=None)
@click.option("--output-dir", "-o", default="./consolidation_scripts", show_default=True)
def scripts(token, db, output_dir):
    """Generate shell scripts for pushing local dirs to GitHub staging branches."""
    from ghapi.branch_manager import BranchManager

    if not token:
        console.print("[red]ERROR: no GitHub token.[/red]")
        sys.exit(1)

    db_path = _db_path(db)
    mgr = BranchManager(token=token, db_path=db_path)
    mgr.generate_consolidation_scripts(output_dir=output_dir)
    console.print(f"[bold green]Scripts written to {output_dir}[/bold green]")


# ── full pipeline ─────────────────────────────────────────────────────────────

@cli.command()
@click.option("--path", "-p", multiple=True, default=None)
@click.option("--token", default=None, envvar="GITHUB_TOKEN")
@click.option("--db", default=None)
@click.option("--output-dir", "-o", default="./reports", show_default=True)
@click.option("--max-depth", default=2, show_default=True)
def full(path, token, db, output_dir, max_depth):
    """Run all phases: scan → fetch-github → analyze → recommend → report."""
    ctx = click.get_current_context()
    paths = list(path) if path else ["/mnt/c/Dev"]

    console.rule("[bold magenta]clear_local_dupes — Full Pipeline")

    # Phase 1
    ctx.invoke(scan, path=paths, db=db, resume=True, exclude=())

    # Phase 2
    if token:
        ctx.invoke(fetch_github, token=token, db=db, skip_forks=False)
    else:
        console.print("[yellow]No GITHUB_TOKEN — skipping GitHub fetch.[/yellow]")

    # Phase 3
    ctx.invoke(analyze, db=db, max_depth=max_depth)

    # Phase 4
    ctx.invoke(recommend, db=db)

    # Phase 5
    ctx.invoke(report, db=db, output_dir=output_dir, fmt="both")

    console.rule("[bold green]Pipeline complete")


# ── status ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--db", default=None)
def status(db):
    """Show a quick summary of what's in the DB."""
    from db.schema import get_conn

    db_path = _db_path(db)
    if not Path(db_path).exists():
        console.print(f"[red]DB not found: {db_path}[/red]")
        sys.exit(1)

    conn = get_conn(db_path)

    def count(table, where=""):
        q = f"SELECT COUNT(*) FROM {table}"
        if where:
            q += f" WHERE {where}"
        return conn.execute(q).fetchone()[0]

    table = Table(title=f"DB: {db_path}", show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Files scanned", str(count("files")))
    table.add_row("Directories", str(count("directories")))
    table.add_row("Exact duplicate pairs", str(count("dir_pairs", "is_exact_duplicate=1")))
    table.add_row("Near-duplicate pairs", str(count("dir_pairs", "is_exact_duplicate=0 AND content_similarity>=0.5")))
    table.add_row("GitHub repos fetched", str(count("github_repos")))
    table.add_row("GitHub files indexed", str(count("github_files")))
    table.add_row("Local↔GitHub matches", str(count("local_github_matches")))
    table.add_row("Recommendations", str(count("recommendations")))
    table.add_row("Pending actions", str(count("recommendations", "status='pending'")))

    console.print(table)
    conn.close()


if __name__ == "__main__":
    cli()
