# 部署指南

本项目提供三种运行方式：

| 方式 | 适用场景 | RoxyBrowser |
| --- | --- | --- |
| macOS 本机 | 推荐使用 RoxyBrowser、需要可见浏览器窗口 | 支持 |
| 本地 Python | 已准备好 Python 环境，直接运行 WebUI | 支持 |
| Docker Compose | 服务化运行、隔离依赖，也可通过网关连接宿主机 Roxy | 需要宿主机调试端口可达 |

> 所有代码和镜像都以 `main` 分支为准。

## 1. 获取指定分支

```bash
git clone --branch main \
  https://github.com/Xujs98/julong-create-gpt-free.git
cd julong-create-gpt-free
```

已有项目目录更新：

```bash
git fetch origin
git switch main
git pull --ff-only origin main
```

## 2. macOS 一键本地部署（推荐 Roxy）

要求 macOS、网络可用。无需预先克隆项目时，直接执行下面一条命令。脚本会克隆 `main`、检查/安装 Homebrew、Git、Python、Node，创建 `.venv`，安装 Python 依赖和 Playwright Chromium，生成 `.env` 登录码，构建前端并启动 WebUI：

```bash
curl -fsSL https://raw.githubusercontent.com/Xujs98/julong-create-gpt-free/main/install-macos.sh | bash
```

默认项目目录为 `~/turb-gpt-free-register`。自定义目录：

```bash
curl -fsSL https://raw.githubusercontent.com/Xujs98/julong-create-gpt-free/main/install-macos.sh \
  | INSTALL_DIR="$HOME/Apps/turb-gpt-free-register" bash
```

已经位于项目目录时，也可以直接运行仓库内脚本：

```bash
chmod +x macos-deploy.sh
./macos-deploy.sh
```

启动后访问：`http://127.0.0.1:5000`

查看登录授权码：

```bash
grep -E '^(WEBUI_AUTH_CODE|AUTH_CODE|WEB_AUTH_CODE)=' .env
```

常用命令：

```bash
./macos-deploy.sh update       # 拉取指定分支最新提交、更新依赖并重启
./macos-deploy.sh restart     # 重启 WebUI
./macos-deploy.sh status      # 查看状态
./macos-deploy.sh logs        # 查看实时日志
./macos-deploy.sh stop        # 停止服务
```

不自动打开浏览器、跳过前端构建或 Chromium 安装：

```bash
OPEN_BROWSER=0 SKIP_FRONTEND_BUILD=1 ./macos-deploy.sh
SKIP_PLAYWRIGHT_INSTALL=1 ./macos-deploy.sh
```

首次运行自动生成的 `.env` 只在本机保存，不提交 Git。请按需填写邮箱、代理、Roxy API 等配置。

## 3. 本地 Python 部署（macOS/Linux）

### 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

准备配置并启动：

```bash
cp .env.example .env       # 已有 .env 时跳过
# 编辑 .env，至少设置 WEBUI_AUTH_CODE
./webui.sh start
```

访问 `http://127.0.0.1:5000`。管理命令：

```bash
./webui.sh status
./webui.sh logs
./webui.sh restart
./webui.sh stop
```

前台调试：

```bash
python web.py --host 127.0.0.1 --port 5000 --verbose
```

### 本地使用 RoxyBrowser

Roxy API 默认地址为 `http://127.0.0.1:50100`。在 WebUI「配置 → RoxyBrowser」填写：

- `ROXY_API_BASE`
- `ROXY_API_TOKEN`
- `ROXY_WORKSPACE_ID`
- `ROXY_PROJECT_ID`

RoxyBrowser 应先在本机启动并开启 API。注册驱动选择 `roxy`，并确认 Roxy 返回的 Chrome/Chromedriver 版本可用。

## 4. Docker Compose 部署

Docker Desktop 启动后，在项目根目录执行以下完整流程：

```bash
cp .env.example .env
# 编辑 .env，至少填写 WEBUI_AUTH_CODE
mkdir -p docker-data
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:5000/login >/dev/null && echo 'WebUI OK'
```

