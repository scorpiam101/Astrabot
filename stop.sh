#!/usr/bin/env bash
# Stops only the processes started by this project (identified by PID file AND command line).
set -u
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

say "Stopping WhatsApp AI Assistant..."
stop_service bridge "$BRIDGE_MARK" "WhatsApp bridge"
stop_service dashboard "$FLASK_MARK" "Dashboard"
if command -v termux-wake-unlock >/dev/null 2>&1; then termux-wake-unlock >/dev/null 2>&1 || true; fi
say "Your data and WhatsApp session were not touched."
