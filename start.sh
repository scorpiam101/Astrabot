#!/usr/bin/env bash
# Starts the dashboard and the WhatsApp bridge, then waits until both respond.
set -u
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

say "Starting WhatsApp AI Assistant..."

[ -f "$ENV_FILE" ] || { bad ".env is missing. Re-run the installer."; exit 1; }
[ -x "$PY" ] || { bad "Python environment missing ($PY). Re-run the installer."; exit 1; }
command -v node >/dev/null 2>&1 || { bad "Node.js not found. Run: pkg install nodejs"; exit 1; }
[ -d "$APP_DIR/bridge/node_modules/@whiskeysockets/baileys" ] || { bad "Baileys is not installed. Re-run the installer."; exit 1; }

if command -v termux-wake-lock >/dev/null 2>&1; then termux-wake-lock >/dev/null 2>&1 || true; fi

start_service dashboard "$FLASK_MARK" "$FLASK_PORT" "$PY" "$FLASK_MARK" || exit 1
wait_ready dashboard "$FLASK_MARK" "$DASH_URL/api/health" 30 || exit 1

start_service bridge "$BRIDGE_MARK" "$BRIDGE_PORT" node "$BRIDGE_MARK" || exit 1
wait_ready bridge "$BRIDGE_MARK" "$BRIDGE_URL/health" 30 || exit 1

say ""
say "Everything is running."
say "Open the dashboard in your phone browser:  $DASH_URL"
if ! grep -qE '^GEMINI_API_KEY=.+' "$ENV_FILE" 2>/dev/null; then
  say "Next: paste your Gemini API key in the dashboard (section 2)."
fi
say "WhatsApp connection state is shown on the dashboard. Run ./status.sh for a summary."
say "Voice notes: Astra can transcribe, translate to English, and reply using your AI provider."
say "WhatsApp commands: open your own chat (Message yourself) and send  #menu  to see all # commands."
say "Leaving? Send #start in your own chat. Back? Send #stop. (Or #away / #back for a summary.)"
