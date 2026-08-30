#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"
BASELINE="76d8ee1"
# Restore only files changed by the qualification-test integration task.
git restore --source "$BASELINE" -- \
  .env.example config/register.py config/webui.py \
  core/chatgpt_plan.py core/db.py core/plan_check_service.py core/sentinel_runner.py \
  tests/test_country_qualification_browser.py tests/test_plan_check_service_refresh.py \
  webui/app.py webui/config_editor.py webui/templates/index.html
git rm -f --ignore-unmatch core/qualification_test.py tests/test_qualification_test.py >/dev/null 2>&1 || true
printf 'rollback restored baseline %s for qualification-test integration files\n' "$BASELINE"
