# -*- coding: utf-8 -*-
"""Versioned ZIP export/import for complete registered-account records."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from core import db

FORMAT_ID = "turb-gpt-account-transfer"
SCHEMA_VERSION = 1
MAX_ACCOUNTS = 1000
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_MANIFEST_BYTES = 20 * 1024 * 1024
MAX_MEMBERS = 5000
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TRANSFER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_POOL_SOURCES = {"outlook", "generic_api", "icloud", "cloudflare_domain"}
_ATTACHMENT_FIELDS = {
    "codex_credential": None,
    "codex_agent_auth": "codex_agent_auth_path",
    "codex_agent_sub2api": "codex_agent_sub2api_path",
}


class AccountTransferError(ValueError):
    """Raised when an account archive is malformed or exceeds safety limits."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_filename(value: str, fallback: str = "attachment.json") -> str:
    name = Path(str(value or "")).name
    stem = re.sub(r"[^A-Za-z0-9._@+-]+", "_", name).strip(". ")[:180]
    windows_stem = stem.upper().split(".", 1)[0]
    windows_reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    windows_reserved.update({f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10)})
    if not stem or windows_stem in windows_reserved:
        stem = fallback
    if not stem.lower().endswith(".json"):
        stem = f"{stem}.json"
    return stem


def _local_json_bytes(value: Any) -> tuple[bytes, str] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    try:
        path = path.resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".json":
            return None
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            return None
        content = path.read_bytes()
        json.loads(content.decode("utf-8"))
        return content, path.name
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _collect_attachments(bundle: dict, codex_by_email: dict[str, list[dict]]) -> list[dict]:
    account = bundle["account"]
    email_key = str(account.get("email") or "").strip().casefold()
    collected: list[tuple[str, str | None, str, bytes]] = []
    for item in codex_by_email.get(email_key, []):
        filename = str(item.get("filename") or "")
        try:
            content, returned_name = db.read_codex_credential(filename)
            raw = content.encode("utf-8")
            json.loads(content)
            collected.append(("codex_credential", None, returned_name, raw))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            continue
    for kind, field in (
        ("codex_agent_auth", "codex_agent_auth_path"),
        ("codex_agent_sub2api", "codex_agent_sub2api_path"),
    ):
        loaded = _local_json_bytes(account.get(field))
        if loaded is not None:
            content, filename = loaded
            collected.append((kind, field, filename, content))

    attachments: list[dict] = []
    transfer_id = bundle["transfer_id"]
    for index, (kind, account_field, filename, content) in enumerate(collected, start=1):
        safe_name = _safe_filename(filename)
        archive_path = f"attachments/{transfer_id}/{index:02d}-{safe_name}"
        attachments.append({
            "kind": kind,
            "account_field": account_field,
            "archive_path": archive_path,
            "filename": safe_name,
            "size": len(content),
            "sha256": _sha256(content),
            "_content": content,
        })
    return attachments


def build_account_archive(account_ids: list[int], *, app_version: str) -> tuple[bytes, str, dict]:
    """Build a portable archive and return bytes, download filename, and summary."""
    if not isinstance(account_ids, list) or not account_ids:
        raise AccountTransferError("account_ids 必须是非空数组")
    if len(account_ids) > MAX_ACCOUNTS:
        raise AccountTransferError(f"单次最多导出 {MAX_ACCOUNTS} 个账号")
    bundles, skipped = db.export_account_transfer_bundles(account_ids)
    if not bundles:
        raise AccountTransferError("没有可导出的账号")

    codex_by_email: dict[str, list[dict]] = {}
    for item in db.list_codex_accounts():
        key = str(item.get("email") or "").strip().casefold()
        if key:
            codex_by_email.setdefault(key, []).append(item)
    for bundle in bundles:
        bundle["attachments"] = _collect_attachments(bundle, codex_by_email)

    exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest_accounts = []
    for bundle in bundles:
        clean = {key: value for key, value in bundle.items() if key != "attachments"}
        clean["attachments"] = [
            {key: value for key, value in attachment.items() if key != "_content"}
            for attachment in bundle["attachments"]
        ]
        manifest_accounts.append(clean)
    manifest = {
        "format": FORMAT_ID,
        "schema_version": SCHEMA_VERSION,
        "exported_at": exported_at,
        "app_version": str(app_version or ""),
        "account_count": len(manifest_accounts),
        "accounts": manifest_accounts,
        "skipped": skipped,
    }
    manifest_content = _json_bytes(manifest)
    if len(manifest_content) > MAX_MANIFEST_BYTES:
        raise AccountTransferError("账号清单过大")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", manifest_content)
        for bundle in bundles:
            for attachment in bundle["attachments"]:
                archive.writestr(attachment["archive_path"], attachment["_content"])
    content = output.getvalue()
    if len(content) > MAX_ARCHIVE_BYTES:
        raise AccountTransferError("导出 ZIP 超过 50 MB，请减少本次选择数量")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"accounts-complete-{stamp}.zip"
    summary = {
        "account_count": len(bundles),
        "attachment_count": sum(len(bundle["attachments"]) for bundle in bundles),
        "skipped": skipped,
    }
    return content, filename, summary


