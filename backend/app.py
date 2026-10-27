"""WhatsApp AI assistant: Flask dashboard + logic (127.0.0.1 only)."""
import hmac
import logging
import platform
import re
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import accounts
import bridge_client
import config
import db
import gemini_client
import provider_router
import commands

STATIC_DIR = Path(__file__).resolve().parent / "static"
JID_RE = re.compile(r"^[0-9]{5,20}@(s\.whatsapp\.net|lid)$")
PHONE_RE = re.compile(r"^[0-9]{8,15}$")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # voice notes arrive as base64
app.json.ensure_ascii = False  # Flask 3 ignores the old JSON_AS_ASCII config key

logging.getLogger("werkzeug").setLevel(logging.WARNING)
log = logging.getLogger("dashboard")

SCHED_POLL_SECONDS = 10           # how often the scheduler looks for due items
SCHED_GRACE_SECONDS = 15 * 60     # later than this and an item is marked "missed" instead of sent
SCHED_RETRY_SECONDS = 60          # wait before retrying after a temporary problem
STATUS_MAX_CHARS = 700
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_wake = threading.Event()

_gemini_slots = threading.Semaphore(2)
_state_lock = threading.Lock()
_last_state = {"value": None}


def now():
    return int(time.time())


def say(level, message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s %s" % (stamp, level.upper(), message), flush=True)


def error(message, status=400):
    return jsonify({"ok": False, "error": message}), status


# ---------------------------------------------------------------- security
def allowed_hosts():
    port = config.flask_port()
    return {"127.0.0.1:%d" % port, "localhost:%d" % port}


@app.before_request
def guard():
    if request.host not in allowed_hosts():
        return error("Host not allowed.", 403)
    if request.path.startswith("/internal/"):
        token = config.get("BRIDGE_TOKEN")
        supplied = request.headers.get("x-bridge-token", "")
        if not token or not hmac.compare_digest(supplied, token):
            return error("Forbidden.", 403)
        return None
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("X-Dashboard") != "1":
            return error("Missing dashboard header.", 403)
        origin = request.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in allowed_hosts()}:
            return error("Origin not allowed.", 403)
    return None


@app.after_request
def headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    return resp


@app.errorhandler(HTTPException)
def http_error(exc):
    return error(exc.description or "Request failed.", exc.code or 400)


@app.errorhandler(Exception)
def unexpected(exc):
    say("error", "Unhandled error: %s: %s" % (type(exc).__name__, exc))
    try:
        db.log_event("error", "dashboard", "%s: %s" % (type(exc).__name__, exc))
    except Exception:
        pass
    return error("Internal error. See logs/dashboard.log.", 500)


def json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(str(STATIC_DIR), name)


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "dashboard", "account": config.ACCOUNT, "time": now()})


# ---------------------------------------------------------------- status
def bridge_snapshot():
    try:
        code, body = bridge_client.status()
    except bridge_client.BridgeDown:
        return False, {"state": "bridge_down", "last_error": None, "registered": False}
    if code != 200:
        return True, {
            "state": "bridge_error",
            "last_error": "Bridge refused the status request (check BRIDGE_TOKEN).",
            "registered": False,
        }
    return True, body


@app.get("/api/status")
def status():
    running, wa = bridge_snapshot()
    # If the owner left OWNER_PHONE blank during installation, use the linked
    # WhatsApp account's number as the initial owner. The dashboard can change it.
    paired_phone = re.sub(r"\D", "", str(wa.get("paired_phone") or ""))
    current_owner = re.sub(r"\D", "", config.get("OWNER_PHONE", ""))
    if paired_phone and not current_owner and re.fullmatch(r"[0-9]{8,15}", paired_phone):
        try:
            config.set_value("OWNER_PHONE", paired_phone)
            current_owner = paired_phone
            db.log_event("info", "owner", "Owner number automatically set from the linked WhatsApp account.")
        except Exception as err:
            db.log_event("warn", "owner", "Could not auto-save linked account as owner: %s" % err)
    state = wa.get("state")
    with _state_lock:
        if state != _last_state["value"]:
            if _last_state["value"] is not None:
                db.log_event("info", "whatsapp", "State changed: %s -> %s" % (_last_state["value"], state))
            _last_state["value"] = state
    counts = db.counts()
    return jsonify(
        {
            "ok": True,
            "dashboard_running": True,
            "account": config.ACCOUNT,
            "bridge_running": running,
            "owner_phone": current_owner,
            "paired_phone": paired_phone,
            "whatsapp": {
                "state": state,
                "registered": bool(wa.get("registered")),
                "pairing_ready": bool(wa.get("pairing_ready")),
                "last_error": wa.get("last_error"),
                "connected_since": wa.get("connected_since"),
            },
            "gemini_configured": provider_router.any_ai_configured(),
            "last_ai_error": db.get_setting("last_ai_error") or None,
            "bot_enabled": db.get_bool("bot_enabled"),
            "manual_approval": db.get_bool("manual_approval"),
            "away": commands.away_active(),
            "away_until": int(db.get_setting("away_until") or 0),
            "away_note": db.get_setting("away_note"),
            "quiet_now": commands.quiet_now(),
            "pending_count": counts["pending"],
            "scheduled_count": counts["scheduled"],
            "time": now(),
        }
    )


