#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/data/run"
LOG_DIR="$ROOT_DIR/data/logs"
CONDA_ENV="${CONDA_ENV:-aiagent}"
API_URL="${API_URL:-http://127.0.0.1:8765}"
FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:5173}"
SANDBOX_IMAGE="${SANDBOX_IMAGE:-commchatbot-sandbox:0.8.0}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

is_windows_bash() {
  [[ "${OS:-}" == "Windows_NT" ]] || uname -s | grep -qiE 'mingw|msys|cygwin'
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "缺少命令：$1" >&2
    exit 1
  fi
}

pid_file() {
  printf '%s/%s.pid' "$RUN_DIR" "$1"
}

read_pid() {
  local file
  file="$(pid_file "$1")"
  [[ -f "$file" ]] && tr -d '[:space:]' < "$file"
}

discover_existing_pid() {
  local name="$1" found=""
  is_windows_bash || return 1
  command -v powershell.exe >/dev/null 2>&1 || return 1
  case "$name" in
    api)
      found="$(powershell.exe -NoProfile -Command '(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess)' 2>/dev/null | tr -d '\r' | head -n 1)"
      ;;
    frontend)
      found="$(powershell.exe -NoProfile -Command '(Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess)' 2>/dev/null | tr -d '\r' | head -n 1)"
      ;;
    worker)
      found="$(powershell.exe -NoProfile -Command '(Get-CimInstance Win32_Process | Where-Object { $_.Name -like "python*" -and $_.CommandLine -match "-m\s+app\.worker" } | Select-Object -First 1 -ExpandProperty ProcessId)' 2>/dev/null | tr -d '\r' | head -n 1)"
      ;;
  esac
  if [[ "$found" =~ ^[0-9]+$ ]] && process_alive "$found"; then
    echo "$found" > "$(pid_file "$name")"
    printf '%s' "$found"
    return 0
  fi
  return 1
}

process_alive() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 1
  if is_windows_bash; then
    tasklist //FI "PID eq $pid" //NH 2>/dev/null | grep -qE "[[:space:]]$pid[[:space:]]"
  else
    kill -0 "$pid" 2>/dev/null
  fi
}

service_status() {
  local name="$1" pid
  pid="$(read_pid "$name" || true)"
  if ! process_alive "$pid"; then
    pid="$(discover_existing_pid "$name" || true)"
  fi
  if process_alive "$pid"; then
    printf '%-10s running (PID %s)\n' "$name" "$pid"
    return 0
  fi
  printf '%-10s stopped\n' "$name"
  return 1
}

start_process() {
  local name="$1" working_dir="$2" log_file="$3"
  shift 3
  local current_pid
  current_pid="$(read_pid "$name" || true)"
  if ! process_alive "$current_pid"; then
    current_pid="$(discover_existing_pid "$name" || true)"
  fi
  if process_alive "$current_pid"; then
    echo "$name 已运行（PID $current_pid）"
    return
  fi
  rm -f "$(pid_file "$name")"
  (
    cd "$working_dir"
    nohup "$@" >> "$log_file" 2>&1 &
    echo "$!" > "$(pid_file "$name")"
  )
  sleep 1
  local new_pid
  new_pid="$(read_pid "$name" || true)"
  if ! process_alive "$new_pid"; then
    echo "$name 启动失败，请查看 $log_file" >&2
    tail -n 30 "$log_file" 2>/dev/null || true
    exit 1
  fi
  echo "$name 已启动（PID $new_pid）"
}

stop_process() {
  local name="$1" pid
  pid="$(read_pid "$name" || true)"
  if ! process_alive "$pid"; then
    rm -f "$(pid_file "$name")"
    echo "$name 未运行"
    return
  fi
  if is_windows_bash; then
    taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
  else
    kill -TERM "$pid" 2>/dev/null || true
    for _ in {1..20}; do
      process_alive "$pid" || break
      sleep 0.25
    done
    process_alive "$pid" && kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$(pid_file "$name")"
  echo "$name 已停止"
}

wait_for_http() {
  local name="$1" url="$2"
  for _ in {1..30}; do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      echo "$name 健康：$url"
      return 0
    fi
    sleep 1
  done
  echo "$name 未在规定时间内就绪：$url" >&2
  return 1
}

check_infrastructure() {
  require_command docker
  if ! docker info >/dev/null 2>&1; then
    echo "Docker 未运行，请先启动 Docker Desktop。" >&2
    exit 1
  fi
  if ! docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1; then
    echo "未找到沙箱镜像，正在构建 $SANDBOX_IMAGE ..."
    docker build -t "$SANDBOX_IMAGE" "$ROOT_DIR/backend/sandbox"
  fi
}

start_all() {
  require_command conda
  require_command curl
  check_infrastructure

  local npm_command="npm"
  if is_windows_bash && command -v npm.cmd >/dev/null 2>&1; then
    npm_command="npm.cmd"
  else
    require_command npm
  fi

  if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
    echo "首次运行：安装前端依赖..."
    (cd "$ROOT_DIR/frontend" && "$npm_command" install)
  fi

  start_process \
    api "$ROOT_DIR" "$LOG_DIR/api.err.log" \
    conda run --no-capture-output -n "$CONDA_ENV" python -m uvicorn \
      app.main:app --app-dir backend --host 127.0.0.1 --port 8765

  start_process \
    worker "$ROOT_DIR/backend" "$LOG_DIR/worker.err.log" \
    conda run --no-capture-output -n "$CONDA_ENV" python -m app.worker

  start_process \
    frontend "$ROOT_DIR/frontend" "$LOG_DIR/frontend.out.log" \
    "$npm_command" run dev -- --host 127.0.0.1

  wait_for_http API "$API_URL/health/integrations"
  wait_for_http 前端 "$FRONTEND_URL/"
  echo
  curl -fsS "$API_URL/health/integrations" || true
  echo
  echo "前端：$FRONTEND_URL"
  echo "API 文档：$API_URL/docs"
}

stop_all() {
  stop_process frontend
  stop_process worker
  stop_process api
}

show_status() {
  local failed=0
  service_status api || failed=1
  service_status worker || failed=1
  service_status frontend || failed=1
  echo
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 3 "$API_URL/health/integrations" 2>/dev/null || true
    echo
  fi
  return "$failed"
}

case "${1:-start}" in
  start)
    start_all
    ;;
  stop)
    stop_all
    ;;
  restart)
    stop_all
    start_all
    ;;
  status)
    show_status
    ;;
  *)
    echo "用法：./start.sh {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
