#!/usr/bin/env bash
set -euo pipefail

app_dir="/app"
runtime_dir="${APP_RUNTIME_DIR:-/app/runtime}"

mkdir -p "$runtime_dir"

persist_path() {
  local relative_path="$1"
  local app_path="$app_dir/$relative_path"
  local runtime_path="$runtime_dir/$relative_path"

  mkdir -p "$(dirname "$runtime_path")"

  if [[ -e "$app_path" && ! -L "$app_path" && ! -e "$runtime_path" ]]; then
    mv "$app_path" "$runtime_path"
  elif [[ -e "$app_path" && ! -L "$app_path" ]]; then
    rm -rf "$app_path"
  fi

  ln -sfn "$runtime_path" "$app_path"
}

for path in \
  data \
  accounts \
  codex_accounts \
  codex_agent_accounts \
  "注册日志"
do
  mkdir -p "$runtime_dir/$path"
  persist_path "$path"
done

for path in \
  "注册成功的邮箱.json" \
  "注册成功的邮箱.txt" \
  "注册成功的token.txt" \
  "注册任务.json" \
  "注册批次日志.json" \
  "用于注册的邮箱.json" \
  "用于注册的邮箱.txt" \
  "用于注册的API邮箱.json" \
  "用于注册的API邮箱.txt" \
  "用于注册的iCloud邮箱.json" \
  "用于注册的iCloud邮箱.txt" \
  "用于注册的域名邮箱.json" \
  "账号分组.json" \
  "codex_导出状态.json"
do
  persist_path "$path"
done

exec "$@"
