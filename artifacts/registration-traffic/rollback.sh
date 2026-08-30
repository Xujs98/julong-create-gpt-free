#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"
BASELINE="99b7637"
git restore --source "$BASELINE" -- \
  core/account_export.py core/browser_use_registration.py core/cloakbrowser_registration.py \
  core/db.py core/roxy_registration.py core/session.py main.py \
  tests/test_account_created_at_ui.py webui/app.py webui/templates/index.html
git rm -f --ignore-unmatch core/traffic.py tests/test_traffic.py >/dev/null 2>&1 || true
printf 'rollback restored baseline %s for registration traffic files\n' "$BASELINE"
