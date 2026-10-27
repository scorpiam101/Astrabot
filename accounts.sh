#!/usr/bin/env bash
# Manage several WhatsApp numbers on this phone. Every account is fully separate:
# its own number, AI keys, owner number, contacts, settings, session, logs and dashboard port.
#
#   ./accounts.sh list                 show all accounts and their dashboard links
#   ./accounts.sh add <name>           create a new account (then open its dashboard to pair it)
#   ./accounts.sh start <name|all>     start one account (or every account)
#   ./accounts.sh stop <name|all>
#   ./accounts.sh restart <name|all>
#   ./accounts.sh logs <name> [dashboard|bridge|all] [-f]
#   ./accounts.sh remove <name>        stop an account and move it aside (nothing is deleted)
#
# "main" is your original installation. Add --quiet for short output.
set -u
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
unset WAI_ACCOUNT WAI_ENV_FILE WAI_DATA_DIR WAI_AUTH_DIR WAI_LOG_DIR

QUIET=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--quiet" ]; then QUIET=1; else ARGS+=("$a"); fi
done
CMD="${ARGS[0]:-help}"
NAME="${ARGS[1]:-}"
REST=("${ARGS[@]:2}")

say()  { printf '%s\n' "$*"; }
note() { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

PYBIN="$APP_DIR/venv/bin/python"; [ -x "$PYBIN" ] || PYBIN="$(command -v python3 || command -v python || true)"

valid_name() { [[ "$1" =~ ^[a-z0-9][a-z0-9_-]{1,23}$ ]] && [ "$1" != "main" ]; }
acc_dir()    { if [ "$1" = "main" ]; then printf '%s' "$APP_DIR"; else printf '%s' "$APP_DIR/accounts/$1"; fi; }
env_val()    { grep -E "^$2=" "$(acc_dir "$1")/.env" 2>/dev/null | head -n 1 | cut -d= -f2- || true; }

all_names() {
  echo main
  local d
  for d in "$APP_DIR"/accounts/*/; do
    [ -f "${d}.env" ] || continue
    d="${d%/}"; d="${d##*/}"
    valid_name "$d" && echo "$d"
  done
}

exists() { [ "$1" = "main" ] && [ -f "$APP_DIR/.env" ] || { [ "$1" != "main" ] && [ -f "$APP_DIR/accounts/$1/.env" ]; }; }

port_busy() {
  [ -n "$PYBIN" ] || return 1
  "$PYBIN" -c "import socket,sys; s=socket.socket(); s.settimeout(1); r=s.connect_ex(('127.0.0.1', int(sys.argv[1]))); s.close(); sys.exit(0 if r==0 else 1)" "$1" 2>/dev/null
}

port_taken() {
  # true if any account's .env already uses this port, or something is listening on it
  local p="$1" n
  while read -r n; do
    [ "$(env_val "$n" FLASK_PORT)" = "$p" ] && return 0
    [ "$(env_val "$n" BRIDGE_PORT)" = "$p" ] && return 0
  done < <(all_names)
  port_busy "$p"
}

http_json() { curl -fsS --noproxy '*' --max-time "${2:-2}" "$1" 2>/dev/null || true; }

state_of() {
  # prints: running|stopped and the WhatsApp state when known
  local n="$1" port out st
  port="$(env_val "$n" FLASK_PORT)"; port="${port:-5000}"
  out="$(http_json "http://127.0.0.1:$port/api/health" 1)"
  if [ -z "$out" ]; then printf 'stopped'; return; fi
  st="$(http_json "http://127.0.0.1:$port/api/status" 3 | "$PYBIN" -c 'import sys,json
try:
    d=json.load(sys.stdin); print(d.get("whatsapp",{}).get("state","unknown"))
except Exception:
    print("unknown")' 2>/dev/null || echo unknown)"
  printf 'running (WhatsApp: %s)' "${st:-unknown}"
}

run_for() {
  # $1 = account name, $2 = script (start.sh, stop.sh, restart.sh)
  local n="$1" script="$2" out
  if [ "$n" = "main" ]; then
    out="$("$APP_DIR/$script" 2>&1)"; local rc=$?
  else
    out="$(WAI_ACCOUNT="$n" "$APP_DIR/$script" 2>&1)"; local rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$out" | tail -n 8 >&2
    return 1
  fi
  [ "$QUIET" = 1 ] || printf '%s\n' "$out" | sed "s/^/  [$n] /"
  return 0
}

targets() {
  # expands "all" or validates one name
  if [ "$1" = "all" ]; then all_names; return; fi
  { [ "$1" = "main" ] || valid_name "$1"; } || die "Invalid account name: $1"
  exists "$1" || die "There is no account called $1. See: ./accounts.sh list"
  echo "$1"
}

