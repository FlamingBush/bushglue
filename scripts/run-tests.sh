#!/usr/bin/env bash
# run-tests.sh — run all checks across the FlamingBush repos.
# Run from FlamingTree/FlamingTree/ or pass the root as $1.
#
# Usage:
#   ./scripts/run-tests.sh          # from repo root
#   bash scripts/run-tests.sh       # explicit shell
#
# Exit code: 0 = all pass, 1 = any failure.

set -euo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
PASS=0
FAIL=0
RESULTS=()

run() {
    local label="$1"; shift
    local dir="$1";  shift
    echo ""
    echo "━━━ $label ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    if (cd "$dir" && "$@"); then
        RESULTS+=("  ✓  $label")
        PASS=$((PASS + 1))
    else
        RESULTS+=("  ✗  $label")
        FAIL=$((FAIL + 1))
    fi
}

# ── bushglue ────────────────────────────────────────────────────────────────
BUSHGLUE="$ROOT/bushglue"
run "bushglue  lint"      "$BUSHGLUE" ruff check .
run "bushglue  typecheck" "$BUSHGLUE" mypy . --ignore-missing-imports
run "bushglue  tests"     "$BUSHGLUE" python -m pytest tests/ -q

# ── t2v ─────────────────────────────────────────────────────────────────────
T2V="$ROOT/t2v"
run "t2v  cargo check"  "$T2V" cargo check --quiet
run "t2v  cargo test"   "$T2V" cargo test --quiet

# ── bbsentimentqq ───────────────────────────────────────────────────────────
BB="$ROOT/bbsentimentqq"
run "bbsentimentqq  lint"      "$BB" ruff check .
run "bbsentimentqq  typecheck" "$BB" mypy . --ignore-missing-imports

# ── speech-to-text ───────────────────────────────────────────────────────────
STT="$ROOT/speech-to-text"
run "speech-to-text  lint"      "$STT" ruff check .
run "speech-to-text  typecheck" "$STT" mypy . --ignore-missing-imports

# ── AIAm-code ────────────────────────────────────────────────────────────────
AIAM="$ROOT/AIAm-code"
run "AIAm-code  lint"      "$AIAM" ruff check .
run "AIAm-code  typecheck" "$AIAM" mypy . --ignore-missing-imports

# ── summary ──────────────────────────────────────────────────────────────────
echo ""
echo "━━━ Results ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
for r in "${RESULTS[@]}"; do echo "$r"; done
echo ""
echo "  $PASS passed  /  $FAIL failed"
echo ""

[ "$FAIL" -eq 0 ]
