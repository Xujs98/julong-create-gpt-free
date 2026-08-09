# -*- coding: utf-8 -*-
"""CloakBrowser 的 Selenium 风格轻量适配层。"""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from config import cloakbrowser as _cfg
from core.proxy_utils import masked_proxy_url, normalize_proxy_url

logger = logging.getLogger(__name__)


def _resolve_cloak_launch_identity() -> tuple[str, str]:
    """解析本次启动的指纹 seed 与用户目录；默认每次使用全新临时画像。"""
    randomize = bool(getattr(_cfg, "CLOAK_RANDOMIZE_FINGERPRINT_EACH_LAUNCH", True))
    configured_seed = str(getattr(_cfg, "CLOAK_FINGERPRINT_SEED", "") or "").strip()
    configured_user_data_dir = str(getattr(_cfg, "CLOAK_USER_DATA_DIR", "") or "").strip()
    if randomize:
        # Cloak 的 fingerprint 参数接受字符串；使用高熵十进制 seed，避免相邻任务复用画像。
        seed = str(secrets.randbelow(2_147_483_646) + 1)
        return seed, ""
    return configured_seed, configured_user_data_dir


@dataclass
class CloakOpenResult:
    profile_id: str = "cloakbrowser"
    raw: dict | None = None


class CloakElement:
    def __init__(self, page, locator=None, handle=None):
        self.page = page
        self.locator = locator
        self.handle = handle

    def _handle(self):
        if self.handle is not None:
            return self.handle
        return self.locator.element_handle(timeout=5000)

    def _eval(self, expression: str, arg: Any = None) -> Any:
        if self.locator is not None:
            try:
                return self.locator.evaluate(expression, arg, timeout=3000)
            except TypeError:
                return self.locator.evaluate(expression, arg)
        return self.handle.evaluate(expression, arg)

    def _eval_handle(self, expression: str, arg: Any = None) -> Any:
        h = self._handle()
        return h.evaluate_handle(expression, arg)

    def is_displayed(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_visible(timeout=800))
            return bool(self.handle.evaluate("el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'"))
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_enabled(timeout=800))
            return bool(self.handle.evaluate("el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"))
        except Exception:
            return False

    def click(self) -> None:
        if self.locator is not None:
            self.locator.click(timeout=10000)
        else:
            self.handle.click(timeout=10000)

    def clear(self) -> None:
        try:
            if self.locator is not None:
                self.locator.fill("", timeout=10000)
            else:
                self.handle.fill("", timeout=10000)
        except Exception:
            # 部分非 input 元素不支持 fill，回退键盘清空。
            self.click()
            self.page.keyboard.press("Meta+A")
            self.page.keyboard.press("Backspace")

    def fill(self, value: str) -> None:
        """原子设置受控输入框，避免逐字符输入时 React 重渲染移动光标。"""
        value = str(value or "")
        if self.locator is not None:
            self.locator.fill(value, timeout=10000)
        else:
            self.handle.fill(value, timeout=10000)

    @property
    def tag_name(self) -> str:
        try:
            return str(self._eval("el => el.tagName.toLowerCase()") or "")
        except Exception:
            return ""

    def send_keys(self, *values: str) -> None:
        # 兼容 Selenium: el.send_keys(Keys.COMMAND, 'a')。
        text = "".join(str(v or "") for v in values)
        lower = text.lower()
        try:
            self.click()
        except Exception:
            pass
        if "\ue03d" in text or "\ue009" in text or "command" in lower or "control" in lower:
            # Selenium Keys.CONTROL/COMMAND 编码可能传入私有区字符；这里按全选处理。
            try:
                self.page.keyboard.press("Meta+A")
            except Exception:
                self.page.keyboard.press("Control+A")
            return
        special_keys = {
            "\ue003": "Backspace",
            "\ue004": "Tab",
            "\ue006": "Enter",
            "\ue007": "Enter",
            "\ue00c": "Escape",
            "\ue017": "Delete",
        }
        if text in special_keys:
            self.page.keyboard.press(special_keys[text])
            return
        try:
            if self.locator is not None:
                try:
                    self.locator.press_sequentially(text, delay=35, timeout=10000)
                except AttributeError:
                    self.locator.type(text, delay=35, timeout=10000)
            else:
                try:
                    self.handle.press_sequentially(text, delay=35)
                except AttributeError:
                    self.handle.type(text, delay=35)
        except Exception:
            self.page.keyboard.type(text, delay=35)

    def get_attribute(self, name: str) -> str | None:
        try:
            if self.locator is not None:
                return self.locator.get_attribute(name, timeout=1000)
            return self.handle.get_attribute(name)
        except Exception:
            return None

    @property
    def text(self) -> str:
        """兼容 Selenium WebElement.text，供共享页面状态/日志逻辑读取按钮文案。"""
        try:
            if self.locator is not None:
                try:
                    return str(self.locator.inner_text(timeout=1000) or "")
                except Exception:
                    return str(self.locator.text_content(timeout=1000) or "")
            return str(self.handle.inner_text() or self.handle.text_content() or "")
        except Exception:
            return ""


