"""Several WhatsApp numbers on one phone.

Every account is a completely separate copy of the runtime: its own .env (API keys, owner number,
ports, bridge token), database, WhatsApp session, provider keys, logs and process IDs. Nothing is
shared between accounts. "main" is the original installation; the others live in accounts/<name>/.

The shell script accounts.sh does the real work (create, start, stop). This module only reads the
state for the dashboard and the #accounts command, and calls that script with validated names.
"""
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

import config

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
SCRIPT = config.ROOT / "accounts.sh"


class AccountError(Exception):
    """A friendly message that can be shown to the owner."""


def valid_name(name):
    return bool(NAME_RE.match(name or ""))


def account_dir(name):
    return config.ROOT if name == "main" else config.ACCOUNTS_DIR / name


def _env_of(name):
    try:
        return config._parse((account_dir(name) / ".env").read_text(encoding="utf-8"))
    except OSError:
        return {}


def names():
    out = ["main"]
    try:
        for p in sorted(config.ACCOUNTS_DIR.iterdir()):
            if p.is_dir() and not p.name.startswith(".") and valid_name(p.name) and (p / ".env").is_file():
                out.append(p.name)
    except OSError:
        pass
    return out


def _get_json(url, timeout=0.8):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def describe(name, check=True):
    env = _env_of(name)
    port = int(env.get("FLASK_PORT") or 0) if str(env.get("FLASK_PORT") or "").isdigit() else 0
    info = {
        "name": name,
        "current": name == config.ACCOUNT,
        "dashboard_port": port,
        "url": "http://127.0.0.1:%d" % port if port else "",
        "owner_phone": re.sub(r"\D", "", env.get("OWNER_PHONE", "")),
        "has_ai_key": bool((env.get("GEMINI_API_KEY") or "").strip()) or (account_dir(name) / "data" / "providers.json").is_file(),
        "running": False,
        "whatsapp": "unknown",
    }
    if check and port:
        if info["current"]:
            info["running"] = True
        else:
            h = _get_json("http://127.0.0.1:%d/api/health" % port)
            info["running"] = bool(h and h.get("ok"))
        if info["running"] and not info["current"]:
            st = _get_json("http://127.0.0.1:%d/api/status" % port, timeout=2.0)
            if st and isinstance(st.get("whatsapp"), dict):
                info["whatsapp"] = str(st["whatsapp"].get("state") or "unknown")
                info["paired_phone"] = re.sub(r"\D", "", str(st.get("paired_phone") or ""))
    return info


def list_accounts(check=True):
    return [describe(n, check) for n in names()]


def run_script(args, timeout=120):
    """Run accounts.sh with already-validated arguments. Returns (ok, output)."""
    if not SCRIPT.is_file():
        raise AccountError("accounts.sh is missing. Re-run the installer.")
    env = {k: v for k, v in os.environ.items() if not k.startswith("WAI_")}
    try:
        p = subprocess.run(["bash", str(SCRIPT)] + list(args), cwd=str(config.ROOT), env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AccountError("That took too long. Check ./accounts.sh list in Termux.")
    return p.returncode == 0, (p.stdout + p.stderr).strip()[-1500:]


def _check_name(name, allow_main):
    name = (name or "").strip().lower()
    if name == "main" and allow_main:
        return name
    if not valid_name(name) or name == "main":
        raise AccountError("Account names use 2 to 24 letters, numbers, - or _ (for example: work, mum, shop). \"main\" is reserved.")
    return name


def add(name):
    name = _check_name(name, allow_main=False)
    if len(name) < 2:
        raise AccountError("Account names need at least 2 characters.")
    if name in names():
        raise AccountError("An account called %s already exists." % name)
    ok, out = run_script(["add", name, "--quiet"])
    if not ok:
        raise AccountError(out.splitlines()[-1] if out else "Could not create the account.")
    ok, out = run_script(["start", name, "--quiet"], timeout=150)
    if not ok:
        raise AccountError("Account created, but it did not start: %s" % (out.splitlines()[-1] if out else "unknown problem"))
    return describe(name)


def start(name):
    name = _check_name(name, allow_main=True)
    if name not in names():
        raise AccountError("There is no account called %s." % name)
    ok, out = run_script(["start", name, "--quiet"], timeout=150)
    if not ok:
        raise AccountError(out.splitlines()[-1] if out else "It did not start.")
    return describe(name)


def stop(name):
    name = _check_name(name, allow_main=True)
    if name == config.ACCOUNT:
        raise AccountError("You are talking to this account right now. Stop it from another account or from Termux with ./accounts.sh stop %s" % name)
    if name not in names():
        raise AccountError("There is no account called %s." % name)
    ok, out = run_script(["stop", name, "--quiet"])
    if not ok:
        raise AccountError(out.splitlines()[-1] if out else "It did not stop.")
    return describe(name)
