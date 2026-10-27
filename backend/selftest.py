"""Offline self-test. Uses a temporary database and fake WhatsApp/Gemini services.

It never touches your real data, never contacts WhatsApp and never calls Gemini.
Run:  ./venv/bin/python backend/selftest.py
"""
import os
import stat
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="wai-selftest-"))
os.environ["WAI_DATA_DIR"] = str(TMP / "data")
os.environ["WAI_ENV_FILE"] = str(TMP / ".env")
(TMP / ".env").write_text("FLASK_PORT=5000\nBRIDGE_PORT=5001\nBRIDGE_TOKEN=selftesttoken123\nGEMINI_API_KEY=\n")
os.chmod(str(TMP / ".env"), 0o600)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts  # noqa: E402
import app as webapp  # noqa: E402
import commands  # noqa: E402
import bridge_client  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import gemini_client  # noqa: E402
import provider_router  # noqa: E402

BASE = "http://127.0.0.1:5000"
DASH = {"X-Dashboard": "1"}
TOKEN = {"x-bridge-token": "selftesttoken123"}
JID_A = "233241234567@s.whatsapp.net"
JID_B = "233201112222@s.whatsapp.net"

results = []
sent_log = []
send_mode = {"mode": "ok"}


def fake_send(jid, text):
    sent_log.append((jid, text))
    if send_mode["mode"] == "ok":
        return 200, {"ok": True, "message_id": "WAMSG%d" % len(sent_log)}
    if send_mode["mode"] == "fail":
        return 502, {"ok": False, "error": "WhatsApp is not connected."}
    raise bridge_client.BridgeDown("The WhatsApp bridge is not responding.")


def fake_generate(setting, system_prompt, history, **kwargs):
    last = history[-1][1] if history else ""
    return "Reply to: " + last[:40], "fake-model"


OWN_JID = "233000000000@s.whatsapp.net"
status_log = []
status_mode = {"mode": "ok"}


def fake_post_status(text, background, jids):
    status_log.append(("post", text, background, list(jids)))
    if status_mode["mode"] == "fail":
        return 502, {"ok": False, "error": "WhatsApp could not post the status."}
    if status_mode["mode"] == "down":
        raise bridge_client.BridgeDown("The WhatsApp bridge is not responding.")
    return 200, {"ok": True, "message_id": "STATUS%d" % len(status_log), "audience": sorted(set(jids) | {OWN_JID})}


def fake_delete_status(message_id, jids):
    status_log.append(("delete", message_id, list(jids)))
    if status_mode["mode"] == "fail":
        return 502, {"ok": False, "error": "WhatsApp could not delete the status."}
    return 200, {"ok": True}


bridge_client.post_status = fake_post_status
bridge_client.delete_status = fake_delete_status
bridge_client.send = fake_send
bridge_client.status = lambda: (200, {"state": "connected", "registered": True, "pairing_ready": False, "last_error": None})
bridge_client.pair = lambda phone: (200, {"code": "ABCD1234"})
gemini_client.generate = fake_generate
provider_router.generate = fake_generate
AI_FORCED = {"on": True}  # pretend an AI provider exists until the API-key checks run
provider_router.any_ai_configured = lambda: AI_FORCED["on"] or bool(config.get("GEMINI_API_KEY"))


def fake_audio(setting, system_prompt, history, audio_base64, mime_type="audio/ogg", api_key=None, reply_mode="same"):
    audio_log.append((mime_type, len(audio_base64)))
    audio_modes.append(reply_mode)
    return "Voice reply", "fake-audio-model"


audio_modes = []


audio_log = []
prompt_log = []
_fake_generate_plain = fake_generate


def fake_generate_logged(setting, system_prompt, history, **kwargs):
    prompt_log.append(system_prompt)
    return _fake_generate_plain(setting, system_prompt, history)


provider_router.generate = fake_generate_logged
provider_router.generate_audio = fake_audio

client = webapp.app.test_client()


def get(path, **kw):
    return client.get(path, base_url=BASE, **kw)


def post(path, body=None, headers=None, **kw):
    h = dict(DASH)
    h.update(headers or {})
    return client.post(path, json=body if body is not None else {}, headers=h, base_url=BASE, **kw)


def settle(timeout=15):
    """Wait for the background reply workers to finish (no fixed sleeps, so slow phones cannot flake)."""
    for t in list(threading.enumerate()):
        if t.name == "wai-incoming":
            t.join(timeout)


def incoming(jid, text, wa_id, name="Kofi", wait=True, ts=None):
    r = client.post(
        "/internal/incoming",
        json={"wa_message_id": wa_id, "jid": jid, "text": text, "name": name, "timestamp": ts or int(time.time())},
        headers=TOKEN,
        base_url=BASE,
    )
    if wait and r.status_code == 200 and not r.get_json().get("duplicate"):
        settle()
    return r


def pending():
    return get("/api/proposals").get_json()["proposals"]


def check(name):
    def wrap(fn):
        try:
            fn()
            results.append((name, True, ""))
            print("PASS  " + name)
        except Exception as err:  # noqa: BLE001
            results.append((name, False, str(err)))
            print("FAIL  %s -> %s" % (name, err))
            traceback.print_exc()
        return fn

    return wrap


db.init_db()


@check("database schema and defaults")
def _():
    with db.conn() as c:
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("settings", "contacts", "messages", "processed_messages", "proposals", "events", "notes"):
        assert t in tables, "missing table " + t
    assert db.get_bool("manual_approval") is True
    assert db.get_bool("bot_enabled") is True


@check("health endpoint and host guard")
def _():
    assert get("/api/health").get_json()["ok"] is True
    assert client.get("/api/health", base_url="http://evil.example").status_code == 403
    assert get("/").status_code == 200


