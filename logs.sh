#!/usr/bin/env bash
# Usage: ./logs.sh [dashboard|bridge|all] [-f]     (-f = follow live, Ctrl+C to stop)
set -u
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
which_log="${1:-all}"; follow="${2:-}"
show() { say "=== $1 ($2) ==="; show_tail "$2" 40; }
case "$which_log" in
  dashboard) f="$LOG_DIR/dashboard.log" ;;
  bridge) f="$LOG_DIR/bridge.log" ;;
  all) show "Dashboard" "$LOG_DIR/dashboard.log"; show "WhatsApp bridge" "$LOG_DIR/bridge.log"; exit 0 ;;
  *) say "Usage: ./logs.sh [dashboard|bridge|all] [-f]"; exit 1 ;;
esac
if [ "$follow" = "-f" ]; then tail -n 20 -f "$f" | sed -u -E 's/AIza[0-9A-Za-z_-]{20,}/[hidden]/g'; else show "$which_log" "$f"; fi