@app.get("/api/diagnostics")
def diagnostics():
    running, wa = bridge_snapshot()
    return jsonify(
        {
            "ok": True,
            "python": platform.python_version(),
            "database": db.counts(),
            "bridge_running": running,
            "whatsapp_state": wa.get("state"),
            "whatsapp_last_error": wa.get("last_error"),
            "gemini_configured": provider_router.any_ai_configured(),
            "gemini_model_setting": db.get_setting("gemini_model"),
            "last_ai_error": db.get_setting("last_ai_error") or None,
            "events": db.recent_events(25),
        }
    )


# ---------------------------------------------------------------- whatsapp
def normalize_phone(raw):
    digits = re.sub(r"[\s+\-().]", "", str(raw or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if not digits.isdigit():
        return None, "Enter digits only, for example 233241234567."
    if digits.startswith("0"):
        return None, "Do not start with 0. Use the country code, e.g. 233 for Ghana (0241234567 becomes 233241234567)."
    if not PHONE_RE.match(digits):
        return None, "The number must be 8 to 15 digits including the country code."
    return digits, None


@app.post("/api/whatsapp/pair")
def whatsapp_pair():
    phone, problem = normalize_phone(json_body().get("phone"))
    if problem:
        return error(problem, 400)
    try:
        code, body = bridge_client.pair(phone)
    except bridge_client.BridgeDown:
        return error("The WhatsApp bridge is not running. Run ./status.sh in Termux.", 503)
    if code == 200 and body.get("code"):
        return jsonify({"ok": True, "code": body["code"]})
    db.log_event("warn", "whatsapp", "Pairing request failed: %s" % (body.get("error") or code))
    return error(body.get("error") or "Pairing failed (HTTP %s)." % code, code if code in (400, 409, 429, 502, 503) else 502)


# ---------------------------------------------------------------- settings
@app.get("/api/settings")
def settings_get():
    return jsonify({"ok": True, "settings": db.all_settings(), "gemini_configured": gemini_client.configured(), "owner_phone": re.sub(r"\D", "", config.get("OWNER_PHONE", ""))})


@app.post("/api/settings")
def settings_post():
    data = json_body()
    updates = {}
    if "owner_phone" in data:
        raw_owner = str(data.get("owner_phone") or "").strip()
        owner_digits = re.sub(r"[\s+().-]", "", raw_owner)
        if owner_digits and not re.fullmatch(r"[0-9]{8,15}", owner_digits):
            return error("Owner number must be 8 to 15 digits including country code, or blank to auto-detect the linked account.")
        try:
            config.set_value("OWNER_PHONE", owner_digits)
        except ValueError as err:
            return error(str(err))
        db.log_event("info", "owner", "Owner number updated from dashboard." if owner_digits else "Owner number cleared; linked-account auto-detection enabled.")
    for key in ("bot_enabled", "manual_approval"):
        if key in data:
            if not isinstance(data[key], bool):
                return error("%s must be true or false." % key)
            updates[key] = "1" if data[key] else "0"
    if updates:
        db.set_setting("start_until", "0")
    if updates.get("manual_approval") == "0" and data.get("confirm_auto") is not True:
        return error("Turning off manual approval requires confirmation.")
    int_rules = {
        "context_messages": (1, 50),
        "max_reply_age_minutes": (1, 1440),
        "auto_reply_per_hour": (1, 60),
    }
    for key, (low, high) in int_rules.items():
        if key in data:
            try:
                value = int(data[key])
            except (TypeError, ValueError):
                return error("%s must be a number." % key)
            if not low <= value <= high:
                return error("%s must be between %d and %d." % (key, low, high))
            updates[key] = str(value)
    if "gemini_model" in data:
        model = str(data["gemini_model"]).strip().replace("models/", "", 1)
        if model != "auto" and not gemini_client.valid_model_name(model):
            return error("Model name is not valid. Use 'auto' or choose a model returned by Load models.")
        updates["gemini_model"] = model
    if "voice_notes" in data:
        if not isinstance(data["voice_notes"], bool):
            return error("voice_notes must be true or false.")
        updates["voice_notes"] = "1" if data["voice_notes"] else "0"
    if "tone" in data:
        tone = str(data["tone"] or "").strip().lower()
        if tone and tone not in commands.TONES:
            return error("Tone must be one of: %s." % ", ".join(commands.TONES))
        updates["tone"] = tone
    if "quiet_hours" in data:
        quiet = str(data["quiet_hours"] or "").strip()
        if quiet and not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]", quiet):
            return error("Quiet hours must look like 22:00-07:00 (24-hour times), or be blank.")
        updates["quiet_hours"] = quiet
    if "system_prompt" in data:
        prompt = str(data["system_prompt"]).strip()
        if not 1 <= len(prompt) <= 2000:
            return error("The instructions must be 1 to 2000 characters.")
        updates["system_prompt"] = prompt
    for key, value in updates.items():
        db.set_setting(key, value)
    if updates:
        db.log_event("info", "settings", "Updated: " + ", ".join(sorted(updates)))
    return jsonify({"ok": True, "settings": db.all_settings(), "owner_phone": re.sub(r"\D", "", config.get("OWNER_PHONE", ""))})


@app.post("/api/gemini/key")
def gemini_key():
    data = json_body()
    if data.get("clear") is True:
        config.set_value("GEMINI_API_KEY", "")
        db.log_event("info", "gemini", "API key removed.")
        return jsonify({"ok": True, "gemini_configured": False})
    key = str(data.get("api_key") or "").strip()
    if not gemini_client.valid_key_format(key):
        return error("That does not look like a Gemini API key (letters, digits, dots, - and _ only).")
    config.set_value("GEMINI_API_KEY", key)
    db.set_setting("last_ai_error", "")
    db.log_event("info", "gemini", "API key saved.")
    return jsonify({"ok": True, "gemini_configured": True})


@app.post("/api/gemini/test")
def gemini_test():
    try:
        model = gemini_client.test_connection(db.get_setting("gemini_model"))
    except gemini_client.GeminiError as err:
        db.set_setting("last_ai_error", str(err))
        db.log_event("warn", "gemini", "Test failed (%s): %s" % (err.kind, err))
        code = 503 if err.kind in ("server", "network", "timeout") else 502
        return error(str(err), code)
    db.set_setting("last_ai_error", "")
    return jsonify({"ok": True, "model": model})


# ---------------------------------------------------------------- Gemini models and logs
@app.get("/api/gemini/models")
def gemini_models():
    try:
        models = gemini_client.list_models()
        models = sorted(set(models), key=lambda x: ("flash" not in x.lower(), x.lower()))
        if not models:
            return error("Google returned no models that support text generation.", 503)
        return jsonify({"ok": True, "models": models})
    except gemini_client.GeminiError as err:
        return error(str(err), 503 if err.kind in ("server", "network", "timeout", "quota") else 502)


def _safe_log_tail(path, limit=250):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        lines = []
    secrets = [config.get("GEMINI_API_KEY") or "", config.get("BRIDGE_TOKEN") or ""]
    cleaned = []
    for line in lines:
        for secret in secrets:
            if secret:
                line = line.replace(secret, "[hidden]")
        line = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[hidden-key]", line)
        cleaned.append(line[:1000])
    return cleaned


@app.get("/api/logs")
def logs_get():
    root = config.LOG_DIR
    return jsonify({"ok": True, "dashboard": _safe_log_tail(root / "dashboard.log"),
                    "bridge": _safe_log_tail(root / "bridge.log"), "events": db.recent_events(100)})


@app.post("/api/logs/clear")
def logs_clear():
    data = json_body()
    target = str(data.get("target") or "all")
    if target not in ("all", "dashboard", "bridge", "events"):
        return error("Choose all, dashboard, bridge, or events.")
    root = config.LOG_DIR
    if target in ("all", "dashboard", "bridge"):
        for name in (("dashboard.log", "bridge.log") if target == "all" else (target + ".log",)):
            try:
                (root / name).write_text("", encoding="utf-8")
            except OSError:
                pass
    if target in ("all", "events"):
        with db.conn() as c:
            c.execute("DELETE FROM events")
    db.log_event("info", "logs", "Logs cleared: " + target)
    return jsonify({"ok": True})


@app.get("/api/commands")
def commands_list():
    sections = []
    for key, icon, title in commands.SECTIONS:
        items = [{"usage": c.usage, "summary": c.summary} for c in commands.COMMANDS.values() if c.section == key]
        sections.append({"key": key, "icon": icon, "title": title, "commands": items})
    return jsonify({"ok": True, "sections": sections, "count": len(commands.COMMANDS)})


# ---------------------------------------------------------------- away mode (dashboard)
@app.post("/api/away")
def away_toggle():
    data = json_body()
    if not isinstance(data.get("enabled"), bool):
        return error("enabled must be true or false.")
    if data["enabled"]:
        note = str(data.get("note") or "").strip()[:200]
        minutes = data.get("minutes")
        args = ""
        if isinstance(minutes, int) and 0 < minutes <= 7 * 24 * 60:
            args = "%dm " % minutes
        reply, _ = commands.dispatch("#away " + args + note, {"jid": "", "self_chat": False})
    else:
        reply, _ = commands.dispatch("#back", {"jid": "", "self_chat": False})
    return jsonify({"ok": True, "message": reply, "away": commands.away_active()})


# ---------------------------------------------------------------- contacts
@app.get("/api/providers")
def providers_get():
    return jsonify({"ok": True, **provider_router.public_list()})

@app.post("/api/providers")
def providers_save():
    data=json_body()
    try:
        result=provider_router.save(data)
    except ValueError as err:
        return error(str(err))
    db.log_event("info","providers","AI provider configuration saved (secret values omitted).")
    return jsonify({"ok":True,**result})

@app.post("/api/providers/order")
def providers_order():
    order=json_body().get("order")
    if not isinstance(order,list) or any(not isinstance(x,str) for x in order): return error("Order must be a list of provider IDs.")
    return jsonify({"ok":True,**provider_router.reorder(order)})

@app.post("/api/providers/test")
def providers_test():
    try: result=provider_router.test(str(json_body().get("id") or ""))
    except Exception as err:
        db.log_event("warn","providers","Provider test failed: %s"%str(err)[:180])
        return error(str(err),502)
    return jsonify(result)

@app.post("/api/providers/remove")
def providers_remove():
    pid=str(json_body().get("id") or "")
    if not pid: return error("Provider ID is required.")
    return jsonify({"ok":True,**provider_router.remove(pid)})

_last_contact_sync = 0
_contact_sync_lock = threading.Lock()


@app.get("/api/contacts")
def contacts_list():
    global _last_contact_sync
    # Baileys learns names through WhatsApp contact-sync events. Merge them at most once a minute.
    if now() - _last_contact_sync >= 60 and _contact_sync_lock.acquire(False):
        try:
            try:
                code, payload = bridge_client.contacts()
                if code == 200:
                    for item in payload.get("contacts", [])[:1000]:
                        jid = str(item.get("jid") or "")
                        if JID_RE.match(jid):
                            db.upsert_synced_contact(jid, item.get("name") or item.get("notify"))
                _last_contact_sync = now()
            except bridge_client.BridgeDown:
                pass
        finally:
            _contact_sync_lock.release()
    return jsonify({"ok": True, "contacts": db.list_contacts()})


@app.post("/api/contacts/toggle")
def contacts_toggle():
    data = json_body()
    jid = str(data.get("jid") or "")
    if not JID_RE.match(jid) or not isinstance(data.get("enabled"), bool):
        return error("Invalid request.")
    if not db.set_contact_enabled(jid, data["enabled"]):
        return error("Contact not found.", 404)
    return jsonify({"ok": True})


@app.get("/api/contacts/profile")
def contact_profile_get():
    jid = request.args.get("jid", "")
    if not JID_RE.match(jid) or not db.get_contact(jid):
        return error("Contact not found.", 404)
    p = db.get_profile(jid)
    return jsonify({"ok": True, "profile": {"style": p["style"], "notes": p["notes"], "memory": p["memory"]}, "limits": db.PROFILE_LIMITS})


@app.post("/api/contacts/profile")
def contact_profile_save():
    data = json_body()
    jid = str(data.get("jid") or "")
    if not JID_RE.match(jid) or not db.get_contact(jid):
        return error("Contact not found.", 404)
    for field in ("style", "notes", "memory"):
        if field in data:
            value = str(data.get(field) or "").strip()
            if len(value) > db.PROFILE_LIMITS[field]:
                return error("%s must be at most %d characters." % (field.title(), db.PROFILE_LIMITS[field]))
            if field == "memory":
                db.set_memory(jid, value, db.get_profile(jid)["memory_upto"] or db.newest_message_id(jid))
            else:
                db.set_profile_field(jid, field, value)
    db.log_event("info", "contacts", "A contact profile was saved from the dashboard.")
    return jsonify({"ok": True})


@app.get("/api/conversation")
def conversation():
    jid = request.args.get("jid", "")
    if not JID_RE.match(jid):
        return error("Invalid contact.")
    contact = db.get_contact(jid)
    if not contact:
        return error("Contact not found.", 404)
    return jsonify({"ok": True, "contact": contact, "messages": db.get_conversation(jid, 100)})


# ---------------------------------------------------------------- accounts (several WhatsApp numbers)
@app.get("/api/accounts")
def accounts_list():
    return jsonify({"ok": True, "current": config.ACCOUNT, "accounts": accounts.list_accounts()})


def _account_action(fn):
    name = str(json_body().get("name") or "").strip().lower()
    try:
        info = fn(name)
    except accounts.AccountError as err:
        return error(str(err), 400)
    db.log_event("info", "accounts", "Account action finished for %s." % name)
    return jsonify({"ok": True, "account": info})


@app.post("/api/accounts/add")
def accounts_add():
    return _account_action(accounts.add)


@app.post("/api/accounts/start")
def accounts_start():
    return _account_action(accounts.start)


@app.post("/api/accounts/stop")
def accounts_stop():
    return _account_action(accounts.stop)


# ---------------------------------------------------------------- proposals
def send_proposal(pid):
    """Send a pending proposal. Marks it sent ONLY after the bridge confirms. Returns (ok, message, http)."""
    row = db.claim_proposal(pid)
    if row is None:
        return False, "This reply is not waiting for approval (already sent, rejected or being sent).", 409
    if not JID_RE.match(row["jid"]):
        db.release_proposal(pid, "Invalid recipient.")
        return False, "Invalid recipient.", 400
    try:
        code, body = bridge_client.send(row["jid"], row["suggestion"])
    except bridge_client.BridgeDown as err:
        db.release_proposal(pid, str(err))
        return False, "Not sent: %s" % err, 503
    if code == 200 and body.get("ok") and body.get("message_id"):
        db.mark_sent(pid, str(body["message_id"]))
        return True, "Sent.", 200
    message = body.get("error") or "WhatsApp did not confirm sending (HTTP %s)." % code
    db.release_proposal(pid, message)
    db.log_event("warn", "send", "Send failed for reply %d: %s" % (pid, message))
    return False, "Not sent: %s" % message, 502


@app.get("/api/proposals")
def proposals_list():
    return jsonify({"ok": True, "proposals": db.list_proposals("pending,sending")})


def proposal_id(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@app.post("/api/proposals/<pid>/approve")
def proposals_approve(pid):
    number = proposal_id(pid)
    if number is None:
        return error("Invalid reply id.")
    ok, message, code = send_proposal(number)
    if ok:
        return jsonify({"ok": True, "message": message})
    return error(message, code)


@app.post("/api/proposals/<pid>/reject")
def proposals_reject(pid):
    number = proposal_id(pid)
    if number is None:
        return error("Invalid reply id.")
    if not db.reject_proposal(number):
        return error("This reply is not waiting for approval.", 409)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- scheduling
def _status_audience(mode):
    """Who gets a status. 'me' = only the linked account itself (the bridge always adds it)."""
    return [] if mode == "me" else db.all_contact_jids(1000)


def _transient(code):
    # Not connected / bridge down / WhatsApp rate limit: safe to try again, nothing was sent.
    return code in (429, 503)


def _after_failure(row, code, message):
    """Decide what happens to a scheduled item whose send did not succeed."""
    sid = row["id"]
    if _transient(code):
        db.retry_later(sid, message, SCHED_RETRY_SECONDS)
        return
    if code == 502:
        message += " It may still have gone through, so check WhatsApp before sending it again."
    db.finish_scheduled(sid, "failed", message)
    db.log_event("warn", "schedule", "Scheduled %s %d failed: %s" % (row["kind"], sid, message))


def run_scheduled(row):
    """Send one claimed item. Marks it sent ONLY after the bridge confirms."""
    sid = row["id"]
    late = now() - row["run_at"]
    if late > SCHED_GRACE_SECONDS:
        db.finish_scheduled(
            sid, "missed",
            "This was due %d minutes ago but the app could not send it on time. "
            "Tap Send now if it is still wanted." % (late // 60),
        )
        db.log_event("warn", "schedule", "Scheduled %s %d was missed." % (row["kind"], sid))
        return
    try:
        if row["kind"] == "message":
            if not row["jid"] or not JID_RE.match(row["jid"]):
                db.finish_scheduled(sid, "failed", "Invalid recipient.")
                return
            code, body = bridge_client.send(row["jid"], row["body"])
            audience = None
        else:
            audience = _status_audience(row["audience"])
            code, body = bridge_client.post_status(row["body"], row["bg"] or "#2563eb", audience)
            if code == 200 and isinstance(body.get("audience"), list):
                audience = body["audience"]  # includes the owner's own number
    except bridge_client.BridgeDown as err:
        db.retry_later(sid, str(err), SCHED_RETRY_SECONDS)
        return
    if code == 200 and body.get("ok") and body.get("message_id"):
        db.mark_scheduled_sent(sid, str(body["message_id"]), audience)
        db.log_event("info", "schedule", "Scheduled %s %d sent." % (row["kind"], sid))
        return
    message = body.get("error") or "WhatsApp did not confirm (HTTP %s)." % code
    _after_failure(row, code, message)


def run_due():
    """Process every item that is due right now. Returns how many were handled."""
    handled = 0
    while True:
        row = db.claim_due(now())
        if row is None:
            return handled
        handled += 1
        try:
            run_scheduled(row)
        except Exception as err:  # never let one bad item stop the queue
            db.finish_scheduled(row["id"], "failed", "%s: %s" % (type(err).__name__, err))
            say("error", "run_scheduled: %s: %s" % (type(err).__name__, err))


def scheduler_loop():
    last_prune = 0
    while True:
        try:
            run_due()
            commands.tick()
            if now() - last_prune > 3600:
                db.prune_old()
                last_prune = now()
        except Exception as err:
            say("error", "scheduler: %s: %s" % (type(err).__name__, err))
        _wake.wait(SCHED_POLL_SECONDS)
        _wake.clear()


def _public_scheduled(item):
    out = dict(item)
    if out["kind"] == "status" and out.get("sent_at"):
        out["expires_at"] = out["sent_at"] + db.STATUS_LIFETIME
    return out


@app.get("/api/scheduled")
def scheduled_list():
    upcoming, history = db.list_scheduled()
    return jsonify(
        {
            "ok": True,
            "now": now(),
            "upcoming": [_public_scheduled(i) for i in upcoming],
            "history": [_public_scheduled(i) for i in history],
            "live_statuses": db.live_statuses(),
        }
    )


@app.post("/api/scheduled")
def scheduled_create():
    data = json_body()
    kind = data.get("kind")
    if kind not in ("message", "status"):
        return error("Choose message or status.")
    text = str(data.get("text") or "").strip()
    limit = STATUS_MAX_CHARS if kind == "status" else db.MAX_TEXT
    if not 1 <= len(text) <= limit:
        return error("The text must be 1 to %d characters." % limit)

    when = data.get("when")
    if when in (None, "", 0):
        run_at = now()
    else:
        try:
            run_at = int(when)
        except (TypeError, ValueError):
            return error("The time is not valid.")
        if run_at < now() - 120:
            return error("That time is in the past. Pick a time in the future, or use Send now.")
        if run_at > now() + 366 * 86400:
            return error("That is more than a year away. Pick a closer date.")
        run_at = max(run_at, now())
    if db.active_scheduled_count() >= 200:
        return error("Too many items are waiting (200). Cancel some first.")

    jid = None
    bg = None
    audience = "known"
    if kind == "message":
        jid = str(data.get("jid") or "")
        if not jid:
            phone, problem = normalize_phone(data.get("phone"))
            if problem:
                return error(problem)
            jid = phone + "@s.whatsapp.net"
        if not JID_RE.match(jid):
            return error("Invalid recipient.")
        db.ensure_contact(jid)
    else:
        bg = str(data.get("bg") or "#2563eb")
        if not COLOR_RE.match(bg):
            return error("Invalid status colour.")
        audience = str(data.get("audience") or "known")
        if audience not in ("me", "known"):
            return error("Invalid audience.")

    sid = db.create_scheduled(kind, jid, text, run_at, bg, audience)
    db.log_event("info", "schedule", "Scheduled %s %d for %s." % (kind, sid, time.strftime("%Y-%m-%d %H:%M", time.localtime(run_at))))
    _wake.set()
    return jsonify({"ok": True, "id": sid})


def _sched_id(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@app.post("/api/scheduled/<sid>/cancel")
def scheduled_cancel(sid):
    number = _sched_id(sid)
    if number is None:
        return error("Invalid id.")
    if not db.cancel_scheduled(number):
        return error("This item is no longer waiting (it may already be sending or sent).", 409)
    return jsonify({"ok": True})


@app.post("/api/scheduled/<sid>/retry")
def scheduled_retry(sid):
    number = _sched_id(sid)
    if number is None:
        return error("Invalid id.")
    if not db.retry_scheduled(number):
        return error("Only failed or missed items can be sent again.", 409)
    _wake.set()
    return jsonify({"ok": True})


@app.post("/api/scheduled/<sid>/delete_status")
def scheduled_delete_status(sid):
    """Remove a status this app posted from WhatsApp (for everyone who could see it)."""
    import json as _json

    number = _sched_id(sid)
    if number is None:
        return error("Invalid id.")
    row = db.get_scheduled(number)
    if not row or row["kind"] != "status" or row["status"] != "sent" or not row["wa_message_id"]:
        return error("This is not a posted status.", 409)
    if now() - (row["sent_at"] or 0) > db.STATUS_LIFETIME:
        return error("This status already expired on WhatsApp (statuses last 24 hours).", 409)
    try:
        audience = _json.loads(row["audience_json"] or "[]")
    except ValueError:
        audience = []
    if not isinstance(audience, list):
        audience = []
    try:
        code, body = bridge_client.delete_status(row["wa_message_id"], audience)
    except bridge_client.BridgeDown as err:
        return error("Not deleted: %s" % err, 503)
    if code == 200 and body.get("ok"):
        db.mark_status_deleted(number)
        db.log_event("info", "schedule", "Status %d deleted." % number)
        return jsonify({"ok": True})
    message = body.get("error") or "WhatsApp did not confirm the deletion (HTTP %s)." % code
    db.log_event("warn", "schedule", "Status %d delete failed: %s" % (number, message))
    return error("Not deleted: %s" % message, code if code in (400, 429, 502, 503) else 502)


# ---------------------------------------------------------------- incoming
def process_incoming(message_id, jid, ts, audio_base64="", mime_type="audio/ogg"):
    """Runs in a background thread: asks the AI and stores a proposal (or sends in auto mode)."""
    try:
        if not db.get_bool("bot_enabled"):
            return
        contact = db.get_contact(jid)
        if not contact or not contact["replies_enabled"]:
            return
        if commands.quiet_now():
            db.log_event("info", "ai", "Quiet hours are active; message not answered.")
            return
        if now() - ts > db.get_int("max_reply_age_minutes", 30) * 60:
            db.log_event("info", "ai", "Skipped an old message (older than the age limit in settings).")
            return
        if audio_base64 and db.get_setting("voice_notes") == "0":
            db.log_event("info", "ai", "Voice note skipped because voice notes are switched off (#voice on).")
            return
        if not provider_router.any_ai_configured():
            db.set_setting("last_ai_error", "No AI provider is configured. Add a Gemini API key or another provider.")
            db.log_event("warn", "ai", "Message not answered: no AI provider is configured.")
            return
        auto = not db.get_bool("manual_approval")
        if auto and db.sent_count_last_hour(jid) >= db.get_int("auto_reply_per_hour", 10):
            db.log_event("warn", "ai", "Automatic reply limit reached for a contact; not replying.")
            return
        history = db.recent_context(jid, db.get_int("context_messages", 12))
        system = commands.prompt_for(jid)
        with _gemini_slots:
            if audio_base64:
                text, model = provider_router.generate_audio(
                    db.get_setting("gemini_model"), system, history, audio_base64, mime_type,
                    reply_mode=db.get_setting("voice_reply") or "same")
            else:
                text, model = provider_router.generate(db.get_setting("gemini_model"), system, history)
        pid = db.create_proposal(message_id, jid, text)
        if pid is None:
            return
        if audio_base64 and db.get_setting("voice_notify") != "0":
            # The owner reads what the voice note said (and the translation) right in their own chat.
            who = (contact["name"] if contact and contact["name"] else "+" + jid.split("@", 1)[0])
            commands.notify_owner("🎤 Voice note from %s\n%s" % (who, text[:1500]))
        threading.Thread(target=_learn_later, args=(jid,), daemon=True, name="wai-learn").start()
        db.set_setting("last_ai_error", "")
        if auto and not db.get_bool("manual_approval") and db.get_bool("bot_enabled"):
            ok, message, _ = send_proposal(pid)
            if not ok:
                db.log_event("warn", "send", "Automatic reply not sent: %s" % message)
    except (gemini_client.GeminiError, provider_router.ProviderError) as err:
        db.set_setting("last_ai_error", "%s %s" % (time.strftime("%H:%M"), err))
        db.log_event("error", "ai", str(err))
        say("error", "AI: %s" % err)
    except Exception as err:  # keep the worker alive, record the problem
        db.log_event("error", "ai", "%s: %s" % (type(err).__name__, err))
        say("error", "process_incoming: %s: %s" % (type(err).__name__, err))


def _learn_later(jid):
    """Update what Astra remembers about this contact once enough new messages have arrived."""
    try:
        with _gemini_slots:
            commands.maybe_learn(jid)
    except Exception as err:
        db.log_event("warn", "learn", "Learning skipped: %s: %s" % (type(err).__name__, err))


_seen_commands = {}
_seen_lock = threading.Lock()


def _first_time(wa_id):
    """True the first time a WhatsApp message id is seen (the bridge may retry deliveries)."""
    with _seen_lock:
        if wa_id in _seen_commands:
            return False
        _seen_commands[wa_id] = now()
        if len(_seen_commands) > 500:
            for key in sorted(_seen_commands, key=_seen_commands.get)[:250]:
                _seen_commands.pop(key, None)
    return True


def handle_owner_command(jid, text, wa_id, self_chat):
    """Run a # command from the owner and send the answer back to the same chat."""
    if not _first_time(wa_id):
        return
    if self_chat or jid.endswith("@s.whatsapp.net") or jid.endswith("@lid"):
        # Remember where the owner's own chat is so reminders and away summaries can reach it.
        if self_chat:
            db.set_setting("owner_jid", jid)
    reply, after = commands.dispatch(text, {"jid": jid, "self_chat": self_chat})
    reply = (reply or "")[:3900]
    try:
        code, body = bridge_client.send(jid, reply)
        if code != 200 or not body.get("ok"):
            db.log_event("warn", "commands", "Could not send command response: %s" % (body.get("error") or code))
    except bridge_client.BridgeDown as err:
        db.log_event("error", "commands", "Could not send command response: %s" % err)
    if after:
        threading.Thread(target=after, daemon=True, name="wai-command-after").start()


@app.post("/internal/incoming")
def internal_incoming():
    data = json_body()
    wa_id = str(data.get("wa_message_id") or "")[:128]
    jid = str(data.get("jid") or "")
    text = str(data.get("text") or "").strip()[: db.MAX_TEXT]
    name = str(data.get("name") or "").strip()[:100]
    self_chat = data.get("self_chat") is True
    audio_base64 = str(data.get("audio_base64") or "")
    mime_type = str(data.get("mime_type") or "audio/ogg")[:80]
    if audio_base64 and len(audio_base64) > 18 * 1024 * 1024:
        return error("Audio is too large. Please send a shorter voice message.", 413)
    if audio_base64 and not text:
        text = "🎤 WhatsApp voice message (transcription pending)"
    if not wa_id or not JID_RE.match(jid) or not text:
        return error("Invalid message.")

    # Messages you send to yourself are only ever commands. Anything else is ignored.
    if self_chat and not text.startswith("#"):
        return jsonify({"ok": True, "command": "ignored"})

    # Owner-only # commands: from your own chat, or from the configured owner number.
    if text.startswith("#") and not audio_base64:
        owner = re.sub(r"\D", "", config.get("OWNER_PHONE", ""))
        sender = jid.split("@", 1)[0].split(":", 1)[0]
        if self_chat or (owner and sender == owner):
            threading.Thread(target=handle_owner_command, args=(jid, text, wa_id, self_chat), daemon=True, name="wai-command").start()
            return jsonify({"ok": True, "command": text.split()[0].lower()})
        # Anyone else's "#something" is just a normal message (a hashtag, a number sign).

    try:
        ts = int(data.get("timestamp") or now())
    except (TypeError, ValueError):
        ts = now()
    ts = min(ts, now())
    message_id = db.save_incoming(jid, name, text, wa_id, ts)
    if message_id is None:
        return jsonify({"ok": True, "duplicate": True})
    threading.Thread(
        target=process_incoming, args=(message_id, jid, ts, audio_base64, mime_type),
        daemon=True, name="wai-incoming",
    ).start()
    return jsonify({"ok": True})


# Let the command engine use the dashboard's own functions (it never imports this file).
commands.deps.bridge_snapshot = bridge_snapshot
commands.deps.send_proposal = send_proposal
commands.deps.run_due = run_due
commands.deps.wake = _wake.set


def main():
    db.init_db()
    db.log_event("info", "dashboard", "Dashboard started.")
    threading.Thread(target=scheduler_loop, daemon=True, name="wai-scheduler").start()
    say("info", "Dashboard listening on http://127.0.0.1:%d" % config.flask_port())
    app.run(host="127.0.0.1", port=config.flask_port(), threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