@check("POST needs dashboard header and same origin")
def _():
    assert client.post("/api/settings", json={}, base_url=BASE).status_code == 403
    assert post("/api/settings", {}, headers={"Origin": "http://evil.example"}).status_code == 403


@check("internal endpoint needs token")
def _():
    r = client.post("/internal/incoming", json={}, base_url=BASE)
    assert r.status_code == 403
    r = client.post("/internal/incoming", json={}, headers={"x-bridge-token": "wrong"}, base_url=BASE)
    assert r.status_code == 403


@check("group and invalid recipients are rejected")
def _():
    assert incoming("1203630@g.us", "hi", "G1", wait=False).status_code == 400
    assert incoming("status@broadcast", "hi", "G2", wait=False).status_code == 400
    assert incoming(JID_A, "   ", "G3", wait=False).status_code == 400


@check("manual approval: proposal saved, nothing sent")
def _():
    gemini_key = post("/api/gemini/key", {"api_key": "A" * 39})
    assert gemini_key.status_code == 200
    r = incoming(JID_A, "Hello there", "M1")
    assert r.status_code == 200
    items = pending()
    assert len(items) == 1, items
    assert items[0]["suggestion"].startswith("Reply to: Hello")
    assert items[0]["contact_name"] == "Kofi"
    assert sent_log == [], "must not send while manual approval is on"


@check("duplicate WhatsApp events are processed once")
def _():
    r = incoming(JID_A, "Hello there", "M1")
    assert r.get_json().get("duplicate") is True
    assert len(pending()) == 1


@check("approve sends once and marks sent after confirmation")
def _():
    pid = pending()[0]["id"]
    r = post("/api/proposals/%d/approve" % pid)
    assert r.status_code == 200, r.get_json()
    assert sent_log[-1][0] == JID_A
    assert pending() == []
    assert db.get_proposal(pid)["status"] == "sent"
    assert any(m["direction"] == "out" for m in db.get_conversation(JID_A))
    assert post("/api/proposals/%d/approve" % pid).status_code == 409


@check("failed send keeps the proposal for retry")
def _():
    incoming(JID_A, "Second question", "M2")
    pid = pending()[0]["id"]
    before = len(sent_log)
    send_mode["mode"] = "fail"
    r = post("/api/proposals/%d/approve" % pid)
    assert r.status_code == 502
    p = pending()[0]
    assert p["status"] == "pending" and "not connected" in p["error"]
    send_mode["mode"] = "down"
    assert post("/api/proposals/%d/approve" % pid).status_code == 503
    assert pending()[0]["status"] == "pending"
    send_mode["mode"] = "ok"
    assert post("/api/proposals/%d/approve" % pid).status_code == 200
    assert len(sent_log) == before + 3 and db.get_proposal(pid)["status"] == "sent"


@check("reject marks rejected and cannot be sent")
def _():
    incoming(JID_A, "Third question", "M3")
    pid = pending()[0]["id"]
    assert post("/api/proposals/%d/reject" % pid).status_code == 200
    assert db.get_proposal(pid)["status"] == "rejected"
    assert post("/api/proposals/%d/approve" % pid).status_code == 409


@check("per-contact switch and global switch work independently")
def _():
    assert post("/api/contacts/toggle", {"jid": JID_A, "enabled": False}).status_code == 200
    incoming(JID_A, "ignored contact", "M4")
    assert pending() == []
    incoming(JID_B, "other contact", "M5", name="Ama")
    assert len(pending()) == 1
    pid = pending()[0]["id"]
    post("/api/proposals/%d/reject" % pid)
    assert post("/api/settings", {"bot_enabled": False}).status_code == 200
    incoming(JID_B, "bot is off", "M6", name="Ama")
    assert pending() == []
    post("/api/settings", {"bot_enabled": True})
    post("/api/contacts/toggle", {"jid": JID_A, "enabled": True})


@check("conversations stay separate per contact")
def _():
    a = [m["body"] for m in get("/api/conversation?jid=" + JID_A).get_json()["messages"]]
    b = [m["body"] for m in get("/api/conversation?jid=" + JID_B).get_json()["messages"]]
    assert "other contact" not in " ".join(a)
    assert "Hello there" not in " ".join(b)
    assert get("/api/conversation?jid=bad").status_code == 400


@check("old messages are not answered")
def _():
    incoming(JID_B, "very old", "OLD1", name="Ama", ts=int(time.time()) - 3 * 3600)
    assert pending() == []


@check("automatic mode needs explicit confirmation, then sends by itself")
def _():
    assert post("/api/settings", {"manual_approval": False}).status_code == 400
    assert db.get_bool("manual_approval") is True
    assert post("/api/settings", {"manual_approval": False, "confirm_auto": True}).status_code == 200
    before = len(sent_log)
    incoming(JID_B, "auto please", "AUTO1", name="Ama")
    assert len(sent_log) == before + 1 and sent_log[-1][0] == JID_B
    post("/api/settings", {"manual_approval": True})


@check("settings validation")
def _():
    assert post("/api/settings", {"context_messages": 0}).status_code == 400
    assert post("/api/settings", {"context_messages": "x"}).status_code == 400
    assert post("/api/settings", {"gemini_model": "bad model!"}).status_code == 400
    assert post("/api/settings", {"gemini_model": "auto", "context_messages": 8}).status_code == 200
    assert db.get_int("context_messages") == 8


