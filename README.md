# rust-clean

> Find every Rust `target/` directory on your machine and `cargo clean` the stale ones.

A single-file Python script with a friendly TUI. Useful when your laptop is wheezing because you have 37 GB of `target/` directories you forgot about.

```
$ rust-clean --dry-run

╭──────────────────────────────────────────────────────────────────────────────╮
│  🦀  rust target cleaner                                                     │
│                                                                              │
│  find & sweep stale cargo build dirs                                         │
╰──────────────────────────────────────────────────────────────────────────────╯

╭─ 🧹  stale target/ dirs (>1.0d old) ─────────────────────────────────────────╮
│   #     size     age   crate                                                 │
│ ──────────────────────────────────────────────────────────────────────────── │
│   1   37.1 GB    5.2d  /Users/antoine/Development/nanogateway/crates         │
│   2   848.1 MB  44.0d  /Users/antoine/Development/durable                    │
│   3   667.0 MB   5.2d  /Users/antoine/Development/tensorzero/crates          │
│ ──────────────────────────────────────────────────────────────────────────── │
│       38.5 GB           3 crate(s) — reclaimable                             │
╰──────────────────────────────────────────────────────────────────────────────╯
```

## Install

**One-liner** (macOS + Linux):

```bash
curl -fsSL https://raw.githubusercontent.com/AntoineToussaint/rust-clean/main/install.sh | bash
```

**Or clone:**

```bash
git clone https://github.com/AntoineToussaint/rust-clean.git
cd rust-clean
./install.sh
```

