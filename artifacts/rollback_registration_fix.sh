#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BASE="09c60c816a5e6b8a15682d0272c8740feb10d8a1"
cd "$ROOT"
# Restore only source/UI/test files changed by this fix; runtime data, .env and logs stay untouched.
git restore --source="$BASE" -- \
  config/cloakbrowser.py config/register.py \
  core/cloakbrowser_driver.py core/cloakbrowser_registration.py \
  core/registration_service.py core/roxy_registration.py main.py \
  tests/test_cloakbrowser_driver.py tests/test_cloakbrowser_registration.py \
  webui/config_editor.py webui/templates/index.html
if git cat-file -e "$BASE:tests/test_registration_service_retry.py" 2>/dev/null; then
  git restore --source="$BASE" -- tests/test_registration_service_retry.py
else
  git restore --staged -- tests/test_registration_service_retry.py 2>/dev/null || true
  rm -f tests/test_registration_service_retry.py
fi
printf '%s\n' "Rollback restored source files to $BASE"