@check("API key is stored privately and never returned")
def _():
    AI_FORCED["on"] = False
    key = "B" * 39
    r = post("/api/gemini/key", {"api_key": key})
    assert r.status_code == 200 and key not in r.get_data(as_text=True)
    assert config.get("GEMINI_API_KEY") == key
    mode = stat.S_IMODE(os.stat(str(config.ENV_PATH)).st_mode)
    assert mode == 0o600, oct(mode)
    for path in ("/api/status", "/api/settings", "/api/diagnostics"):
        assert key not in get(path).get_data(as_text=True), path
    assert post("/api/gemini/key", {"api_key": "short"}).status_code == 400
    assert post("/api/gemini/key", {"api_key": "has space " * 5}).status_code == 400
    assert get("/api/status").get_json()["gemini_configured"] is True
    assert post("/api/gemini/key", {"clear": True}).status_code == 200
    assert get("/api/status").get_json()["gemini_configured"] is False


@check("missing API key is reported, not hidden")
def _():
    incoming(JID_B, "no key now", "NK1", name="Ama")
    assert pending() == []
    assert "API key" in (get("/api/status").get_json()["last_ai_error"] or "")


@check("pairing input validation and pass-through")
def _():
    assert post("/api/whatsapp/pair", {"phone": "0241234567"}).status_code == 400
    assert post("/api/whatsapp/pair", {"phone": "abc"}).status_code == 400
    r = post("/api/whatsapp/pair", {"phone": "+233 24 123 4567"})
    assert r.status_code == 200 and r.get_json()["code"] == "ABCD1234"


@check("gemini client helpers")
def _():
    assert gemini_client._best_flash(
        ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.8-flash-lite", "gemini-4-flash-preview", "gemini-3.10-flash"]
    ) == "gemini-3.10-flash"
    merged = gemini_client._build_contents([("out", "x"), ("in", "a"), ("in", "b"), ("out", "c"), ("in", "d")])
    assert [c["role"] for c in merged] == ["user", "model", "user"]
    assert merged[0]["parts"][0]["text"] == "a\nb"
    try:
        gemini_client._build_contents([("out", "only model")])
        raise AssertionError("expected error")
    except gemini_client.GeminiError:
        pass
    ok = {"candidates": [{"content": {"parts": [{"text": " Hi "}]}}]}
    assert gemini_client._extract_text(ok) == "Hi"
    for bad in ({}, {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}, {"promptFeedback": {"blockReason": "SAFETY"}}):
        try:
            gemini_client._extract_text(bad)
            raise AssertionError("expected error")
        except gemini_client.GeminiError:
            pass



# ------------------------------------------------------------------ scheduling
def sched(body):
    return post("/api/scheduled", body)


def item(sid):
    return db.get_scheduled(sid)


def make_due(sid):
    with db.conn() as c:
        c.execute("UPDATE scheduled_items SET run_at=?, not_before=NULL WHERE id=?", (int(time.time()) - 5, sid))


@check("scheduled input validation")
def _():
    far = int(time.time()) + 3600
    assert sched({"kind": "nope", "text": "x"}).status_code == 400
    assert sched({"kind": "message", "text": "  ", "jid": JID_A, "when": far}).status_code == 400
    assert sched({"kind": "message", "text": "hi", "jid": JID_A, "when": int(time.time()) - 7200}).status_code == 400
    assert sched({"kind": "message", "text": "hi", "jid": JID_A, "when": int(time.time()) + 400 * 86400}).status_code == 400
    assert sched({"kind": "message", "text": "hi", "jid": JID_A, "when": "soon"}).status_code == 400
    assert sched({"kind": "message", "text": "hi", "phone": "0241234567", "when": far}).status_code == 400
    assert sched({"kind": "message", "text": "hi", "jid": "1203630@g.us", "when": far}).status_code == 400
    assert sched({"kind": "status", "text": "hi", "bg": "red", "when": far}).status_code == 400
    assert sched({"kind": "status", "text": "hi", "audience": "everyone", "when": far}).status_code == 400
    assert sched({"kind": "status", "text": "x" * 701, "when": far}).status_code == 400
    assert client.post("/api/scheduled", json={"kind": "status", "text": "hi"}, base_url=BASE).status_code == 403


@check("future message waits, cancel works, nothing sent early")
def _():
    before = len(sent_log)
    r = sched({"kind": "message", "text": "See you tomorrow", "jid": JID_A, "when": int(time.time()) + 3600})
    assert r.status_code == 200, r.get_json()
    sid = r.get_json()["id"]
    assert webapp.run_due() == 0 and len(sent_log) == before
    assert item(sid)["status"] == "scheduled"
    listing = get("/api/scheduled").get_json()
    assert [i["id"] for i in listing["upcoming"]] == [sid]
    assert post("/api/scheduled/%d/cancel" % sid).status_code == 200
    assert item(sid)["status"] == "cancelled"
    assert post("/api/scheduled/%d/cancel" % sid).status_code == 409
    make_due(sid)
    assert webapp.run_due() == 0 and len(sent_log) == before, "a cancelled item must never be sent"


@check("due message is sent once, stored in the conversation, and non-ASCII text survives")
def _():
    before = len(sent_log)
    text = "Ayɛ ɔ \U0001F600 good morning"
    r = sched({"kind": "message", "text": text, "jid": JID_A, "when": int(time.time()) + 3600})
    sid = r.get_json()["id"]
    make_due(sid)
    assert webapp.run_due() == 1
    assert sent_log[-1] == (JID_A, text) and len(sent_log) == before + 1
    assert item(sid)["status"] == "sent" and item(sid)["wa_message_id"]
    assert webapp.run_due() == 0 and len(sent_log) == before + 1, "must not send twice"
    bodies = [m["body"] for m in db.get_conversation(JID_A)]
    assert text in bodies
    raw = get("/api/conversation?jid=" + JID_A).get_data(as_text=True)
    assert "ɛ" in raw and "\\u025b" not in raw, "JSON should keep real characters"


