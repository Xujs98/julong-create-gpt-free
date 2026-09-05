#!/usr/bin/env bash
set -euo pipefail

image="${DOCKER_IMAGE:-qq1371446705/turb-gpt-free-register}"
tag="${1:-${DOCKER_TAG:-latest}}"
platform="${DOCKER_PLATFORM:-}"
full_image="${image}:${tag}"
expected_branch="${EXPECTED_GIT_BRANCH:-codex/long-term-platform-foundation}"

current_branch="$(git branch --show-current 2>/dev/null || true)"
if [[ "$current_branch" != "$expected_branch" ]]; then
  echo "当前 Git 分支为 ${current_branch:-<detached>}，本命令只发布 ${expected_branch}。" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 Docker 命令，请先安装并启动 Docker Desktop。" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker 服务未就绪，请先启动 Docker Desktop。" >&2
  exit 1
fi

revision="$(git rev-parse HEAD)"
build_args=(
  build
  --pull
  --tag "$full_image"
  --label "org.opencontainers.image.revision=$revision"
  --label "org.opencontainers.image.version=$tag"
  --label "com.qq1371446705.git.branch=$current_branch"
)
if [[ -n "$platform" ]]; then
  build_args+=(--platform "$platform")
fi
build_args+=(.)

echo "==> 构建镜像：$full_image"
docker "${build_args[@]}"

echo "==> 推送镜像：$full_image"
if ! docker push "$full_image"; then
  echo "推送失败。请确认已执行 docker login，且 Docker Hub 仓库名为 ${image}。" >&2
  exit 1
fi

echo "==> 完成：$full_image"
