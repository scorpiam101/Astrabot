"""Configuration helpers: reads and safely rewrites the private .env file."""
import os
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.environ.get("WAI_ENV_FILE") or (ROOT / ".env"))
DATA_DIR = Path(os.environ.get("WAI_DATA_DIR") or (ROOT / "data"))
DB_PATH = DATA_DIR / "app.db"
LOG_DIR = Path(os.environ.get("WAI_LOG_DIR") or (ROOT / "logs"))
# Every WhatsApp number runs as its own account with its own folder, database, session and keys.
# "main" is the original installation; anything else lives in accounts/<name>/.
ACCOUNT = (os.environ.get("WAI_ACCOUNT") or "main").strip() or "main"
ACCOUNTS_DIR = ROOT / "accounts"

_lock = threading.Lock()
_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _parse(text):
    out = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        value = m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[m.group(1)] = value
    return out


def load_env():
    try:
        return _parse(ENV_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def get(key, default=""):
    value = load_env().get(key)
    return default if value is None else value


def flask_port():
    try:
        return int(get("FLASK_PORT", "5000"))
    except ValueError:
        return 5000


def bridge_port():
    try:
        return int(get("BRIDGE_PORT", "5001"))
    except ValueError:
        return 5001


def set_value(key, value):
    """Atomically update one KEY=value line in .env (permissions 600)."""
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
        raise ValueError("Invalid setting name.")
    if re.search(r"[\s\"'\\$`#]", value):
        raise ValueError("Value contains characters that are not allowed.")
    with _lock:
        try:
            lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        new_line = f"{key}={value}"
        replaced = False
        for i, line in enumerate(lines):
            m = _LINE.match(line)
            if m and m.group(1) == key and not line.lstrip().startswith("#"):
                lines[i] = new_line
                replaced = True
                break
        if not replaced:
            lines.append(new_line)
        tmp = ENV_PATH.with_name(ENV_PATH.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(str(tmp), str(ENV_PATH))
        os.chmod(str(ENV_PATH), 0o600)