@check("message to a new phone number creates the contact")
def _():
    r = sched({"kind": "message", "text": "Hello new friend", "phone": "+233 55 123 4567"})
    assert r.status_code == 200, r.get_json()
    sid = r.get_json()["id"]
    assert item(sid)["jid"] == "233551234567@s.whatsapp.net"
    assert webapp.run_due() == 1
    assert sent_log[-1] == ("233551234567@s.whatsapp.net", "Hello new friend")
    assert db.get_contact("233551234567@s.whatsapp.net") is not None


@check("temporary problems retry the same item without duplicating it")
def _():
    before = len(sent_log)
    sid = sched({"kind": "message", "text": "Retry me", "jid": JID_B}).get_json()["id"]
    send_mode["mode"] = "down"
    assert webapp.run_due() == 1
    it = item(sid)
    assert it["status"] == "scheduled" and it["attempts"] == 1 and it["error"]
    assert webapp.run_due() == 0, "must wait before retrying"
    send_mode["mode"] = "ok"
    make_due(sid)
    assert webapp.run_due() == 1
    assert item(sid)["status"] == "sent"
    assert [t for j, t in sent_log[before:] if t == "Retry me"].count("Retry me") == 2  # 1 failed attempt + 1 success


@check("unclear WhatsApp failure is not auto-retried; manual send now works")
def _():
    sid = sched({"kind": "message", "text": "Maybe delivered", "jid": JID_B}).get_json()["id"]
    send_mode["mode"] = "fail"
    assert webapp.run_due() == 1
    it = item(sid)
    assert it["status"] == "failed" and "check WhatsApp" in it["error"]
    assert webapp.run_due() == 0
    assert post("/api/scheduled/%d/retry" % sid).status_code == 200
    send_mode["mode"] = "ok"
    assert webapp.run_due() == 1 and item(sid)["status"] == "sent"
    assert post("/api/scheduled/%d/retry" % sid).status_code == 409


@check("items that are far overdue are marked missed, not sent")
def _():
    before = len(sent_log)
    sid = sched({"kind": "message", "text": "Too late", "jid": JID_B, "when": int(time.time()) + 3600}).get_json()["id"]
    with db.conn() as c:
        c.execute("UPDATE scheduled_items SET run_at=? WHERE id=?", (int(time.time()) - 3 * 3600, sid))
    assert webapp.run_due() == 1
    assert item(sid)["status"] == "missed" and len(sent_log) == before
    assert post("/api/scheduled/%d/retry" % sid).status_code == 200
    assert webapp.run_due() == 1 and item(sid)["status"] == "sent" and sent_log[-1][1] == "Too late"


@check("status: audience, posting, and deleting")
def _():
    status_log.clear()
    # audience "me" sends an empty list (the bridge adds the owner's own number)
    me_id = sched({"kind": "status", "text": "Only for me", "audience": "me", "bg": "#7c3aed"}).get_json()["id"]
    assert webapp.run_due() == 1
    assert status_log[-1][:3] == ("post", "Only for me", "#7c3aed") and status_log[-1][3] == []
    assert item(me_id)["status"] == "sent"

    # audience "known" includes everyone who has been in contact
    sid = sched({"kind": "status", "text": "Hello everyone", "audience": "known"}).get_json()["id"]
    assert webapp.run_due() == 1
    posted = status_log[-1]
    assert JID_A in posted[3] and JID_B in posted[3]
    assert sid in [s["id"] for s in get("/api/scheduled").get_json()["live_statuses"]]

    # delete: uses the stored id and the same audience (including the owner's number)
    r = post("/api/scheduled/%d/delete_status" % sid)
    assert r.status_code == 200, r.get_json()
    deleted = status_log[-1]
    assert deleted[0] == "delete" and deleted[1] == item(sid)["wa_message_id"]
    assert OWN_JID in deleted[2] and JID_A in deleted[2]
    assert item(sid)["status"] == "deleted"
    assert sid not in [s["id"] for s in get("/api/scheduled").get_json()["live_statuses"]]
    assert post("/api/scheduled/%d/delete_status" % sid).status_code == 409


@check("status delete failures keep the status; expired statuses cannot be deleted")
def _():
    status_log.clear()
    sid = sched({"kind": "status", "text": "Keep me on failure", "audience": "me"}).get_json()["id"]
    webapp.run_due()
    status_mode["mode"] = "fail"
    r = post("/api/scheduled/%d/delete_status" % sid)
    assert r.status_code == 502 and "Not deleted" in r.get_json()["error"]
    assert item(sid)["status"] == "sent"
    status_mode["mode"] = "ok"
    with db.conn() as c:
        c.execute("UPDATE scheduled_items SET sent_at=? WHERE id=?", (int(time.time()) - 25 * 3600, sid))
    r = post("/api/scheduled/%d/delete_status" % sid)
    assert r.status_code == 409 and "expired" in r.get_json()["error"]
    assert sid not in [s["id"] for s in get("/api/scheduled").get_json()["live_statuses"]]
    # a message is not a status
    mid = sched({"kind": "message", "text": "plain", "jid": JID_A}).get_json()["id"]
    webapp.run_due()
    assert post("/api/scheduled/%d/delete_status" % mid).status_code == 409


@check("status posting problems: bridge down retries, WhatsApp error fails")
def _():
    sid = sched({"kind": "status", "text": "Flaky", "audience": "me"}).get_json()["id"]
    status_mode["mode"] = "down"
    webapp.run_due()
    assert item(sid)["status"] == "scheduled" and item(sid)["attempts"] == 1
    status_mode["mode"] = "fail"
    make_due(sid)
    webapp.run_due()
    assert item(sid)["status"] == "failed"
    status_mode["mode"] = "ok"


@check("an interrupted send is never resent automatically")
def _():
    before = len(sent_log)
    sid = db.create_scheduled("message", JID_A, "Half sent", int(time.time()) - 5, None, "known")
    with db.conn() as c:
        c.execute("UPDATE scheduled_items SET status='sending' WHERE id=?", (sid,))
    db.init_db()
    it = item(sid)
    assert it["status"] == "failed" and "Check WhatsApp" in it["error"]
    assert webapp.run_due() == 0 and len(sent_log) == before


