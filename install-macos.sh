#!/usr/bin/env bash
set -Eeuo pipefail

# Bootstrap the repository from any macOS terminal, then hand off to the
# repository-local installer. This file is intentionally usable via curl | bash.

REPO_URL="${REPO_URL:-https://github.com/Xujs98/julong-create-gpt-free.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/turb-gpt-free-register}"

log() {
  printf '[macOS一键部署] %s\n' "$*"
}

die() {
  printf '[macOS一键部署] 错误：%s\n' "$*" >&2
  exit 1
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${1:-}" == "help" ]]; then
  cat <<'HELP'
用法：
  curl -fsSL https://raw.githubusercontent.com/Xujs98/julong-create-gpt-free/main/install-macos.sh | bash

可选环境变量：
  INSTALL_DIR=~/turb-gpt-free-register   项目安装目录
  REPO_BRANCH=main                       Git 分支
  OPEN_BROWSER=0                         启动后不自动打开浏览器
  PORT=5000                              WebUI 端口
HELP
  exit 0
fi

command -v git >/dev/null 2>&1 || die "未找到 git，请先安装 Xcode Command Line Tools。"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  log "更新已有项目：$INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch origin "$REPO_BRANCH"
  git -C "$INSTALL_DIR" switch "$REPO_BRANCH" 2>/dev/null \
    || git -C "$INSTALL_DIR" switch -c "$REPO_BRANCH" --track "origin/$REPO_BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$REPO_BRANCH"
elif [[ -e "$INSTALL_DIR" ]]; then
  die "安装目录已存在但不是 Git 仓库：$INSTALL_DIR"
else
  log "克隆 $REPO_BRANCH 到 $INSTALL_DIR"
  git clone --branch "$REPO_BRANCH" --single-branch "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
exec ./macos-deploy.sh "$@"
