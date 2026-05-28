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
from collections import defaultdict
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
from rich.prompt import Confirm, FloatPrompt, Prompt
from rich.table import Table
from rich.text import Text

console = Console()

SKIP_DIRS = {
    # macOS
    ".Trash", "Library", "Pictures", "Music", "Movies",
    # Linux
    "snap", ".var",
    # Rust + build tooling
    ".cargo", ".rustup", ".cache", ".git", "node_modules",
    # Other package managers (large, never contain crates)
    ".npm", ".yarn", ".pnpm", ".gem", ".gradle", ".m2", ".bundle", ".cocoapods",
    # Editors / IDEs
    ".vscode", ".cursor", ".idea", ".zed", ".vim", ".emacs.d", ".claude",
    # Language version managers
    ".pyenv", ".rbenv", ".nvm", ".gvm", ".sdkman", ".asdf", ".volta",
    # Cloud / infra tooling
    ".docker", ".kube", ".aws", ".gcloud", ".azure", ".orbstack",
    # XDG config / privacy
    ".config", ".local", ".ssh", ".gnupg",
}

DEFAULTS: dict = {
    "roots": ["~", "~/.pilot"],
    "ignore": [],
    "days": 1.0,
    "size_jobs": 16,
    "clean_jobs": 4,
}


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "rust-clean" / "config.toml"


COMPLETION_BASH = r"""
_rust_clean() {
    local cur prev opts
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    opts="--root --days --hours --dry-run --yes --size-jobs --clean-jobs --config --init --show-config --completion --cache -h --help"
    case "$prev" in
        --root|--config) COMPREPLY=( $(compgen -f -- "$cur") ); return ;;
        --completion) COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") ); return ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
    fi
}
complete -F _rust_clean rust-clean
""".lstrip()

COMPLETION_ZSH = r"""
#compdef rust-clean
_rust_clean() {
    _arguments -s \
        '*--root[Where to search]:path:_files -/' \
        '--days[Min age in days]:days:' \
        '--hours[Min age in hours]:hours:' \
        '--dry-run[List candidates, do not clean]' \
        '--yes[Skip confirmation prompt]' \
        '--size-jobs[Parallel sizing workers]:N:' \
        '--clean-jobs[Parallel cargo-clean workers]:N:' \
        '--config[Config path]:file:_files' \
        '--init[Write a starter config and exit]' \
        '--show-config[Print resolved config and exit]' \
        '--completion[Print completion script]:shell:(bash zsh fish)' \
        '--cache[Clean ~/.cargo via cargo-cache]' \
        '(-h --help)'{-h,--help}'[Show help]'
}
_rust_clean "$@"
""".lstrip()

COMPLETION_FISH = r"""
complete -c rust-clean -l root -d "Where to search" -r -F
complete -c rust-clean -l days -d "Min age in days" -x
complete -c rust-clean -l hours -d "Min age in hours" -x
complete -c rust-clean -l dry-run -d "List candidates, do not clean"
complete -c rust-clean -l yes -d "Skip confirmation prompt"
complete -c rust-clean -l size-jobs -d "Parallel sizing workers" -x
complete -c rust-clean -l clean-jobs -d "Parallel cargo-clean workers" -x
complete -c rust-clean -l config -d "Config path" -r -F
complete -c rust-clean -l init -d "Write a starter config and exit"
complete -c rust-clean -l show-config -d "Print resolved config and exit"
complete -c rust-clean -l completion -d "Print completion script" -x -a "bash zsh fish"
complete -c rust-clean -l cache -d "Clean ~/.cargo via cargo-cache"
complete -c rust-clean -s h -l help -d "Show help"
""".lstrip()

COMPLETIONS = {"bash": COMPLETION_BASH, "zsh": COMPLETION_ZSH, "fish": COMPLETION_FISH}


