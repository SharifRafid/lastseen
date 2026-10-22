#!/bin/sh
# lastseen installer for macOS and Linux.
#   curl -LsSf https://raw.githubusercontent.com/sharifrafid/lastseen/main/install.sh | sh
# Re-run it any time to upgrade. Set LASTSEEN_REF to install a branch or tag other than main.
set -eu

REF="${LASTSEEN_REF:-main}"
SOURCE="https://github.com/sharifrafid/lastseen/archive/refs/heads/${REF}.zip"
case "$REF" in v[0-9]*) SOURCE="https://github.com/sharifrafid/lastseen/archive/refs/tags/${REF}.zip" ;; esac
PORT="${LASTSEEN_PORT:-8765}"

say() { printf '%s\n' "$*"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

say "This will install lastseen, a private location history for your Apple devices."
say "  1. Install uv (a small Python tool manager) if you don't have it"
say "  2. Install lastseen with its own Python, in your user account (no admin needed)"
say "  3. Start lastseen so you can finish setup in your browser"
say ""

if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        fail "curl or wget is needed to download uv."
    fi
    if [ -f "$HOME/.local/bin/env" ]; then
        # shellcheck disable=SC1091
        . "$HOME/.local/bin/env"
    fi
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
    command -v uv >/dev/null 2>&1 || fail "uv was installed but can't be found. Open a new terminal and run this installer again."
fi

# Upgrading: stop the running lastseen first so the new version takes over.
if command -v lastseen >/dev/null 2>&1; then
    say "Stopping the running lastseen..."
    lastseen stop >/dev/null 2>&1 || true
fi

say "Installing lastseen..."
uv tool install --force --python 3.12 "$SOURCE"
uv tool update-shell >/dev/null 2>&1 || true

BIN_DIR="$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")"
LASTSEEN="$BIN_DIR/lastseen"
[ -x "$LASTSEEN" ] || LASTSEEN="lastseen"

case "$(uname -s)" in
    Darwin) DATA_DIR="$HOME/Library/Application Support/lastseen" ;;
    *) DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/lastseen" ;;
esac
DATA_DIR="${LASTSEEN_DATA_DIR:-$DATA_DIR}"

LOG="${TMPDIR:-/tmp}/lastseen-start.log"

wait_running() {
    i=0
    while [ "$i" -lt 20 ]; do
        "$LASTSEEN" status 2>/dev/null | grep -q "^Running: *yes" && return 0
        sleep 1
        i=$((i + 1))
    done
    return 1
}

say ""
say "lastseen is installed."
if "$LASTSEEN" service status >/dev/null 2>&1; then
    # Autostart is on: restart the background service with the new version.
    "$LASTSEEN" service install >"$LOG" 2>&1 || true
    if wait_running; then
        say "lastseen is running in the background: http://127.0.0.1:${PORT}"
    else
        say "lastseen didn't start. Details:"
        cat "$LOG"
    fi
elif [ "$(uname -s)" = "Darwin" ] || [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    nohup "$LASTSEEN" >"$LOG" 2>&1 &
    if wait_running; then
        say "Finish setup in your browser: http://127.0.0.1:${PORT}"
        say "(It should open by itself. Later, just run: lastseen)"
    else
        say "lastseen didn't start. Details:"
        cat "$LOG"
        say "Fix the problem above, then run: lastseen"
    fi
else
    say "This machine has no screen, so pick one way to open the setup page:"
    say "  - From another computer on your network:"
    say "      lastseen --host 0.0.0.0"
    say "    then open the \"?setup=...\" link it prints."
    say "  - Over SSH from your own computer (nothing exposed to the network):"
    say "      ssh -L ${PORT}:localhost:${PORT} <this-server>   then run: lastseen"
    say "    and open http://127.0.0.1:${PORT} on your computer."
    say "  - Or use Docker: see https://github.com/sharifrafid/lastseen#server--docker"
fi
say ""
say "Open a new terminal if the 'lastseen' command isn't found yet."
say "To stop it:     lastseen stop"
say "To uninstall:   lastseen service uninstall; lastseen stop; uv tool uninstall lastseen"
say "Your history and settings live in: $DATA_DIR"
