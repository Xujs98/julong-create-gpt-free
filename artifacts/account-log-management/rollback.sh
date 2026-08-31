#!/usr/bin/env bash
set -euo pipefail
TARGET_COMMIT="${1:-389a39b437ea6ce2d06aa880ba7e4c460f2a33e4}"
MODE="${ROLLBACK_MODE:-apply}"
if [[ "$MODE" == "dry-run" ]]; then
  git revert --no-commit "$TARGET_COMMIT"
else
  git revert --no-edit "$TARGET_COMMIT"
fi
