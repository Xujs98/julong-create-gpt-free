#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"
BASELINE="5db8a6bfcd15d8298b87a62df25dc50d0b4bffa0"
git restore --source "$BASELINE" -- core/db.py tests/test_domain_email_import.py
printf 'rollback restored domain summary compatibility files from %s\n' "$BASELINE"
