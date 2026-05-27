#!/usr/bin/env bash
# Installs `rust-clean` to ~/.local/bin. Works on macOS and Linux.
# Usage: ./install.sh [--dev] [--yes]
#   --dev   symlink instead of copy (edits to source propagate)
#   --yes   non-interactive: auto-install uv if missing

set -euo pipefail

DEV=0
YES=0
for arg in "$@"; do
    case "$arg" in
        --dev) DEV=1 ;;
        --yes|-y) YES=1 ;;
        -h|--help)
            sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# Colors (TTY only)
if [ -t 1 ]; then
    B=$'\033[1m'; D=$'\033[2m'; R=$'\033[0m'
    GRN=$'\033[32m'; YEL=$'\033[33m'; RED=$'\033[31m'; MAG=$'\033[35m'
else
    B=""; D=""; R=""; GRN=""; YEL=""; RED=""; MAG=""
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf '  %s!%s %s\n' "$YEL" "$R" "$*"; }
err()  { printf '  %s✗%s %s\n' "$RED" "$R" "$*" >&2; }

say ""
say "${MAG}${B}🦀  rust-clean installer${R}"
say "${D}    cross-platform installer for the rust target cleaner${R}"
say ""

# Locate source script: next to this installer (git clone) or fetch from GitHub (curl|bash)
REMOTE_URL="https://raw.githubusercontent.com/AntoineToussaint/rust-clean/main/clean_rust_targets.py"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
LOCAL_SRC="$HERE/clean_rust_targets.py"
TMP_SRC=""

if [ -n "$HERE" ] && [ -f "$LOCAL_SRC" ]; then
    SRC="$LOCAL_SRC"
    ok "using local source: $SRC"
else
    if [ "$DEV" -eq 1 ]; then
        err "--dev requires running install.sh from the cloned repo"
        exit 1
    fi
    TMP_SRC="$(mktemp -t rust-clean.XXXXXX.py)"
    trap 'rm -f "$TMP_SRC"' EXIT
    if ! curl -fsSL "$REMOTE_URL" -o "$TMP_SRC"; then
        err "failed to fetch script from $REMOTE_URL"
        exit 1
    fi
    SRC="$TMP_SRC"
    ok "fetched source: $REMOTE_URL"
fi

# Detect uv
if ! command -v uv >/dev/null 2>&1; then
    warn "uv not found"
    if [ "$YES" -eq 1 ]; then
        REPLY="y"
    else
        printf "  install uv via the official installer? [Y/n] "
        read -r REPLY < /dev/tty || REPLY="n"
    fi
    case "${REPLY:-y}" in
        y|Y|yes|"")
            curl -LsSf https://astral.sh/uv/install.sh | sh
            # Pick up uv on PATH for the rest of this script
            export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
            command -v uv >/dev/null || { err "uv install failed"; exit 1; }
            ok "uv installed"
            ;;
        *)
            err "uv is required. install from https://docs.astral.sh/uv/ and re-run."
            exit 1 ;;
    esac
else
    ok "uv found: $(command -v uv)"
fi

# Detect cargo (warn only)
if command -v cargo >/dev/null 2>&1; then
    ok "cargo found: $(command -v cargo)"
else
    warn "cargo not found — install Rust from https://rustup.rs to actually clean"
fi

# Install destination
DEST_DIR="$HOME/.local/bin"
DEST="$DEST_DIR/rust-clean"
mkdir -p "$DEST_DIR"

# Remove old entry if present
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
    rm -f "$DEST"
fi

if [ "$DEV" -eq 1 ]; then
    ln -s "$SRC" "$DEST"
    ok "symlinked $DEST → $SRC"
else
    cp "$SRC" "$DEST"
    chmod +x "$DEST"
    ok "installed $DEST"
fi

# PATH check
case ":$PATH:" in
    *":$DEST_DIR:"*)
        ok "$DEST_DIR is on PATH"
        ;;
    *)
        warn "$DEST_DIR is not on PATH"
        case "${SHELL:-}" in
            */zsh)  RC="~/.zshrc" ;;
            */bash) RC="~/.bashrc (or ~/.bash_profile on macOS)" ;;
            */fish) RC="~/.config/fish/config.fish" ;;
            *)      RC="your shell rc file" ;;
        esac
        say ""
        say "  add this to $RC:"
        say "    ${B}export PATH=\"\$HOME/.local/bin:\$PATH\"${R}"
        ;;
esac

say ""
say "${GRN}${B}✓ installed${R}  — try:"
say "    ${B}rust-clean --dry-run${R}"
say ""