class _SwitchTo:
    def __init__(self, driver: "CloakSeleniumDriver"):
        self._driver = driver

    def window(self, handle: str) -> None:
        self._driver._switch_window(handle)


class CloakSeleniumDriver:
    """只实现本项目 Roxy Selenium 流程实际用到的 WebDriver 子集。"""

    def __init__(self, browser: Any, context: Any | None, page: Any, proxy_bridge: Any | None = None):
        self.browser = browser
        self.context = context
        self.page = page
        self.proxy_bridge = proxy_bridge
        self._page_load_timeout_ms = int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90) * 1000
        self.switch_to = _SwitchTo(self)

    @property
    def current_url(self) -> str:
        self._ensure_live_page()
        return str(getattr(self.page, "url", "") or "")

    @property
    def window_handles(self) -> list[str]:
        pages = self._pages()
        return [str(i) for i in range(len(pages))]

    def _pages(self) -> list[Any]:
        try:
            if self.context is not None:
                return list(self.context.pages)
        except Exception:
            pass
        try:
            contexts = list(getattr(self.browser, "contexts", []) or [])
            pages = []
            for ctx in contexts:
                pages.extend(list(getattr(ctx, "pages", []) or []))
            return pages or [self.page]
        except Exception:
            return [self.page]

    @staticmethod
    def _page_is_closed(page: Any) -> bool:
        """兼容 Playwright Page.is_closed；Selenium/Mock 页面按未关闭处理。"""
        try:
            return page.is_closed() is True
        except Exception:
            return False

    def _ensure_live_page(self) -> None:
        """Cloudflare 验证后若旧 Page 被导航关闭，自动切到当前活动页面。"""
        if not self._page_is_closed(self.page):
            return
        pages = [page for page in self._pages() if not self._page_is_closed(page)]
        if not pages:
            return
        self.page = pages[-1]
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def _switch_window(self, handle: str) -> None:
        pages = self._pages()
        idx = int(handle)
        self.page = pages[idx]
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def set_page_load_timeout(self, seconds: int) -> None:
        self._page_load_timeout_ms = int(seconds) * 1000
        try:
            self.page.set_default_navigation_timeout(self._page_load_timeout_ms)
            self.page.set_default_timeout(self._page_load_timeout_ms)
        except Exception:
            pass

    def get(self, url: str) -> None:
        self._ensure_live_page()
        self.page.goto(url, wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def refresh(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def delete_all_cookies(self) -> None:
        """清空当前指纹环境 cookies。"""
        try:
            if self.context is not None:
                self.context.clear_cookies()
        except Exception:
            pass

    def get_cookies(self, urls: list[str] | None = None) -> list[dict]:
        """读取当前指纹环境 cookies，兼容 Playwright Context。"""
        try:
            if self.context is not None:
                return list(self.context.cookies(urls or ["https://chatgpt.com/"]) or [])
        except TypeError:
            try:
                return list(self.context.cookies() or [])
            except Exception:
                pass
        except Exception:
            pass
        return []

    def add_cookie(self, cookie: dict) -> None:
        """向当前指纹环境写入单个 cookie。"""
        if self.context is None:
            raise RuntimeError("CloakBrowser 当前没有可用 browser context")
        self.context.add_cookies([cookie])

    def quit(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            if self.proxy_bridge is not None:
                self.proxy_bridge.close()
                self.proxy_bridge = None
        except Exception:
            pass

    def find_elements(self, by: Any, selector: str) -> list[CloakElement]:
        self._ensure_live_page()
        loc = self._locator(by, selector)
        try:
            count = min(int(loc.count()), 200)
        except Exception:
            count = 0
        return [CloakElement(self.page, loc.nth(i)) for i in range(count)]

    def find_element(self, by: Any, selector: str) -> CloakElement:
        els = self.find_elements(by, selector)
        if not els:
            raise RuntimeError(f"找不到页面元素: {selector}")
        return els[0]

    def _locator(self, by: Any, selector: str):
        by_s = str(by or "").lower()
        if "xpath" in by_s or str(selector).startswith("//"):
            return self.page.locator(f"xpath={selector}")
        return self.page.locator(selector)

    def execute_script(self, script: str, *args: Any) -> Any:
        self._ensure_live_page()
        return self._evaluate(script, args=args, async_mode=False)

    def execute_async_script(self, script: str, *args: Any) -> Any:
        self._ensure_live_page()
        return self._evaluate(script, args=args, async_mode=True)

    def execute_cdp_cmd(self, cmd: str, params: dict | None = None) -> Any:
        params = params or {}
        try:
            client = self.context.new_cdp_session(self.page) if self.context is not None else self.page.context.new_cdp_session(self.page)
            return client.send(cmd, params)
        except Exception as exc:
            logger.debug("[Cloak] CDP 命令失败 %s: %s", cmd, exc)
            return None

    def _serialize_args(self, args: tuple[Any, ...]) -> tuple[CloakElement | None, list[Any]]:
        """拆分 Selenium 脚本参数。

        Playwright 的 JSHandle/ElementHandle 不能可靠地嵌在 dict/list payload 中跨
        page.evaluate 传递；Selenium 脚本最常见模式是 `arguments[0]` 为元素，
        因此这里把第一个 CloakElement 作为真实 DOM `el` 传入，其它参数保持
        JSON 可序列化。
        """
        first_el = args[0] if args and isinstance(args[0], CloakElement) else None
        rest = list(args[1:] if first_el else args)
        cleaned = []
        for item in rest:
            if isinstance(item, CloakElement):
                # 极少数脚本会传多个元素；用真实 handle 直接会在嵌套 payload 中失效，
                # 这里退化为 None，比把错误对象传进 JS 更安全。
                cleaned.append(None)
            else:
                cleaned.append(item)
        return first_el, cleaned

    @staticmethod
    def _unwrap_js_result(page, handle: Any) -> Any:
        try:
            element = handle.as_element()
        except Exception:
            element = None
        if element is not None:
            return CloakElement(page, handle=element)
        try:
            return handle.json_value()
        except Exception as exc:
            msg = str(exc)
            if "Execution context was destroyed" in msg or "navigation" in msg.lower():
                logger.info("[Cloak] JS 执行后页面发生跳转，忽略返回值读取失败：%s", msg[:160])
                return {"ok": True, "reason": "navigation_after_script"}
            raise
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _evaluate(self, script: str, args: tuple[Any, ...], async_mode: bool) -> Any:
        first_el, serial_args = self._serialize_args(args)
        if async_mode:
            wrapper = """async ({script, args}) => {
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            element_wrapper = """async (el, payload) => {
              const args = [el, ...payload.args];
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', payload.script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            if first_el is not None:
                result = first_el._eval(element_wrapper, {"script": script, "args": serial_args})
            else:
                result = self.page.evaluate(wrapper, {"script": script, "args": serial_args})
            if isinstance(result, dict) and result.get("__cloak_timeout"):
                raise TimeoutError("execute_async_script timeout")
            return result

        # Selenium 脚本经常以 `return ...` 为主体；用 Function 保持语义。
        wrapper = """({script, args}) => {
          const fn = new Function(...args.map((_, i) => 'a' + i), script);
          return fn(...args);
        }"""
        element_wrapper = """(el, payload) => {
          const args = [el, ...payload.args];
          const fn = new Function(...args.map((_, i) => 'a' + i), payload.script);
          return fn(...args);
        }"""
        if first_el is not None:
            handle = first_el._eval_handle(element_wrapper, {"script": script, "args": serial_args})
        else:
            handle = self.page.evaluate_handle(wrapper, {"script": script, "args": serial_args})
        return self._unwrap_js_result(self.page, handle)


def _normalize_proxy(proxy: str | None) -> str | None:
    """兼容代理池的四段格式，并转换为 CloakBrowser 接受的 URL。"""
    normalized = normalize_proxy_url(proxy, default_scheme="auto")
    if normalized and normalized.lower().startswith("socks5://"):
        return f"socks5h://{normalized.split('://', 1)[1]}"
    return normalized


def _detect_cloak_exit_geo(proxy_url: str | None = None) -> dict:
    """按当前/代理出口检测地理信息，供 Cloak 显式 locale/timezone 使用。"""
    try:
        import requests
        from config import browser as _browser_cfg
        endpoints = list(getattr(_browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
        timeout = float(getattr(_browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)
    except Exception:
        return {}
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            timezone = data.get("timezone")
            if isinstance(timezone, dict):
                timezone = timezone.get("id") or timezone.get("name")
            geo = {
                "ip": data.get("ip") or data.get("query"),
                "country": (data.get("country") or data.get("country_code") or data.get("countryCode") or "").upper(),
                "region": data.get("region") or data.get("regionName"),
                "city": data.get("city"),
                "timezone": timezone or "",
                "org": data.get("org") or data.get("isp") or (data.get("connection") or {}).get("org"),
            }
            if geo.get("country") or geo.get("timezone"):
                logger.info(
                    "[Cloak] 出口IP地理信息：ip=%s country=%s city=%s timezone=%s",
                    geo.get("ip") or "?", geo.get("country") or "?", geo.get("city") or "?", geo.get("timezone") or "?",
                )
                return geo
        except Exception as exc:
            logger.debug("[Cloak] 出口 IP 地理检测失败 endpoint=%s: %s: %s", url, type(exc).__name__, exc)
    return {}


def _build_cloak_locale_options(proxy_url: str | None = None) -> dict:
    """生成 Cloak/Playwright 双层语言时区配置。"""
    explicit_locale = str(getattr(_cfg, "CLOAK_LOCALE", "") or "").strip()
    explicit_timezone = str(getattr(_cfg, "CLOAK_TIMEZONE", "") or "").strip()
    out = {}
    if explicit_locale:
        out["locale"] = explicit_locale
        # Accept-Language 用 config.browser 自动推断更完整；显式时给一个保守值。
        out["accept_language"] = f"{explicit_locale},{explicit_locale.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7"
    if explicit_timezone:
        out["timezone"] = explicit_timezone
    if explicit_locale and explicit_timezone:
        return out
    try:
        from config import browser as _browser_cfg
        follow_proxy_geo = bool(getattr(_browser_cfg, "AUTO_BROWSER_LOCALE_FROM_IP", True))
    except Exception:
        follow_proxy_geo = True
    if not follow_proxy_geo:
        # 关闭跟随代理时，显式构造自定义地区画像，不能让 Cloak 回退到宿主机语言/时区。
        try:
            from config.browser import build_browser_environment
            profile = build_browser_environment({})
            out.setdefault("locale", str(profile.get("navigator_language") or ""))
            out.setdefault("timezone", str(profile.get("timezone_iana") or ""))
            out.setdefault("accept_language", str(profile.get("accept_language") or ""))
        except Exception as exc:
            logger.debug("[Cloak] 构建自定义地区画像失败：%s: %s", type(exc).__name__, exc)
        return {k: v for k, v in out.items() if v}
    if not bool(getattr(_cfg, "CLOAK_GEOIP", True)):
        return out
    try:
        from config.browser import build_browser_environment
        geo = _detect_cloak_exit_geo(proxy_url)
        profile = build_browser_environment(geo)
        out.setdefault("locale", str(profile.get("navigator_language") or ""))
        out.setdefault("timezone", str(profile.get("timezone_iana") or ""))
        out.setdefault("accept_language", str(profile.get("accept_language") or ""))
        out["geo"] = geo
    except Exception as exc:
        logger.debug("[Cloak] 构建自动语言/时区失败：%s: %s", type(exc).__name__, exc)
    return {k: v for k, v in out.items() if v}


def build_cloak_driver(
    proxy: str | None = None,
    *,
    headless: bool | None = None,
) -> tuple[CloakSeleniumDriver, CloakOpenResult]:
    """启动 CloakBrowser 并返回 Selenium 风格 driver。

    proxy=None  时按 config.proxy.PROXY_POOL 随机抽取；
    proxy=""    时显式禁用代理；
    proxy="..." 时使用指定代理。
    headless=None 使用配置值；显式传 False 可保证登录态植入打开可见窗口。
    """
    if proxy is None and bool(getattr(_cfg, "CLOAK_USE_PROXY", True)):
        try:
            from config.proxy import pick_proxy
            proxy = pick_proxy()
        except Exception:
            proxy = None
    try:
        from cloakbrowser import launch, launch_persistent_context
    except ImportError as exc:
        raise RuntimeError("未安装 cloakbrowser，请执行：pip install cloakbrowser") from exc

    launch_args = [
        str(arg) for arg in (getattr(_cfg, "CLOAK_EXTRA_ARGS", []) or [])
        if not str(arg).startswith("--fingerprint=")
    ]
    seed, user_data_dir = _resolve_cloak_launch_identity()
    if seed:
        launch_args.append(f"--fingerprint={seed}")

    proxy_url = _normalize_proxy(proxy) if bool(getattr(_cfg, "CLOAK_USE_PROXY", True)) else None
    locale_opts = _build_cloak_locale_options(proxy_url)
    browser_proxy_url = proxy_url
    proxy_bridge = None
    if proxy_url:
        from core.socks_bridge import AuthenticatedSocksBridge, needs_authenticated_socks_bridge
        if needs_authenticated_socks_bridge(proxy_url):
            proxy_bridge = AuthenticatedSocksBridge(proxy_url).start()
            browser_proxy_url = proxy_bridge.proxy_url
    # geoip=True 交给 CloakBrowser 根据当前出口 IP 自动匹配 timezone/locale/WebRTC。
    # 之前只有显式 proxy_url 时才开启；如果用户走系统代理/VPN/透明代理，代码层面
    # 看不到 proxy_url，会误关 geoip，导致语言/时区不跟随出口。这里改为完全尊重配置。
    try:
        from config import browser as _browser_cfg
        follow_proxy_geo = bool(getattr(_browser_cfg, "AUTO_BROWSER_LOCALE_FROM_IP", True))
    except Exception:
        follow_proxy_geo = True
    opts = {
        "headless": bool(getattr(_cfg, "CLOAK_HEADLESS", False)) if headless is None else bool(headless),
        "humanize": bool(getattr(_cfg, "CLOAK_HUMANIZE", True)),
        "geoip": bool(getattr(_cfg, "CLOAK_GEOIP", True)) and follow_proxy_geo,
    }
    if locale_opts.get("locale"):
        opts["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        opts["timezone"] = locale_opts["timezone"]
    if browser_proxy_url:
        opts["proxy"] = browser_proxy_url
    if launch_args:
        opts["args"] = launch_args
    license_key = str(getattr(_cfg, "CLOAK_LICENSE_KEY", "") or "").strip()
    if license_key:
        opts["license_key"] = license_key

    randomize_fingerprint = bool(getattr(_cfg, "CLOAK_RANDOMIZE_FINGERPRINT_EACH_LAUNCH", True))
    logger.info(
        "[Cloak] 启动 CloakBrowser：headless=%s humanize=%s geoip=%s proxy=%s locale=%s timezone=%s accept_language=%s random_fingerprint=%s fingerprint_seed_tail=%s persistent=%s",
        opts.get("headless"), opts.get("humanize"), opts.get("geoip"),
        masked_proxy_url(proxy_url) or "无", opts.get("locale") or "自动/默认", opts.get("timezone") or "自动/默认",
        locale_opts.get("accept_language") or "自动/默认", randomize_fingerprint,
        seed[-6:] if seed else "自动/默认", bool(user_data_dir),
    )
    if randomize_fingerprint and str(getattr(_cfg, "CLOAK_USER_DATA_DIR", "") or "").strip():
        logger.info("[Cloak] 每次随机指纹已开启，本次使用临时上下文，不复用配置中的用户目录")
    context_kwargs = {}
    if locale_opts.get("locale"):
        context_kwargs["locale"] = locale_opts["locale"]
    # CloakBrowser 已通过 --fingerprint-timezone 注入时区；再把同一值传给
    # Playwright context 会在当前二进制上触发冲突，导致 Intl 时区回退到宿主机。
    # 仅在启动参数没有时区时使用 context fallback，保持各版本兼容。
    if locale_opts.get("timezone") and not opts.get("timezone"):
        context_kwargs["timezone_id"] = locale_opts["timezone"]
    if locale_opts.get("accept_language"):
        context_kwargs["extra_http_headers"] = {"Accept-Language": locale_opts["accept_language"]}

    try:
        if user_data_dir:
            context = launch_persistent_context(user_data_dir, **opts)
            page = context.new_page()
            browser = getattr(context, "browser", None) or context
            # persistent context 的 locale/timezone 已通过 launch_persistent_context 参数传入。
        else:
            browser = launch(**opts)
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
    except Exception:
        if proxy_bridge is not None:
            proxy_bridge.close()
        raise

    driver = CloakSeleniumDriver(browser=browser, context=context, page=page, proxy_bridge=proxy_bridge)
    driver.upstream_proxy_url = proxy_url
    # Roxy/Cloak 共用部分页面操作函数；给共享函数一个显式日志前缀，
    # 避免 Cloak 注册流程里出现 `[Roxy注册]`。
    driver._registration_log_prefix = "[Cloak注册]"
    driver.set_page_load_timeout(int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90))
    return driver, CloakOpenResult(raw={
        "driver": "cloakbrowser",
        "proxy": masked_proxy_url(proxy_url),
        "proxy_bridge": bool(proxy_bridge),
        "locale": locale_opts,
        "headless_override": headless is not None,
        "random_fingerprint": randomize_fingerprint,
        "fingerprint_seed_tail": seed[-6:] if seed else "",
        "persistent": bool(user_data_dir),
        "options": {k: v for k, v in opts.items() if k not in {"license_key", "proxy"}},
    })
