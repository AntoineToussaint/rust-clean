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

```bash
git clone https://github.com/AntoineToussaint/rust-clean.git
cd rust-clean
./install.sh
```

The installer:
- checks for [`uv`](https://docs.astral.sh/uv/) (offers to install it via the official installer if missing)
- copies `rust-clean` into `~/.local/bin`
- verifies `~/.local/bin` is on your `PATH` (prints the right shell-rc hint if not)

**Requirements:** `uv` and `cargo`. Works on macOS and Linux.

### Install flags

| Flag | Effect |
|---|---|
| `--dev` | Symlink instead of copy. Edits to the source propagate. |
| `--yes` | Non-interactive: auto-install `uv` if missing. |

### One-liner

```bash
curl -fsSL https://raw.githubusercontent.com/AntoineToussaint/rust-clean/main/install.sh | bash
```

> The installer needs the script next to it, so the one-liner clones first then runs. If you'd rather do it by hand: download `clean_rust_targets.py`, `chmod +x`, drop it anywhere on `PATH` as `rust-clean`.

## Usage

```bash
rust-clean --dry-run      # show what would be cleaned (no changes)
rust-clean                # scan, list, prompt, clean
rust-clean --yes          # skip the confirmation prompt
rust-clean --days 7       # only clean target/ dirs idle for 7+ days
rust-clean --root ~/code  # scan a specific directory
```

By default `rust-clean` scans `~` and `~/.pilot`, skipping the usual irrelevant dirs (`Library`, `node_modules`, `.git`, `.cache`, `.cargo`, `.rustup`, etc.).

### Why "stale"?

A `target/` is considered stale if the most recent file in its top-level is older than `--days` (default 1). The check uses mtime, not rustc fingerprints — so `cargo build` resets the clock, and `touch target/` would too. If you want fingerprint-aware cleanup, look at [`cargo-sweep`](https://crates.io/crates/cargo-sweep).

## Configuration

`rust-clean` reads `~/.config/rust-clean/config.toml` (or `$XDG_CONFIG_HOME/rust-clean/config.toml`) if present. CLI flags always win.

Bootstrap a starter config:

```bash
rust-clean --init
```

That writes:

```toml
# Directories to scan for Cargo.toml files (supports ~ expansion).
roots = ["~", "~/.pilot"]

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

## All flags

| Flag | Default | Notes |
|---|---|---|
| `--root PATH` | `~`, `~/.pilot` | Repeatable. Overrides config. |
| `--days N` | `1.0` | Minimum age (days). |
| `--dry-run` | off | List candidates, don't clean. |
| `--yes` | off | Skip the y/N prompt. |
| `--size-jobs N` | `16` | Parallel workers for `du`-style sizing. |
| `--clean-jobs N` | `4` | Parallel `cargo clean` workers. Disk-bound — modest values are best. |
| `--config PATH` | `~/.config/rust-clean/config.toml` | Alternate config file. |
| `--init` | | Write a starter config and exit. |
| `--show-config` | | Print resolved config and exit. |

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
| config file | ✓ | | | |

Use `cargo-sweep` if you want maximum correctness. Use `rust-clean` if you want a pretty TUI and a config file.

## License

[MIT](./LICENSE)
