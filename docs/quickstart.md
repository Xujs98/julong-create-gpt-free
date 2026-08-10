# julong-create-gpt-free 快速启动与使用教程

本教程从拉取项目开始，依次完成系统工具、Python/Node 环境、项目依赖、浏览器工具、配置和 WebUI 启动。

[返回项目首页](../README.md)

## 1. 准备系统工具

需要以下基础环境：

- Git
- Python 3.10 或更高版本
- Node.js 18 或更高版本
- 可正常使用的代理，或已配置好的本地代理端口

检查当前环境：

```bash
git --version
python3 --version
node --version
```

macOS 使用 Homebrew 安装：

```bash
brew install git python node
```

Ubuntu / Debian 安装：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nodejs npm
```

## 2. 拉取项目

```bash
git clone https://github.com/Xujs98/julong-create-gpt-free.git
cd julong-create-gpt-free
```

后续所有命令都在项目根目录执行。

## 3. 创建 Python 环境并安装全部依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

确认关键依赖可以加载：

```bash
python -c "import flask, selenium, playwright, curl_cffi, pyotp; print('Python dependencies OK')"
```

安装 Playwright Chromium 工具：

```bash
python -m playwright install chromium
```

每次重新打开终端后，先进入项目并激活环境：

```bash
cd julong-create-gpt-free
source .venv/bin/activate
```

## 4. 选择注册驱动

项目支持以下注册驱动：

| 驱动 | 额外准备 |
|---|---|
| `roxy` | 安装并启动 RoxyBrowser，准备 API Token、工作区 ID、项目 ID |
| `cloak` | Python 依赖安装后可使用；首次运行会准备 CloakBrowser binary |
| `browser_use` | 准备 Browser Use Cloud API Key |
| `skyvern` | 准备 Skyvern API Key |
| `protocol` | 不启动真实浏览器，依赖当前协议链路和 Sentinel Node 工具 |

默认驱动是 `roxy`。没有配置 RoxyBrowser 时，可在 WebUI「配置」页面或 `.env` 中选择其他已准备好的驱动。

## 5. 创建并填写 `.env`

```bash
cp .env.example .env
```

至少设置 WebUI 登录码：

```dotenv
WEBUI_AUTH_CODE=请替换为自己的登录码
```

常用配置示例：

```dotenv
REGISTRATION_DRIVER=cloak
USE_EMAIL_SERVICE=True
EMAIL_SOURCE=icloud
ENABLE_CREATE_PASSWORD=True
ENABLE_2FA=False
AUTO_BROWSER_LOCALE_FROM_IP=True
```

代理池建议在 WebUI「配置 → 代理」中填写，每行一个代理地址。也可以在 `.env` 使用带换行转义的值：

```dotenv
PROXY_POOL="socks5h://127.0.0.1:7897"
PLAN_CHECK_PROXY_MODE=auto
```

不同邮箱来源还需要填写对应的 API Key、邮箱素材或取码地址。所有 Token、密码和 API Key 都保存在本地 `.env`，不要提交到 Git。

## 6. 启动 WebUI

首次启动前赋予脚本执行权限：

```bash
chmod +x webui.sh
```

后台启动：

```bash
./webui.sh start
```

启动成功后打开：

<http://127.0.0.1:5000>

使用 `.env` 中的 `WEBUI_AUTH_CODE` 登录。

常用管理命令：

```bash
./webui.sh status
./webui.sh logs
./webui.sh restart
./webui.sh stop
```

需要启动后自动打开浏览器：

```bash
OPEN_BROWSER=1 ./webui.sh start
```

也可以前台运行，方便直接观察日志：

```bash
python web.py --open-browser
```

## 7. 首次使用顺序

1. 打开「配置」，选择注册驱动并填写代理、邮箱来源及对应 API Key。
2. 打开「邮箱池」，导入邮箱素材并确认邮箱状态为可用。
3. 回到「注册」，设置数量和线程数，点击「开始注册」。
4. 在任务日志中查看当前步骤和失败原因。
5. 在「账号」页面查看套餐、2FA、Token、Session、Codex、Agent 和查活状态。
6. 使用「查看分组」筛选账号；选中账号后使用「移动」调整分组。

账号搜索支持普通邮箱、Token、来源搜索，也支持联合表达式：

```text
!free
free(可Plus试用)&&[2FA]
free&&![2FA]
[提链]&&[2FA]
```

## 8. 启动失败排查

查看服务状态和日志：

```bash
./webui.sh status
./webui.sh logs
```

检查端口：

```bash
lsof -i :5000
```

重新安装依赖并重启：

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
./webui.sh restart
```

如果浏览器注册无法启动，重点检查：注册驱动、代理连通性、浏览器工具/API Key、邮箱取码配置以及 `logs/webui.log`。