def _read_upload(source: bytes | bytearray | BinaryIO) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        content = bytes(source)
        if len(content) > MAX_ARCHIVE_BYTES:
            raise AccountTransferError("ZIP 文件超过 50 MB")
        return content
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_ARCHIVE_BYTES:
            raise AccountTransferError("ZIP 文件超过 50 MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AccountTransferError(f"ZIP 包含非法路径: {name or '(空)'}")
    if info.flag_bits & 0x1:
        raise AccountTransferError("ZIP 不支持加密成员")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise AccountTransferError(f"ZIP 不支持符号链接: {name}")


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    with archive.open(info, "r") as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise AccountTransferError(f"ZIP 成员过大: {info.filename}")
    return content


def parse_account_archive(source: bytes | bytearray | BinaryIO) -> dict:
    """Validate an uploaded archive and return bundles with verified attachment bytes."""
    content = _read_upload(source)
    if len(content) < 4 or content[:2] != b"PK":
        raise AccountTransferError("上传文件不是有效的 ZIP")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content), "r")
    except zipfile.BadZipFile as exc:
        raise AccountTransferError("上传文件不是有效的 ZIP") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_MEMBERS:
            raise AccountTransferError("ZIP 成员数量超出限制")
        by_name: dict[str, zipfile.ZipInfo] = {}
        total_size = 0
        for info in infos:
            _validate_member(info)
            if info.is_dir():
                raise AccountTransferError(f"ZIP 不应包含目录成员: {info.filename}")
            if info.filename in by_name:
                raise AccountTransferError(f"ZIP 包含重名成员: {info.filename}")
            total_size += int(info.file_size or 0)
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise AccountTransferError("ZIP 解压后超过 100 MB")
            by_name[info.filename] = info
        manifest_info = by_name.get("manifest.json")
        if manifest_info is None:
            raise AccountTransferError("ZIP 缺少 manifest.json")
        try:
            manifest = json.loads(_read_member(archive, manifest_info, MAX_MANIFEST_BYTES).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AccountTransferError("manifest.json 格式错误") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_ID:
            raise AccountTransferError("ZIP 不是账号完整迁移包")
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise AccountTransferError(f"不支持的迁移包版本: {manifest.get('schema_version')}")
        bundles = manifest.get("accounts")
        if not isinstance(bundles, list) or not bundles or len(bundles) > MAX_ACCOUNTS:
            raise AccountTransferError(f"迁移包账号数量必须为 1-{MAX_ACCOUNTS}")
        if manifest.get("account_count") != len(bundles):
            raise AccountTransferError("迁移包账号数量与清单不一致")

        declared = {"manifest.json"}
        transfer_ids: set[str] = set()
        emails: set[str] = set()
        parsed_bundles: list[dict] = []
        for bundle in bundles:
            if not isinstance(bundle, dict):
                raise AccountTransferError("迁移包账号条目格式错误")
            transfer_id = str(bundle.get("transfer_id") or "")
            account = bundle.get("account")
            if not _TRANSFER_ID_RE.fullmatch(transfer_id) or transfer_id in transfer_ids:
                raise AccountTransferError("迁移包 transfer_id 非法或重复")
            if not isinstance(account, dict):
                raise AccountTransferError(f"账号记录格式错误: {transfer_id}")
            email = str(account.get("email") or "").strip()
            if not email or email.casefold() in emails:
                raise AccountTransferError("迁移包账号邮箱为空或重复")
            transfer_ids.add(transfer_id)
            emails.add(email.casefold())

            pool_entry = bundle.get("email_pool")
            if pool_entry is not None:
                if not isinstance(pool_entry, dict) or pool_entry.get("source") not in _POOL_SOURCES:
                    raise AccountTransferError(f"邮箱池来源格式错误: {transfer_id}")
                pool_record = pool_entry.get("record")
                if not isinstance(pool_record, dict) or str(pool_record.get("email") or "").strip().casefold() != email.casefold():
                    raise AccountTransferError(f"邮箱池记录与账号不匹配: {transfer_id}")

            descriptors = bundle.get("attachments") or []
            if not isinstance(descriptors, list) or len(descriptors) > 20:
                raise AccountTransferError(f"附件清单格式错误: {transfer_id}")
            attachments = []
            for descriptor in descriptors:
                if not isinstance(descriptor, dict):
                    raise AccountTransferError(f"附件描述格式错误: {transfer_id}")
                kind = descriptor.get("kind")
                expected_field = _ATTACHMENT_FIELDS.get(str(kind or ""), "invalid")
                archive_path = str(descriptor.get("archive_path") or "")
                if expected_field == "invalid" or descriptor.get("account_field") != expected_field:
                    raise AccountTransferError(f"附件类型格式错误: {transfer_id}")
                if archive_path in declared or archive_path not in by_name:
                    raise AccountTransferError(f"附件缺失或重复引用: {archive_path}")
                info = by_name[archive_path]
                if info.file_size > MAX_ATTACHMENT_BYTES or int(descriptor.get("size") or -1) != info.file_size:
                    raise AccountTransferError(f"附件大小校验失败: {archive_path}")
                expected_hash = str(descriptor.get("sha256") or "").lower()
                if not _SHA256_RE.fullmatch(expected_hash):
                    raise AccountTransferError(f"附件哈希格式错误: {archive_path}")
                raw = _read_member(archive, info, MAX_ATTACHMENT_BYTES)
                if _sha256(raw) != expected_hash:
                    raise AccountTransferError(f"附件哈希校验失败: {archive_path}")
                try:
                    json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AccountTransferError(f"附件不是有效 JSON: {archive_path}") from exc
                clean_descriptor = dict(descriptor)
                clean_descriptor["filename"] = _safe_filename(str(descriptor.get("filename") or ""))
                clean_descriptor["_content"] = raw
                attachments.append(clean_descriptor)
                declared.add(archive_path)
            parsed_bundles.append({
                "transfer_id": transfer_id,
                "account": dict(account),
                "email_pool": pool_entry,
                "attachments": attachments,
            })
        unexpected = set(by_name) - declared
        if unexpected:
            raise AccountTransferError(f"ZIP 包含未声明成员: {sorted(unexpected)[0]}")
        return {"manifest": manifest, "bundles": parsed_bundles}


def _write_unique_json(directory: Path, filename: str, content: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix or ".json"
    for index in range(1, 10000):
        name = safe_name if index == 1 else f"{stem}-imported-{index}{suffix}"
        path = directory / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(content)
            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise
    raise AccountTransferError("本地附件文件名冲突过多")


def import_account_archive(source: bytes | bytearray | BinaryIO, *, group_id: int) -> dict:
    """Validate and restore an archive into an existing target account group."""
    parsed = parse_account_archive(source)
    destinations = db.account_transfer_attachment_dirs()
    created: list[tuple[str, Path]] = []
    path_overrides: dict[str, dict[str, str]] = {}
    try:
        for bundle in parsed["bundles"]:
            transfer_id = bundle["transfer_id"]
            for attachment in bundle["attachments"]:
                kind = attachment["kind"]
                path = _write_unique_json(
                    destinations[kind],
                    attachment["filename"],
                    attachment["_content"],
                )
                created.append((transfer_id, path))
                account_field = attachment.get("account_field")
                if account_field:
                    path_overrides.setdefault(transfer_id, {})[account_field] = str(path)
        bundles = [
            {key: value for key, value in bundle.items() if key != "attachments"}
            for bundle in parsed["bundles"]
        ]
        result = db.import_account_transfer_bundles(
            bundles,
            int(group_id),
            attachment_paths=path_overrides,
        )
    except Exception:
        for _, path in created:
            path.unlink(missing_ok=True)
        raise

    imported_ids = {item["transfer_id"] for item in result["imported"]}
    restored_count = 0
    for transfer_id, path in created:
        if transfer_id in imported_ids:
            restored_count += 1
        else:
            path.unlink(missing_ok=True)
    result.update({
        "ok": True,
        "attachment_count": restored_count,
        "schema_version": SCHEMA_VERSION,
    })
    return result
