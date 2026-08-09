#!/usr/bin/env python3
"""Back up JSON/TXT state, migrate it to SQLite, and verify exact parity."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402
from core.sqlite_store import SQLiteStore  # noqa: E402


SOURCE_FILES = (
    "用于注册的邮箱.json",
    "用于注册的邮箱.txt",
    "用于注册的API邮箱.json",
    "用于注册的API邮箱.txt",
    "用于注册的iCloud邮箱.json",
    "用于注册的iCloud邮箱.txt",
    "用于注册的域名邮箱.json",
    "账号分组.json",
    "注册成功的邮箱.json",
    "注册成功的邮箱.txt",
    "注册成功的token.txt",
    "注册任务.json",
    "codex_导出状态.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _backup_sources(artifact_dir: Path) -> tuple[list[str], list[str]]:
    backup_dir = artifact_dir / "source-backup"
    present: list[str] = []
    missing: list[str] = []
    manifest: list[str] = []
    for relative in SOURCE_FILES:
        source = ROOT / relative
        if source.exists():
            destination = backup_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            present.append(relative)
            manifest.append(f"{_sha256(source)}  {relative}")
        else:
            missing.append(relative)
            manifest.append(f"MISSING  {relative}")
    (artifact_dir / "present-files.txt").write_text(
        "\n".join(present) + ("\n" if present else ""),
        encoding="utf-8",
    )
    (artifact_dir / "missing-files.txt").write_text(
        "\n".join(missing) + ("\n" if missing else ""),
        encoding="utf-8",
    )
    (artifact_dir / "baseline-sha256.txt").write_text(
        "\n".join(manifest) + "\n",
        encoding="utf-8",
    )
    return present, missing


def _verify_sources_unchanged(artifact_dir: Path, present: list[str]) -> None:
    backup_dir = artifact_dir / "source-backup"
    for relative in present:
        if _sha256(ROOT / relative) != _sha256(backup_dir / relative):
            raise RuntimeError(f"迁移期间源文件发生变化: {relative}")


def _write_rollback_wrapper(artifact_dir: Path) -> Path:
    path = artifact_dir / "rollback-sqlite-migration.sh"
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
exec "$ROOT/tools/rollback_sqlite_migration.sh" "$HERE"
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _id_signature(rows: list[dict]) -> str:
    ids = [row.get("id") for row in rows]
    return _canonical_digest(ids)


def migrate(artifact_dir: Path) -> dict[str, Any]:
    if artifact_dir.exists() and any(artifact_dir.iterdir()):
        raise RuntimeError(f"审计目录非空: {artifact_dir}")
    artifact_dir.mkdir(parents=True, exist_ok=True)

    present, missing = _backup_sources(artifact_dir)
    collections, documents = db._sqlite_source_snapshot()
    baseline_counts = {name: len(rows) for name, rows in collections.items()}
    baseline_digests = {name: _canonical_digest(rows) for name, rows in collections.items()}
    baseline_ids = {name: _id_signature(rows) for name, rows in collections.items()}
    document_digests = {name: _canonical_digest(value) for name, value in documents.items()}

    sqlite_path = Path(db._SQLITE_PATH)
    if sqlite_path.exists():
        previous = artifact_dir / "sqlite-before.sqlite3"
        source = SQLiteStore(sqlite_path)
        source.initialize()
        with sqlite3.connect(str(sqlite_path)) as src, sqlite3.connect(str(previous)) as dst:
            src.backup(dst)

    migrated_counts = db.initialize_sqlite_storage(force=True)
    store = SQLiteStore(sqlite_path)
    integrity = store.integrity_check()
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity_check 失败: {integrity}")

    for name, expected in collections.items():
        actual = store.load_records(name)
        if len(actual) != len(expected):
            raise RuntimeError(f"记录数不一致: {name}")
        if _canonical_digest(actual) != baseline_digests[name]:
            raise RuntimeError(f"字段内容不一致: {name}")
        if _id_signature(actual) != baseline_ids[name]:
            raise RuntimeError(f"ID 顺序不一致: {name}")
    for name, expected in documents.items():
        actual = store.load_document(name, None)
        if _canonical_digest(actual) != document_digests[name]:
            raise RuntimeError(f"文档内容不一致: {name}")

    _verify_sources_unchanged(artifact_dir, present)
    rollback_path = _write_rollback_wrapper(artifact_dir)
    report_lines = [
        "SQLite migration verification",
        "baseline_command=python3 tools/migrate_to_sqlite.py",
        "baseline_exit_code=0",
        f"source_present={len(present)}",
        f"source_missing={len(missing)}",
        *[f"baseline_count.{name}={count}" for name, count in sorted(baseline_counts.items())],
        f"modified_command=PRAGMA integrity_check on {sqlite_path}",
        "modified_exit_code=0",
        f"sqlite_integrity={integrity}",
        *[f"sqlite_count.{name}={count}" for name, count in sorted(migrated_counts.items())],
        "payload_digest_match=true",
        "id_signature_match=true",
        "source_sha256_unchanged=true",
        f"rollback_command={rollback_path}",
    ]
    report_path = artifact_dir / "migration-verification.txt"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return {
        "sqlite_path": str(sqlite_path),
        "artifact_dir": str(artifact_dir),
        "report_path": str(report_path),
        "rollback_path": str(rollback_path),
        "counts": baseline_counts,
        "integrity": integrity,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=ROOT / "artifacts" / "sqlite-migration-20260809",
    )
    args = parser.parse_args()
    artifact_dir = args.artifact_dir.resolve()
    result = migrate(artifact_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
