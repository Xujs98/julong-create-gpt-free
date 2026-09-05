# 部署指南

本项目提供三种运行方式：

| 方式 | 适用场景 | RoxyBrowser |
| --- | --- | --- |
| macOS 本机 | 推荐使用 RoxyBrowser、需要可见浏览器窗口 | 支持 |
| 本地 Python | 已准备好 Python 环境，直接运行 WebUI | 支持 |
| Docker Compose | 服务化运行、隔离依赖、远程/云端浏览器 | 不支持直接连接宿主机 Roxy |

> 所有代码和镜像都以 `codex/long-term-platform-foundation` 分支为准。

## 1. 获取指定分支

```bash
git clone --branch codex/long-term-platform-foundation \
  https://github.com/Xujs98/julong-create-gpt-free.git
cd julong-create-gpt-free
```

已有项目目录更新：

```bash
git fetch origin
git switch codex/long-term-platform-foundation
git pull --ff-only origin codex/long-term-platform-foundation
```

## 2. macOS 一键本地部署（推荐 Roxy）

要求 macOS、网络可用。脚本会检查/安装 Homebrew、Git、Python、Node，创建 `.venv`，安装 Python 依赖和 Playwright Chromium，生成 `.env` 登录码，构建前端并启动 WebUI：

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

### 使用 Docker Hub 镜像

登录 Docker Hub 后拉取并启动：

```bash
docker login
git clone --branch codex/long-term-platform-foundation \
  https://github.com/Xujs98/julong-create-gpt-free.git
cd julong-create-gpt-free
cp .env.example .env
# 编辑 .env
docker compose pull
docker compose up -d --no-build
```

发布电脑从指定分支构建并推送：

```bash
git switch codex/long-term-platform-foundation
git pull --ff-only origin codex/long-term-platform-foundation
docker login
make docker-push
```

指定版本标签：

```bash
make docker-push TAG=v1.0.0
```

其他电脑更新镜像：

```bash
git pull --ff-only origin codex/long-term-platform-foundation
docker compose pull
docker compose up -d --no-build
```

## 5. Docker 与 RoxyBrowser 的边界

标准 Docker Compose 容器内的 `127.0.0.1` 指向容器自身，不是 macOS 宿主机。Roxy 还会返回宿主机的调试地址和 Chromedriver 路径，容器无法直接使用这些宿主机 GUI/驱动资源。因此：

- 使用 Roxy 注册时，运行 `./macos-deploy.sh` 或 `./webui.sh`，不要把注册驱动放在容器里。
- Docker 部署建议选择 `browser_use`、`skyvern` 或 `protocol` 驱动。
- 如果只想让容器访问宿主机 HTTP 服务，可尝试 `ROXY_API_BASE=http://host.docker.internal:50100`，但这不能解决宿主机 Chromedriver/调试地址不可见的问题。

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
git pull --ff-only origin codex/long-term-platform-foundation
docker compose up -d --build --force-recreate
```
