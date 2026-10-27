# shellcheck shell=bash
# Shared helpers. Sourced by start.sh, stop.sh, status.sh, diagnose.sh, etc.

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Several WhatsApp numbers: WAI_ACCOUNT picks which one a script works on. Empty or "main" is the
# original installation. Any other name lives in accounts/<name>/ with its own .env, database,
# WhatsApp session, AI keys, logs and process IDs. The program code itself is shared.
ACCOUNT="${WAI_ACCOUNT:-main}"
if [ "$ACCOUNT" = "main" ]; then
  ACC_DIR="$APP_DIR"
else
  case "$ACCOUNT" in
    *[!a-z0-9_-]*|-*|_*|"") echo "Invalid account name: $ACCOUNT" >&2; exit 1 ;;
  esac
  ACC_DIR="$APP_DIR/accounts/$ACCOUNT"
  [ -d "$ACC_DIR" ] || { echo "There is no account called $ACCOUNT. Create it with: ./accounts.sh add $ACCOUNT" >&2; exit 1; }
fi
RUN_DIR="$ACC_DIR/run"
LOG_DIR="$ACC_DIR/logs"
DATA_DIR="$ACC_DIR/data"
AUTH_DIR="$DATA_DIR/wa_auth"
ENV_FILE="$ACC_DIR/.env"
export WAI_ACCOUNT="$ACCOUNT" WAI_ENV_FILE="$ENV_FILE" WAI_DATA_DIR="$DATA_DIR" WAI_AUTH_DIR="$AUTH_DIR" WAI_LOG_DIR="$LOG_DIR"
PY="$APP_DIR/venv/bin/python"
FLASK_MARK="$APP_DIR/backend/app.py"
BRIDGE_MARK="$APP_DIR/bridge/bridge.js"

mkdir -p "$RUN_DIR" "$LOG_DIR"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  [ OK ] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; }

env_get() {
  # Prints the value of KEY from .env (never echo this for secrets).
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n 1 | cut -d= -f2- || true
}

FLASK_PORT="$(env_get FLASK_PORT)"; FLASK_PORT="${FLASK_PORT:-5000}"
BRIDGE_PORT="$(env_get BRIDGE_PORT)"; BRIDGE_PORT="${BRIDGE_PORT:-5001}"
DASH_URL="http://127.0.0.1:$FLASK_PORT"
BRIDGE_URL="http://127.0.0.1:$BRIDGE_PORT"

redact() { sed -E 's/AIza[0-9A-Za-z_-]{20,}/[hidden]/g'; }

read_pid() {
  # $1 = pid file. Prints digits only, or nothing.
  if [ -f "$1" ]; then tr -dc '0-9' < "$1" 2>/dev/null || true; fi
}

is_ours() {
  # $1 = pid, $2 = marker text that must appear in the process command line.
  local pid="$1" marker="$2" cmd
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  if [ -r "/proc/$pid/cmdline" ]; then
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cmd" in
      *"$marker"*) return 0 ;;
      *) return 1 ;;
    esac
  fi
  return 0
}

service_running() {
  # $1 = name (dashboard|bridge), $2 = marker. Cleans stale PID files.
  local pf="$RUN_DIR/$1.pid" pid
  pid="$(read_pid "$pf")"
  if [ -n "$pid" ] && is_ours "$pid" "$2"; then return 0; fi
  [ -f "$pf" ] && rm -f "$pf"
  return 1
}

port_in_use() {
  "$PY" -c "import socket,sys; s=socket.socket(); s.settimeout(1); r=s.connect_ex(('127.0.0.1', int(sys.argv[1]))); s.close(); sys.exit(0 if r==0 else 1)" "$1" 2>/dev/null
}

http_ok() { curl -fsS --noproxy '*' --max-time 3 -o /dev/null "$1" 2>/dev/null; }

rotate_log() {
  local f="$1" size
  [ -f "$f" ] || return 0
  size="$(wc -c < "$f" | tr -d ' ')"
  if [ "${size:-0}" -gt 5242880 ]; then mv -f "$f" "$f.1"; fi
}

show_tail() {
  # $1 = log file, $2 = lines
  if [ -f "$1" ]; then tail -n "${2:-20}" "$1" | redact | sed 's/^/    | /'; else say "    (no log file yet: $1)"; fi
}

start_service() {
  # $1 name, $2 marker, $3 port, then the command to run
  local name="$1" marker="$2" port="$3"; shift 3
  local log="$LOG_DIR/$name.log"
  if service_running "$name" "$marker"; then
    ok "$name already running (pid $(read_pid "$RUN_DIR/$name.pid"))"
    return 0
  fi
  if port_in_use "$port"; then
    bad "$name: port $port is already used by another program."
    say "       Close that program, or change the port in $ENV_FILE and run ./restart.sh"
    return 1
  fi
  rotate_log "$log"
  (
    cd "$APP_DIR" || exit 1
    PYTHONUNBUFFERED=1 nohup "$@" >> "$log" 2>&1 &
    echo $! > "$RUN_DIR/$name.pid"
  )
  return 0
}

wait_ready() {
  # $1 name, $2 marker, $3 url, $4 seconds
  local name="$1" marker="$2" url="$3" secs="$4" i=0
  while [ "$i" -lt "$secs" ]; do
    if ! service_running "$name" "$marker"; then
      bad "$name stopped unexpectedly. Last log lines:"
      show_tail "$LOG_DIR/$name.log" 15
      return 1
    fi
    if http_ok "$url"; then ok "$name is responding ($url)"; return 0; fi
    sleep 1; i=$((i + 1))
  done
  bad "$name did not respond within ${secs}s. Last log lines:"
  show_tail "$LOG_DIR/$name.log" 15
  return 1
}

stop_service() {
  # $1 name, $2 marker, $3 label
  local pf="$RUN_DIR/$1.pid" pid i
  if ! service_running "$1" "$2"; then say "  $3: not running"; return 0; fi
  pid="$(read_pid "$pf")"
  kill "$pid" 2>/dev/null || true
  i=0
  while [ "$i" -lt 20 ] && is_ours "$pid" "$2"; do sleep 0.5; i=$((i + 1)); done
  if is_ours "$pid" "$2"; then
    warn "$3 did not stop politely; forcing it (pid $pid only)"
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$pf"
  ok "$3 stopped"
}