def render_config(roots: list[str], days: float, ignore: list[str] | None = None) -> str:
    """Serialize a config dict as TOML with comments."""
    ignore = ignore or []
    return f"""\
# rust-clean config — https://github.com/AntoineToussaint/rust-clean
# CLI flags override anything set here.

# Directories to scan for Cargo.toml files (supports ~ expansion).
roots = {roots!r}

# Extra directory names to skip (added to the built-in skip list).
ignore = {ignore!r}

# Minimum age in days for a target/ dir to be considered stale.
days = {days}

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
    path.write_text(render_config(DEFAULTS["roots"], DEFAULTS["days"]))
    return True


def _common_ancestor(paths: list[Path]) -> Path:
    """Lowest common ancestor of a list of absolute paths. Empty list → root."""
    if not paths:
        return Path("/")
    if len(paths) == 1:
        # For a singleton, the LCA is the path itself — not very useful as a
        # scan root. Use its parent instead so the suggestion covers siblings.
        return paths[0].parent
    common: list[str] = []
    for parts in zip(*(p.parts for p in paths)):
        if len(set(parts)) == 1:
            common.append(parts[0])
        else:
            break
    return Path(*common) if common else Path("/")


def init_wizard(path: Path, scan_root: Path | None = None) -> bool:
    """Scan ~, cluster crates into smart parent roots, let user pick + ignore."""
    if path.exists():
        if not Confirm.ask(f"  [yellow]{path} exists. overwrite?[/]", default=False):
            return False

    home = (scan_root or Path.home()).expanduser().resolve()
    console.print(f"  [dim]scanning {home} for Rust crates (this may take a moment)…[/]")

    # First pass: collect all crates, grouped by their depth-1 dir under home.
    # Then for each group, compute the LCA → that's a tighter suggestion.
    # Example: 14 worktrees in ~/.pilot/v2/worktrees/* → suggests
    # ~/.pilot/v2/worktrees (not ~/.pilot, which would also touch caches etc.).
    groups: dict[str, list[Path]] = defaultdict(list)
    progress = Progress(
        SpinnerColumn(style="magenta"),
        TextColumn("[bold]found {task.fields[count]} crates"),
        TextColumn("[dim]{task.fields[detail]}"),
        console=console,
        transient=True,
    )
    with progress:
        task = progress.add_task("", total=None, count=0, detail="")
        for crate, _target in find_rust_targets(home):
            try:
                rel = crate.relative_to(home)
                top = rel.parts[0] if rel.parts else "."
            except ValueError:
                top = "(other)"
            groups[top].append(crate)
            progress.update(task, count=sum(len(v) for v in groups.values()),
                            detail=str(crate))

    total = sum(len(v) for v in groups.values())
    if total == 0:
        console.print("  [yellow]no Rust crates found.[/] writing defaults.\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_config(DEFAULTS["roots"], DEFAULTS["days"]))
        console.print(f"  [green]✓[/] wrote [bold]{path}[/]\n")
        return True

    console.print(f"  [green]✓[/] found [bold]{total}[/] crate(s) "
                  f"across [bold]{len(groups)}[/] cluster(s) under {home}\n")

    # Compute the suggested root for each cluster: LCA of its crates.
    # Then sort by crate count (densest first) so the user starts with the
    # biggest wins.
    home_str = str(home)
    home_label = "~" if home == Path.home() else home_str

    def displayable(p: Path) -> str:
        return str(p).replace(home_str, home_label, 1)

    suggestions: list[tuple[str, int, list[Path]]] = []
    for _top, crates in groups.items():
        root = _common_ancestor(crates)
        # Safety clamp: never suggest above home.
        try:
            root.relative_to(home)
        except ValueError:
            root = home
        suggestions.append((displayable(root), len(crates), crates))

    suggestions.sort(key=lambda x: -x[1])
    selected: list[str] = []

    console.print("  [bold]include as scan root?[/] [dim](smart-grouped by lowest common ancestor)[/]")
    for label, count, crates in suggestions:
        # Show 1-2 example paths so the user can sanity-check.
        sample = displayable(crates[0])
        more = f" [dim](+{count - 1} more)[/]" if count > 1 else ""
        console.print(f"    [dim]example:[/] {sample}{more}")
        if Confirm.ask(
            f"    [cyan]{label}[/] [dim]({count} crate(s))[/]",
            default=True,
        ):
            selected.append(label)

    if not selected:
        console.print("  [yellow]nothing selected; using defaults.[/]")
        selected = DEFAULTS["roots"]

    # Optional extra ignores
    extra = Prompt.ask(
        "\n  [bold]extra dir names to ignore[/] [dim](comma-separated, blank to skip)[/]",
        default="",
        show_default=False,
    ).strip()
    ignores = [s.strip() for s in extra.split(",") if s.strip()] if extra else []

    days = FloatPrompt.ask("  [bold]min age in days[/]", default=DEFAULTS["days"])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(selected, days, ignores))
    console.print(f"\n  [green]✓[/] wrote config to [bold]{path}[/]")
    console.print(f"  [dim]roots: {selected}[/]")
    if ignores:
        console.print(f"  [dim]ignore: {ignores}[/]")
    console.print(f"  [dim]edit it, or re-run [bold]rust-clean --init[/] any time.[/]\n")
    return True


def find_rust_targets(root: Path, extra_skip: set[str] | None = None):
    root = root.expanduser().resolve()
    skip = SKIP_DIRS | (extra_skip or set())
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Skip explicitly-listed names. We *don't* skip every dot-dir by name
        # since some (e.g. ~/.pilot) are legitimate project containers.
        dirnames[:] = [d for d in dirnames if d not in skip]
        if "Cargo.toml" in filenames:
            crate = Path(dirpath)
            target = crate / "target"
            if target.is_dir() and not target.is_symlink():
                yield crate, target
            dirnames[:] = []


# Subdirs to skip when measuring "last source edit" — anything cargo or git
# touches mechanically would otherwise mask real activity.
_SRC_MTIME_SKIP = {"target", ".git", "node_modules", ".direnv"}


def crate_source_mtime(crate: Path) -> float:
    """Latest mtime across the crate's source tree, ignoring target/ & .git/.

    Proxy for "when was this crate last worked on". target/ mtime is unusable:
    cargo rewrites target/.rustc_info.json on every invocation, so even a
    single `cargo check` from a daemon keeps the dir looking fresh forever.
    """
    latest = 0.0
    try:
        latest = crate.stat().st_mtime
    except OSError:
        pass
    for dirpath, dirnames, filenames in os.walk(crate, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SRC_MTIME_SKIP]
        for f in filenames:
            try:
                m = os.path.getmtime(os.path.join(dirpath, f))
                if m > latest:
                    latest = m
            except OSError:
                continue
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


def scan(roots: list[str], cutoff: float, workers: int,
         extra_skip: set[str] | None = None):
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
            for crate, target in find_rust_targets(root, extra_skip):
                progress.update(task, detail=str(crate))
                if target in seen:
                    continue
                seen.add(target)
                mtime = crate_source_mtime(crate)
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


def render_table(candidates, age_hours: float, unit: str) -> Table:
    threshold = age_hours if unit == "h" else age_hours / 24
    table = Table(
        title=f"[bold]🧹  stale target/ dirs (>{threshold:g}{unit} old)[/]",
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
        age_d = (now - mtime) / 86400
        age_str = f"{age_d * 24:.1f}h" if unit == "h" else f"{age_d:.1f}d"
        table.add_row(
            str(i),
            Text(human_size(size), style=size_color(size)),
            Text(age_str, style=age_color(age_d)),
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


def clean_cache(yes: bool, dry_run: bool) -> int:
    """Wrap `cargo cache --autoclean` to sweep ~/.cargo registry/git caches."""
    cargo_dir = Path("~/.cargo").expanduser()
    if not cargo_dir.exists():
        console.print(f"  [yellow]{cargo_dir} doesn't exist; nothing to do.[/]")
        return 0

    if not shutil.which("cargo"):
        console.print("[red bold]error:[/] `cargo` not found in PATH")
        return 1

    # Ensure cargo-cache is available.
    if not shutil.which("cargo-cache"):
        console.print("  [yellow]cargo-cache[/] [dim]is not installed.[/]")
        if not yes and not Confirm.ask(
            "  install it via [bold]cargo install cargo-cache[/]?", default=True
        ):
            console.print("  [dim]install yourself with:[/] [bold]cargo install cargo-cache[/]")
            return 1
        console.print("  [dim]installing cargo-cache (this can take a minute)…[/]")
        r = subprocess.run(["cargo", "install", "cargo-cache"])
        if r.returncode != 0 or not shutil.which("cargo-cache"):
            console.print("[red]cargo install failed.[/]")
            return r.returncode or 1

    console.print(f"  [dim]sizing[/] {cargo_dir}…")
    before = dir_size(cargo_dir)
    console.print(f"  [bold]{cargo_dir}[/] currently uses [bold]{human_size(before)}[/]\n")

    if dry_run:
        console.print("  [dim]dry-run: showing what cargo-cache would do[/]\n")
        subprocess.run(["cargo", "cache", "--dry-run", "--autoclean"])
        return 0

    if not yes and not Confirm.ask(
        "  [bold]proceed with cargo cache --autoclean?[/] [dim](removes registry sources, "
        "downloaded .crate files, and git checkouts — all re-fetchable)[/]",
        default=False,
    ):
        console.print("[yellow]aborted.[/]")
        return 0

    console.print()
    r = subprocess.run(["cargo", "cache", "--autoclean"])
    if r.returncode != 0:
        console.print(f"[red]cargo cache failed (exit {r.returncode})[/]")
        return r.returncode

    after = dir_size(cargo_dir)
    reclaimed = max(0, before - after)
    console.print(Panel(
        f"[bold green]✓ freed ~{human_size(reclaimed)}[/]  "
        f"[dim]({human_size(before)} → {human_size(after)})[/]",
        border_style="green", box=ROUNDED,
    ))
    return 0


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
    age = p.add_mutually_exclusive_group()
    age.add_argument("--days", type=float, default=None, help="Min age in days")
    age.add_argument("--hours", type=float, default=None,
                     help="Min age in hours (useful for debugging)")
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
    p.add_argument("--completion", choices=["bash", "zsh", "fish"],
                   help="Print a shell completion script and exit")
    p.add_argument("--cache", action="store_true",
                   help="Clean ~/.cargo caches via cargo-cache (instead of target/ dirs)")
    args = p.parse_args()

    if args.completion:
        sys.stdout.write(COMPLETIONS[args.completion])
        return 0

    console.print(banner())

    if args.cache:
        return clean_cache(args.yes, args.dry_run)

    cfg_path = args.config or config_path()

    if args.init:
        # --yes or non-TTY → write static defaults (scriptable).
        # Interactive → run the wizard (scan + pick roots + ignores + days).
        if args.yes or not sys.stdin.isatty():
            if write_default_config(cfg_path, force=args.yes):
                console.print(f"  [green]✓[/] wrote starter config to [bold]{cfg_path}[/]\n")
            else:
                console.print("[yellow]aborted.[/]")
            return 0
        if init_wizard(cfg_path):
            return 0
        console.print("[yellow]aborted.[/]")
        return 0

    cfg = {**DEFAULTS, **load_config(cfg_path)}
    roots = args.root or cfg["roots"]
    extra_skip = set(cfg.get("ignore") or [])
    size_jobs = args.size_jobs if args.size_jobs is not None else cfg["size_jobs"]
    clean_jobs = args.clean_jobs if args.clean_jobs is not None else cfg["clean_jobs"]

    # Age: --hours wins over --days; both fall back to config days. Track
    # the unit the user specified so the UI can match it.
    if args.hours is not None:
        age_hours, age_unit = args.hours, "h"
    elif args.days is not None:
        age_hours, age_unit = args.days * 24, "d"
    else:
        age_hours, age_unit = cfg["days"] * 24, "d"

    if args.show_config:
        src = "config file" if cfg_path.exists() else "built-in defaults"
        console.print(f"  [dim]source:[/] {src} [dim]({cfg_path})[/]")
        console.print(f"  [bold]roots[/]      = {roots}")
        console.print(f"  [bold]ignore[/]     = {sorted(extra_skip)}")
        console.print(f"  [bold]days[/]       = {age_hours / 24}")
        console.print(f"  [bold]size_jobs[/]  = {size_jobs}")
        console.print(f"  [bold]clean_jobs[/] = {clean_jobs}\n")
        return 0

    if not shutil.which("cargo"):
        console.print("[red bold]error:[/] `cargo` not found in PATH")
        return 1

    cutoff = time.time() - age_hours * 3600
    candidates = scan(roots, cutoff, size_jobs, extra_skip)

    if not candidates:
        console.print("\n[bold green]✨ nothing to clean — you're tidy![/]\n")
        return 0

    console.print()
    console.print(render_table(candidates, age_hours, age_unit))

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
