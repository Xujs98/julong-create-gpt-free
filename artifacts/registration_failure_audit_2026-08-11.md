# 注册失败日志审计（脱敏）

- 生成日期：2026-08-11（当前日期）
- 数据范围：authoritative SQLite 中 17 个注册任务、对应注册日志、15 条账号 2FA 状态，以及既有 2FA 验证记录。
- 时钟说明：日志行出现 2026-08-12 时间戳，而当前日期为 2026-08-11；按任务顺序分析，时间戳应先校准主机时钟。
- 脱敏：邮箱、验证码、TOTP、access token、CSRF、用户/设备标识、IP、代理凭据、完整 URL 查询参数均已删除或抽象化。

## 1. 总结

| 范围 | 总数 | 成功 | 技术失败 | 手动停止 | 备注 |
|---|---:|---:|---:|---:|---|
| 当前注册任务 | 17 | 7 | 9 | 1 | 技术成功率（排除 Job9）7/16=43.75% |
| 批次 12 前端显示 | 10 | 4 | 6 | 含 1 | failed_count 合并 stopped/cancelled |
| 历史备份任务 | 47 | 11 | 19 | 3 stopped + 14 cancelled | 技术成功率（排除停止/取消）11/30=36.67% |
| 账号 2FA 状态 | 15 | 8 | 7 | — | 2FA 为注册后置步骤 |

**批次计数说明：** `core/db.py:_registration_batch_snapshot` 将 `failed`、`stopped`、`cancelled` 汇总到 `failed_count`。因此截图中的“失败 6”包含 Job9 的手动停止。

## 2. 17 个任务逐项结果

| Job | 状态 | 分类 |
|---:|---|---|
| 1 | failed | cloudflare_headless |
| 2 | failed | icloud_otp_timeout |
| 3 | failed | cloudflare_headless_retry |
| 4 | success | registration_success_cloudflare_recovered |
| 5 | success | registration_success_cloudflare_recovered |
| 6 | failed | otp_page_was_cloudflare_challenge |
| 7 | success | registration_success |
| 8 | failed | browser_page_closed |
| 9 | stopped | manual_stop（手动停止，不计技术根因） |
| 10 | failed | email_input_not_ready |
| 11 | failed | login_password_branch_mismatch |
| 12 | failed | navigation_timeout |
| 13 | success | registration_success |
| 14 | success | registration_success_cloudflare_recovered |
| 15 | failed | navigation_timeout |
| 16 | success | registration_success |
| 17 | success | registration_success_post_step_2fa_warning |

## 3. 全部技术失败日志

### 1. Job 1 · initial navigation / Cloudflare challenge

- 日志引用：`JOB_LOG_01`
- 状态：`failed`
- 根因：CloakBrowser 以无头模式打开后进入交互式 Cloudflare 验证；当前策略在 headless=True 时直接结束任务。
- 可修复性：`high`
- 证据摘要：
  - 启动记录 headless=True；随后出现 Cloudflare challenge。
  - 最终错误为“检测到 Cloudflare 人机验证；请关闭无头后重试”。
- 修复建议：
  - 挑战出现时自动切换可见窗口或转入人工/Agent 接管，不直接抛错。
  - 重试时更换出口与浏览器上下文，不复用同一出口和同一 challenge 状态。
  - 把 challenge 结果记录为 challenge_blocked，而不是泛化为普通失败。

### 2. Job 2 · email OTP retrieval

- 日志引用：`JOB_LOG_02`
- 状态：`failed`
- 根因：邮箱 HTML 取码在三轮等待中均未得到新验证码；每轮等待后点击重发，但页面已离开 OTP 页并停在创建密码页错误状态。
- 可修复性：`medium`
- 证据摘要：
  - OTP 第 1/3、2/3、3/3 均记录 iCloud HTML 验证码超时。
  - 重发后记录“页面已离开验证码页”，末尾页面阶段为 create-account/password，标题为通用错误页。
- 修复建议：
  - 重发前保存阶段快照；重发后确认仍处于同一授权会话和 OTP 页面。
  - iCloud 客户端记录 HTTP 状态、响应摘要、最后轮询时间和“无验证码/旧验证码/接口错误”分类。
  - 延长总等待并加入邮箱来源 fallback；连续无邮件时重新建立授权会话，而非在错误页继续轮询。