@check("old bookkeeping rows are pruned")
def _():
    with db.conn() as c:
        c.execute("INSERT INTO processed_messages(wa_message_id, jid, created_at) VALUES ('OLDX', ?, ?)", (JID_A, int(time.time()) - 40 * 86400))
    db.prune_old()
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM processed_messages WHERE wa_message_id='OLDX'").fetchone()["n"] == 0
        assert c.execute("SELECT COUNT(*) AS n FROM processed_messages").fetchone()["n"] > 0


# ---------------------------------------------------------------- # commands
SELF_JID = "233000000000@s.whatsapp.net"


def cmd(text, wa_id=None, jid=SELF_JID, self_chat=True):
    """Send a # command the way the bridge does and return what Astra answered."""
    before = len(sent_log)
    wa_id = wa_id or "CMD%d" % (len(sent_log) + int(time.time() * 1000) % 100000 + len(results))
    r = client.post(
        "/internal/incoming",
        json={"wa_message_id": wa_id, "jid": jid, "text": text, "name": "", "timestamp": int(time.time()), "self_chat": self_chat},
        headers=TOKEN, base_url=BASE,
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    for t in list(threading.enumerate()):
        if t.name in ("wai-command", "wai-command-after", "wai-incoming"):
            t.join(10)
    new = sent_log[before:]
    return new[-1][1] if new else ""


AI_FORCED["on"] = True
db.set_setting("manual_approval", "1")
db.set_setting("bot_enabled", "1")
config.set_value("OWNER_PHONE", "233000000000")


@check("# menu lists the commands and is owner-only")
def _():
    out = cmd("#menu")
    for word in ("#start", "#stop", "#away", "#back", "#menu", "#send", "#remind", "#pending"):
        assert word in out, word
    assert len(out) < 3900
    assert "Control" in cmd("#menu control") and "#snooze" in cmd("#menu control")
    assert "Usage" in cmd("#help send")
    before = len(sent_log)
    # a stranger's #menu is an ordinary message, never a command, and gets no menu back
    r = incoming(JID_A, "#menu", "STRANGER1")
    assert r.status_code == 200 and not any("ASTRA MENU" in t for _, t in sent_log[before:])
    # a message you wrote in another chat (not self chat) is never a command
    assert cmd("#menu", wa_id="NOTSELF", jid=JID_A, self_chat=False) == ""


@check("self chat ignores everything except # commands, and repeats are ignored")
def _():
    r = client.post("/internal/incoming", json={"wa_message_id": "SC1", "jid": SELF_JID, "text": "just a note to self", "timestamp": int(time.time()), "self_chat": True}, headers=TOKEN, base_url=BASE)
    assert r.get_json().get("command") == "ignored"
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) n FROM messages WHERE body='just a note to self'").fetchone()["n"] == 0
    first = cmd("#ping", wa_id="DUPPING")
    assert "Pong" in first
    n = len(sent_log)
    assert cmd("#ping", wa_id="DUPPING") == ""  # same WhatsApp message id: not run twice
    assert len(sent_log) == n


@check("#start / #stop work like away-and-back")
def _():
    assert "OFF" in cmd("#stop") and db.get_bool("bot_enabled") is False
    assert "ON" in cmd("#start") and db.get_bool("bot_enabled") is True and db.get_bool("manual_approval") is False
    assert "paused" in cmd("#stop 30m") and int(db.get_setting("snooze_until")) > time.time()
    cmd("#resume")
    assert db.get_bool("bot_enabled") and int(db.get_setting("snooze_until")) == 0
    assert "Manual mode" in cmd("#manual") and db.get_bool("manual_approval")
    assert "Usage" in cmd("#snooze") and "Usage" in cmd("#snooze banana")


@check("snooze resumes by itself")
def _():
    cmd("#snooze 10m")
    assert not db.get_bool("bot_enabled")
    db.set_setting("snooze_until", str(int(time.time()) - 1))
    webapp.commands.tick()
    assert db.get_bool("bot_enabled") and int(db.get_setting("snooze_until")) == 0


@check("away mode: auto replies, AI is told you are away, #back restores and summarizes")
def _():
    cmd("#manual")
    incoming(JID_A, "ping before away", "AW0")
    with db.conn() as c:
        c.execute("UPDATE proposals SET status='rejected' WHERE status='pending'")
    time.sleep(1.1)  # earlier checks ran in the same second; keep them out of the away summary
    out = cmd("#away 2h at the gym")
    assert "Away mode ON" in out and webapp.commands.away_active()
    assert db.get_bool("bot_enabled") and not db.get_bool("manual_approval")
    prompt_log.clear()
    sent_before = len(sent_log)
    incoming(JID_B, "are you around?", "AW1", name="Ama")
    assert any("away" in p.lower() and "gym" in p.lower() for p in prompt_log), prompt_log
    assert len(sent_log) == sent_before + 1  # auto reply went out
    back = cmd("#back")
    assert "Welcome back" in back and "1 message" in back
    assert not webapp.commands.away_active()
    assert db.get_bool("manual_approval") is True  # previous mode restored
    assert "weren't" in cmd("#back")


@check("away mode expires on its own")
def _():
    cmd("#away 5m")
    db.set_setting("away_until", str(int(time.time()) - 1))
    webapp.commands.tick()
    assert not webapp.commands.away_active()


