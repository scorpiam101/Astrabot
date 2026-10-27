#!/usr/bin/env bash
# Short status report. Distinguishes: process running vs. responding vs. WhatsApp connected.
set -u
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

say "WhatsApp AI Assistant status"
for spec in "dashboard|$FLASK_MARK|$DASH_URL/api/health|Dashboard" "bridge|$BRIDGE_MARK|$BRIDGE_URL/health|WhatsApp bridge"; do
  IFS='|' read -r name mark url label <<< "$spec"
  if service_running "$name" "$mark"; then
    if http_ok "$url"; then ok "$label: running and responding (pid $(read_pid "$RUN_DIR/$name.pid"))"
    else bad "$label: process exists but does not respond. See: ./logs.sh $name"; fi
  else
    bad "$label: not running. Start it with ./start.sh"
  fi
done

if http_ok "$DASH_URL/api/health"; then
  curl -fsS --noproxy '*' --max-time 5 "$DASH_URL/api/status" 2>/dev/null | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("  [WARN] Could not read dashboard status."); sys.exit(0)
wa = d.get("whatsapp", {})
labels = {
 "connected": "WhatsApp CONNECTED",
 "connecting": "WhatsApp connecting...",
 "awaiting_pairing": "WhatsApp not linked yet - open the dashboard and pair with your phone number",
 "disconnected": "WhatsApp disconnected (retrying automatically)",
 "logged_out": "WhatsApp LOGGED OUT - run ./reset_whatsapp_session.sh then pair again",
 "bridge_down": "WhatsApp bridge is down",
}
print("  [INFO] " + labels.get(wa.get("state"), "WhatsApp state: %s" % wa.get("state")))
if wa.get("last_error"): print("  [INFO] Last WhatsApp note: %s" % wa["last_error"])
print("  [INFO] Gemini API key: %s" % ("configured" if d.get("gemini_configured") else "NOT configured"))
print("  [INFO] Manual approval: %s, bot %s, %s reply(ies) waiting" % ("ON" if d.get("manual_approval") else "OFF (automatic replies)", "enabled" if d.get("bot_enabled") else "DISABLED", d.get("pending_count", 0)))
'
fi
say ""
say "Dashboard: $DASH_URL     Logs: ./logs.sh"