The installer:
- checks for [`uv`](https://docs.astral.sh/uv/) (offers to install it via the official installer if missing)
- copies `rust-clean` into `~/.local/bin`
- installs shell completions for your `$SHELL` (zsh / bash / fish) — opt out with `--no-completions`
- verifies `~/.local/bin` is on your `PATH` (prints the right shell-rc hint if not)

**Requirements:** `uv` and `cargo`. Works on macOS and Linux.

### Install flags

| Flag | Effect |
|---|---|
| `--dev` | Symlink to the local checkout instead of copying. Edits propagate. (Requires clone.) |
| `--yes` | Non-interactive: auto-install `uv` if missing. Pipeable: `curl ... \| bash -s -- --yes`. |
| `--no-completions` | Skip installing shell completions. |

## Usage

```bash
rust-clean --dry-run      # show what would be cleaned (no changes)
rust-clean                # scan, list, prompt, clean
rust-clean --yes          # skip the confirmation prompt
rust-clean --days 7       # only clean target/ dirs idle for 7+ days
rust-clean --hours 2      # debugging: anything not built in the last 2 hours
rust-clean --root ~/code  # scan a specific directory
```

By default `rust-clean` scans `~` and `~/.pilot`, skipping the usual irrelevant dirs (`Library`, `node_modules`, `.git`, `.cache`, `.cargo`, `.rustup`, etc.). Each root is walked recursively, so **git worktrees, monorepos, and nested crates are all picked up automatically** — every `Cargo.toml` with a sibling `target/` shows up as its own row.

### Why "stale"?

A `target/` is considered stale if the most recent file in its top-level is older than `--days` (default 1). The check uses mtime, not rustc fingerprints — so `cargo build` resets the clock, and `touch target/` would too. If you want fingerprint-aware cleanup, look at [`cargo-sweep`](https://crates.io/crates/cargo-sweep).

## Configuration

`rust-clean` reads `~/.config/rust-clean/config.toml` (or `$XDG_CONFIG_HOME/rust-clean/config.toml`) if present. CLI flags always win.

### Interactive setup (recommended)

```bash
rust-clean --init
```

Scans your home directory, groups the discovered crates by top-level dir, and walks you through picking which to include as scan roots, plus any extra dir names to ignore. Example:

```
✓ found 47 crate(s) across 3 top-level dir(s) under /Users/antoine

include as scan root?
  ~/Development (28 crate(s)) [Y/n]
  ~/.pilot      (14 crate(s)) [Y/n]
  ~/code         (5 crate(s)) [Y/n]

extra dir names to ignore (comma-separated, blank to skip):
> archive,vendored

min age in days [1.0]: 3
✓ wrote config to /Users/antoine/.config/rust-clean/config.toml
```

For scripted environments, `rust-clean --init --yes` skips the wizard and writes a static-default config.

### Config file format

```toml
# Directories to scan for Cargo.toml files (supports ~ expansion).
roots = ["~/Development", "~/.pilot"]

# Extra dir names to skip (added to the built-in skip list).
ignore = ["archive", "vendored"]

# Minimum age in days for a target/ dir to be considered stale.
days = 1.0

# Parallel workers when computing target/ sizes (I/O bound).
size_jobs = 16

# Parallel `cargo clean` workers (disk bound; keep modest).
clean_jobs = 4
```

Check what `rust-clean` resolved (file + flags merged):

```bash
rust-clean --show-config
```

## Shell completions

The installer auto-installs completions for your `$SHELL`. To install manually, or re-install:

```bash
# zsh
rust-clean --completion zsh > ~/.zsh/completions/_rust-clean
# then in ~/.zshrc: fpath=(~/.zsh/completions $fpath); autoload -Uz compinit && compinit

# bash
rust-clean --completion bash > ~/.local/share/rust-clean/completion.bash
# then in ~/.bashrc (or ~/.bash_profile on macOS): source ~/.local/share/rust-clean/completion.bash

# fish (auto-loaded from this path)
rust-clean --completion fish > ~/.config/fish/completions/rust-clean.fish
```

## All flags

| Flag | Default | Notes |
|---|---|---|
| `--root PATH` | `~`, `~/.pilot` | Repeatable. Overrides config. |
| `--days N` | `1.0` | Minimum age in days. |
| `--hours N` | | Minimum age in hours (mutually exclusive with `--days`). |
| `--dry-run` | off | List candidates, don't clean. |
| `--yes` | off | Skip the y/N prompt. |
| `--size-jobs N` | `16` | Parallel workers for `du`-style sizing. |
| `--clean-jobs N` | `4` | Parallel `cargo clean` workers. Disk-bound — modest values are best. |
| `--config PATH` | `~/.config/rust-clean/config.toml` | Alternate config file. |
| `--init` | | Interactive wizard to bootstrap a config (or `--init --yes` for static defaults). |
| `--show-config` | | Print resolved config and exit. |
| `--completion {bash,zsh,fish}` | | Print a shell completion script to stdout and exit. |

## How it works

1. Walks each root with `os.walk`, skipping irrelevant dirs.
2. For every `Cargo.toml` with a sibling `target/`, records it (and stops descending — workspaces share one `target/`).
3. Filters by `target/`'s most-recent mtime.
4. Sizes survivors in parallel (16 workers).
5. Shows you the table.
6. On confirm, runs `cargo clean` in each crate dir in parallel (4 workers).

## Comparison

| | rust-clean | [cargo-sweep](https://crates.io/crates/cargo-sweep) | [cargo-clean-all](https://crates.io/crates/cargo-clean-all) | [kondo](https://github.com/tbillington/kondo) |
|---|---|---|---|---|
| age-based cleanup | ✓ | ✓ | ✓ | manual |
| rustc-fingerprint aware | | ✓ | | |
| Rust-specific | ✓ | ✓ | ✓ | no (multi-language) |
| pretty preview table | ✓ | | | TUI |
| parallel sizing/cleaning | ✓ | | | |
| config file + interactive setup | ✓ | | | |
| shell completions | ✓ | | | |

Use `cargo-sweep` if you want maximum correctness. Use `rust-clean` if you want a pretty TUI and a config file.

## License

[MIT](./LICENSE)
