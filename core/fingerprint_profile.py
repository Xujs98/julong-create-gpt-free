# -*- coding: utf-8 -*-
"""账号级浏览器画像持久化。

画像文件只保存浏览器/硬件/地区字段，不保存邮箱明文、密码、Token、Cookie
或代理认证信息。账号文件名由标准化账号 key 的 SHA-256 生成。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from config import browser as browser_config


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PROFILE_DIR = _PROJECT_ROOT / "data" / "fingerprint_profiles"
_LOCK = threading.RLock()
_RUNTIME_ONLY_KEYS = {
    "react_listening_key",
    "react_container_key",
    "react_resources_key",
}
_SENSITIVE_KEY_PARTS = (
    "password",
    "access_token",
    "refresh_token",
    "id_token",
    "cookie",
    "authorization",
    "proxy_password",
    "proxy_auth",
    "secret",
)


def normalize_fingerprint_key(value: str) -> str:
    """标准化账号 key；邮箱大小写差异映射到同一画像。"""
    normalized = str(value or "").strip().casefold()
    if not normalized:
        raise ValueError("fingerprint_key 不能为空")
    return normalized


def fingerprint_key_hash(value: str) -> str:
    normalized = normalize_fingerprint_key(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def profile_path(value: str, profile_dir: Path | str | None = None) -> Path:
    root = Path(profile_dir) if profile_dir is not None else _DEFAULT_PROFILE_DIR
    return root / f"{fingerprint_key_hash(value)}.json"


def _sanitize(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.casefold()
            if key_text in _RUNTIME_ONLY_KEYS:
                continue
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                continue
            cleaned[key_text] = _sanitize(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def sanitize_browser_profile(profile: dict) -> dict:
    cleaned = _sanitize(deepcopy(profile or {}))
    return cleaned if isinstance(cleaned, dict) else {}


def load_browser_profile(
    fingerprint_key: str,
    *,
    profile_dir: Path | str | None = None,
) -> dict | None:
    """读取账号画像；文件损坏或 schema 不匹配时返回 None 以便重建。"""
    path = profile_path(fingerprint_key, profile_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expected_id = fingerprint_key_hash(fingerprint_key)
    if str(payload.get("fingerprint_id") or "") != expected_id:
        return None
    if int(payload.get("schema_version") or 0) != int(browser_config.FINGERPRINT_PROFILE_SCHEMA_VERSION):
        return None
    profile = payload.get("profile")
    if not isinstance(profile, dict):
        return None
    cleaned = sanitize_browser_profile(profile)
    cleaned["fingerprint_id"] = expected_id
    cleaned["fingerprint_schema_version"] = int(browser_config.FINGERPRINT_PROFILE_SCHEMA_VERSION)
    return cleaned


def save_browser_profile(
    fingerprint_key: str,
    profile: dict,
    *,
    profile_dir: Path | str | None = None,
) -> dict:
    """原子保存账号画像，并返回实际落盘的清理后画像。"""
    path = profile_path(fingerprint_key, profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass

    profile_id = fingerprint_key_hash(fingerprint_key)
    cleaned = sanitize_browser_profile(profile)
    cleaned["fingerprint_id"] = profile_id
    cleaned["fingerprint_schema_version"] = int(browser_config.FINGERPRINT_PROFILE_SCHEMA_VERSION)
    payload = {
        "schema_version": int(browser_config.FINGERPRINT_PROFILE_SCHEMA_VERSION),
        "fingerprint_id": profile_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": cleaned,
    }

    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{profile_id}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            Path(temp_name).chmod(0o600)
        except OSError:
            pass
        os.replace(temp_name, path)
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
    return deepcopy(cleaned)


def get_or_create_browser_profile(
    fingerprint_key: str,
    *,
    geo: dict | None = None,
    profile_dir: Path | str | None = None,
) -> dict:
    """按账号读取或创建稳定画像；同一账号始终复用同一个画像文件。"""
    with _LOCK:
        existing = load_browser_profile(fingerprint_key, profile_dir=profile_dir)
        if existing is not None:
            return existing
        stable_id = fingerprint_key_hash(fingerprint_key)
        generated = browser_config.pick_browser_profile(
            geo,
            stable_key=normalize_fingerprint_key(fingerprint_key),
        )
        generated["fingerprint_id"] = stable_id
        return save_browser_profile(fingerprint_key, generated, profile_dir=profile_dir)


def session_fingerprint_kwargs(account_key: str | None) -> dict:
    """返回会话构造参数；开关关闭时为空，保持旧调用签名和随机行为。"""
    if not bool(getattr(browser_config, "ENABLE_HIGH_FIDELITY_FINGERPRINT", False)):
        return {}
    key = str(account_key or "").strip()
    return {"fingerprint_key": key} if key else {}
