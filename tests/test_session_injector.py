import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

from core import session_injector


class _FakeDriver:
    """模拟 Cloak Selenium 适配器，验证植入顺序而不启动真实浏览器。"""

    def __init__(self):
        self.calls = []
        self.upstream_proxy_url = "http://user:pass@proxy.example.test:8080"

    def get(self, url):
        self.calls.append(("get", url))

    def delete_all_cookies(self):
        self.calls.append(("delete_all_cookies",))

    def add_cookie(self, cookie):
        self.calls.append(("add_cookie", cookie))

    def execute_async_script(self, script):
        self.calls.append(("verify",))
        return {"status": 200, "data": {"accessToken": "TOKEN"}}

    def quit(self):
        self.calls.append(("quit",))


def test_inject_one_uses_visible_driver_and_verifies_saved_cookie():
    """植入应强制可见窗口，并在再次打开首页后校验完整 session。"""
    driver = _FakeDriver()
    opened = SimpleNamespace(profile_id="profile-1")
    account = {
        "id": 7,
        "email": "user@example.test",
        "extra_json": json.dumps({"session": {"accessToken": "TOKEN", "cookies": [{"name": "sid", "value": "COOKIE", "domain": ".chatgpt.com", "path": "/"}]}}),
    }
    session_injector._OPEN_SESSIONS.clear()
    with patch.object(session_injector, "build_cloak_driver", return_value=(driver, opened)) as launch:
        result = session_injector._inject_one(account)

    assert result["ok"] is True
    launch.assert_called_once_with(proxy=None, headless=False)
    assert driver.calls == [
        ("get", "https://chatgpt.com/"),
        ("delete_all_cookies",),
        ("add_cookie", {"name": "sid", "value": "COOKIE", "domain": ".chatgpt.com", "path": "/"}),
        ("get", "https://chatgpt.com/"),
        ("verify",),
    ]
    assert session_injector._OPEN_SESSIONS[7][0] is driver
    session_injector.close_injected_sessions([7])
    assert ("quit",) in driver.calls


def test_inject_one_reports_accounts_without_cookies_without_launching():
    """旧账号缺少浏览器 cookies 时给出明确原因，不创建空白浏览器。"""
    account = {"id": 8, "email": "old@example.test", "extra_json": json.dumps({"session": {"accessToken": "TOKEN"}})}
    with patch.object(session_injector, "build_cloak_driver") as launch:
        result = session_injector._inject_one(account)
    assert result["ok"] is False
    assert "cookies" in result["reason"]
    launch.assert_not_called()


def test_started_session_keeps_owner_thread_alive_until_close():
    """真实植入完成后，创建 driver 的宿主线程应持续到关闭接口发出信号。"""
    account = {"id": 9, "email": "owner@example.test"}

    def fake_inject(account, *, keep_open, stop_event, on_ready):
        result = {"ok": True, "id": account["id"], "email": account["email"]}
        on_ready(result)
        assert keep_open is True
        stop_event.wait(timeout=3)
        return result

    session_injector._SESSION_CONTROLS.clear()
    with patch.object(session_injector, "_inject_one", side_effect=fake_inject):
        result = session_injector._start_injected_session(account)
        control = session_injector._SESSION_CONTROLS[9]
        assert result["ok"] is True
        assert control.thread is not None and control.thread.is_alive()
        assert session_injector.close_injected_sessions([9]) == 1
        control.thread.join(timeout=1)
        assert not control.thread.is_alive()


def test_refresh_execution_context_error_does_not_close_browser():
    """刷新造成 execution context 错误时，宿主线程应继续持有窗口。"""
    stop_event = threading.Event()

    class _RefreshDriver:
        browser = SimpleNamespace(is_connected=lambda: True)
        context = None
        page = SimpleNamespace(is_closed=lambda: False)

        def __init__(self):
            self.calls = 0

        def execute_script(self, _script):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("Execution context was destroyed")
            stop_event.set()

    driver = _RefreshDriver()
    thread = threading.Thread(target=session_injector._keep_browser_owned, args=(driver, stop_event), daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert driver.calls >= 3