case "$CMD" in
  list|ls|status)
    say "Your WhatsApp accounts:"
    while read -r n; do
      port="$(env_val "$n" FLASK_PORT)"
      owner="$(env_val "$n" OWNER_PHONE)"
      key="no AI key yet"
      { [ -n "$(env_val "$n" GEMINI_API_KEY)" ] || [ -f "$(acc_dir "$n")/data/providers.json" ]; } && key="AI key set"
      printf '  %-12s %-34s http://127.0.0.1:%s   owner: %s   %s\n' "$n" "$(state_of "$n")" "${port:-?}" "${owner:-auto}" "$key"
    done < <(all_names)
    ;;

  add|new|create)
    [ -n "$NAME" ] || die "Usage: ./accounts.sh add <name>   (example: ./accounts.sh add work)"
    valid_name "$NAME" || die "Account names use 2 to 24 lower-case letters, numbers, - or _ . \"main\" is reserved."
    [ ! -e "$APP_DIR/accounts/$NAME" ] || die "An account called $NAME already exists."
    [ -f "$APP_DIR/.env" ] || die "Install the main account first (run the installer)."
    base=5010
    while port_taken "$base" || port_taken "$((base + 1))"; do base=$((base + 10)); [ "$base" -lt 5990 ] || die "No free ports left."; done
    d="$APP_DIR/accounts/$NAME"
    mkdir -p "$d/data/wa_auth" "$d/data/backups" "$d/logs" "$d/run"
    chmod 700 "$APP_DIR/accounts" "$d/data" "$d/data/backups" "$d/data/wa_auth" 2>/dev/null || true
    ( umask 077
      {
        printf 'FLASK_PORT=%s\n' "$base"
        printf 'BRIDGE_PORT=%s\n' "$((base + 1))"
        printf 'BRIDGE_TOKEN=%s\n' "$("$PYBIN" -c 'import secrets; print(secrets.token_hex(24))')"
        printf 'GEMINI_API_KEY=\n'
        printf 'OWNER_PHONE=\n'
      } > "$d/.env" )
    chmod 600 "$d/.env"
    say "Account \"$NAME\" created."
    note "  Dashboard:  http://127.0.0.1:$base"
    note "  Next: ./accounts.sh start $NAME   then open that dashboard, add this account's AI key,"
    note "        and pair its WhatsApp number (WhatsApp tab > Get pairing code)."
    ;;

  start)
    [ -n "$NAME" ] || die "Usage: ./accounts.sh start <name|all>"
    while read -r n; do
      note "Starting $n ..."
      run_for "$n" start.sh || die "Account $n did not start. Try: ./accounts.sh logs $n"
      port="$(env_val "$n" FLASK_PORT)"
      say "Account $n is running: http://127.0.0.1:${port:-5000}"
    done < <(targets "$NAME")
    ;;

  stop)
    [ -n "$NAME" ] || die "Usage: ./accounts.sh stop <name|all>"
    while read -r n; do
      run_for "$n" stop.sh || die "Could not stop $n."
      say "Account $n stopped."
    done < <(targets "$NAME")
    ;;

  restart)
    [ -n "$NAME" ] || die "Usage: ./accounts.sh restart <name|all>"
    while read -r n; do
      run_for "$n" restart.sh || die "Could not restart $n."
      say "Account $n restarted."
    done < <(targets "$NAME")
    ;;

  logs)
    [ -n "$NAME" ] || die "Usage: ./accounts.sh logs <name> [dashboard|bridge|all] [-f]"
    { [ "$NAME" = "main" ] || { valid_name "$NAME" && exists "$NAME"; }; } || die "There is no account called $NAME."
    if [ "$NAME" = "main" ]; then exec "$APP_DIR/logs.sh" "${REST[@]}"; else WAI_ACCOUNT="$NAME" exec "$APP_DIR/logs.sh" "${REST[@]}"; fi
    ;;

  remove|rm|delete)
    [ -n "$NAME" ] || die "Usage: ./accounts.sh remove <name>"
    valid_name "$NAME" || die "Only extra accounts can be removed (not main)."
    exists "$NAME" || die "There is no account called $NAME."
    if [ ! -t 0 ]; then die "Removing needs you to confirm by typing, so run it in Termux directly."; fi
    printf 'This stops "%s" and moves it aside (its chats, keys and WhatsApp session are kept in a folder you can delete later).\nType the account name to confirm: ' "$NAME"
    read -r answer
    [ "$answer" = "$NAME" ] || die "Not confirmed. Nothing was changed."
    run_for "$NAME" stop.sh || true
    dest="$APP_DIR/accounts/.removed-$NAME-$(date +%Y%m%d-%H%M%S)"
    mv "$APP_DIR/accounts/$NAME" "$dest" || die "Could not move the account."
    say "Account $NAME removed. Its files were kept in: $dest"
    say "To erase them for good:  rm -rf \"$dest\""
    ;;

  help|-h|--help|"")
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    ;;

  *)
    die "Unknown command: $CMD. Try ./accounts.sh help"
    ;;
esac
