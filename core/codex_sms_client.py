# -*- coding: utf-8 -*-
"""Codex 接码助手 API 客户端。

协议来自 https://sms.kkdos.store/api-docs：CDK 本身承担鉴权，客户端不保存额外 API token。
"""
import json
from urllib.parse import quote, urljoin


class CodexSmsError(RuntimeError):
    """Codex 接码助手请求错误。"""


class CodexSmsNoNumbers(CodexSmsError):
    """CDK 暂无可用号码。"""


class CodexSmsClient:
    def __init__(self, base_url: str, http, timeout: int = 30):
        self.base_url = str(base_url or "https://sms.kkdos.store").strip().rstrip("/")
        self.http = http
        self.timeout = int(timeout or 30)
        try:
            self.http.timeout = self.timeout
        except Exception:
            pass

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    @staticmethod
    def _json(resp) -> dict:
        try:
            data = resp.json()
        except Exception:
            try:
                data = json.loads(resp.text or "{}")
            except Exception:
                data = {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _error(data: dict, text: str = "") -> str:
        return str(data.get("error") or data.get("message") or text or "请求失败").strip()

    def request_session(self, cdk: str) -> dict:
        cdk = str(cdk or "").strip().upper()
        if not cdk:
            raise CodexSmsError("CDK 不能为空")
        resp = self.http.post(
            self._url("/api/v1/code/request"),
            headers={"Content-Type": "application/json"},
            data=json.dumps({"cdk": cdk}),
        )
        data = self._json(resp)
        if resp.status_code == 429:
            raise CodexSmsError("Codex 接码助手请求频率受限，请稍后重试")
        if resp.status_code >= 400 or data.get("state") == "error":
            msg = self._error(data, getattr(resp, "text", ""))
            if "invalid" in msg.lower() or "unavailable" in msg.lower():
                raise CodexSmsNoNumbers(f"CDK 不可用：{msg}")
            raise CodexSmsError(f"Codex 接码助手 HTTP {resp.status_code}: {msg[:240]}")
        session_id = str(data.get("sessionId") or "").strip()
        phone = str(data.get("phone") or "").strip()
        if not session_id or not phone:
            raise CodexSmsError(f"Codex 接码助手响应缺少 sessionId/phone：{str(data)[:240]}")
        data["sessionId"] = session_id
        data["phone"] = phone
        data["cdk"] = cdk
        return data

    def get_status(self, session_id: str) -> dict:
        sid = quote(str(session_id or "").strip(), safe="")
        if not sid:
            raise CodexSmsError("sessionId 不能为空")
        resp = self.http.get(self._url(f"/api/v1/code/{sid}"))
        data = self._json(resp)
        if resp.status_code >= 400:
            raise CodexSmsError(f"Codex 接码助手 HTTP {resp.status_code}: {self._error(data, getattr(resp, 'text', ''))[:240]}")
        return data

    def switch_session(self, session_id: str) -> dict:
        sid = quote(str(session_id or "").strip(), safe="")
        resp = self.http.post(
            self._url(f"/api/v1/code/{sid}/switch"),
            headers={"Content-Type": "application/json"},
            data="{}",
        )
        data = self._json(resp)
        if resp.status_code >= 400:
            raise CodexSmsError(f"Codex 接码助手换号失败：{self._error(data, getattr(resp, 'text', ''))[:240]}")
        return data

    def batch_redeem(self, cdks: list[str]) -> dict:
        values = [str(x or "").strip().upper() for x in (cdks or []) if str(x or "").strip()]
        if not values:
            raise CodexSmsError("请至少提供一个 CDK")
        if len(values) > 100:
            raise CodexSmsError("每批最多检查 100 个 CDK")
        resp = self.http.post(
            self._url("/api/cdk/redeem/batch"),
            headers={"Content-Type": "application/json"},
            data=json.dumps({"cdks": values}),
        )
        data = self._json(resp)
        if resp.status_code == 429:
            raise CodexSmsError("Codex 接码助手批量请求频率受限，请稍后重试")
        if resp.status_code >= 400:
            raise CodexSmsError(f"Codex 接码助手批量检查失败：{self._error(data, getattr(resp, 'text', ''))[:240]}")
        items = data.get("items")
        if not isinstance(items, list):
            raise CodexSmsError("Codex 接码助手批量响应缺少 items")
        return {"items": items, "input_count": len(values)}

    def phone_availability(self) -> dict:
        resp = self.http.get(self._url("/api/public/metrics/phone-availability"))
        data = self._json(resp)
        if resp.status_code >= 400:
            raise CodexSmsError(f"号码库存查询失败：HTTP {resp.status_code}")
        return data