访问 `http://127.0.0.1:5000`。如果健康检查未通过，先查看：

```bash
docker compose logs --tail=200 app
docker compose ps
```

### 从源码构建并启动

```bash
cp .env.example .env       # 首次运行
# 编辑 .env，至少设置 WEBUI_AUTH_CODE
mkdir -p docker-data
docker compose up -d --build
docker compose ps
```

访问：`http://127.0.0.1:5000`

查看日志和停止：

```bash
docker compose logs -f app
docker compose down
```

修改宿主机端口：

```bash
APP_PORT=8000 docker compose up -d --build
```

Compose 会把项目根目录 `.env` 挂载到容器 `/app/.env`，运行数据挂载到 `docker-data/`。配置保存功能已兼容 `.env` 文件绑定挂载。

更新到 GitHub `main` 的最新代码并重建：

```bash
git fetch origin
git switch main
git pull --ff-only origin main
docker compose up -d --build --force-recreate
```

备份本地运行数据前先停服务：

```bash
docker compose down
tar -czf "docker-data-$(date +%Y%m%d-%H%M%S).tar.gz" docker-data .env
```

### Docker 调用宿主机 RoxyBrowser

可以通过内网 API 地址访问宿主机 Roxy，例如：

```dotenv
REGISTRATION_DRIVER=roxy
ROXY_API_BASE=http://192.168.31.123:50000
ROXY_API_TOKEN=你的Roxy密钥
ROXY_DEBUGGER_HOST=host.docker.internal
ROXY_DOCKER_WEBDRIVER_URL=http://host.docker.internal:9515
ROXY_OPEN_HEADLESS=True
REGISTRATION_TRAFFIC_MODE=stable
```

`ROXY_API_BASE` 只解决容器访问 Roxy API；`/browser/open` 返回的
`127.0.0.1:<端口>` 还必须由宿主机 Chromedriver 处理。先在 macOS 宿主机启动桥接：

```bash
./tools/roxy-chromedriver-bridge.sh
```

桥接使用 Roxy 自带的 macOS Chromedriver，并监听 `9515`；脚本会自动使用当前 Mac
架构对应的驱动（Intel=`mac-x64`，Apple Silicon=`mac-arm64`）。Docker 通过
`host.docker.internal` 访问它。这样 Roxy、Chrome 和 Chromedriver 始终处于同一
macOS 命名空间，注册页面的指纹和本机部署一致。桥接不可达时，程序只会在
Chrome for Testing 确实提供对应 Linux 架构驱动时尝试容器内回退。Apple 芯片
Docker 使用 `linux-arm64`，而部分旧版 Roxy Chrome（例如 152）没有该下载包，
此时必须修复桥接状态；日志会直接提示运行本脚本和检查
`http://host.docker.internal:9515/status`，不会再把 404 下载错误误判为可用方案。

Docker 的 Roxy 会话同样执行「注册模式」对应的 CDP 请求规则。注册固定从
`chatgpt.com/auth/login` 进入，以保留完整 OAuth/挑战上下文；OAuth 回调进入
ChatGPT 后会停止非必要的首页资源下载，再读取 `/api/auth/session`，核心 Auth、
Sentinel 和挑战请求保持放行。

启动 RoxyBrowser 后重建应用：

```bash
docker compose up -d --build --force-recreate
docker compose logs -f app
```

确认桥接和容器都可达：

```bash
curl http://127.0.0.1:9515/status
docker compose exec app python -c "import requests; print(requests.get('http://host.docker.internal:9515/status', timeout=3).json())"
```

如果 Docker 运行在 Linux 而不是 Docker Desktop，`host.docker.internal` 可能没有自动解析，
请把 `ROXY_DEBUGGER_HOST` 改成宿主机可达且 Roxy 调试端口允许访问的 IP；API 地址仍使用
宿主机内网 IP。Roxy 调试端口必须能从容器网络访问。

