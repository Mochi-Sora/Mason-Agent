#!/usr/bin/env sh
# ============================================================================
# Mason Agent Installer - Android / Termux
# ============================================================================
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Mochi-Sora/Mason-Agent/MasonAgent/scripts/install-termux.sh | sh
#
# Termux is a first-class target of the main installer (scripts/install.sh):
# it knows the Termux package deps, creates the venv with Termux's own Python,
# clones the repo, installs it editable, and walks the
# [termux-all] -> [termux] -> core extras ladder with constraints-termux.txt.
#
# So this entry point does NOT reimplement any of that. It bootstraps the few
# tools needed to *fetch and run* install.sh (pkg is the only package manager
# on Termux and needs no root), then hands over to it. Dependencies are
# installed before the framework on every platform, Termux included.
#
# Overrides: MASON_REPO_SLUG, MASON_BRANCH, MASON_DEFAULT_BRANCH,
# MASON_INSTALL_SH_URL, or any flag install.sh accepts, e.g.
#   curl -fsSL .../install-termux.sh | sh -s -- --no-venv --skip-setup
set -eu

REPO_SLUG="${MASON_REPO_SLUG:-Mochi-Sora/Mason-Agent}"

# Print the branch to fetch: MASON_BRANCH if the user pinned one, otherwise the
# repository's default branch, asked of the remote itself.
#
# Called after the bootstrap stage below, not at the top: git is only
# guaranteed to exist once pkg has installed it. Asking the remote is what
# keeps this script working across a rename of the default branch -- the old
# name keeps serving bytes from raw.githubusercontent while `git clone
# --branch <old-name>` fails with "Remote branch <old-name> not found".
# MASON_DEFAULT_BRANCH is the last resort when the remote cannot be asked.
resolve_branch() {
    if [ -n "${MASON_BRANCH:-}" ]; then
        printf '%s\n' "$MASON_BRANCH"
        return 0
    fi
    if [ -n "${MASON_DEFAULT_BRANCH:-}" ]; then
        printf '%s\n' "$MASON_DEFAULT_BRANCH"
        return 0
    fi
    resolved=""
    if command -v git >/dev/null 2>&1; then
        resolved=$(git ls-remote --symref "https://github.com/$REPO_SLUG.git" HEAD 2>/dev/null \
            | sed -n 's|^ref:[[:space:]]*refs/heads/\([^[:space:]]*\).*|\1|p' | head -n 1) \
            || resolved=""
    fi
    printf '%s\n' "${resolved:-MasonAgent}"
}

info() { printf '\033[0;34m->\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[!]\033[0m %s\n' "$*"; }

if ! command -v pkg >/dev/null 2>&1; then
    warn "pkg not found - this script is for Termux (Android)."
    warn "On Linux/macOS/WSL2 use: scripts/install.sh"
fi

# ---------------------------------------------------------------------------
# 1. Bootstrap the tools install.sh needs to run: bash (it is a bash script),
#    curl, git (the framework is installed from a clone), and TLS roots.
# ---------------------------------------------------------------------------
if command -v pkg >/dev/null 2>&1; then
    info "Installing Termux prerequisites (bash, curl, git, ca-certificates)..."
    pkg update -y >/dev/null 2>&1 || true
    pkg install -y bash curl git ca-certificates >/dev/null 2>&1 \
        || warn "pkg install failed - continuing with whatever is already installed"
fi

for tool in bash curl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        warn "$tool is required but was not found on PATH"
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# 2. Download the real installer and run it.
#    Downloaded to a file rather than piped into the shell: `curl | bash`
#    executes a partially written script if the connection drops, and the
#    reported error then points at a half-parsed line instead of the network.
# ---------------------------------------------------------------------------
if [ -n "${MASON_INSTALL_SH_URL:-}" ]; then
    INSTALL_SH_URL="$MASON_INSTALL_SH_URL"
else
    BRANCH=$(resolve_branch)
    INSTALL_SH_URL="https://raw.githubusercontent.com/$REPO_SLUG/$BRANCH/scripts/install.sh"
    info "Using repository default branch: $BRANCH"
    # Hand the same branch to install.sh so the script we fetch and the clone
    # it makes can never disagree. Placed before "$@", so an explicit
    # --branch passed by the user still wins (install.sh takes the last flag).
    set -- --branch "$BRANCH" "$@"
fi

tmp="$(mktemp 2>/dev/null || mktemp -t mason-install 2>/dev/null || echo /data/data/com.termux/files/usr/tmp/mason-install.sh)"
info "Fetching $INSTALL_SH_URL"
if ! curl -fsSL "$INSTALL_SH_URL" -o "$tmp"; then
    warn "Could not download install.sh - check the network or \$MASON_INSTALL_SH_URL"
    rm -f "$tmp" 2>/dev/null || true
    exit 1
fi

# CRLF -> LF: a transfer that mangled line endings would otherwise trip
# `set -e` on the first bash-ism in the script.
if command -v tr >/dev/null 2>&1; then
    tr -d '\r' < "$tmp" > "$tmp.lf" 2>/dev/null && mv "$tmp.lf" "$tmp" 2>/dev/null || true
fi
chmod +x "$tmp" 2>/dev/null || true
ok "Installer downloaded"

info "Installing dependencies first, then Mason..."
rc=0
bash "$tmp" "$@" || rc=$?
rm -f "$tmp" 2>/dev/null || true

if [ "$rc" -ne 0 ]; then
    warn "Mason install failed (exit $rc)"
    warn "Re-run with more detail: bash scripts/install.sh --skip-setup"
fi
exit "$rc"
