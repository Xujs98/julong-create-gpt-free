# -*- coding: utf-8 -*-
"""
注册后处理模块：
    1. 拉取 /api/auth/session，从中抽取 accessToken / user 信息
    2. 设置 2FA（TOTP），返回 secret
    3. 把账号信息（邮箱 + accessToken + TOTP secret）落盘成 JSON

整体复用注册阶段的 BrowserSession（同一 cookie jar / 同一 IP / 同一 UA），
避免再起新会话被风控关联或缺失登录态。
"""
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
import threading
from urllib.parse import quote, urlencode

import pyotp

from core.session import BrowserSession
from core.humanize import delay as human_delay

logger = logging.getLogger(__name__)

# 输出目录（与项目根 .claude/ 工作区分离，单独放在 accounts/）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_DIR = _PROJECT_ROOT / "accounts"
_BATCH_ARCHIVE_LOCK = threading.RLock()


def _account_material_line(email: str, row: dict | None = None) -> str:
    """输出 邮箱----账号密码----2FA查看器；邮箱池凭据不混入账号密码。"""
    row = row or {}
    parts = [str(row.get("email") or email)]
    password = str(row.get("registration_password") or "").strip()
    secret = str(row.get("totp_secret") or "").strip()
    if password:
        parts.append(password)
    if secret:
        parts.append(f"https://2fa.fb.tools/{quote(secret, safe='')}")
    return "----".join(parts)


def _account_copy_line(material_line: str, access_token: str, totp_secret: str | None = None) -> str:
    """生成包含 token 的整行归档，方便从批次汇总文件里复制。"""
    return f"{material_line}----{access_token}" if access_token else material_line


def create_batch_archive_dir(count: int, workers: int = 1) -> Path:
    """为一次运行创建批次归档目录，例如 accounts/20260509-10个-3线程。"""
    day = datetime.now().strftime("%Y%m%d")
    base_name = f"{day}-{count}个" if workers <= 1 else f"{day}-{count}个-{workers}线程"
    folder = _ACCOUNTS_DIR / base_name
    suffix = 2
    while folder.exists():
        folder = _ACCOUNTS_DIR / f"{base_name}-{suffix}"
        suffix += 1
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "注册成功的邮箱.txt").write_text("", encoding="utf-8")
    (folder / "注册成功的token.txt").write_text("", encoding="utf-8")
    (folder / "注册成功整行.txt").write_text("", encoding="utf-8")
    (folder / "注册成功账号.json").write_text("[]\n", encoding="utf-8")
    return folder


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def _append_batch_archive(
    *,
    row_id: int,
    email: str,
    access_token: str,
    totp_secret: str | None,
    email_source: str | None,
    proxy_used: str | None,
    extra: dict,
    batch_dir: Path | None,
) -> Path:
    """把注册成功账号追加到本次批次目录的 TXT/JSON 文件中。"""
    from core import db

    folder = batch_dir or create_batch_archive_dir(count=1)
    row = db.get_account(row_id) or {}
    folder.mkdir(parents=True, exist_ok=True)
    material_line = _account_material_line(email, row)
    copy_line = _account_copy_line(material_line, access_token, totp_secret)
    archive = {
        "id": row_id,
        "email": email,
        "email_source": email_source,
        "proxy_used": proxy_used,
        "access_token": access_token,
        "totp_secret": totp_secret,
        "material_line": material_line,
        "copy_line": copy_line,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "row": row,
        "extra": extra,
    }

    with _BATCH_ARCHIVE_LOCK:
        _append_line(folder / "注册成功的邮箱.txt", material_line)
        _append_line(folder / "注册成功的token.txt", access_token)
        _append_line(folder / "注册成功整行.txt", copy_line)

        json_path = folder / "注册成功账号.json"
        try:
            rows = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else []
        except Exception:
            rows = []
        if not isinstance(rows, list):
            rows = []
        rows.append(archive)
        json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return folder


