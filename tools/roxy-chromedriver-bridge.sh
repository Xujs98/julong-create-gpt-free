#!/usr/bin/env bash
set -Eeuo pipefail

# Expose a host-native Chromedriver so Docker can attach to host Roxy.
# The driver and Roxy then share the same macOS loopback namespace. This is
# intentionally separate from the local Selenium registration path.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${ROXY_DOCKER_WEBDRIVER_PORT:-9515}"
PID_FILE="${ROXY_DOCKER_WEBDRIVER_PID_FILE:-$ROOT_DIR/run/roxy-chromedriver.pid}"
LOG_FILE="${ROXY_DOCKER_WEBDRIVER_LOG_FILE:-$ROOT_DIR/logs/roxy-chromedriver.log}"

log() { printf '[Roxy Docker桥接] %s\n' "$*"; }
die() { printf '[Roxy Docker桥接] 错误：%s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "此桥接脚本需要运行在 macOS 宿主机"
command -v curl >/dev/null 2>&1 || die "需要 curl"

status_ok() {
  curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/status" >/dev/null 2>&1
}

case "${1:-start}" in
  help|-h|--help)
    sed -n '1,12p' "$0"
    printf '\n用法：%s [start|status|stop]\n' "$0"
    exit 0
    ;;
  status)
    if status_ok 2>/dev/null; then
      curl -fsS "http://127.0.0.1:${PORT}/status"
      exit 0
    fi
    die "桥接未运行：http://127.0.0.1:${PORT}"
    ;;
  stop)
    if [[ -f "$PID_FILE" ]]; then
      old_pid="$(<"$PID_FILE")"
      if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
        kill "$old_pid" 2>/dev/null || true
        log "已停止桥接：pid=$old_pid"
      fi
      rm -f "$PID_FILE"
    else
      log "桥接 PID 文件不存在"
    fi
    exit 0
    ;;
  start|'') ;;
  *) die "未知命令：$1；使用 --help 查看用法" ;;
esac

driver="${ROXY_CHROMEDRIVER_PATH:-}"
if [[ -z "$driver" ]]; then
  roxy_root="$HOME/Library/Application Support/RoxyBrowser/chrome-bin"
  if [[ -d "$roxy_root" ]]; then
    while IFS= read -r candidate; do
      [[ -x "$candidate" ]] && driver="$candidate"
    done < <(
      find "$roxy_root" -type f -name chromedriver -perm -111 2>/dev/null \
        | awk -F/ '{print $(NF-1) " " $0}' \
        | sort -n \
        | tail -1 \
        | cut -d' ' -f2-
    )
  fi
fi
[[ -x "$driver" ]] || die "找不到 Chromedriver；设置 ROXY_CHROMEDRIVER_PATH 后重试"

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

if status_ok; then
  log "桥接已运行：http://host.docker.internal:${PORT}"
  exit 0
fi

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(<"$PID_FILE")"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    die "端口 ${PORT} 已被其他进程占用（pid=${old_pid}）"
  fi
  rm -f "$PID_FILE"
fi

log "启动宿主机 Chromedriver：$driver --port=$PORT"
nohup "$driver" \
  "--port=${PORT}" \
  --allowed-ips='' \
  --allowed-origins='*' \
  --log-level=WARNING \
  >>"$LOG_FILE" 2>&1 </dev/null &
pid=$!
printf '%s\n' "$pid" > "$PID_FILE"

for _ in {1..30}; do
  if status_ok; then
    log "桥接启动成功：http://host.docker.internal:${PORT}"
    exit 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    die "Chromedriver 启动失败，请查看 $LOG_FILE"
  fi
  sleep 0.2
done

die "等待 Chromedriver 就绪超时，请查看 $LOG_FILE"
