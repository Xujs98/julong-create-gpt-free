# -*- coding: utf-8 -*-
"""通用提链服务注册表。

协议服务由项目代码提供；用户新增的 API 服务以 JSON 保存到 ``.env``，
避免 CDK 出现在版本库或普通配置文件中。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from config.env_loader import load_env, write_env_values


API_SERVICES_ENV = "EXTRACT_LINK_API_SERVICES_JSON"
SUPPORTED_API_LINK_TYPES = {"pix", "upi", "kakao_pay", "ideal"}
BUILTIN_PROTOCOL_SERVICES = (
    {
        "id": "pp",
        "name": "PP提链",
        "mode": "protocol",
        "protocol": "pp",
        "requires_cdk": False,
        "description": "项目内置 PayPal 协议提链，无需 CDK 配额",
    },
)


def _raw_api_services() -> list[dict[str, Any]]:
    load_env(override=True)
    raw = str(os.getenv(API_SERVICES_ENV, "") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _normalize_service_id(value: Any, name: str = "") -> str:
    service_id = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    if service_id:
        return service_id[:64]
    base = re.sub(r"[^a-z0-9_-]+", "-", str(name or "").strip().lower()).strip("-")
    return (base[:48] or "api") + "-" + uuid.uuid4().hex[:8]


def _normalize_api_service(payload: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    name = str(payload.get("name") or existing.get("name") or "").strip()
    api_base = str(payload.get("api_base") or existing.get("api_base") or "").strip().rstrip("/")
    cdk = str(payload.get("cdk") if "cdk" in payload else existing.get("cdk") or "").strip()
    link_type = str(payload.get("link_type") or existing.get("link_type") or "pix").strip().lower()
    try:
        workers = int(payload.get("workers") or existing.get("workers") or 3)
    except (TypeError, ValueError):
        workers = 3
    if not name:
        raise ValueError("提链 API 服务名称为空")
    if not re.match(r"^https?://", api_base, re.I):
        raise ValueError("提链服务地址必须以 http:// 或 https:// 开头")
    if not cdk:
        raise ValueError("提链 CDK 为空")
    if link_type not in SUPPORTED_API_LINK_TYPES:
        raise ValueError("提链类型无效，仅支持 pix / upi / kakao_pay / ideal")
    service_id = _normalize_service_id(payload.get("id") or existing.get("id"), name)
    return {
        "id": service_id,
        "name": name[:80],
        "mode": "api",
        "api_base": api_base,
        "cdk": cdk,
        "link_type": link_type,
        "workers": max(1, min(20, workers)),
        "requires_cdk": True,
    }


def _legacy_api_service() -> dict[str, Any] | None:
    load_env(override=True)
    api_base = str(os.getenv("EXTRACT_LINK_API_BASE", "") or "").strip().rstrip("/")
    cdk = str(os.getenv("EXTRACT_LINK_CDK", "") or "").strip()
    if not api_base or not cdk:
        return None
    try:
        workers = int(os.getenv("EXTRACT_LINK_WORKERS", "3") or 3)
    except (TypeError, ValueError):
        workers = 3
    return {
        "id": "legacy-api",
        "name": "旧版 API 提链",
        "mode": "api",
        "api_base": api_base,
        "cdk": cdk,
        "link_type": str(os.getenv("EXTRACT_LINK_TYPE", "pix") or "pix").strip().lower(),
        "workers": max(1, min(20, workers)),
        "requires_cdk": True,
        "legacy": True,
    }


def list_services(*, mask_secrets: bool = True) -> list[dict[str, Any]]:
    services = [dict(item) for item in BUILTIN_PROTOCOL_SERVICES]
    api_services = []
    seen = set()
    for raw in _raw_api_services():
        try:
            service = _normalize_api_service(raw, existing=raw)
        except ValueError:
            continue
        if service["id"] in seen:
            continue
        seen.add(service["id"])
        api_services.append(service)
    legacy = _legacy_api_service()
    if legacy and legacy["id"] not in seen:
        api_services.append(legacy)
    services.extend(api_services)
    if mask_secrets:
        for service in services:
            if service.get("mode") == "api":
                service["has_cdk"] = bool(service.get("cdk"))
                service.pop("cdk", None)
    return services


def get_service(service_id: str, *, mode: str | None = None) -> dict[str, Any] | None:
    wanted = str(service_id or "").strip().lower()
    wanted_mode = str(mode or "").strip().lower()
    for service in list_services(mask_secrets=False):
        if str(service.get("id") or "").lower() != wanted:
            continue
        if wanted_mode and service.get("mode") != wanted_mode:
            continue
        return service
    return None


def resolve_service(*, mode: str | None = None, provider: str | None = None) -> dict[str, Any]:
    load_env(override=True)
    selected_mode = str(mode or os.getenv("EXTRACT_LINK_MODE", "protocol") or "protocol").strip().lower()
    selected_provider = str(provider or os.getenv("EXTRACT_LINK_PROVIDER", "pp") or "pp").strip().lower()
    if selected_mode not in {"api", "protocol"}:
        raise ValueError("提链方式无效，仅支持 api / protocol")
    service = get_service(selected_provider, mode=selected_mode)
    if service:
        return service
    candidates = [item for item in list_services(mask_secrets=False) if item.get("mode") == selected_mode]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(f"未找到已选择的{selected_mode}提链服务: {selected_provider}")


def save_api_service(payload: dict[str, Any]) -> dict[str, Any]:
    rows = _raw_api_services()
    requested_id = _normalize_service_id(payload.get("id"), str(payload.get("name") or "")) if payload.get("id") else ""
    existing = next((row for row in rows if str(row.get("id") or "").lower() == requested_id), None)
    normalized = _normalize_api_service(payload, existing=existing)
    replaced = False
    next_rows = []
    for row in rows:
        if str(row.get("id") or "").lower() == normalized["id"].lower():
            next_rows.append(normalized)
            replaced = True
        else:
            next_rows.append(row)
    if not replaced:
        next_rows.append(normalized)
    write_env_values({API_SERVICES_ENV: json.dumps(next_rows, ensure_ascii=False, separators=(",", ":"))})
    public = dict(normalized)
    public["has_cdk"] = True
    public.pop("cdk", None)
    return public


def delete_api_service(service_id: str) -> bool:
    wanted = str(service_id or "").strip().lower()
    if not wanted or wanted == "legacy-api":
        return False
    rows = _raw_api_services()
    next_rows = [row for row in rows if str(row.get("id") or "").strip().lower() != wanted]
    if len(next_rows) == len(rows):
        return False
    write_env_values({API_SERVICES_ENV: json.dumps(next_rows, ensure_ascii=False, separators=(",", ":"))})
    return True