def follow_oauth_callback(session: BrowserSession, continue_url: str, referer: str = "https://auth.openai.com/about-you") -> str:
    """
    步骤12.5: 跟随 create_account 返回的 continue_url，完成 OAuth 回调。

    create_account 成功后返回的 continue_url 一般指向
        https://auth.openai.com/authorize/continue?...
    它会再 302 到
        https://chatgpt.com/api/auth/callback/openai?code=...&state=...
    回调请求会让 chatgpt.com 设置 `__Secure-next-auth.session-token` cookie，
    之后 /api/auth/session 才能返回 accessToken。

    Returns:
        重定向链最终落点 URL（一般是 chatgpt.com 站内地址）
    """
    if not continue_url:
        raise ValueError("continue_url 为空，无法完成 OAuth 回调")

    # continue_url 通常是 auth.openai.com/authorize/continue；
    # OTP 后 external_url 分支也可能直接给 chatgpt.com 回调地址。
    # 按目标域名选择导航头，避免 auth step 正确但请求头语义不一致。
    if str(continue_url).startswith("https://chatgpt.com"):
        headers = session.get_chatgpt_navigate_headers(referer=referer)
    else:
        headers = session.get_auth_navigate_headers(referer=referer)

    logger.info(f"[OAuth回调] 跟随 continue_url 完成 OAuth 回调...")
    resp = session.get(continue_url, headers=headers, allow_redirects=True)
    logger.info(f"[OAuth回调] 完成, 最终落点: {resp.url}")
    return resp.url


def fetch_session(session: BrowserSession) -> dict:
    """
    GET https://chatgpt.com/api/auth/session
    注册成功后立刻调用，拿到 accessToken / user / account / expires。

    Returns:
        完整 session JSON，包含字段:
            - accessToken: str (Bearer token, 用于 backend-api 调用)
            - user: {id, name, email, idp, iat, mfa}
            - account: {id, planType, structure, ...}
            - expires: ISO 时间字符串
    """
    url = "https://chatgpt.com/api/auth/session"
    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")

    logger.info("[Session] 拉取 ChatGPT session 信息...")
    resp = session.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("accessToken"):
        logger.error(f"[Session] 响应中没有 accessToken: {data}")
        raise RuntimeError("未拿到 accessToken，登录态可能未建立")

    user = data.get("user") or {}
    account = data.get("account") or {}
    logger.info(
        f"[Session] 成功，user_id={user.get('id')}, email={user.get('email')}, "
        f"plan={account.get('planType')}, mfa={user.get('mfa')}"
    )
    return data


def browser_session_from_driver(
    driver,
    proxy: str | None = None,
    *,
    fingerprint_key: str | None = None,
) -> BrowserSession:
    """把 Playwright/Selenium 浏览器登录态复制到协议会话，供注册后 2FA 使用。"""
    from core.fingerprint_profile import session_fingerprint_kwargs
    session = BrowserSession(
        proxy=proxy,
        detect_exit_geo=False,
        **session_fingerprint_kwargs(fingerprint_key),
    )
    cookies = []
    context = getattr(driver, "context", None)
    page = getattr(driver, "page", None)
    try:
        if context is not None and callable(getattr(context, "cookies", None)):
            cookies = list(context.cookies() or [])
        elif page is not None and callable(getattr(getattr(page, "context", None), "cookies", None)):
            cookies = list(page.context.cookies() or [])
        elif callable(getattr(driver, "get_cookies", None)):
            cookies = list(driver.get_cookies() or [])
    except Exception:
        session.session.close()
        raise
    if not cookies:
        session.session.close()
        raise RuntimeError("浏览器未返回可同步的登录 Cookie")

    for item in cookies:
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if not name:
            continue
        domain = str(item.get("domain") or "")
        path = str(item.get("path") or "/") or "/"
        session.session.cookies.set(name, value, domain=domain, path=path, secure=bool(item.get("secure")))
        if name == "oai-did" and value:
            session.device_id = value

    try:
        env = driver.execute_script(
            "return {userAgent:navigator.userAgent,language:navigator.language,"
            "languages:Array.from(navigator.languages || []),"
            "acceptLanguage:navigator.languages?.join(',') || navigator.language,"
            "platform:navigator.platform,vendor:navigator.vendor,"
            "userAgentData:navigator.userAgentData ? navigator.userAgentData.toJSON() : null,"
            "screenWidth:screen.width,screenHeight:screen.height,"
            "devicePixelRatio:window.devicePixelRatio,"
            "hardwareConcurrency:navigator.hardwareConcurrency,"
            "deviceMemory:navigator.deviceMemory,"
            "timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,"
            "timezoneOffset:new Date().getTimezoneOffset()};"
        ) or {}
        if env.get("userAgent"):
            user_agent = str(env["userAgent"])
            session.browser_profile["user_agent"] = user_agent
            # 让协议层 Chrome 版本与真实浏览器 UA 保持同一主版本/完整版本。
            match = re.search(r"(?:Chrome|Chromium)/([0-9][0-9.]*)", user_agent)
            if match:
                full_version = match.group(1)
                session.browser_profile["chrome_full_version"] = full_version
                session.browser_profile["chrome_major"] = full_version.split(".", 1)[0]
        if env.get("language"):
            session.browser_profile["navigator_language"] = str(env["language"])
        if env.get("languages"):
            session.browser_profile["navigator_languages"] = [str(x) for x in env["languages"] if str(x)]
        if env.get("acceptLanguage"):
            session.browser_profile["accept_language"] = str(env["acceptLanguage"])
        if env.get("platform"):
            session.browser_profile["navigator_platform"] = str(env["platform"])
        if env.get("vendor"):
            session.browser_profile["navigator_vendor"] = str(env["vendor"])
        ua_data = env.get("userAgentData") or {}
        if ua_data.get("platform"):
            session.browser_profile["user_agent_data_platform"] = str(ua_data["platform"])
            session.browser_profile["sec_ch_ua_platform"] = f'"{ua_data["platform"]}"'
        if "mobile" in ua_data:
            session.browser_profile["sec_ch_ua_mobile"] = "?1" if ua_data["mobile"] else "?0"
        if ua_data.get("brands"):
            session.browser_profile["sec_ch_ua"] = ", ".join(
                f'"{item.get("brand", "")}";v="{item.get("version", "")}"'
                for item in ua_data["brands"]
                if item.get("brand") and item.get("version")
            )
        runtime_map = {
            "screenWidth": "screen_width",
            "screenHeight": "screen_height",
            "devicePixelRatio": "device_pixel_ratio",
            "hardwareConcurrency": "hardware_concurrency",
            "deviceMemory": "device_memory",
            "timezone": "timezone_iana",
            "timezoneOffset": "timezone_offset_minutes",
        }
        for source, target in runtime_map.items():
            if env.get(source) is not None:
                session.browser_profile[target] = env[source]
    except Exception:
        pass
    logger.info("[2FA] 已从浏览器同步登录态：cookies=%s proxy=%s", len(cookies), "已配置" if session.proxy else "直连")
    return session