### 使用 Docker Hub 镜像

登录 Docker Hub 后拉取并启动：

```bash
docker login
git clone --branch main \
  https://github.com/Xujs98/julong-create-gpt-free.git
cd julong-create-gpt-free
cp .env.example .env
# 编辑 .env
docker compose pull
docker compose up -d --no-build
```

发布电脑从指定分支构建并推送：

```bash
git switch main
git pull --ff-only origin main
docker login
make docker-push
```

指定版本标签：

```bash
make docker-push TAG=v1.0.0
```

其他电脑更新镜像：

```bash
git pull --ff-only origin main
docker compose pull
docker compose up -d --no-build
```

## 5. Docker 与 RoxyBrowser 的边界

标准 Docker Compose 容器内的 `127.0.0.1` 指向容器自身，不是 macOS 宿主机。Roxy 返回的调试地址属于宿主机命名空间。推荐使用 `ROXY_DOCKER_WEBDRIVER_URL` 把原始调试地址交给宿主机 Chromedriver；桥接不可达时仅在 Chrome for Testing 存在匹配架构资源时回退到容器内 Linux Chromedriver。

- Docker Desktop：设置 `ROXY_DEBUGGER_HOST=host.docker.internal`，程序会解析成网关 IP。
- Linux Docker：设置宿主机可达的 `ROXY_DEBUGGER_HOST`，并确认 Roxy 的调试端口允许容器访问。
- Roxy API 可使用宿主机内网地址（例如 `http://192.168.31.123:50000`）；这只负责 API，不等于调试端口已经可达。
- 如不希望开放宿主机 Roxy 调试端口，仍可使用 `browser_use`、`skyvern` 或 `protocol` 驱动。

### 本机与 Docker 的注册行为边界

- 本机 Roxy：Selenium 和 Roxy 位于同一宿主机命名空间，保留 `127.0.0.1:<debug-port>`，继续使用原有 Cloudflare 等待逻辑。
- Docker Roxy：API 从容器访问宿主机；配置桥接时，容器把原始 `127.0.0.1:<调试端口>` 交给宿主机 macOS Chromedriver。未配置桥接时才尝试容器内匹配版本的 Linux Chromedriver；如果 Apple 芯片 Docker 的旧版 Chrome 没有 `linux-arm64` 资源，日志会提示恢复宿主机桥接。
- 本机部署会忽略 `ROXY_DOCKER_WEBDRIVER_URL`，仍按原有本机 Selenium 路径运行。
- 两种模式都会读取项目 `.env`；Docker 不会把 Roxy 指纹搬进容器，实际浏览器和 Profile 仍由宿主机 Roxy 创建。
- Docker 新 Profile/新代理触发的挑战会标记为 `BrowserProxyChallenge`，任务服务隔离当前出口并重建 Profile；本机不会使用这个标记或这套代理隔离分支。
- Auth 返回 `Route Error` 时，Docker 会标记为 `DockerRoxyRouteError` 并换出口；本机保留原有 `AuthRouteError` 错误路径，避免改变本机任务的重试策略。

## 6. 配置、授权码和数据

`.env` 不在 Git 中同步。每台电脑都要单独创建并填写：

```dotenv
WEBUI_AUTH_CODE=自定义登录码
```

没有设置时，WebUI 会在启动日志打印本次临时授权码：

```bash
docker compose logs --no-color app | grep -Ei 'temporary auth code|临时授权码'
```

数据目录：

- 本地运行：项目目录下的运行数据文件
- Docker：`docker-data/` 持久化到宿主机

备份前请先停止服务，并注意账号、Token、Cookie 等敏感数据。

## 7. 常见检查

```bash
git branch --show-current
docker compose ps
curl -I http://127.0.0.1:5000/login
lsof -i :5000
```

如果保存配置报 `Device or resource busy`，请确认使用的是当前分支最新代码，并重建容器：

```bash
git pull --ff-only origin main
docker compose up -d --build --force-recreate
```
