#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
PATCH="$SCRIPT_DIR/patch.diff"
cd "$ROOT"
git apply --check -R "$PATCH"
git apply -R "$PATCH"
printf '%s\n' "[OK] Rolled back 2FA email OTP resend patch"