def _trigger_reauth(session: BrowserSession, email: str) -> str:
    """
    步骤2-3: 发起密码重认证，返回 OpenAI authorize URL。
    重定向链会自动触发邮箱发送一份新的 OTP（用于 2FA 重认证）。
    """
    # 重新拿一次 csrf（旧的可能已过期）
    csrf_url = "https://chatgpt.com/api/auth/csrf"
    csrf_resp = session.get(csrf_url, headers=session.get_nextauth_headers(referer="https://chatgpt.com/"))
    csrf_resp.raise_for_status()
    csrf_token = csrf_resp.json()["csrfToken"]
    logger.info(f"[2FA] 重认证 CSRF: {csrf_token[:20]}...")

    # POST /api/auth/signin/openai 带 reauth 参数
    query = {
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "max_age": "0",
        "ext-oai-did": session.device_id,
    }
    signin_url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query)

    headers = session.get_nextauth_headers(referer="https://chatgpt.com/")
    headers["content-type"] = "application/x-www-form-urlencoded"
    headers["origin"] = "https://chatgpt.com"

    body = urlencode({
        "callbackUrl": "https://chatgpt.com/?action=enable&factor=totp",
        "csrfToken": csrf_token,
        "json": "true",
    })

    logger.info("[2FA] 发起重认证 signin/openai...")
    resp = session.post(signin_url, headers=headers, data=body)
    resp.raise_for_status()
    auth_url = resp.json().get("url")
    if not auth_url:
        raise RuntimeError(f"未拿到 reauth authorize URL: {resp.text}")
    return auth_url


def _follow_reauth(session: BrowserSession, auth_url: str) -> None:
    """
    步骤3: 跟随 authorize URL 触发邮箱 OTP 发送。
    auth.openai.com 会重定向到 /email-verification 页面，期间发送 OTP 邮件。
    """
    headers = session.get_auth_navigate_headers(referer="https://chatgpt.com/")
    logger.info("[2FA] 跟随 authorize URL，触发 OTP 发送...")
    resp = session.get(auth_url, headers=headers, allow_redirects=True)
    logger.info(f"[2FA] 落点 URL: {resp.url}")


