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
from core.proxy_utils import normalize_proxy_url

logger = logging.getLogger(__name__)

_TWOFA_BROWSER_FETCH_TIMEOUT_MS = 20_000
_TWOFA_BROWSER_SCRIPT_TIMEOUT_SECONDS = 25.0
_TWOFA_ACTIVATE_ATTEMPTS = 3
_TWOFA_REAUTH_OTP_ATTEMPTS = 3
# Browser fetch can spend several seconds in proxy/anti-bot queues.  Keep a
# larger safety margin so the first activation request is not submitted near a
# TOTP boundary and then invalidates the enrollment session.
_TWOFA_MIN_CODE_LIFETIME_SECONDS = 20.0

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


def _capture_proxy_geo(extra: dict, proxy_used: str | None) -> dict:
    """统一保存注册出口 GeoIP，兼容各注册驱动的元数据格式。

    纯协议会话已经在初始化时探测过出口；Roxy/Cloak/远端浏览器则可能只
    返回国家码或启动参数，因此这里先复用已有嵌套结果，缺失时再用真实
    代理地址做一次短 GeoIP 查询。查询失败不影响账号保存。
    """
    from core import db

    try:
        encoded = json.dumps(extra or {}, ensure_ascii=False)
        geo = db._account_proxy_geo({"extra_json": encoded})
    except Exception:
        encoded = "{}"
        geo = {}
    if geo:
        return geo

    proxy_text = str(proxy_used or "").strip()
    # 脱敏地址、provider:country 标记和空代理都没有可请求的出口 URL。
    if not proxy_text or "***" in proxy_text:
        code = db._account_proxy_country_code({
            "proxy_used": proxy_text,
            "extra_json": encoded,
        })
        return {"country_code": code} if code else {}

    # 代理池允许 ``host:port:user:password`` 四段格式。Roxy 会在创建环境时
    # 将它标准化，但旧调用路径可能仍把原始值传到这里；先标准化后再探测，
    # 否则新供应商的出口只能保存主机名，页面就没有 jp/us 地区标记。
    try:
        probe_proxy = normalize_proxy_url(proxy_text, default_scheme="auto") or proxy_text
    except (TypeError, ValueError):
        probe_proxy = proxy_text
    if "://" not in probe_proxy:
        code = db._account_proxy_country_code({
            "proxy_used": proxy_text,
            "extra_json": encoded,
        })
        return {"country_code": code} if code else {}

    try:
        from core.proxy_test import test_proxy

        result = test_proxy(probe_proxy)
        geo = {
            "ip": str(result.get("ip") or "").strip(),
            "country": str(result.get("country") or "").strip(),
            "country_code": str(result.get("country_code") or "").strip().upper(),
            "region": str(result.get("region") or "").strip(),
            "city": str(result.get("city") or "").strip(),
            "timezone": str(result.get("timezone") or "").strip(),
        }
        return {key: value for key, value in geo.items() if value}
    except Exception as exc:
        logger.debug("[Save] 注册代理 GeoIP 记录失败：%s: %s", type(exc).__name__, str(exc)[:180])
        return {}


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
    if resp.status_code != 200:
        error_text = _response_error_text(resp)
        if _is_invalid_email_otp_response(resp.status_code, error_text):
            from core.openai_auth import EmailOtpInvalidError
            raise EmailOtpInvalidError(
                f"2FA 重认证邮箱验证码无效或已过期: status={resp.status_code}, body={str(resp.text or '')[:240]}"
            )
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
    resp = None
    for attempt in range(1, _TWOFA_ACTIVATE_ATTEMPTS + 1):
        resp = session.post(url, headers=headers, data=body)
        if resp.status_code == 200:
            break
        if attempt < _TWOFA_ACTIVATE_ATTEMPTS and resp.status_code in {429, 500, 502, 503, 504}:
            logger.warning("[2FA] enroll 暂时异常 HTTP %s，第 %s/%s 次重试", resp.status_code, attempt + 1, _TWOFA_ACTIVATE_ATTEMPTS)
            time.sleep(min(2.0 * attempt, 5.0))
            continue
        logger.error(f"[2FA] enroll 失败 {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    assert resp is not None
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
    totp_code = _totp_code_with_margin(totp)

    def _activate(code: str, *, include_factor_type: bool = True):
        payload = {"code": str(code), "session_id": str(session_id)}
        if include_factor_type:
            payload["factor_type"] = "totp"
        return session.post(url, headers=headers, data=json.dumps(payload))

    include_factor_type = True
    resp = None
    for attempt in range(1, _TWOFA_ACTIVATE_ATTEMPTS + 1):
        logger.info("[2FA] 激活 enrollment（第 %s/%s 次）", attempt, _TWOFA_ACTIVATE_ATTEMPTS)
        resp = _activate(totp_code, include_factor_type=include_factor_type)
        error_text = _response_error_text(resp)

        # 兼容曾经把 factor_type 视为额外字段的旧接口版本。
        if resp.status_code in {400, 422} and _factor_type_is_rejected(error_text):
            include_factor_type = False
            logger.info("[2FA] 当前接口拒绝 factor_type，使用旧版最小字段重试")
            resp = _activate(totp_code, include_factor_type=include_factor_type)
            error_text = _response_error_text(resp)

        if resp.status_code == 200:
            break
        if attempt < _TWOFA_ACTIVATE_ATTEMPTS and _is_invalid_totp_response(resp.status_code, error_text):
            logger.info("[2FA] 激活验证码已失效，等待下一时间窗口重试")
            totp_code = _totp_code_with_margin(totp, force_next=True)
            continue
        if attempt < _TWOFA_ACTIVATE_ATTEMPTS and resp.status_code in {429, 500, 502, 503, 504}:
            logger.warning("[2FA] 激活接口暂时异常 HTTP %s，短暂等待后重试", resp.status_code)
            time.sleep(min(2.0 * attempt, 5.0))
            totp_code = _totp_code_with_margin(totp)
            continue
        break
    assert resp is not None
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

    from core.openai_auth import EmailOtpInvalidError, send_email_otp

    excluded = {str(previous_otp).strip()} if str(previous_otp or "").strip() else set()
    if excluded:
        logger.info("[2FA] 排除注册阶段已使用 OTP，等待邮箱出现新验证码")
    current_otp = str(otp_code or "").strip() or None
    continue_url = None
    for otp_attempt in range(1, _TWOFA_REAUTH_OTP_ATTEMPTS + 1):
        if current_otp is None:
            try:
                if _email_cfg.USE_EMAIL_SERVICE:
                    from core.email_provider import wait_for_otp
                    logger.info(
                        "[2FA] 自动等待邮箱重认证 OTP（第 %s/%s 次）...",
                        otp_attempt,
                        _TWOFA_REAUTH_OTP_ATTEMPTS,
                    )
                    current_otp = wait_for_otp(
                        email,
                        after_ts=reauth_otp_after_ts,
                        exclude_codes=excluded,
                    )
                else:
                    logger.info("")
                    logger.info(
                        "[2FA] 请检查邮箱，输入新收到的 6 位验证码（第 %s/%s 次）",
                        otp_attempt,
                        _TWOFA_REAUTH_OTP_ATTEMPTS,
                    )
                    current_otp = input(">>> 2FA 验证码: ").strip()
            except Exception as exc:
                if otp_attempt >= _TWOFA_REAUTH_OTP_ATTEMPTS:
                    raise
                logger.warning(
                    "[2FA] 未收到重认证验证码，重新发送后继续等待（下一轮 %s/%s）：%s: %s",
                    otp_attempt + 1,
                    _TWOFA_REAUTH_OTP_ATTEMPTS,
                    type(exc).__name__,
                    str(exc)[:180],
                )
                reauth_otp_after_ts = time.time()
                send_email_otp(session)
                human_delay("api")
                current_otp = None
                continue

        human_delay("otp_input")
        try:
            continue_url = _validate_reauth_otp(session, current_otp)
            break
        except EmailOtpInvalidError as exc:
            excluded.add(str(current_otp))
            if otp_attempt >= _TWOFA_REAUTH_OTP_ATTEMPTS:
                raise
            logger.warning(
                "[2FA] 重认证验证码错误/过期，重新发送并获取新验证码（下一轮 %s/%s）：%s",
                otp_attempt + 1,
                _TWOFA_REAUTH_OTP_ATTEMPTS,
                str(exc)[:180],
            )
            reauth_otp_after_ts = time.time()
            send_email_otp(session)
            human_delay("api")
            current_otp = None

    if not continue_url:
        raise RuntimeError("2FA 重认证邮箱验证码验证未完成")
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


def _response_error_text(response) -> str:
    try:
        return str(response.text or "").lower()
    except Exception:
        return ""


def _factor_type_is_rejected(error_text: str) -> bool:
    return "factor_type" in str(error_text or "").lower() and any(
        word in str(error_text or "").lower()
        for word in ("extra", "unexpected", "unknown", "not permitted", "not allowed")
    )


def _factor_type_is_required(error_text: str) -> bool:
    text = str(error_text or "").lower()
    return "factor_type" in text and any(
        word in text for word in ("required", "missing", "field required")
    )


def _is_invalid_totp_response(status: int, error_text: str) -> bool:
    text = str(error_text or "").lower()
    return int(status or 0) in {400, 403, 422} and any(
        marker in text for marker in ("invalid_code", "invalid code", "invalid_request", "invalid request")
    )


def _is_invalid_email_otp_response(status: int, error_text: str) -> bool:
    text = str(error_text or "").lower()
    return int(status or 0) == 401 or (
        int(status or 0) in {400, 403, 422}
        and any(
            marker in text
            for marker in (
                "invalid_code",
                "invalid code",
                "invalid_otp",
                "invalid otp",
                "expired_code",
                "expired code",
                "code_invalid",
            )
        )
    )


def _totp_code_with_margin(totp, *, force_next: bool = False) -> str:
    """生成至少保留一段提交时间的 TOTP，失败重试时进入下一窗口。"""
    try:
        interval = max(1.0, float(getattr(totp, "interval", 30) or 30))
    except (TypeError, ValueError):
        interval = 30.0
    remaining = interval - (time.time() % interval)
    if force_next or remaining < _TWOFA_MIN_CODE_LIFETIME_SECONDS:
        wait_seconds = remaining + 0.35
        logger.info("[2FA] 等待 %.1fs 进入稳定 TOTP 时间窗口", wait_seconds)
        time.sleep(wait_seconds)
    return str(totp.now())


def _browser_fetch(
    driver,
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    stage: str = "browser_fetch",
    timeout_ms: int = _TWOFA_BROWSER_FETCH_TIMEOUT_MS,
    allow_redirects: bool = True,
) -> dict:
    """在当前指纹浏览器页面内发送请求，复用真实 TLS、Cookie 和浏览器指纹。"""
    # 注册流程可能把 Selenium 异步脚本超时设为 8/12 秒；若小于浏览器
    # 内 AbortController 的 20 秒，驱动会先抛 TimeoutException，无法返回
    # 可重试的阶段信息。2FA 请求期间提升到略大于 fetch 超时的值。
    set_script_timeout = getattr(driver, "set_script_timeout", None)
    if callable(set_script_timeout):
        try:
            set_script_timeout(max(_TWOFA_BROWSER_SCRIPT_TIMEOUT_SECONDS, timeout_ms / 1000.0 + 5.0))
        except Exception:
            logger.debug("[2FA] 设置浏览器异步脚本超时失败", exc_info=True)

    try:
        result = driver.execute_async_script(
            r"""
        const url = String(arguments[0] || '');
        const method = String(arguments[1] || 'GET');
        const headers = arguments[2] || {};
        const body = arguments[3];
        const allowRedirects = arguments[4] !== false;
        const timeoutMs = Math.max(1000, Number(arguments[5] || 20000));
        const done = arguments[arguments.length - 1];
        (async () => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort('twofa_fetch_timeout'), timeoutMs);
          try {
            const options = {method, headers, credentials: 'include', redirect: allowRedirects ? 'follow' : 'error', signal: controller.signal};
            if (body !== null && body !== undefined) options.body = String(body);
            const resp = await fetch(url, options);
            const text = await resp.text();
            let data = null;
            try { data = JSON.parse(text); } catch (_) {}
            clearTimeout(timer);
            done({ok: true, status: resp.status, url: resp.url, data, body: text.slice(0, 1200)});
          } catch (e) {
            clearTimeout(timer);
            const timedOut = e && (e.name === 'AbortError' || String(e).includes('twofa_fetch_timeout'));
            done({ok: false, status: 0, timedOut, error: String(e && (e.stack || e.message) || e).slice(0, 700)});
          }
        })();
        """,
        str(url),
        str(method or "GET").upper(),
        dict(headers or {}),
        body,
        bool(allow_redirects),
        int(timeout_ms),
        ) or {}
    except Exception as exc:
        # Selenium TimeoutException 的具体类来自驱动实现；用类名和消息
        # 归一化，便于激活阶段进入 session 状态确认与重试分支。
        detail = str(exc).strip() or type(exc).__name__
        marker = f"{type(exc).__name__} {detail}".lower()
        if "timeout" in marker or "timed out" in marker:
            raise Browser2FARequestError(stage, 0, f"浏览器异步脚本超时: {detail[:600]}") from exc
        raise
    if not result.get("ok"):
        detail = result.get("error") or (f"浏览器请求超过 {int(timeout_ms)}ms" if result.get("timedOut") else "浏览器请求未返回结果")
        raise Browser2FARequestError(stage, int(result.get("status") or 0), detail)
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
        stage="session_fetch",
    )
    data = _browser_response_data(result, "session")
    if not data.get("accessToken"):
        raise Browser2FARequestError("session", int(result.get("status") or 0), "响应缺少 accessToken")
    return data


