"""Codex OAuth/CPA credential export format adapters.

The adapters deliberately keep the source credential fields intact while
normalising the fields consumed by Cockpit Tools, sub2api and CAP imports.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _jwt_claims(token: Any) -> dict[str, Any]:
    value = _text(token)
    parts = value.split(".")
    if len(parts) < 2:
        return {}
    try:
        raw = parts[1] + ("=" * (-len(parts[1]) % 4))
        parsed = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return {}


def _nested(claims: dict[str, Any], namespace: str) -> dict[str, Any]:
    value = claims.get(namespace)
    return value if isinstance(value, dict) else {}


def normalize_credential(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with common Codex OAuth fields filled from JWT claims."""
    if not isinstance(raw, dict):
        raise ValueError("Codex credential must be an object")
    result = dict(raw)
    id_claims = _jwt_claims(result.get("id_token"))
    access_claims = _jwt_claims(result.get("access_token"))
    id_auth = _nested(id_claims, "https://api.openai.com/auth")
    id_profile = _nested(id_claims, "https://api.openai.com/profile")
    access_auth = _nested(access_claims, "https://api.openai.com/auth")
    result["email"] = _text(result.get("email") or id_claims.get("email") or id_profile.get("email"))
    result["account_id"] = _text(
        result.get("account_id")
        or result.get("chatgpt_account_id")
        or id_auth.get("chatgpt_account_id")
        or access_auth.get("chatgpt_account_id")
    )
    result["chatgpt_account_id"] = _text(result.get("chatgpt_account_id") or result.get("account_id"))
    result["chatgpt_user_id"] = _text(
        result.get("chatgpt_user_id")
        or id_auth.get("chatgpt_user_id")
        or access_auth.get("chatgpt_user_id")
    )
    result["organization_id"] = _text(
        result.get("organization_id")
        or access_auth.get("poid")
        or id_auth.get("organization_id")
    )
    result["plan_type"] = _text(
        result.get("plan_type")
        or access_auth.get("chatgpt_plan_type")
        or id_auth.get("chatgpt_plan_type")
    )
    result["client_id"] = _text(result.get("client_id") or access_claims.get("client_id") or id_claims.get("aud"))
    result["expired"] = _text(result.get("expired") or result.get("expires_at"))
    result["type"] = _text(result.get("type") or "codex")
    return result


def build_cockpit_tools_records(credentials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cockpit Tools accepts an array of CPA-compatible credential objects."""
    return [normalize_credential(item) for item in credentials]


def build_sub2api_payload(
    credentials: list[dict[str, Any]],
    *,
    concurrency: int = 3,
    priority: int = 50,
) -> dict[str, Any]:
    """Build the OAuth account envelope used by sub2api imports."""
    accounts = []
    for raw in credentials:
        item = normalize_credential(raw)
        expires_at = _text(item.get("expires_at") or item.get("expired"))
        account = {
            "name": _text(item.get("email") or item.get("account_id") or "codex-account"),
            "platform": "openai",
            "concurrency": max(1, int(concurrency)),
            "priority": int(priority),
            "type": "oauth",
            "credentials": {
                "access_token": _text(item.get("access_token")),
                "expires_at": expires_at,
                "refresh_token": _text(item.get("refresh_token")),
                "client_id": _text(item.get("client_id")),
                "id_token": _text(item.get("id_token")),
                "email": _text(item.get("email")),
                "chatgpt_account_id": _text(item.get("chatgpt_account_id") or item.get("account_id")),
                "chatgpt_user_id": _text(item.get("chatgpt_user_id")),
                "organization_id": _text(item.get("organization_id")),
                "plan_type": _text(item.get("plan_type")),
                "subscription_expires_at": _text(item.get("subscription_expires_at")),
            },
            "extra": {"auth_provider": _text(item.get("auth_provider") or "password")},
        }
        accounts.append(account)
    return {
        "exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "proxies": [],
        "accounts": accounts,
        "type": "sub2api-data",
        "version": 1,
    }


def build_cap_records(credentials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CAP is the single CPA-compatible object format, one object per account."""
    return [normalize_credential(item) for item in credentials]


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
