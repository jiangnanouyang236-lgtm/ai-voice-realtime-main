#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/run}"
PYTHON_BIN="${PYTHON_BIN:-python}"
CARGO_BIN="${CARGO_BIN:-cargo}"
LOG_MAX_SIZE_MB="${LOG_MAX_SIZE_MB:-100}"
LOG_BACKUPS="${LOG_BACKUPS:-5}"
LOG_ROTATE_INTERVAL_SEC="${LOG_ROTATE_INTERVAL_SEC:-60}"
LOAD_ENV="${LOAD_ENV:-1}"

mkdir -p "$LOG_DIR" "$RUN_DIR"

if [[ "$LOAD_ENV" == "1" && -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

declare -A SERVICE_COMMANDS=(
  [stt]="$PYTHON_BIN stt/stt_grpc_server.py"
  [llm]="$PYTHON_BIN llm/llm_grpc_server.py"
  [tts]="$PYTHON_BIN tts/tts_grpc_server.py"
  [gateway]="$PYTHON_BIN gateway/gateway_server.py"
  [admin]="$PYTHON_BIN admin-ui/backend/app.py"
  [mcp-robot]="$PYTHON_BIN mcp_servers/robot_sse_server.py"
  [mcp-singing]="$PYTHON_BIN mcp_servers/singing_sse_server.py"
  [mcp-utils]="$PYTHON_BIN mcp_servers/utils_sse_server.py"
  [rust-client]="cd rust_client && $CARGO_BIN run --release"
)

DEFAULT_SERVICES=(stt llm tts gateway admin mcp-robot mcp-singing mcp-utils)

usage() {
  cat <<'EOF'
Usage:
  scripts/nohup_service.sh list
  scripts/nohup_service.sh start <service|all>
  scripts/nohup_service.sh stop <service|all>
  scripts/nohup_service.sh restart <service|all>
  scripts/nohup_service.sh status [service|all]
  scripts/nohup_service.sh tail <service> [lines]
  scripts/nohup_service.sh rotate [service|all]
  scripts/nohup_service.sh start-rotator
  scripts/nohup_service.sh stop-rotator

Environment:
  LOG_DIR=/path/to/logs                  default: ./logs
  RUN_DIR=/path/to/pids                  default: ./run
  LOG_MAX_SIZE_MB=100                    rotate threshold
  LOG_BACKUPS=5                          rotated files kept per service
  LOG_ROTATE_INTERVAL_SEC=60             background rotator interval
  LOAD_ENV=1                             source .env before start

Examples:
  scripts/nohup_service.sh start all
  scripts/nohup_service.sh tail gateway
  LOG_MAX_SIZE_MB=50 scripts/nohup_service.sh start-rotator
EOF
}

service_names() {
  local target="${1:-all}"
  if [[ "$target" == "all" ]]; then
    printf '%s\n' "${DEFAULT_SERVICES[@]}"
  else
    printf '%s\n' "$target"
  fi
}

ensure_service() {
  local service="$1"
  if [[ -z "${SERVICE_COMMANDS[$service]:-}" ]]; then
    echo "Unknown service: $service" >&2
    echo "Run: $0 list" >&2
    exit 2
  fi
}

service_log_dir() {
  printf '%s/%s' "$LOG_DIR" "$1"
}

service_log_file() {
  printf '%s/%s.log' "$(service_log_dir "$1")" "$1"
}

service_pid_file() {
  printf '%s/%s.pid' "$RUN_DIR" "$1"
}

rotator_pid_file() {
  printf '%s/log-rotator.pid' "$RUN_DIR"
}

is_running_pid() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

is_running() {
  local pid_file
  pid_file="$(service_pid_file "$1")"
  [[ -f "$pid_file" ]] && is_running_pid "$(cat "$pid_file")"
}

rotate_one() {
  local service="$1"
  ensure_service "$service"

  local log_file max_bytes size backup
  log_file="$(service_log_file "$service")"
  [[ -f "$log_file" ]] || return 0

  max_bytes=$((LOG_MAX_SIZE_MB * 1024 * 1024))
  size="$(wc -c < "$log_file" | tr -d ' ')"
  [[ "$size" -gt "$max_bytes" ]] || return 0

  mkdir -p "$(service_log_dir "$service")"
  backup="$LOG_BACKUPS"
  if [[ "$backup" -le 0 ]]; then
    : > "$log_file"
    echo "[$(date '+%F %T')] truncated $log_file"
    return 0
  fi

  rm -f "$log_file.$backup"
  for ((i = backup - 1; i >= 1; i--)); do
    [[ -f "$log_file.$i" ]] && mv "$log_file.$i" "$log_file.$((i + 1))"
  done
  cp "$log_file" "$log_file.1"
  : > "$log_file"
  echo "[$(date '+%F %T')] rotated $log_file (${size} bytes)"
}

rotate_target() {
  local target="${1:-all}"
  local service
  for service in $(service_names "$target"); do
    rotate_one "$service"
  done
}

start_rotator() {
  local pid_file log_file pid
  pid_file="$(rotator_pid_file)"
  log_file="$LOG_DIR/log-rotator.log"

  if [[ -f "$pid_file" ]] && is_running_pid "$(cat "$pid_file")"; then
    echo "log rotator already running: pid $(cat "$pid_file")"
    return 0
  fi

  mkdir -p "$LOG_DIR" "$RUN_DIR"
  nohup bash -lc '
    set -euo pipefail
    while true; do
      "'"$0"'" rotate all >> "'"$log_file"'" 2>&1 || true
      sleep "'"$LOG_ROTATE_INTERVAL_SEC"'"
    done
  ' >> "$log_file" 2>&1 &
  pid="$!"
  echo "$pid" > "$pid_file"
  echo "started log rotator: pid $pid, log $log_file"
}

stop_rotator() {
  local pid_file pid
  pid_file="$(rotator_pid_file)"
  if [[ ! -f "$pid_file" ]]; then
    echo "log rotator not running"
    return 0
  fi
  pid="$(cat "$pid_file")"
  if is_running_pid "$pid"; then
    kill "$pid"
    echo "stopped log rotator: pid $pid"
  else
    echo "log rotator stale pid: $pid"
  fi
  rm -f "$pid_file"
}

start_one() {
  local service="$1"
  ensure_service "$service"

  if is_running "$service"; then
    echo "$service already running: pid $(cat "$(service_pid_file "$service")")"
    return 0
  fi

  local log_dir log_file pid_file command pid
  log_dir="$(service_log_dir "$service")"
  log_file="$(service_log_file "$service")"
  pid_file="$(service_pid_file "$service")"
  command="${SERVICE_COMMANDS[$service]}"

  mkdir -p "$log_dir" "$RUN_DIR"
  rotate_one "$service"
  {
    echo
    echo "========== $(date '+%F %T') starting $service =========="
    echo "cwd=$ROOT_DIR"
    echo "cmd=$command"
  } >> "$log_file"

  nohup bash -lc "cd '$ROOT_DIR' && exec $command" >> "$log_file" 2>&1 &
  pid="$!"
  echo "$pid" > "$pid_file"
  echo "started $service: pid $pid, log $log_file"
}

stop_one() {
  local service="$1"
  ensure_service "$service"

  local pid_file pid
  pid_file="$(service_pid_file "$service")"
  if [[ ! -f "$pid_file" ]]; then
    echo "$service not running"
    return 0
  fi

  pid="$(cat "$pid_file")"
  if is_running_pid "$pid"; then
    kill "$pid"
    echo "stopped $service: pid $pid"
  else
    echo "$service stale pid: $pid"
  fi
  rm -f "$pid_file"
}

status_one() {
  local service="$1"
  ensure_service "$service"

  local pid_file log_file
  pid_file="$(service_pid_file "$service")"
  log_file="$(service_log_file "$service")"
  if [[ -f "$pid_file" ]] && is_running_pid "$(cat "$pid_file")"; then
    printf '%-12s running pid=%s log=%s\n' "$service" "$(cat "$pid_file")" "$log_file"
  else
    printf '%-12s stopped log=%s\n' "$service" "$log_file"
  fi
}

tail_one() {
  local service="$1"
  local lines="${2:-200}"
  ensure_service "$service"
  mkdir -p "$(service_log_dir "$service")"
  touch "$(service_log_file "$service")"
  tail -n "$lines" -f "$(service_log_file "$service")"
}

list_services() {
  local service
  echo "Available services:"
  for service in "${!SERVICE_COMMANDS[@]}"; do
    printf '  %-12s %s\n' "$service" "${SERVICE_COMMANDS[$service]}"
  done | sort
  echo
  echo "Default all:"
  printf '  %s\n' "${DEFAULT_SERVICES[*]}"
}

main() {
  local action="${1:-}"
  local target="${2:-all}"
  local service

  case "$action" in
    list)
      list_services
      ;;
    start)
      start_rotator
      for service in $(service_names "$target"); do start_one "$service"; done
      ;;
    stop)
      for service in $(service_names "$target"); do stop_one "$service"; done
      ;;
    restart)
      for service in $(service_names "$target"); do stop_one "$service"; done
      for service in $(service_names "$target"); do start_one "$service"; done
      ;;
    status)
      for service in $(service_names "$target"); do status_one "$service"; done
      ;;
    tail)
      [[ "$target" != "all" ]] || { echo "tail requires a service name" >&2; exit 2; }
      tail_one "$target" "${3:-200}"
      ;;
    rotate)
      rotate_target "$target"
      ;;
    start-rotator)
      start_rotator
      ;;
    stop-rotator)
      stop_rotator
      ;;
    help|-h|--help|"")
      usage
      ;;
    *)
      echo "Unknown action: $action" >&2
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