### 3. Job 3 · retry navigation / Cloudflare challenge

- 日志引用：`JOB_LOG_03`
- 状态：`failed`
- 根因：Job 1 的重试仍使用无头模式，且复用了同一出口，挑战条件未改变。
- 可修复性：`high`
- 证据摘要：
  - 重试记录 headless=True；challenge URL/标题再次出现。
  - 错误与 Job 1 相同。
- 修复建议：
  - 重试策略区分可恢复网络错误与需变更执行环境的 challenge 错误。
  - challenge 重试强制 visible 或 protocol，并轮换出口。

### 4. Job 6 · OTP DOM readiness

- 日志引用：`JOB_LOG_06`
- 状态：`failed`
- 根因：邮箱验证码已取到，但浏览器实际落在 Cloudflare “Just a moment” 空 DOM（/api/accounts/email-otp/send），流程直接查找 OTP 输入框，未先处理挑战页。
- 可修复性：`high`
- 证据摘要：
  - 记录已收到 OTP。
  - 最终状态 inputs=[]，页面标题 Just a moment，正文包含 security verification；随后报 OTP 输入框超时。
- 修复建议：
  - 输入 OTP 前统一执行页面阶段探测：challenge → 等待/接管，login_password → 已注册分类，otp → 输入。
  - 挑战完成后重新读取 DOM 并重新获取当前 OTP；不要在空 DOM 上消费验证码重试次数。

### 5. Job 8 · browser lifecycle

- 日志引用：`JOB_LOG_08`
- 状态：`failed`
- 根因：等待邮箱提交下一阶段期间 Page/Context 被关闭；固定流程随后继续调用 evaluate，触发 TargetClosedError。
- 可修复性：`high`
- 证据摘要：
  - 邮箱提交后等待约 90 秒仍停留登录页。
  - 重填重试时状态包含 TargetClosedError: target page/context/browser has been closed。
- 修复建议：
  - 检测到页面关闭时先尝试切换 context 中的活动页；无活动页则重建浏览器并从授权阶段恢复。
  - 将 browser_closed 作为可重试阶段，限制次数并轮换出口，避免直接把任务判死。
  - 记录关闭来源（用户关闭、进程退出、浏览器崩溃、代理断开）。

### 6. Job 10 · email input discovery

- 日志引用：`JOB_LOG_10`
- 状态：`failed`
- 根因：页面仍在本地化登录入口，但可见 input/actions 为空；固定 DOM selector 和技术属性均未命中，流程未等待下一次渲染或切换入口。
- 可修复性：`high`
- 证据摘要：
  - 等待下一步超时后，页面 URL 为 /auth/login，inputs=[]、actions=[]。
  - 最终错误为找不到邮箱输入框/邮箱入口（未使用文字识别）。
- 修复建议：
  - 将空 DOM 视为 loading/transition 状态，增加 readyState、网络空闲和渲染去抖。
  - 允许稳定的语义/ARIA 入口作为最后 fallback，但排除第三方登录按钮。
  - 同一页超时后刷新一次或重新打开授权 URL，再决定是否重建浏览器。

### 7. Job 11 · password/OTP branch classification

- 日志引用：`JOB_LOG_11`
- 状态：`failed`
- 根因：流程已进入登录密码页并出现密码错误，后续仍按 OTP 页面查找输入框；说明邮箱已注册或密码分支判定失配。
- 可修复性：`high`
- 证据摘要：
  - 最终 URL 为 /log-in/password；password input ariaInvalid=true。
  - 页面错误文本为 Incorrect email address or password；随后报 OTP 输入框超时。
- 修复建议：
  - OTP 输入前把 /log-in/password、ariaInvalid 和密码错误文本归类为 existing_account/password_branch_mismatch。
  - 立即结束该邮箱任务并按邮箱池策略停用/隔离，避免继续等待 OTP。
  - 密码注册分支确认服务端 page_type 后再填密码，禁止从登录密码页回退到注册 OTP。

### 8. Job 12 · initial navigation / network timeout