def _validate_reauth_otp(session: BrowserSession, code: str) -> str:
    """
    步骤4: 提交邮箱 OTP 验证。
    返回 continue_url（带 code 参数的 callback URL，用于跳回 chatgpt.com）。
    """
    url = "https://auth.openai.com/api/accounts/email-otp/validate"
    headers = session.get_auth_headers(referer="https://auth.openai.com/email-verification")
    body = json.dumps({"code": code})

    logger.info(f"[2FA] 提交重认证 OTP: {code}")
    resp = session.post(url, headers=headers, data=body)
    resp.raise_for_status()
    data = resp.json()
    continue_url = data.get("continue_url")
    if not continue_url:
        raise RuntimeError(f"OTP 验证响应缺少 continue_url: {data}")
    return continue_url


def _exchange_new_token(session: BrowserSession, continue_url: str) -> str:
    """
    步骤5: 跟随 continue_url 完成回调，再次拉 /api/auth/session 拿到新 accessToken
    （此时 token 内嵌的 pwd_auth_time 是新鲜的，2FA enroll 才会接受）。
    """
    headers = session.get_auth_navigate_headers(referer="https://auth.openai.com/email-verification")
    logger.info("[2FA] 跟随 continue_url，刷新 session-token cookie...")
    session.get(continue_url, headers=headers, allow_redirects=True)

    # 拿新的 accessToken
    new_session = fetch_session(session)
    new_token = new_session["accessToken"]
    logger.info(f"[2FA] 新 accessToken（含新鲜 pwd_auth_time）: {new_token[:40]}...")
    return new_token


