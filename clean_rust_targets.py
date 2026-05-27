#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich>=13.7"]
# ///
"""Find Rust `target/` dirs on the system and `cargo clean` the ones older than N days."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.box import ROUNDED
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

console = Console()

SKIP_DIRS = {
    # macOS
    ".Trash", "Library", "Pictures", "Music", "Movies",
    # Linux
    "snap", ".var",
    # shared
    ".cargo", ".rustup", ".cache", ".git", "node_modules",
}

DEFAULTS: dict = {
    "roots": ["~", "~/.pilot"],
    "days": 1.0,
    "size_jobs": 16,
    "clean_jobs": 4,
}


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "rust-clean" / "config.toml"


DEFAULT_CONFIG_TEMPLATE = """\
# rust-clean config — https://github.com/AntoineToussaint/rust-clean
# CLI flags override anything set here.

# Directories to scan for Cargo.toml files (supports ~ expansion).
roots = ["~", "~/.pilot"]

# Minimum age in days for a target/ dir to be considered stale.
days = 1.0

# Parallel workers when computing target/ sizes (I/O bound).
size_jobs = 16

# Parallel `cargo clean` workers (disk bound; keep modest).
clean_jobs = 4
"""


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        console.print(f"[yellow]warning:[/] could not read {path}: {e}")
        return {}


def write_default_config(path: Path, force: bool = False) -> bool:
    if path.exists() and not force:
        if not Confirm.ask(
            f"[yellow]{path} already exists. overwrite?[/]", default=False
        ):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEMPLATE)
    return True


def find_rust_targets(root: Path):
    root = root.expanduser().resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if "Cargo.toml" in filenames:
            crate = Path(dirpath)
            target = crate / "target"
            if target.is_dir() and not target.is_symlink():
                yield crate, target
            dirnames[:] = []


def dir_mtime(path: Path) -> float:
    try:
        latest = path.stat().st_mtime
    except OSError:
        return 0.0
    try:
        for entry in path.iterdir():
            try:
                latest = max(latest, entry.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return latest


def dir_size(path: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path, followlinks=False):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                continue
    return total


def human_size(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024:
            return f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} PB"


def age_color(days: float) -> str:
    if days < 7:
        return "yellow"
    if days < 30:
        return "orange3"
    return "red"


def size_color(n: int) -> str:
    gb = n / (1024**3)
    if gb < 1:
        return "green"
    if gb < 5:
        return "yellow"
    return "red bold"


def banner() -> Panel:
    title = Text("🦀  rust target cleaner", style="bold magenta")
    subtitle = Text("\nfind & sweep stale cargo build dirs", style="dim")
    return Panel(Group(title, subtitle), box=ROUNDED, border_style="magenta", padding=(0, 2))


def scan(roots: list[str], cutoff: float, workers: int):
    """Walk roots, find stale target/ dirs, size them in parallel."""
    stale: list[tuple[Path, Path, float]] = []
    seen: set[Path] = set()

    progress = Progress(
        SpinnerColumn(style="magenta"),
        TextColumn("[bold]{task.description}"),
        TextColumn("[dim]{task.fields[detail]}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )

    with progress:
        for raw in roots:
            root = Path(raw).expanduser()
            if not root.exists():
                console.print(f"[dim]skip[/] [yellow]{root}[/] (does not exist)")
                continue
            task = progress.add_task(f"scanning {root}", detail="", total=None)
            for crate, target in find_rust_targets(root):
                progress.update(task, detail=str(crate))
                if target in seen:
                    continue
                seen.add(target)
                mtime = dir_mtime(target)
                if mtime < cutoff:
                    stale.append((crate, target, mtime))
            progress.remove_task(task)

        if not stale:
            return []

        size_task = progress.add_task(
            f"sizing {len(stale)} target dir(s)", detail="", total=len(stale),
        )
        candidates: list[tuple[Path, Path, float, int]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(dir_size, t): (c, t, m) for c, t, m in stale}
            for fut in as_completed(futures):
                crate, target, mtime = futures[fut]
                progress.update(size_task, detail=str(crate))
                candidates.append((crate, target, mtime, fut.result()))
                progress.advance(size_task)
        progress.remove_task(size_task)

    candidates.sort(key=lambda x: x[3], reverse=True)
    return candidates


def render_table(candidates, days: float) -> Table:
    table = Table(
        title=f"[bold]🧹  stale target/ dirs (>{days}d old)[/]",
        box=ROUNDED,
        border_style="magenta",
        header_style="bold magenta",
        title_justify="left",
        show_lines=False,
        expand=True,
    )
    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("size", justify="right")
    table.add_column("age", justify="right")
    table.add_column("crate", overflow="fold")

    now = time.time()
    for i, (crate, _, mtime, size) in enumerate(candidates, 1):
        age = (now - mtime) / 86400
        table.add_row(
            str(i),
            Text(human_size(size), style=size_color(size)),
            Text(f"{age:.1f}d", style=age_color(age)),
            str(crate),
        )

    total = sum(s for *_, s in candidates)
    table.add_section()
    table.add_row(
        "",
        Text(human_size(total), style="bold green"),
        "",
        Text(f"{len(candidates)} crate(s) — reclaimable", style="bold green"),
    )
    return table


def _run_clean(crate: Path):
    r = subprocess.run(
        ["cargo", "clean"], cwd=crate,
        capture_output=True, text=True,
    )
    return crate, r.returncode, r.stderr


def clean_all(candidates, workers: int) -> int:
    failed = 0
    progress = Progress(
        SpinnerColumn(style="green"),
        TextColumn("[bold]cleaning[/] [dim]({task.fields[active]} active)[/]"),
        BarColumn(complete_style="green", finished_style="bold green"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        task = progress.add_task("", total=len(candidates), active=0)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_run_clean, c) for c, *_ in candidates]
            progress.update(task, active=min(workers, len(candidates)))
            remaining = len(futures)
            for fut in as_completed(futures):
                crate, rc, stderr = fut.result()
                if rc == 0:
                    console.print(f"  [green]✓[/] {crate}")
                else:
                    failed += 1
                    console.print(f"  [red]✗[/] {crate} [dim](exit {rc})[/]")
                    if stderr.strip():
                        console.print(f"    [red dim]{stderr.strip().splitlines()[-1]}[/]")
                remaining -= 1
                progress.update(task, active=min(workers, remaining))
                progress.advance(task)
    return failed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", action="append", default=None,
                   help="Where to search (repeatable). Overrides config.")
    p.add_argument("--days", type=float, default=None, help="Min age in days")
    p.add_argument("--dry-run", action="store_true", help="List candidates, don't clean")
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p.add_argument("--size-jobs", type=int, default=None, help="Parallel sizing workers")
    p.add_argument("--clean-jobs", type=int, default=None, help="Parallel cargo-clean workers")
    p.add_argument("--config", type=Path, default=None,
                   help=f"Config path (default: {config_path()})")
    p.add_argument("--init", action="store_true",
                   help="Write a starter config file and exit")
    p.add_argument("--show-config", action="store_true",
                   help="Print resolved config (after merging file + flags) and exit")
    args = p.parse_args()

    console.print(banner())

    cfg_path = args.config or config_path()

    if args.init:
        if write_default_config(cfg_path):
            console.print(f"  [green]✓[/] wrote starter config to [bold]{cfg_path}[/]")
            console.print(f"  [dim]edit it, then run [bold]rust-clean --dry-run[/][/]\n")
            return 0
        console.print("[yellow]aborted.[/]")
        return 0

    cfg = {**DEFAULTS, **load_config(cfg_path)}
    roots = args.root or cfg["roots"]
    days = args.days if args.days is not None else cfg["days"]
    size_jobs = args.size_jobs if args.size_jobs is not None else cfg["size_jobs"]
    clean_jobs = args.clean_jobs if args.clean_jobs is not None else cfg["clean_jobs"]

    if args.show_config:
        src = "config file" if cfg_path.exists() else "built-in defaults"
        console.print(f"  [dim]source:[/] {src} [dim]({cfg_path})[/]")
        console.print(f"  [bold]roots[/]      = {roots}")
        console.print(f"  [bold]days[/]       = {days}")
        console.print(f"  [bold]size_jobs[/]  = {size_jobs}")
        console.print(f"  [bold]clean_jobs[/] = {clean_jobs}\n")
        return 0

    if not shutil.which("cargo"):
        console.print("[red bold]error:[/] `cargo` not found in PATH")
        return 1

    cutoff = time.time() - days * 86400
    candidates = scan(roots, cutoff, size_jobs)

    if not candidates:
        console.print("\n[bold green]✨ nothing to clean — you're tidy![/]\n")
        return 0

    console.print()
    console.print(render_table(candidates, days))

    if args.dry_run:
        console.print("\n[dim]dry-run — no changes made.[/]\n")
        return 0

    if not args.yes and not Confirm.ask("\n[bold]proceed with cargo clean?[/]", default=False):
        console.print("[yellow]aborted.[/]")
        return 0

    console.print()
    failed = clean_all(candidates, clean_jobs)

    total = sum(s for *_, s in candidates)
    if failed:
        console.print(Panel(
            f"[bold]cleaned[/] [green]{len(candidates) - failed}[/]/[bold]{len(candidates)}[/]  "
            f"([red]{failed} failed[/])  •  freed ~[bold green]{human_size(total)}[/]",
            border_style="yellow", box=ROUNDED,
        ))
    else:
        console.print(Panel(
            f"[bold green]✓ cleaned {len(candidates)} crate(s)[/]  •  "
            f"freed ~[bold green]{human_size(total)}[/]",
            border_style="green", box=ROUNDED,
        ))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted.[/]")
        raise SystemExit(130)