def _browser_enroll_totp(driver, access_token: str) -> tuple[str, str]:
    """使用当前浏览器网络栈注册 TOTP enrollment。"""
    device_id = _browser_device_id(driver)
    language = str(driver.execute_script("return navigator.language || 'en-US';") or "en-US")
    result = None
    for attempt in range(1, _TWOFA_ACTIVATE_ATTEMPTS + 1):
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
                "origin": "https://chatgpt.com",
                "referer": "https://chatgpt.com/",
            },
            body=json.dumps({"factor_type": "totp"}),
            stage="enroll_fetch",
        )
        status = int(result.get("status") or 0)
        if 200 <= status < 300:
            break
        if attempt < _TWOFA_ACTIVATE_ATTEMPTS and status in {429, 500, 502, 503, 504}:
            logger.warning("[2FA] enrollment 暂时异常 HTTP %s，第 %s/%s 次重试", status, attempt + 1, _TWOFA_ACTIVATE_ATTEMPTS)
            time.sleep(min(2.0 * attempt, 5.0))
            continue
        break
    assert result is not None
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
                "origin": "https://chatgpt.com",
                "referer": "https://chatgpt.com/",
            },
            body=json.dumps(payload),
            stage="activate_fetch",
        )

    include_factor_type = True
    minimal_payload_tried = False
    code = _totp_code_with_margin(totp)
    for attempt in range(1, _TWOFA_ACTIVATE_ATTEMPTS + 1):
        try:
            result = _activate(code, include_factor_type=include_factor_type)
        except Browser2FARequestError as exc:
            if exc.stage == "activate_fetch" and exc.status == 0 and _browser_mfa_enabled(driver):
                logger.info("[2FA] 激活请求超时，但当前 session 已确认 MFA 开启")
                return
            if attempt < _TWOFA_ACTIVATE_ATTEMPTS and exc.stage == "activate_fetch" and exc.status == 0:
                logger.warning("[2FA] 激活请求超时，第 %s/%s 次重试", attempt + 1, _TWOFA_ACTIVATE_ATTEMPTS)
                code = _totp_code_with_margin(totp, force_next=True)
                continue
            raise

        status = int(result.get("status") or 0)
        error_text = str(result.get("body") or "").lower()
        if status in {400, 422} and _factor_type_is_rejected(error_text):
            include_factor_type = False
            logger.info("[2FA] 浏览器内接口拒绝 factor_type，使用最小字段重试")
            result = _activate(code, include_factor_type=include_factor_type)
            status = int(result.get("status") or 0)
            error_text = str(result.get("body") or "").lower()

        # 部署版本有时只返回通用 invalid_request，而不指出是多余字段。
        # 对同一稳定验证码做一次无 factor_type 兼容请求；若服务端明确要求
        # 字段，则恢复完整 payload，后续按验证码/新 enrollment 逻辑处理。
        if (
            status == 400
            and "invalid_request" in error_text
            and include_factor_type
            and not minimal_payload_tried
        ):
            minimal_payload_tried = True
            logger.info("[2FA] 浏览器内激活返回通用 invalid_request，尝试兼容最小字段")
            fallback = _activate(code, include_factor_type=False)
            fallback_status = int(fallback.get("status") or 0)
            fallback_error = str(fallback.get("body") or "").lower()
            if 200 <= fallback_status < 300:
                data = _browser_response_data(fallback, "activate")
                if data.get("success") or _browser_mfa_enabled(driver):
                    return
            if not _factor_type_is_required(fallback_error):
                include_factor_type = False
                result, status, error_text = fallback, fallback_status, fallback_error

        if 200 <= status < 300:
            data = _browser_response_data(result, "activate")
            if data.get("success"):
                return
            if _browser_mfa_enabled(driver):
                logger.info("[2FA] 激活响应 success=false，但 session 已确认 MFA 开启")
                return
            raise Browser2FARequestError("activate", status, f"响应 success=false: {data}")

        if attempt < _TWOFA_ACTIVATE_ATTEMPTS and _is_invalid_totp_response(status, error_text):
            logger.info("[2FA] 浏览器内激活验证码已失效，等待下一时间窗口重试")
            code = _totp_code_with_margin(totp, force_next=True)
            continue
        if attempt < _TWOFA_ACTIVATE_ATTEMPTS and status in {429, 500, 502, 503, 504}:
            logger.warning("[2FA] 浏览器内激活接口暂时异常 HTTP %s，短暂等待后重试", status)
            time.sleep(min(2.0 * attempt, 5.0))
            code = _totp_code_with_margin(totp)
            continue
        if _browser_mfa_enabled(driver):
            logger.info("[2FA] 激活返回 HTTP %s，但 session 已确认 MFA 开启", status)
            return
        _browser_response_data(result, "activate")