@check("quiet hours block AI replies")
def _():
    now_t = time.localtime()
    start = "%02d:%02d" % ((now_t.tm_hour - 1) % 24, now_t.tm_min)
    end = "%02d:%02d" % ((now_t.tm_hour + 1) % 24, now_t.tm_min)
    assert "Quiet hours set" in cmd("#quiet %s-%s" % (start, end))
    assert webapp.commands.quiet_now()
    cmd("#manual")
    n = len(pending())
    incoming(JID_B, "midnight message", "Q1", name="Ama")
    assert len(pending()) == n
    cmd("#quiet off")
    assert not webapp.commands.quiet_now()
    assert "Usage" in cmd("#quiet 25:00-07:00")


@check("time parsing")
def _():
    pw = webapp.commands.parse_when
    t, used = pw(["30m", "hello"])
    assert used == 1 and 1700 < t - time.time() < 1900
    t, used = pw(["in", "2h", "x"])
    assert used == 2 and 7100 < t - time.time() < 7300
    t, used = pw(["tomorrow", "8am", "x"])
    assert used == 2 and time.localtime(t).tm_hour == 8 and t > time.time()
    t, used = pw(["18:30", "x"])
    assert used == 1 and time.localtime(t).tm_hour == 18 and time.localtime(t).tm_min == 30 and t > time.time()
    t, used = pw(["fri", "9:00"])
    assert used == 2 and time.localtime(t).tm_wday == 4
    assert pw(["hello"]) == (None, 0) and pw(["money", "9:00"]) == (None, 0)
    assert webapp.commands.parse_duration("1h30m") == 5400 and webapp.commands.parse_duration("45") == 2700 and webapp.commands.parse_duration("x") is None


@check("#send, #schedule, #remind, #scheduled and #cancel")
def _():
    before = len(sent_log)
    out = cmd('#send 233241234567 Hello from commands')
    assert "Sent" in out or "Hello from commands" in sent_log[-1][1], out
    assert sent_log[before][0] == JID_A and sent_log[before][1] == "Hello from commands"
    out = cmd("#schedule 233201112222 2h Dinner at 8?")
    assert "Scheduled" in out
    sid = int(out.split("#")[1].split()[0])
    assert item(sid)["status"] == "scheduled" and item(sid)["body"] == "Dinner at 8?"
    out = cmd("#remind 30m call mum")
    assert "remind you" in out
    listing = cmd("#scheduled")
    assert "Dinner at 8?" in listing and "call mum" in listing
    assert "Cancelled" in cmd("#cancel %d" % sid) and item(sid)["status"] == "cancelled"
    assert "Usage" in cmd("#send 233241234567")
    assert "match" in cmd("#send nobody-named-this hi") or "No contact" in cmd("#send nobody-named-this hi")
    assert "Cancelled" in cmd("#cancel all")


@check("#pending, #edit, #approve and #reject")
def _():
    cmd("#manual")
    incoming(JID_A, "edit me please", "PE1")
    incoming(JID_B, "reject me please", "PE2", name="Ama")
    rows = pending()
    assert len(rows) >= 2
    listing = cmd("#pending")
    assert "edit me please" in listing and "reject me please" in listing
    a_id = [r for r in rows if r["jid"] == JID_A][-1]["id"]
    b_id = [r for r in rows if r["jid"] == JID_B][-1]["id"]
    assert "updated" in cmd("#edit %d Edited by owner" % a_id)
    before = len(sent_log)
    assert "Sent 1" in cmd("#approve %d" % a_id)
    assert sent_log[before] == (JID_A, "Edited by owner")
    assert "Rejected 1" in cmd("#reject %d" % b_id)
    assert all(r["id"] not in (a_id, b_id) for r in pending())
    assert "Usage" in cmd("#approve") and "Usage" in cmd("#approve banana")


@check("#mute, #unmute, #muted and #contacts")
def _():
    db.ensure_contact(JID_A, "Kofi")
    out = cmd("#mute kofi")
    assert "Muted" in out, out
    assert db.get_contact(JID_A)["replies_enabled"] == 0
    assert "Kofi" in cmd("#muted") and "Kofi" in cmd("#contacts kofi")
    assert "Unmuted" in cmd('#unmute "Kofi"')
    assert db.get_contact(JID_A)["replies_enabled"] == 1
    assert "Muted" in cmd("#mute 233241234567") and "Unmuted" in cmd("#unmute 233241234567")


@check("#note, #notes and #delnote")
def _():
    assert "Saved note" in cmd("#note buy bread")
    assert "buy bread" in cmd("#notes") and "buy bread" in cmd("#notes bread")
    assert "Deleted 1" in cmd("#delnote 1") or "Deleted" in cmd("#delnote all")
    assert "No notes" in cmd("#notes")


@check("#ask, #translate, #draft and #summarize use the AI")
def _():
    assert "Reply to: " in cmd("#ask what is two plus two")
    assert "Reply to: " in cmd("#translate french good morning")
    incoming(JID_A, "hello summarize", "SUM1")
    assert "Reply to: " in cmd("#summarize 233241234567")
    assert "Draft" in cmd("#draft 233241234567 say thanks")
    assert "Usage" in cmd("#ask")


@check("settings commands validate input")
def _():
    assert "set to 5" in cmd("#limit 5") and db.get_int("auto_reply_per_hour") == 5
    assert "between" in cmd("#limit 999")
    assert "set to 20" in cmd("#context 20") and "set to 45" in cmd("#maxage 45")
    assert "tone set" in cmd("#tone brief").lower() and db.get_setting("tone") == "brief"
    assert "Options" in cmd("#tone wild") or "Usage" in cmd("#tone wild")
    cmd("#tone reset")
    assert "Model set" in cmd("#model gemini-2.5-flash") and "valid model name" in cmd("#model bad name!")
    cmd("#model auto")
    assert "OFF" in cmd("#voice off") and db.get_setting("voice_notes") == "0"
    assert "ON" in cmd("#voice on")
    assert "Instructions" in cmd("#prompt")
    assert "updated" in cmd("#prompt set Be nice") and db.get_setting("system_prompt") == "Be nice"
    cmd("#prompt reset")