- 日志引用：`JOB_LOG_12`
- 状态：`failed`
- 根因：CloakBrowser 导航到登录页 90 秒超时；当前入口直接 driver.get，无导航级重试或出口轮换。
- 可修复性：`high`
- 证据摘要：
  - Page.goto Timeout 90000ms exceeded，目标路径为 /auth/login。
  - 页面仍显示本地化登录标题，说明连接/响应未在 domcontentloaded 前完成。
- 修复建议：
  - 导航使用有限重试、指数退避和独立 connect/page timeout。
  - 超时后重建页面或浏览器，并从代理池选择新出口；记录 DNS/TLS/HTTP 阶段。
  - 在注册批次开始前执行代理连通性和目标域名预检。

### 9. Job 15 · initial navigation / network timeout

- 日志引用：`JOB_LOG_15`
- 状态：`failed`
- 根因：登录页导航触发 net::ERR_TIMED_OUT，随后进入 chrome-error 页面。
- 可修复性：`high`
- 证据摘要：
  - Page.goto 报 net::ERR_TIMED_OUT。
  - 失败现场 URL 为 chrome-error://chromewebdata/，标题仍为目标站点。
- 修复建议：
  - 与 Job 12 共用导航重试/换出口机制，并对 chrome-error 页面做明确分类。
  - 失败时保留代理健康结果，避免下一任务继续抽到同一不可用出口。

### Job 9 特别标记

- 状态：`stopped`。日志记录为用户手动停止，属于人工中断，不应归入技术失败根因。

## 4. 2FA 日志审计

- Job 17：注册结果为 `success`，日志中出现后置 2FA 浏览器 async-script timeout；应在账号详情显示“注册成功 / 2FA 待重试”，不应回写为注册失败。
- 当前账号快照：15 条记录中 2FA 成功 8、失败 7。失败分类为 HTTP 422×4、HTTP 403×1、浏览器 async timeout×1、enrollment HTTP 500×1。
- 已确认历史根因：重认证复用旧 OTP（401）、TOTP 临界窗口过期（403 invalid_code）、接口瞬态 429/5xx、浏览器请求超出脚本超时。
- 已有验证样本：修复后邮箱池样本 4/4 成功，最新完整样本 1/1 成功；定向回归 22 passed。

## 5. 代码定位

- `core/cloakbrowser_registration.py`：_run_cloak_registration_impl, _wait_for_cloudflare_challenge call sites, OTP loop, 2FA post-step handling
- `core/roxy_registration.py`：_wait_email_submit_next_state, _submit_email_and_wait_next, _type_otp, _wait_after_email_otp_submit, _click_resend_email_otp, _click_create_password_entry_if_present
- `core/cloakbrowser_driver.py`：CloakSeleniumDriver.get, _ensure_live_page, execute_script/execute_async_script
- `core/icloud_client.py`：fetch_latest_otp
- `core/email_provider.py`：wait_for_otp, resolve_email_source, release_email_if_unconsumed
- `core/registration_service.py`：_should_disable_failed_registration_email, _run_one_job, retry_job
- `core/db.py`：_registration_batch_snapshot
- `main.py`：run_registration, protocol password/OTP branch
- `core/account_export.py`：browser/protocol 2FA enrollment and activation retry paths

## 6. 优先修复顺序

1. **challenge policy**：无头挑战转可见/接管或换协议，减少 Job1/3 类失败。
2. **navigation resilience**：Cloak 导航超时、chrome-error、TargetClosed 进入重建/换出口流程。
3. **OTP stage guard**：挑战页、登录密码页和空 DOM 不再误报 OTP selector 缺失。
4. **mail delivery observability**：区分无邮件、旧码、接口错误和授权会话失效，支持来源 fallback。
5. **branch classification**：已注册邮箱/密码分支失配及时隔离，避免浪费 3 分钟 OTP 等待。
6. **batch metrics**：UI 分开 success、technical_failed、stopped、cancelled，成功率统计更准确。

## 7. 审计结论

当前失败并非单一邮箱验证码问题，主要集中在交互式挑战、导航/浏览器生命周期、OTP 页面阶段判定、邮箱投递可观测性和注册/登录分支识别。先完成前 5 项后，再以排除手动停止、分离 2FA 后置步骤的口径进行 20+ 个任务连续实测，分别统计技术成功率、challenge 命中率、网络重试恢复率和邮箱 OTP 到达率。