def _enroll_totp(session: BrowserSession, access_token: str) -> tuple[str, str]:
    """
    步骤6: 注册 TOTP，返回 (secret, session_id)
    """
    url = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()

    body = json.dumps({"factor_type": "totp"})

    logger.info("[2FA] 注册 TOTP...")
    resp = session.post(url, headers=headers, data=body)
    if resp.status_code != 200:
        logger.error(f"[2FA] enroll 失败 {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    secret = data.get("secret")
    session_id = data.get("session_id")
    if not secret or not session_id:
        raise RuntimeError(f"enroll 响应字段缺失: {data}")
    logger.info(f"[2FA] TOTP secret 已获取: {secret[:4]}...{secret[-4:]}")
    return secret, session_id


def _activate_totp(
    session: BrowserSession,
    access_token: str,
    secret: str,
    session_id: str,
) -> bool:
    """
    步骤7: 用 secret 生成 6 位 TOTP 码，激活 2FA。
    """
    url = "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"
    headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers["authorization"] = f"Bearer {access_token}"
    headers["oai-device-id"] = session.device_id
    headers["oai-language"] = session.navigator_language()
    headers["origin"] = "https://chatgpt.com"

    # 2026-08 当前 activate_enrollment 明确要求 factor_type；缺少该字段会
    # 返回 422 body.factor_type Field required。部分旧接口曾拒绝额外字段，
    # 因此保留一次兼容回退，但主请求始终发送完整字段。
    totp = pyotp.TOTP(str(secret).replace(" ", "").strip())
    totp_code = totp.now()

    def _activate(code: str, *, include_factor_type: bool = True):
        payload = {"code": str(code), "session_id": str(session_id)}
        if include_factor_type:
            payload["factor_type"] = "totp"
        return session.post(url, headers=headers, data=json.dumps(payload))

    logger.info(f"[2FA] 激活 enrollment, code={totp_code}")
    include_factor_type = True
    resp = _activate(totp_code, include_factor_type=include_factor_type)
    try:
        error_text = str(resp.text or "").lower()
    except Exception:
        error_text = ""

    # 兼容曾经把 factor_type 视为额外字段的旧接口版本。
    if resp.status_code in {400, 422} and (
        "factor_type" in error_text
        and any(word in error_text for word in ("extra", "unexpected", "unknown", "not permitted", "not allowed"))
    ):
        include_factor_type = False
        logger.info("[2FA] 当前接口拒绝 factor_type，使用旧版最小字段重试")
        resp = _activate(totp_code, include_factor_type=include_factor_type)

    # 30 秒窗口边界附近生成的验证码可能在请求抵达时刚好轮换；仅对
    # 明确的 invalid_request 重试一次，并重新生成验证码，避免重复提交旧码。
    if resp.status_code == 400:
        try:
            error_text = resp.text.lower()
        except Exception:
            error_text = ""
        if "invalid_request" in error_text or "invalid request" in error_text:
            next_code = totp.now()
            if next_code != totp_code:
                logger.info("[2FA] 激活验证码跨越时间窗口，使用新验证码重试")
                resp = _activate(next_code, include_factor_type=include_factor_type)
    if resp.status_code != 200:
        logger.error(f"[2FA] activate 失败 {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"激活返回 success=false: {data}")
    return True


def setup_2fa(
    session: BrowserSession,
    email: str,
    otp_code: str | None = None,
    previous_otp: str | None = None,
) -> str:
    """
    完整的 2FA 设置流程。
    会触发再发一份邮箱验证码：
        - USE_EMAIL_SERVICE=True 时自动从 Outlook 账号池拉取
        - 否则需要用户手动输入

    Args:
        session: 已完成注册的会话
        email: 账号邮箱（用作 login_hint）
        otp_code: 邮箱验证码（None 则按上述策略获取）
        previous_otp: 注册阶段已经使用过的邮箱验证码；自动取码时必须排除

    Returns:
        TOTP secret（Base32 字符串），可直接用于 pyotp.TOTP() 生成 6 位动态码
    """
    # 用模块属性读，支持 WebUI 热加载
    from config import email as _email_cfg

    logger.info("=" * 60)
    logger.info("开始设置 2FA")
    logger.info("=" * 60)

    # 阶段一：重认证
    reauth_otp_after_ts = time.time()
    auth_url = _trigger_reauth(session, email)
    human_delay("api")
    _follow_reauth(session, auth_url)
    human_delay("navigate")

    if otp_code is None:
        if _email_cfg.USE_EMAIL_SERVICE:
            from core.email_provider import wait_for_otp
            logger.info("[2FA] 自动等待邮箱重认证 OTP...")
            excluded = {str(previous_otp).strip()} if str(previous_otp or "").strip() else set()
            if excluded:
                logger.info("[2FA] 排除注册阶段已使用 OTP，等待邮箱出现新验证码")
            otp_code = wait_for_otp(
                email,
                after_ts=reauth_otp_after_ts,
                exclude_codes=excluded,
            )
        else:
            logger.info("")
            logger.info("[2FA] 请检查邮箱，输入新收到的 6 位验证码")
            otp_code = input(">>> 2FA 验证码: ").strip()

    human_delay("otp_input")
    continue_url = _validate_reauth_otp(session, otp_code)
    human_delay("api")
    new_token = _exchange_new_token(session, continue_url)
    human_delay("api")

    # 阶段二：enroll + activate
    secret, session_id = _enroll_totp(session, new_token)
    human_delay("form")
    _activate_totp(session, new_token, secret, session_id)

    logger.info("=" * 60)
    logger.info(f"✅ 2FA 设置完成! Secret: {secret[:4]}...{secret[-4:]}")
    logger.info("=" * 60)
    return secret


class Browser2FARequestError(RuntimeError):
    """浏览器内 2FA 请求失败，并保留阶段、状态码和响应摘要。"""

    def __init__(self, stage: str, status: int, detail: str):
        self.stage = str(stage or "request")
        self.status = int(status or 0)
        self.detail = str(detail or "")[:700]
        super().__init__(f"{self.stage} HTTP {self.status}: {self.detail}".rstrip())


def _browser_fetch(driver, url: str, *, method: str = "GET", headers: dict | None = None, body: str | None = None) -> dict:
    """在当前指纹浏览器页面内发送请求，复用真实 TLS、Cookie 和浏览器指纹。"""
    result = driver.execute_async_script(
        r"""
        const url = String(arguments[0] || '');
        const method = String(arguments[1] || 'GET');
        const headers = arguments[2] || {};
        const body = arguments[3];
        const done = arguments[arguments.length - 1];
        (async () => {
          try {
            const options = {method, headers, credentials: 'include', redirect: 'follow'};
            if (body !== null && body !== undefined) options.body = String(body);
            const resp = await fetch(url, options);
            const text = await resp.text();
            let data = null;
            try { data = JSON.parse(text); } catch (_) {}
            done({ok: true, status: resp.status, url: resp.url, data, body: text.slice(0, 1200)});
          } catch (e) {
            done({ok: false, status: 0, error: String(e && (e.stack || e.message) || e).slice(0, 700)});
          }
        })();
        """,
        str(url),
        str(method or "GET").upper(),
        dict(headers or {}),
        body,
    ) or {}
    if not result.get("ok"):
        raise Browser2FARequestError("browser_fetch", int(result.get("status") or 0), result.get("error") or "浏览器请求未返回结果")
    return result


def _browser_response_data(result: dict, stage: str) -> dict:
    """校验浏览器请求状态并返回 JSON 对象。"""
    status = int(result.get("status") or 0)
    if not 200 <= status < 300:
        raise Browser2FARequestError(stage, status, result.get("body") or "")
    data = result.get("data")
    if not isinstance(data, dict):
        raise Browser2FARequestError(stage, status, f"响应不是 JSON 对象: {str(result.get('body') or '')[:400]}")
    return data


def _browser_device_id(driver) -> str:
    """读取当前浏览器 oai-did，供 2FA 接口头和重认证参数复用。"""
    try:
        cookies = driver.get_cookies(["https://chatgpt.com/", "https://auth.openai.com/"])
    except TypeError:
        cookies = driver.get_cookies()
    except Exception:
        cookies = []
    for item in cookies or []:
        if str(item.get("name") or "") == "oai-did" and item.get("value"):
            return str(item["value"])
    return ""


def _browser_session_info(driver) -> dict:
    """直接从当前 ChatGPT 页面读取登录 Session，避免切换到协议指纹。"""
    result = _browser_fetch(
        driver,
        "/api/auth/session",
        headers={"accept": "application/json", "cache-control": "no-cache", "pragma": "no-cache"},
    )
    data = _browser_response_data(result, "session")
    if not data.get("accessToken"):
        raise Browser2FARequestError("session", int(result.get("status") or 0), "响应缺少 accessToken")
    return data


def _browser_enroll_totp(driver, access_token: str) -> tuple[str, str]:
    """使用当前浏览器网络栈注册 TOTP enrollment。"""
    device_id = _browser_device_id(driver)
    language = str(driver.execute_script("return navigator.language || 'en-US';") or "en-US")
    result = _browser_fetch(
        driver,
        "/backend-api/accounts/mfa/enroll",
        method="POST",
        headers={
            "accept": "application/json",
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
            "oai-device-id": device_id,
            "oai-language": language,
        },
        body=json.dumps({"factor_type": "totp"}),
    )
    data = _browser_response_data(result, "enroll")
    secret = str(data.get("secret") or "").strip()
    session_id = str(data.get("session_id") or "").strip()
    if not secret or not session_id:
        raise Browser2FARequestError("enroll", int(result.get("status") or 0), f"响应字段缺失: {data}")
    logger.info("[2FA] 浏览器内 TOTP enrollment 已创建：secret=%s...%s", secret[:4], secret[-4:])
    return secret, session_id


def _browser_activate_totp(driver, access_token: str, secret: str, session_id: str) -> None:
    """在当前浏览器中激活 TOTP，并兼容接口字段与验证码窗口变化。"""
    device_id = _browser_device_id(driver)
    language = str(driver.execute_script("return navigator.language || 'en-US';") or "en-US")
    totp = pyotp.TOTP(str(secret).replace(" ", "").strip())

    def _activate(code: str, *, include_factor_type: bool) -> dict:
        payload = {"code": str(code), "session_id": str(session_id)}
        if include_factor_type:
            payload["factor_type"] = "totp"
        return _browser_fetch(
            driver,
            "/backend-api/accounts/mfa/user/activate_enrollment",
            method="POST",
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {access_token}",
                "content-type": "application/json",
                "oai-device-id": device_id,
                "oai-language": language,
            },
            body=json.dumps(payload),
        )

    code = totp.now()
    include_factor_type = True
    result = _activate(code, include_factor_type=include_factor_type)
    error_text = str(result.get("body") or "").lower()
    if int(result.get("status") or 0) in {400, 422} and "factor_type" in error_text and any(
        word in error_text for word in ("extra", "unexpected", "unknown", "not permitted", "not allowed")
    ):
        include_factor_type = False
        logger.info("[2FA] 浏览器内接口拒绝 factor_type，使用最小字段重试")
        result = _activate(code, include_factor_type=include_factor_type)
        error_text = str(result.get("body") or "").lower()

    if int(result.get("status") or 0) == 400 and ("invalid_request" in error_text or "invalid request" in error_text):
        next_code = totp.now()
        if next_code != code:
            logger.info("[2FA] 激活验证码已跨时间窗口，使用新验证码重试")
            result = _activate(next_code, include_factor_type=include_factor_type)

    data = _browser_response_data(result, "activate")
    if not data.get("success"):
        raise Browser2FARequestError("activate", int(result.get("status") or 0), f"响应 success=false: {data}")


def _browser_trigger_reauth(driver, email: str) -> str:
    """在 ChatGPT 页面内创建 2FA 重认证授权地址。"""
    csrf = _browser_response_data(
        _browser_fetch(driver, "/api/auth/csrf", headers={"accept": "application/json"}),
        "reauth_csrf",
    ).get("csrfToken")
    if not csrf:
        raise Browser2FARequestError("reauth_csrf", 200, "响应缺少 csrfToken")
    query = urlencode({
        "connection": "password",
        "login_hint": email,
        "reauth": "password",
        "max_age": "0",
        "ext-oai-did": _browser_device_id(driver),
    })
    body = urlencode({
        "callbackUrl": "https://chatgpt.com/?action=enable&factor=totp",
        "csrfToken": csrf,
        "json": "true",
    })
    data = _browser_response_data(
        _browser_fetch(
            driver,
            f"/api/auth/signin/openai?{query}",
            method="POST",
            headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
            body=body,
        ),
        "reauth_signin",
    )
    auth_url = str(data.get("url") or "").strip()
    if not auth_url:
        raise Browser2FARequestError("reauth_signin", 200, f"响应缺少 url: {data}")
    return auth_url


def _wait_for_browser_host(driver, host: str, timeout: int = 30) -> str:
    """等待浏览器落到指定域名，返回最终 URL。"""
    end = time.time() + max(1, int(timeout or 30))
    current = ""
    while time.time() < end:
        try:
            current = str(driver.current_url or "")
            if (urlparse(current).hostname or "").lower().endswith(host.lower()):
                return current
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"浏览器未在 {timeout}s 内进入 {host}，最后地址: {current[:300]}")