@check("info commands answer without errors")
def _():
    for name in ("#status", "#stats", "#health", "#logs", "#errors", "#time", "#uptime", "#version", "#about", "#settings",
                 "#owner", "#recent", "#providers", "#statuses", "#muted", "#pending", "#scheduled", "#notes", "#ping", "#help"):
        out = cmd(name)
        assert out and "hit a problem" not in out, (name, out)
    assert "Did you mean" in cmd("#pendin") or "#pending" in cmd("#pendin")
    assert "don't know" in cmd("#zzzzzz")


@check("#poststatus posts and #delstatus removes")
def _():
    out = cmd("#poststatus Hello status")
    assert "Status posted" in out, out
    sid = int(out.split("#delstatus")[1].split()[0].strip(". "))
    assert status_log[-1][0] == "post" and status_log[-1][1] == "Hello status"
    assert "deleted" in cmd("#delstatus %d" % sid)
    assert "Usage" in cmd("#poststatus")


@check("#restart needs confirmation")
def _():
    assert "confirm" in cmd("#restart").lower()


@check("voice notes are understood, and can be switched off")
def _():
    db.set_setting("voice_notes", "1")
    cmd("#start")
    audio_log.clear()
    r = client.post("/internal/incoming", json={"wa_message_id": "VN1", "jid": JID_B, "text": "", "name": "Ama", "timestamp": int(time.time()),
                                                "audio_base64": "QUJD" * 20, "mime_type": "audio/ogg"}, headers=TOKEN, base_url=BASE)
    assert r.status_code == 200
    settle()
    assert audio_log and audio_log[0][0] == "audio/ogg"
    with db.conn() as c:
        assert c.execute("SELECT id FROM proposals WHERE suggestion='Voice reply'").fetchone() is not None
    cmd("#voice off")
    audio_log.clear()
    client.post("/internal/incoming", json={"wa_message_id": "VN2", "jid": JID_B, "text": "", "name": "Ama", "timestamp": int(time.time()),
                                            "audio_base64": "QUJD" * 20, "mime_type": "audio/ogg"}, headers=TOKEN, base_url=BASE)
    settle()
    assert not audio_log
    cmd("#voice on")


@check("dashboard: commands list, away toggle, models and logs endpoints")
def _():
    d = get("/api/commands").get_json()
    assert d["count"] >= 50 and any(s["key"] == "send" for s in d["sections"])
    assert post("/api/away", {"enabled": True, "note": "testing", "minutes": 30}).get_json()["away"] is True
    assert get("/api/status").get_json()["away"] is True
    assert post("/api/away", {"enabled": False}).get_json()["away"] is False
    assert post("/api/away", {"enabled": "yes"}).status_code == 400
    assert post("/api/settings", {"tone": "formal", "quiet_hours": "22:00-07:00", "voice_notes": True}).status_code == 200
    s = get("/api/settings").get_json()["settings"]
    assert s["tone"] == "formal" and s["quiet_hours"] == "22:00-07:00" and s["voice_notes"] is True
    assert post("/api/settings", {"quiet_hours": "99:00-07:00"}).status_code == 400
    assert post("/api/settings", {"tone": "rude"}).status_code == 400
    post("/api/settings", {"tone": "", "quiet_hours": ""})
    assert get("/api/logs").get_json()["ok"] is True



@check("timed #start switches itself off, timed #stop switches itself back on")
def _():
    cmd("#manual")
    assert db.get_bool("manual_approval") is True
    out = cmd("#start 2h")
    assert "ON" in out and "switch OFF" in out, out
    assert db.get_bool("bot_enabled") and not db.get_bool("manual_approval")
    assert int(db.get_setting("start_until")) > time.time() + 7000
    assert "Timed start" in cmd("#status")
    assert "didn't understand" in cmd("#start banana")
    db.set_setting("start_until", str(int(time.time()) - 5))
    commands.tick()
    assert not db.get_bool("bot_enabled"), "timed #start did not turn the bot off"
    assert db.get_bool("manual_approval") is True, "manual approval was not restored"
    assert db.get_setting("start_until") == "0"
    cmd("#start")
    assert db.get_bool("bot_enabled") and db.get_setting("start_until") == "0"
    out = cmd("#stop 30m")
    assert not db.get_bool("bot_enabled") and int(db.get_setting("snooze_until")) > time.time() + 1500, out
    db.set_setting("snooze_until", str(int(time.time()) - 5))
    commands.tick()
    assert db.get_bool("bot_enabled"), "timed #stop did not resume"
    cmd("#manual")


@check("per-contact style and notes reach only that contact's prompt")
def _():
    db.set_setting("learn_contacts", "0")
    db.ensure_contact(JID_A, "Kofi")
    db.ensure_contact(JID_B, "Ama")
    assert "Got it" in cmd('#style "Kofi" speak Twi, casual, call him boss')
    assert "Remembered" in cmd("#remember Kofi my cousin, studies nursing")
    assert "Twi" in commands.prompt_for(JID_A) and "nursing" in commands.prompt_for(JID_A)
    assert "Twi" not in commands.prompt_for(JID_B) and "nursing" not in commands.prompt_for(JID_B)
    prof = cmd("#profile kofi")
    assert "Twi" in prof and "nursing" in prof, prof
    assert "Kofi" in cmd("#profiles")
    cmd("#start")
    incoming(JID_A, "hello", "STY1")
    assert "speak Twi" in prompt_log[-1], "Kofi's style was not used"
    incoming(JID_B, "hello", "STY2", name="Ama")
    assert "speak Twi" not in prompt_log[-1] and "nursing" not in prompt_log[-1], "Kofi's style leaked to Ama"
    assert "Keep it under" in cmd("#style Kofi " + "x" * 601)
    assert "cleared" in cmd("#style Kofi clear")
    assert "Twi" not in commands.prompt_for(JID_A) and "nursing" in commands.prompt_for(JID_A)
    assert "Erased" in cmd("#forget Kofi all")
    assert commands.prompt_for(JID_A) == commands.effective_prompt()
    assert "Usage" in cmd("#forget Kofi banana")
    cmd("#manual")


