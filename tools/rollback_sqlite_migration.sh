#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_DIR="${1:-}"

if [[ -z "$ARTIFACT_DIR" ]]; then
  ARTIFACT_DIR="$(find "$ROOT/artifacts" -maxdepth 1 -type d -name 'sqlite-migration-*' | sort | tail -1)"
fi
if [[ -z "$ARTIFACT_DIR" || ! -d "$ARTIFACT_DIR/source-backup" ]]; then
  echo "Migration backup directory not found" >&2
  exit 1
fi

BACKUP_DIR="$ARTIFACT_DIR/source-backup"
while IFS= read -r rel; do
  [[ -z "$rel" ]] && continue
  mkdir -p "$(dirname "$ROOT/$rel")"
  cp -p "$BACKUP_DIR/$rel" "$ROOT/$rel"
done < "$ARTIFACT_DIR/present-files.txt"

while IFS= read -r rel; do
  [[ -z "$rel" ]] && continue
  rm -f "$ROOT/$rel"
done < "$ARTIFACT_DIR/missing-files.txt"

STAMP="$(date +%Y%m%d-%H%M%S)"
DISABLED_DIR="$ARTIFACT_DIR/sqlite-disabled-$STAMP"
mkdir -p "$DISABLED_DIR"
for path in "$ROOT/data/registration.sqlite3"*; do
  [[ -e "$path" ]] || continue
  mv "$path" "$DISABLED_DIR/"
done

python3 - "$ROOT/.env" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
key = "TURB_STORAGE_BACKEND"
updated = []
found = False
for line in lines:
    if line.lstrip().startswith(f"{key}="):
        updated.append(f"{key}=json")
        found = True
    else:
        updated.append(line)
if not found:
    updated.extend(["", "# Storage rollback mode", f"{key}=json"])
path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
PY

echo "Rollback complete: JSON backend enabled; SQLite preserved in $DISABLED_DIR"
