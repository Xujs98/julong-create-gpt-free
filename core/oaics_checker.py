# -*- coding: utf-8 -*-
"""OAICS Checkout 会话识别工具。

各国资格查询由 :mod:`core.qualification_test` 负责；本模块只处理
ChatGPT Checkout 响应中的 OAICS/Stripe 会话识别，不包含旧的外部资格协议。
"""
from __future__ import annotations

import re
from typing import Any, Iterable


SUPPORTED_SESSION_PREFIXES = ("oaics_", "cs_")
_SESSION_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(oaics_|cs_)[A-Za-z0-9_-]+")


def _walk_values(value: Any, *, depth: int = 0) -> Iterable[Any]:
    if depth > 10:
        return
    if isinstance(value, dict):
        for item in value.values():
            yield item
            if isinstance(item, (dict, list, tuple)):
                yield from _walk_values(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield item
            if isinstance(item, (dict, list, tuple)):
                yield from _walk_values(item, depth=depth + 1)


def extract_checkout_session_id(payload: Any) -> str:
    """从常见字段或嵌套 URL 中提取 Checkout 会话 ID。"""
    if not isinstance(payload, dict):
        raise ValueError("checkout response must be a JSON object")
    candidates = [
        payload.get("checkout_session_id"),
        payload.get("session_id"),
        payload.get("id"),
        *_walk_values(payload),
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text.startswith(SUPPORTED_SESSION_PREFIXES):
            return text
        match = _SESSION_PATTERN.search(text)
        if match:
            return match.group(0)
    raise ValueError("checkout response did not contain a supported oaics_/cs_ session id")


def detect_oaics_checkout(payload: Any, *, billing_country: str = "") -> dict[str, Any]:
    """根据 Checkout 会话前缀判断账号是否具备 OAICS 资格。"""
    session_id = extract_checkout_session_id(payload)
    is_oaics = session_id.startswith("oaics_")
    processor = str(payload.get("processor_entity") or "").strip()
    if not processor:
        processor = "openai_llc" if str(billing_country).upper() == "US" else "openai_ie"
    return {
        "is_oaics": is_oaics,
        "session_kind": "oaics" if is_oaics else "stripe_cs",
        "processor_entity": processor,
    }
