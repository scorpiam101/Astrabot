#!/usr/bin/env bash
# Usage: ./set_browser.sh ubuntu|macos|safari|windows
# Changes how this linked device presents itself to WhatsApp, then restarts the bridge.
set -u
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"
choice="${1:-}"
case "$choice" in
  ubuntu|macos|safari|windows) ;;
  *) say "Usage: ./set_browser.sh ubuntu|macos|safari|windows"; say "Current: $(env_get WA_BROWSER)"; exit 1 ;;
esac
if grep -qE '^WA_BROWSER=' "$ENV_FILE"; then
  sed -i "s/^WA_BROWSER=.*/WA_BROWSER=$choice/" "$ENV_FILE"
else
  printf 'WA_BROWSER=%s\n' "$choice" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
ok "Browser identity set to: $choice"
say "Now run ./reset_whatsapp_session.sh (type YES), then pair again from the dashboard."
