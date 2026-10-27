"""Talks to the local Node.js WhatsApp bridge on 127.0.0.1 (never leaves the phone)."""
import json
import socket
import urllib.error
import urllib.request

import config

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BridgeDown(Exception):
    pass


def _call(method, path, payload=None, timeout=10):
    url = "http://127.0.0.1:%d%s" % (config.bridge_port(), path)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "x-bridge-token": config.get("BRIDGE_TOKEN"),
        },
    )
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as err:
        try:
            body = json.loads(err.read().decode("utf-8") or "{}")
        except Exception:
            body = {}
        return err.code, body
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
        raise BridgeDown("The WhatsApp bridge is not responding.")
    except ValueError:
        raise BridgeDown("The WhatsApp bridge returned an unreadable answer.")


def status():
    return _call("GET", "/status", timeout=3)


def contacts():
    return _call("GET", "/contacts", timeout=8)


def pair(phone):
    return _call("POST", "/pair", {"phone": phone}, timeout=45)


def send(jid, text):
    return _call("POST", "/send", {"jid": jid, "text": text}, timeout=35)


def post_status(text, background, jids):
    return _call("POST", "/status/post", {"text": text, "background": background, "jids": jids}, timeout=60)


def delete_status(message_id, jids):
    return _call("POST", "/status/delete", {"message_id": message_id, "jids": jids}, timeout=60)
