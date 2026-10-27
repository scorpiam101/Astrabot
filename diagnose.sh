#!/usr/bin/env bash
# Prints a full health report. Never prints secrets (API key, bridge token, pairing codes).
set -u
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

PROBLEMS=0
fail() { bad "$*"; PROBLEMS=$((PROBLEMS + 1)); }
section() { say ""; say "== $* =="; }

say "WhatsApp AI Assistant - diagnostic report ($(date '+%Y-%m-%d %H:%M:%S'))"
say "Project folder: $APP_DIR"

section "Runtime"
if [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux ]; then ok "Termux detected"; else warn "Not running inside Termux"; fi
if command -v node >/dev/null 2>&1; then
  NODE_V="$(node -v)"; NODE_MAJOR="${NODE_V#v}"; NODE_MAJOR="${NODE_MAJOR%%.*}"
  if [ "${NODE_MAJOR:-0}" -ge 20 ]; then ok "Node.js $NODE_V"; else fail "Node.js $NODE_V is too old (need 20 or newer)"; fi
else fail "Node.js not found (pkg install nodejs)"; fi
command -v npm >/dev/null 2>&1 && ok "npm $(npm -v)" || fail "npm not found"
command -v curl >/dev/null 2>&1 && ok "curl available" || fail "curl not found (pkg install curl)"
if [ -x "$PY" ]; then
  ok "Python environment: $("$PY" -V 2>&1)"
  if "$PY" -c "import flask" 2>/dev/null; then ok "Flask installed ($("$PY" -c "import importlib.metadata as m; print(m.version('flask'))"))"; else fail "Flask is not installed in the venv"; fi
else fail "Python virtual environment missing: $PY"; fi

section "Files and syntax"
for f in backend/app.py backend/config.py backend/db.py backend/gemini_client.py backend/provider_router.py backend/commands.py backend/bridge_client.py backend/selftest.py; do
  if [ ! -f "$APP_DIR/$f" ]; then fail "missing $f"; continue; fi
  if [ -x "$PY" ] && "$PY" -c "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" "$APP_DIR/$f" 2>/tmp/wai-syntax.$$; then ok "$f"; else fail "$f has a syntax problem: $(head -n 3 /tmp/wai-syntax.$$ 2>/dev/null)"; fi
done
rm -f /tmp/wai-syntax.$$
for f in backend/static/index.html backend/static/app.css backend/static/app.js; do
  [ -f "$APP_DIR/$f" ] && ok "$f" || fail "missing $f"
done
if [ -f "$APP_DIR/bridge/bridge.js" ]; then
  if node --check "$APP_DIR/bridge/bridge.js" 2>/tmp/wai-node.$$; then ok "bridge/bridge.js"; else fail "bridge.js syntax problem: $(head -n 3 /tmp/wai-node.$$)"; fi
  rm -f /tmp/wai-node.$$
else fail "missing bridge/bridge.js"; fi

section "Node packages"
BP="$APP_DIR/bridge/node_modules/@whiskeysockets/baileys/package.json"
if [ -f "$BP" ]; then
  BV="$(node -p "require('$BP').version" 2>/dev/null)"
  case "$BV" in
    7.*-*) warn "Baileys $BV is a 7.x release candidate (chosen on purpose)" ;;
    *-*) fail "Baileys $BV is a pre-release. Re-run the installer to get the stable version." ;;
    7.*) ok "Baileys $BV (7.x line)" ;;
    6.*) ok "Baileys $BV (stable 6.x line)" ;;
    *)   warn "Baileys $BV (installer pins the 6.x stable line)" ;;
  esac
else fail "Baileys is not installed (cd bridge && npm install)"; fi
for pkg in pino @hapi/boom; do
  [ -f "$APP_DIR/bridge/node_modules/$pkg/package.json" ] && ok "$pkg installed" || fail "$pkg not installed"