def _browser_mfa_enabled(driver) -> bool:
    """激活请求超时后确认服务端是否已经完成 MFA，避免重复提交 enrollment。"""
    try:
        data = _browser_session_info(driver)
        user = data.get("user") or {}
        return bool(data.get("mfa") or user.get("mfa"))
    except Exception:
        return False


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
            stage="reauth_signin_fetch",
        ),
        "reauth_signin",
    )
    auth_url = str(data.get("url") or "").strip()
    if not auth_url:
        raise Browser2FARequestError("reauth_signin", 200, f"响应缺少 url: {data}")
    return auth_url


def _browser_resend_reauth_otp(driver) -> None:
    """在当前 Auth 页面重新发送 2FA 重认证邮箱验证码。"""
    result = _browser_fetch(
        driver,
        "/api/accounts/email-otp/send",
        headers={
            "accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "cache-control": "no-cache",
            "pragma": "no-cache",
        },
        stage="reauth_otp_resend_fetch",
    )
    status = int(result.get("status") or 0)
    if not 200 <= status < 400:
        raise Browser2FARequestError("reauth_otp_resend", status, result.get("body") or "")
    logger.info("[2FA] 浏览器内重新发送重认证邮箱验证码完成，HTTP %s", status)


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

    excluded = {str(previous_otp).strip()} if str(previous_otp or "").strip() else set()
    otp_code = None
    data = None
    for otp_attempt in range(1, _TWOFA_REAUTH_OTP_ATTEMPTS + 1):
        if otp_code is None:
            try:
                if _email_cfg.USE_EMAIL_SERVICE:
                    from core.email_provider import wait_for_otp
                    logger.info(
                        "[2FA] 浏览器内重认证等待新的邮箱验证码（第 %s/%s 次）",
                        otp_attempt,
                        _TWOFA_REAUTH_OTP_ATTEMPTS,
                    )
                    otp_code = wait_for_otp(email, after_ts=otp_after_ts, exclude_codes=excluded)
                else:
                    logger.info(
                        "[2FA] 请检查邮箱，输入新的 6 位重认证验证码（第 %s/%s 次）",
                        otp_attempt,
                        _TWOFA_REAUTH_OTP_ATTEMPTS,
                    )
                    otp_code = input(">>> 2FA 验证码: ").strip()
            except Exception as exc:
                if otp_attempt >= _TWOFA_REAUTH_OTP_ATTEMPTS:
                    raise
                logger.warning(
                    "[2FA] 浏览器内未收到重认证验证码，重新发送后继续等待（下一轮 %s/%s）：%s: %s",
                    otp_attempt + 1,
                    _TWOFA_REAUTH_OTP_ATTEMPTS,
                    type(exc).__name__,
                    str(exc)[:180],
                )
                otp_after_ts = time.time()
                _browser_resend_reauth_otp(driver)
                human_delay("api")
                otp_code = None
                continue

        try:
            data = _browser_response_data(
                _browser_fetch(
                    driver,
                    "/api/accounts/email-otp/validate",
                    method="POST",
                    headers={"accept": "application/json", "content-type": "application/json"},
                    body=json.dumps({"code": otp_code}),
                    stage="reauth_otp_fetch",
                ),
                "reauth_otp",
            )
            break
        except Browser2FARequestError as exc:
            if not _is_invalid_email_otp_response(exc.status, exc.detail) or otp_attempt >= _TWOFA_REAUTH_OTP_ATTEMPTS:
                raise
            excluded.add(str(otp_code))
            logger.warning(
                "[2FA] 浏览器内重认证验证码错误/过期，重新发送并获取新验证码（下一轮 %s/%s）",
                otp_attempt + 1,
                _TWOFA_REAUTH_OTP_ATTEMPTS,
            )
            otp_after_ts = time.time()
            _browser_resend_reauth_otp(driver)
            human_delay("api")
            otp_code = None

    if not isinstance(data, dict):
        raise Browser2FARequestError("reauth_otp", 0, "重认证邮箱验证码验证未完成")
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
    reauth_done = False
    last_activation_error: Browser2FARequestError | None = None
    # 某些后端在第一次激活验证码过期/格式不匹配后会使 enrollment session
    # 失效；只换 TOTP 时间窗口仍会对同一个 session 连续失败。重新 enroll
    # 一次并从新 secret/session 开始，避免把可恢复错误记成最终 2FA 失败。
    for enrollment_attempt in range(1, 3):
        try:
            secret, session_id = _browser_enroll_totp(driver, token)
        except Browser2FARequestError as exc:
            if exc.stage != "enroll" or exc.status not in {401, 403} or reauth_done:
                raise
            logger.info("[2FA] enrollment 要求新鲜认证，转入浏览器内邮箱 OTP 重认证：HTTP %s", exc.status)
            token = _browser_reauthenticate(driver, email, previous_otp=previous_otp)
            reauth_done = True
            secret, session_id = _browser_enroll_totp(driver, token)
        try:
            _browser_activate_totp(driver, token, secret, session_id)
            last_activation_error = None
            break
        except Browser2FARequestError as exc:
            last_activation_error = exc
            if exc.stage != "activate" or enrollment_attempt >= 2:
                raise
            if not _is_invalid_totp_response(exc.status, exc.detail):
                raise
            logger.warning(
                "[2FA] 当前 enrollment 激活失败，重新创建 enrollment 后重试（%s/2）：HTTP %s",
                enrollment_attempt + 1,
                exc.status,
            )
            human_delay("api")
    if last_activation_error is not None:
        raise last_activation_error
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
    extra = dict(extra or {})
    proxy_geo = _capture_proxy_geo(extra, proxy_used)
    if proxy_geo:
        # 顶层字段便于列表接口/搜索直接使用；extra 中仍保留完整驱动元数据。
        extra["proxy_geo"] = proxy_geo
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
    # 在配置开关开启时入队，由专用线程池异步查询并回写，避免占用注册工作线程。
    try:
        from config import register as register_cfg

        auto_check = bool(getattr(register_cfg, "OAICS_CHECK_AFTER_REGISTRATION", True))
        country_auto_check = bool(getattr(register_cfg, "COUNTRY_QUALIFICATION_CHECK_AFTER_REGISTRATION", False))
    except Exception:
        # 配置模块异常时保留历史默认行为，避免影响注册结果保存。
        auto_check = True
        country_auto_check = False
    try:
        from core.plan_check_service import enqueue_account_plan_check

        queue_kwargs = dict(
            account_id=row_id,
            email=email,
            access_token=access_token,
            trigger="registration_auto",
            check_oaics=auto_check,
        )
        # 保持旧调用方的默认参数形状；仅在开关开启时显式开启国家查询。
        if country_auto_check:
            queue_kwargs["check_country_qualification"] = True
        queued = enqueue_account_plan_check(**queue_kwargs)
        if queued.get("accepted"):
            enabled = []
            if auto_check:
                enabled.append("OAICS")
            if country_auto_check:
                enabled.append("各国资格")
            if enabled:
                logger.info(f"[Plan] 注册后自动查询套餐及{'、'.join(enabled)}已入队: id={row_id}, email={email}")
            else:
                logger.info(f"[Plan] 注册后自动查询套餐已入队，按开关跳过 OAICS/各国资格: id={row_id}, email={email}")
        elif queued.get("busy"):
            logger.info(f"[Plan] 账号已有套餐/OAICS/各国资格查询，注册流程不重复入队: id={row_id}, email={email}")
        else:
            logger.warning(f"[Plan] 注册后自动查询套餐/OAICS/各国资格入队失败（不影响注册结果）: {email}, {queued.get('error')}")
    except Exception as exc:
        logger.warning(
            f"[Plan] 注册后自动查询入队异常（不影响注册结果）: "
            f"{email}, {type(exc).__name__}: {str(exc)[:180]}"
        )
    return row_id
