#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"
PATCH="${TMPDIR:-/tmp}/otp-agent-2fa-fix.rollback.patch"
gzip -dc "artifacts/otp-agent-2fa-fix-20260812/patch.diff.gz" > "$PATCH"
git apply -R --check "$PATCH"
git apply -R "$PATCH"
printf '%s\n' 'rollback applied'