done
if [ -f "$BP" ] && (cd "$APP_DIR/bridge" && node -e '
(async () => {
  const m = await import("@whiskeysockets/baileys");
  const cjs = m.default && typeof m.default === "object" ? m.default : null;
  const f = typeof m.default === "function" ? m.default : (cjs && cjs.default) || m.makeWASocket;
  if (typeof f !== "function") process.exit(1);
})().catch(() => process.exit(1));
' 2>/dev/null); then
  ok "Baileys loads and exposes makeWASocket"
else fail "Baileys could not be loaded by Node"; fi

section "Configuration (secrets are never shown)"
if [ -f "$ENV_FILE" ]; then
  MODE="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo '?')"
  if [ "$MODE" = "600" ]; then ok ".env exists with private permissions (600)"; else warn ".env permissions are $MODE (fixing to 600)"; chmod 600 "$ENV_FILE" 2>/dev/null; fi
  [ "$(env_get BRIDGE_TOKEN | wc -c)" -gt 16 ] && ok "internal bridge token is set" || fail "BRIDGE_TOKEN missing in .env (re-run installer)"
  if grep -qE '^GEMINI_API_KEY=.+' "$ENV_FILE"; then ok "Gemini API key is configured"; else warn "Gemini API key is NOT configured (add it in the dashboard, section 2)"; fi
else fail ".env file is missing"; fi

section "WhatsApp session files"
if [ -f "$AUTH_DIR/creds.json" ]; then
  REG="$("$PY" -c "import json,sys; c=json.load(open(sys.argv[1])); print('linked' if c.get('account') and c.get('me') else 'not linked yet')" "$AUTH_DIR/creds.json" 2>/dev/null || echo unreadable)"
  ok "session files present ($REG)"
else warn "no WhatsApp session saved yet (pair from the dashboard)"; fi
if [ -d "$AUTH_DIR" ]; then
  AM="$(stat -c '%a' "$AUTH_DIR" 2>/dev/null || echo '?')"
  [ "$AM" = "700" ] && ok "session folder permissions 700" || { warn "session folder permissions $AM (fixing to 700)"; chmod 700 "$AUTH_DIR" 2>/dev/null; }
fi

section "Database"
if [ -f "$DATA_DIR/app.db" ] && [ -x "$PY" ]; then
  "$PY" - "$DATA_DIR/app.db" <<'PYEOF'
import sqlite3, sys
want = {"settings", "contacts", "messages", "processed_messages", "proposals", "events"}
try:
    c = sqlite3.connect(sys.argv[1])
    have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = want - have
    if missing:
        print("  [FAIL] missing tables: " + ", ".join(sorted(missing))); sys.exit(1)
    counts = {t: c.execute("SELECT COUNT(*) FROM " + t).fetchone()[0] for t in ("contacts", "messages", "proposals")}
    print("  [ OK ] schema complete; contacts=%(contacts)d messages=%(messages)d proposals=%(proposals)d" % counts)
except Exception as e:
    print("  [FAIL] database problem: %s" % e); sys.exit(1)
PYEOF
  [ $? -eq 0 ] || PROBLEMS=$((PROBLEMS + 1))
else warn "database not created yet (it is created on first start)"; fi

section "Services and ports"
for spec in "dashboard|$FLASK_MARK|$DASH_URL/api/health|$FLASK_PORT|Dashboard" "bridge|$BRIDGE_MARK|$BRIDGE_URL/health|$BRIDGE_PORT|WhatsApp bridge"; do
  IFS='|' read -r name mark url port label <<< "$spec"
  if service_running "$name" "$mark"; then
    if http_ok "$url"; then ok "$label running and healthy on 127.0.0.1:$port"; else fail "$label process exists but does not answer on port $port"; fi
  else
    if [ -x "$PY" ] && port_in_use "$port"; then fail "$label is stopped but port $port is used by another program"; else warn "$label is not running (./start.sh)"; fi
  fi
done
if http_ok "$DASH_URL/api/health"; then
  curl -fsS --noproxy '*' --max-time 5 "$DASH_URL/api/status" 2>/dev/null | "$PY" -c '
import json, sys
d = json.load(sys.stdin); wa = d.get("whatsapp", {})
print("  [INFO] WhatsApp state: %s%s" % (wa.get("state"), " (last note: %s)" % wa["last_error"] if wa.get("last_error") else ""))
if d.get("last_ai_error"): print("  [INFO] Last AI problem: %s" % d["last_ai_error"])
' 2>/dev/null || warn "could not read dashboard status"
fi

section "Offline self-test (temporary database, no WhatsApp, no Gemini)"
if [ -x "$PY" ] && [ -f "$APP_DIR/backend/selftest.py" ]; then
  if ST="$("$PY" "$APP_DIR/backend/selftest.py" 2>&1)"; then
    ok "$(printf '%s\n' "$ST" | tail -n 1)"
  else
    fail "self-test failed:"; printf '%s\n' "$ST" | grep -E '^(FAIL|Traceback|AssertionError)' | head -n 8 | sed 's/^/    | /'
  fi
else warn "self-test skipped"; fi

section "Recent errors in logs"
for name in dashboard bridge; do
  f="$LOG_DIR/$name.log"
  if [ -f "$f" ]; then
    E="$(grep -E 'ERROR|Traceback|Error:' "$f" | tail -n 6 | redact)"
    if [ -n "$E" ]; then warn "$name.log has recent errors:"; printf '%s\n' "$E" | sed 's/^/    | /'; else ok "$name.log: no recent errors"; fi
  else say "  (no $name.log yet)"; fi
done

say ""
if [ "$PROBLEMS" -eq 0 ]; then say "RESULT: no problems found."; else say "RESULT: $PROBLEMS problem(s) found. Read the [FAIL] lines above."; fi
say "Live logs:  ./logs.sh dashboard   |   ./logs.sh bridge   |   ./logs.sh bridge -f"
[ "$PROBLEMS" -eq 0 ]