def _browser_reauthenticate(driver, email: str, previous_otp: str | None = None) -> str:
    """完全在指纹浏览器内完成邮箱 OTP 重认证并返回新 access token。"""
    from config import email as _email_cfg

    otp_after_ts = time.time()
    auth_url = _browser_trigger_reauth(driver, email)
    logger.info("[2FA] 浏览器内发起重认证并跳转 Auth 页面")
    driver.get(auth_url)
    _wait_for_browser_host(driver, "auth.openai.com", timeout=30)
    human_delay("navigate")

    if _email_cfg.USE_EMAIL_SERVICE:
        from core.email_provider import wait_for_otp
        excluded = {str(previous_otp).strip()} if str(previous_otp or "").strip() else set()
        logger.info("[2FA] 浏览器内重认证等待新的邮箱验证码")
        otp_code = wait_for_otp(email, after_ts=otp_after_ts, exclude_codes=excluded)
    else:
        logger.info("[2FA] 请检查邮箱，输入新的 6 位重认证验证码")
        otp_code = input(">>> 2FA 验证码: ").strip()

    data = _browser_response_data(
        _browser_fetch(
            driver,
            "/api/accounts/email-otp/validate",
            method="POST",
            headers={"accept": "application/json", "content-type": "application/json"},
            body=json.dumps({"code": otp_code}),
        ),
        "reauth_otp",
    )
    continue_url = str(data.get("continue_url") or "").strip()
    if not continue_url:
        raise Browser2FARequestError("reauth_otp", 200, f"响应缺少 continue_url: {data}")

    logger.info("[2FA] 重认证验证码通过，浏览器跟随 OAuth 回调")
    driver.get(continue_url)
    try:
        _wait_for_browser_host(driver, "chatgpt.com", timeout=30)
    except RuntimeError:
        driver.get("https://chatgpt.com/")
        _wait_for_browser_host(driver, "chatgpt.com", timeout=30)
    return str(_browser_session_info(driver)["accessToken"])


