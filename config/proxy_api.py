"""Provider-independent proxy API registry and provider query adapters (no I/O)."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


def detect_provider(url: str) -> str:
    parsed = urlsplit(url)
    keys = {key.lower() for key, _ in parse_qsl(parsed.query)}
    return "b2proxy" if {"zone", "ptype"} <= keys or {"count", "stype", "sesstype"} <= keys or "bestgo.work" == parsed.hostname or str(parsed.hostname).endswith(".bestgo.work") else "cliproxy"


def validate_api_entries(value) -> list[dict]:
    """Validate without returning URLs/credentials in error messages."""
    if isinstance(value, str):
        if len(value) > 250_000:
            raise ValueError("API管理内容过大")
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError("API管理必须是有效 JSON 列表") from None
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("API管理支持最多 50 个 API")
    result, ids, urls = [], set(), set()
    for index, entry in enumerate(value, 1):
        error = f"第 {index} 个 API"
        if not isinstance(entry, dict):
            raise ValueError(f"{error}格式错误")
        identity, name, url = (str(entry.get(key) or "").strip() for key in ("id", "name", "url"))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity) or identity in ids:
            raise ValueError(f"{error}标识无效或重复")
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError(f"{error}名称需为 1-80 个字符")
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
        except ValueError:
            valid = False
        if not valid or len(url) > 4096 or re.search(r"[\s\x00-\x1f\x7f]", url):
            raise ValueError(f"{error}地址需为有效 HTTP/HTTPS URL（分隔符使用转义形式）")
        if url in urls:
            raise ValueError(f"{error}地址重复")
        provider = str(entry.get("provider") or detect_provider(url)).lower()
        if provider not in {"cliproxy", "b2proxy"}:
            raise ValueError(f"{error}供应商仅支持 CliProxy 或 b2proxy")
        if not isinstance(entry.get("enabled"), bool):
            raise ValueError(f"{error}选中状态需为布尔值")
        if provider == "b2proxy":
            proto = dict(parse_qsl(parsed.query)).get("proto", "http").lower()
            if proto not in {"http", "https", "socks5", "socks5h", "socks4"}:
                raise ValueError(f"{error}代理协议不受支持")
        result.append(dict(id=identity, name=name, url=url, provider=provider, enabled=entry["enabled"]))
        ids.add(identity)
        urls.add(url)
    return result


def load_api_entries(raw: str, legacy_url: str) -> list[dict]:
    # An explicit [] means empty; only an absent registry migrates the legacy API.
    if not str(raw or "").strip():
        return [dict(id="legacy", name="原有 API", url=legacy_url,
                     provider=detect_provider(legacy_url), enabled=True)]
    return validate_api_entries(raw)


def build_request_url(entry: dict, *, region: str, count: int, duration: int,
                      delimiter: str, data_type: str, session_type: str) -> str:
    raw = entry["url"]
    encoded = quote(region, safe="")
    for token in ("region", "country", "country_code"):
        raw = raw.replace("{" + token + "}", encoded)
    parsed = urlsplit(raw)
    original = parse_qsl(parsed.query, keep_blank_values=True)
    if entry["provider"] == "b2proxy":
        # b2proxy global mix has NO region parameter (CliProxy uses Rand).
        keys = {"region", "count", "stype", "split", "sesstype"}
        generated = [] if region.lower() == "rand" else [("region", region)]
        generated.extend([("count", str(count)), ("stype", data_type),
                          ("split", r"\r\n" if delimiter == "rn" else r"\n"),
                          ("sessType", session_type)])
        if not any(k.lower() == "proto" for k, _ in original):
            generated.append(("proto", "http"))
    else:
        keys = {"region", "num", "time", "format", "type"}
        generated = [("region", region), ("num", str(count))]
        if session_type != "rotating":
            generated.append(("time", str(duration)))
        generated.extend([("format", delimiter), ("type", data_type)])
    query = generated + [(k, v) for k, v in original if k.lower() not in keys]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
