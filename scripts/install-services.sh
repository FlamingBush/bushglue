#!/usr/bin/env bash
# install-services.sh — install Bush Glue systemd service files
#
# Usage:
#   ./scripts/install-services.sh [OPTIONS]
#
# Options:
#   --user USERNAME        User= in service files (default: current user)
#   --repos-dir PATH       Base repos directory (default: $HOME/repos)
#   --target odroid|wsl2   Service variant to install (default: auto-detect)
#   --enable               Enable and start each service after install
#   --dry-run              Print what would happen; make no changes
#
# Example:
#   sudo ./scripts/install-services.sh --user odroid --target odroid --enable

set -euo pipefail

# ── defaults ──────────────────────────────────────────────────────────────────
USERNAME="$(whoami)"
REPOS_DIR="$HOME/repos"
TARGET=""
ENABLE=false
DRY_RUN=false

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUSHGLUE_DIR="$(dirname "$SCRIPT_DIR")"

# ── arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            USERNAME="$2"; shift 2 ;;
        --repos-dir)
            REPOS_DIR="$2"; shift 2 ;;
        --target)
            TARGET="$2"
            if [[ "$TARGET" != "odroid" && "$TARGET" != "wsl2" ]]; then
                echo "ERROR: --target must be 'odroid' or 'wsl2'" >&2
                exit 1
            fi
            shift 2 ;;
        --enable)
            ENABLE=true; shift ;;
        --dry-run)
            DRY_RUN=true; shift ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0 ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            exit 1 ;;
    esac
done

# ── auto-detect target ────────────────────────────────────────────────────────
if [[ -z "$TARGET" ]]; then
    if grep -qi "microsoft" /proc/version 2>/dev/null; then
        TARGET="wsl2"
    else
        TARGET="odroid"
    fi
    echo "INFO: Auto-detected target: $TARGET"
fi

# ── choose service directory ──────────────────────────────────────────────────
if [[ "$TARGET" == "odroid" ]]; then
    SERVICE_SRC_DIR="$BUSHGLUE_DIR/systemd/odroid"
else
    SERVICE_SRC_DIR="$BUSHGLUE_DIR/systemd"
fi

if [[ ! -d "$SERVICE_SRC_DIR" ]]; then
    echo "ERROR: Service source directory not found: $SERVICE_SRC_DIR" >&2
    exit 1
fi

# Collect only the service files at this level (not subdirs)
mapfile -t SERVICE_FILES < <(find "$SERVICE_SRC_DIR" -maxdepth 1 -name "*.service" | sort)

if [[ ${#SERVICE_FILES[@]} -eq 0 ]]; then
    echo "ERROR: No .service files found in $SERVICE_SRC_DIR" >&2
    exit 1
fi

# ── compute repos subdir relative to home ────────────────────────────────────
# Default repos dir is $HOME/repos → %h/repos already in service files.
# If caller overrides --repos-dir, we compute the subdir portion.
USER_HOME="$(eval echo "~$USERNAME")"
REPOS_SUBDIR="$(realpath --relative-to="$USER_HOME" "$REPOS_DIR" 2>/dev/null || echo "")"
CUSTOM_REPOS=false
if [[ "$REPOS_SUBDIR" != "repos" && -n "$REPOS_SUBDIR" ]]; then
    CUSTOM_REPOS=true
fi

DEST_DIR="/etc/systemd/system"

# ── summary header ────────────────────────────────────────────────────────────
echo "========================================"
echo " Bush Glue service installer"
echo "========================================"
echo "  Target:    $TARGET"
echo "  User:      $USERNAME"
echo "  Repos dir: $REPOS_DIR  (subdir: ${REPOS_SUBDIR:-repos})"
echo "  Source:    $SERVICE_SRC_DIR"
echo "  Dest:      $DEST_DIR"
echo "  Enable:    $ENABLE"
echo "  Dry-run:   $DRY_RUN"
echo "----------------------------------------"

INSTALLED=()
FAILED=()

for SRC in "${SERVICE_FILES[@]}"; do
    SERVICE_NAME="$(basename "$SRC")"
    DEST="$DEST_DIR/$SERVICE_NAME"

    if [[ ! -f "$SRC" ]]; then
        echo "  SKIP (not a file): $SRC"
        continue
    fi

    echo "  Processing: $SERVICE_NAME"

    # Build the modified content in a temp file
    TMPFILE="$(mktemp /tmp/bushglue-service-XXXXXX.service)"
    trap 'rm -f "$TMPFILE"' EXIT

    cp "$SRC" "$TMPFILE"

    # Substitute User= line
    sed -i "s|^User=.*|User=$USERNAME|" "$TMPFILE"

    # If custom repos dir, replace %h/repos/ with %h/<subdir>/
    if $CUSTOM_REPOS; then
        sed -i "s|%h/repos/|%h/$REPOS_SUBDIR/|g" "$TMPFILE"
    fi

    if $DRY_RUN; then
        echo "    [dry-run] would copy to: $DEST"
        echo "    [dry-run] diff vs source:"
        diff "$SRC" "$TMPFILE" | sed 's/^/      /' || true
        INSTALLED+=("$SERVICE_NAME")
        rm -f "$TMPFILE"
        trap - EXIT
        continue
    fi

    # Copy to /etc/systemd/system/
    if ! sudo cp "$TMPFILE" "$DEST"; then
        echo "  ERROR: failed to copy $SERVICE_NAME to $DEST" >&2
        FAILED+=("$SERVICE_NAME")
        rm -f "$TMPFILE"
        trap - EXIT
        continue
    fi
    sudo chmod 644 "$DEST"
    echo "    Installed: $DEST"
    INSTALLED+=("$SERVICE_NAME")
    rm -f "$TMPFILE"
    trap - EXIT
done

# ── daemon-reload ─────────────────────────────────────────────────────────────
if ! $DRY_RUN && [[ ${#INSTALLED[@]} -gt 0 ]]; then
    echo "  Running: sudo systemctl daemon-reload"
    if ! sudo systemctl daemon-reload; then
        echo "  WARNING: systemctl daemon-reload failed" >&2
    fi
fi

# ── enable + start ────────────────────────────────────────────────────────────
if $ENABLE && ! $DRY_RUN; then
    for SERVICE_NAME in "${INSTALLED[@]}"; do
        echo "  Enabling: $SERVICE_NAME"
        if ! sudo systemctl enable --now "$SERVICE_NAME"; then
            echo "  WARNING: failed to enable $SERVICE_NAME" >&2
        fi
    done
elif $ENABLE && $DRY_RUN; then
    for SERVICE_NAME in "${INSTALLED[@]}"; do
        echo "  [dry-run] would run: sudo systemctl enable --now $SERVICE_NAME"
    done
fi

# ── summary ───────────────────────────────────────────────────────────────────
echo "========================================"
echo " Summary"
echo "========================================"
echo "  Installed (${#INSTALLED[@]}):"
for s in "${INSTALLED[@]}"; do
    echo "    - $s"
done
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "  FAILED (${#FAILED[@]}):"
    for s in "${FAILED[@]}"; do
        echo "    - $s"
    done
    exit 1
fi
echo "  Done."