def setup_2fa_from_browser(
    driver,
    email: str,
    proxy: str | None = None,
    previous_otp: str | None = None,
    access_token: str | None = None,
) -> str:
    """复用当前指纹浏览器的网络栈设置 2FA，避免 Cookie 桥接后的 CF 403。"""
    del proxy  # 浏览器已经使用注册阶段的原代理出口，无需另建协议代理会话。
    token = str(access_token or "").strip() or str(_browser_session_info(driver)["accessToken"])
    logger.info("[2FA] 复用当前指纹浏览器网络栈开始设置 TOTP")
    try:
        secret, session_id = _browser_enroll_totp(driver, token)
    except Browser2FARequestError as exc:
        if exc.stage != "enroll" or exc.status not in {401, 403}:
            raise
        logger.info("[2FA] enrollment 要求新鲜认证，转入浏览器内邮箱 OTP 重认证：HTTP %s", exc.status)
        token = _browser_reauthenticate(driver, email, previous_otp=previous_otp)
        secret, session_id = _browser_enroll_totp(driver, token)
    _browser_activate_totp(driver, token, secret, session_id)
    logger.info("[2FA] 浏览器内 TOTP 设置完成：secret=%s...%s", secret[:4], secret[-4:])
    return secret


def save_account_data(
    email: str,
    access_token: str,
    totp_secret: str | None = None,
    extra: dict | None = None,
    output_path: Path | None = None,  # 兼容老接口，已废弃
    email_source: str | None = None,
    proxy_used: str | None = None,
    batch_dir: Path | None = None,
    registration_method: str | None = None,
) -> int:
    """
    将账号信息保存到本地 JSON/TXT 文件存储。
    返回新插入/更新的 row id。
    """
    from core.db import insert_account
    extra = extra or {}
    if not registration_method:
        # 兼容旧调用方：从驱动专属 extra 节点推断注册方式；没有浏览器节点的
        # 历史账号按纯协议处理，后续仍可由数据库字段覆盖。
        explicit = str(extra.get("registration_method") or extra.get("registration_driver") or "").strip()
        if explicit:
            registration_method = explicit
        elif "roxybrowser" in extra:
            registration_method = "roxy"
        elif "cloakbrowser" in extra:
            registration_method = "cloak"
        elif "skyvern" in extra:
            registration_method = "skyvern"
        elif "browser_use" in extra:
            registration_method = "browser_use"
        else:
            registration_method = "protocol"
    user = extra.get("user") or {}
    account = extra.get("account") or {}
    # 从 extra.codex 抽出顶层 codex 状态/错误，方便 WebUI 直接读账号字段
    codex = extra.get("codex") or {}
    codex_status = codex.get("status")  # success / failed / skipped
    codex_error = None
    if codex_status == "failed":
        codex_error = codex.get("message")

    row_id = insert_account(
        email=email,
        access_token=access_token,
        totp_secret=totp_secret,
        user_id=user.get("id"),
        user_name=user.get("name"),
        plan_type=account.get("planType"),
        expires_at=extra.get("expires"),
        device_id=extra.get("device_id"),
        proxy_used=proxy_used,
        email_source=email_source,
        extra=extra,
        codex_status=codex_status,
        codex_error=codex_error,
        registration_method=registration_method,
    )
    batch_folder = _append_batch_archive(
        row_id=row_id,
        email=email,
        access_token=access_token,
        totp_secret=totp_secret,
        email_source=email_source,
        proxy_used=proxy_used,
        extra=extra,
        batch_dir=batch_dir,
    )
    logger.info(f"[Save] 账号已写入 DB, id={row_id}, email={email}")
    logger.info(f"[Save] 批次归档目录: {batch_folder}")
    # session 中的 account.planType 不能说明 Plus 试用资格。账号落库后只负责
    # 入队，由专用线程池异步查询并回写，避免占用注册工作线程。
    try:
        from core.plan_check_service import enqueue_account_plan_check

        queued = enqueue_account_plan_check(
            account_id=row_id,
            email=email,
            access_token=access_token,
            trigger="registration_auto",
        )
        if queued.get("accepted"):
            logger.info(f"[Plan] 注册后自动查询已入队: id={row_id}, email={email}")
        elif queued.get("busy"):
            logger.info(f"[Plan] 账号已有套餐查询，注册流程不重复入队: id={row_id}, email={email}")
        else:
            logger.warning(f"[Plan] 注册后自动查询入队失败（不影响注册结果）: {email}, {queued.get('error')}")
    except Exception as exc:
        logger.warning(
            f"[Plan] 注册后自动查询入队异常（不影响注册结果）: "
            f"{email}, {type(exc).__name__}: {str(exc)[:180]}"
        )
    return row_id
