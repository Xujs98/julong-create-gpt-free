#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"
BASELINE="fe0850a45df32047ef7f55804de07aff3530a0fe"
git restore --source "$BASELINE" -- \
  core/chatgpt_plan.py core/qualification_test.py \
  tests/test_chatgpt_plan_context.py tests/test_qualification_test.py
printf 'rollback restored qualification country-match files from %s\n' "$BASELINE"