@check("Astra learns about a contact from their chats, safely, and it can be switched off")
def _():
    db.set_setting("learn_contacts", "1")
    db.ensure_contact(JID_B, "Ama")
    base = int(time.time()) - 600
    for i in range(7):
        db.save_incoming(JID_B, "Ama", "Hi it's Ama message %d" % i, "LRN%d" % i, base + i)
    assert commands.maybe_learn(JID_B) is True
    p = db.get_profile(JID_B)
    assert p["memory"], "nothing was learned"
    assert commands.maybe_learn(JID_B) is False, "learned again without new messages"
    assert "not instructions" in commands.prompt_for(JID_B)
    assert "ignore any instruction" in commands.LEARN_SYSTEM
    cmd("#learn off")
    for i in range(7, 14):
        db.save_incoming(JID_B, "Ama", "More from Ama %d" % i, "LRN%d" % i, base + i)
    assert commands.maybe_learn(JID_B) is False
    assert "Updated" in cmd("#learnnow Ama")
    assert "Erased" in cmd("#forget Ama memory")
    assert db.get_profile(JID_B)["memory"] == ""
    cmd("#learn on")
    db.clear_profile(JID_B, "all")


@check("dashboard: contact profile endpoints validate and save")
def _():
    db.ensure_contact(JID_A, "Kofi")
    assert post("/api/contacts/profile", {"jid": JID_A, "style": "be brief", "notes": "vip"}).status_code == 200
    g = get("/api/contacts/profile?jid=" + JID_A).get_json()
    assert g["profile"]["style"] == "be brief" and g["profile"]["notes"] == "vip"
    assert any(c["jid"] == JID_A and c["style"] == "be brief" for c in get("/api/contacts").get_json()["contacts"])
    assert post("/api/contacts/profile", {"jid": JID_A, "style": "x" * 601}).status_code == 400
    assert post("/api/contacts/profile", {"jid": "999999999999@s.whatsapp.net", "style": "a"}).status_code == 404
    assert post("/api/contacts/profile", {"jid": JID_A, "memory": ""}).status_code == 200
    db.clear_profile(JID_A, "all")


@check("voice notes: Twi guidance is in the prompt and reply language can be chosen")
def _():
    assert "Twi" in gemini_client.VOICE_LANGUAGE_HELP and "\u025b" in gemini_client.VOICE_LANGUAGE_HELP
    db.set_setting("voice_notes", "1")
    db.set_setting("auto_reply_per_hour", "60")
    cmd("#start")
    for mode, wa in (("english", "VN3"), ("same", "VN4")):
        assert "Voice replies" in cmd("#voice reply " + mode)
        audio_modes.clear()
        client.post("/internal/incoming", json={"wa_message_id": wa, "jid": JID_B, "text": "", "name": "Ama", "timestamp": int(time.time()),
                                                "audio_base64": "QUJD" * 20, "mime_type": "audio/ogg"}, headers=TOKEN, base_url=BASE)
        settle()
        assert audio_modes and audio_modes[-1] == mode, (mode, audio_modes)
    assert "Usage" in cmd("#voice reply klingon")
    cmd("#manual")


@check("accounts: names are validated before anything reaches a shell; accounts stay separate")
def _():
    assert accounts.names()[0] == "main"
    calls = []
    real = accounts.run_script
    accounts.run_script = lambda args, timeout=120: (calls.append(list(args)) or (True, ""))
    try:
        for bad in ("main", "", "x;rm -rf ~", "../etc", "A b", "-x", "a" * 30, "$(id)", "a"):
            assert post("/api/accounts/add", {"name": bad}).status_code == 400, bad
        assert not calls, "an invalid name reached the shell: %r" % calls
        r = post("/api/accounts/add", {"name": "work"})
        assert r.status_code == 200, r.get_json()
        assert calls == [["add", "work", "--quiet"], ["start", "work", "--quiet"]], calls
        assert post("/api/accounts/stop", {"name": config.ACCOUNT}).status_code == 400
        assert post("/api/accounts/start", {"name": "ghost"}).status_code == 400
    finally:
        accounts.run_script = real
    lst = get("/api/accounts").get_json()
    assert lst["current"] == config.ACCOUNT and lst["accounts"][0]["name"] == "main" and lst["accounts"][0]["current"]
    assert get("/api/status").get_json()["account"] == config.ACCOUNT
    assert "main" in cmd("#accounts")
    assert config.DATA_DIR == Path(os.environ["WAI_DATA_DIR"]), "data folder is not taken from the account settings"
    assert str(provider_router.STORE).startswith(str(config.DATA_DIR)), "AI provider keys are not stored per account"



@check("dashboard page: every element the script uses exists in the HTML")
def _():
    import re as _re
    js = (Path(__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")
    html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    used = set(_re.findall(r"\$\('([A-Za-z0-9_]+)'\)", js)) | set(_re.findall(r"(?:setText|show)\('([A-Za-z0-9_]+)'", js))
    have = set(_re.findall(r'id="([A-Za-z0-9_]+)"', html))
    missing = sorted(u for u in used if u not in have)
    assert not missing, "the script uses ids that are not in the page: %s" % missing



failed = [r for r in results if not r[1]]
print("\n%d checks, %d failed" % (len(results), len(failed)))
sys.exit(1 if failed else 0)
