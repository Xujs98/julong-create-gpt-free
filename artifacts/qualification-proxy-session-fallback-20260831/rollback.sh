#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
PATCH="$SCRIPT_DIR/qualification-proxy-session-fallback.patch"
cd "$ROOT"
git apply --check -R "$PATCH"
git apply -R "$PATCH"
printf '%s\n' '[OK] 已回滚资格查询代理 session 回退修复'
