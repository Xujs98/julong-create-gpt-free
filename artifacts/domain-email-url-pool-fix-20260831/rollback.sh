#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"
BASELINE="0f0b4582e800fe327e9c1862b124b6979b27be39"
git restore --source "$BASELINE" -- \
  .env.example config/email.py core/db.py core/email_provider.py \
  webui/app.py webui/config_editor.py webui/templates/index.html \
  tests/test_account_email_badges_ui.py tests/test_domain_email_import.py \
  tests/test_email_provider_cloudflare.py tests/test_icloud_webui.py
printf 'rollback restored domain email URL-pool files from %s\n' "$BASELINE"
