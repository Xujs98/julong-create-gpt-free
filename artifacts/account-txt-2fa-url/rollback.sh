#!/usr/bin/env bash
set -euo pipefail
TARGET_COMMIT="${1:-36484ecc7b9819690fe33915cf179fb4bf6e07b3}"
MODE="${ROLLBACK_MODE:-apply}"
if [[ "$MODE" == "dry-run" ]]; then
  git revert --no-commit "$TARGET_COMMIT"
else
  git revert --no-edit "$TARGET_COMMIT"
fi
