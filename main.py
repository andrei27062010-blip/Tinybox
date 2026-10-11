#!/usr/bin/env python3
"""Tinybox: lightweight, dependency-free server dashboard."""
import json
import os
import platform
import random
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
STARTED_AT = time.monotonic()


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def read_int(path):
    value = read_text(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def memory_metrics():
    # Prefer cgroup limits/current usage so container metrics describe this container.
    for base in (Path("/sys/fs/cgroup"), Path("/sys/fs/cgroup/memory")):
        current_path = base / ("memory.current" if base.name == "cgroup" else "memory.usage_in_bytes")
        limit_path = base / ("memory.max" if base.name == "cgroup" else "memory.limit_in_bytes")
        current = read_int(current_path)
        limit_text = read_text(limit_path)
        try:
            limit = int(limit_text) if limit_text and limit_text != "max" else None
        except ValueError:
            limit = None
        if current is not None and limit is not None and 0 < limit < (1 << 60):
            return {"memTotalBytes": limit, "memAvailBytes": max(0, limit - current),
                    "memorySource": "container cgroup"}

    values = {}
    info = read_text("/proc/meminfo")
    if info:
        for line in info.splitlines():
            key, separator, rest = line.partition(":")
            fields = rest.split()
            if separator and key in ("MemTotal", "MemAvailable") and fields:
                try:
                    values[key] = int(fields[0]) * 1024
                except ValueError:
                    pass
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    return {"memTotalBytes": total, "memAvailBytes": available,
            "memorySource": "host /proc/meminfo" if total else "unavailable"}


def collect_metrics():
    uptime = read_text("/proc/uptime")
    load = read_text("/proc/loadavg")
    process_status = read_text("/proc/self/status") or ""
    process_rss = 0
    process_threads = 1
    for line in process_status.splitlines():
        if line.startswith("VmRSS:"):
            try:
                process_rss = int(line.split()[1]) * 1024
            except (ValueError, IndexError):
                pass
        elif line.startswith("Threads:"):
            try:
                process_threads = int(line.split()[1])
            except (ValueError, IndexError):
                pass

    loads = [None, None, None]
    if load:
        try:
            loads = [float(value) for value in load.split()[:3]]
        except ValueError:
            pass

    memory = memory_metrics()
    return {
        "hostname": platform.node() or "unknown",
        "os": platform.system().lower(),
        "arch": platform.machine() or "unknown",
        "cpus": os.cpu_count() or 1,
        "uptimeSeconds": float(uptime.split()[0]) if uptime else 0,
        "load1": loads[0], "load5": loads[1], "load15": loads[2],
        "memTotalBytes": memory["memTotalBytes"],
        "memAvailBytes": memory["memAvailBytes"],
        "memorySource": memory["memorySource"],
        "processRssBytes": process_rss,
        "threads": process_threads,
        "pythonVersion": platform.python_version(),
        "collectedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "processUptimeSeconds": time.monotonic() - STARTED_AT,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "Tinybox/1.0"
    sys_version = ""

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def send_bytes(self, status, body, content_type, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def route(self):
        path = urlsplit(self.path).path
        if path == "/api/health":
            self.send_bytes(200, b'{"status":"ok"}\n', "application/json; charset=utf-8")
            return
        if path == "/api/metrics":
            body = json.dumps(collect_metrics(), separators=(",", ":")).encode("utf-8")
            self.send_bytes(200, body, "application/json; charset=utf-8")
            return
        files = {
            "/": ("index.html", "text/html; charset=utf-8", "no-cache"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8", "public, max-age=300"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8", "public, max-age=300"),
        }
        item = files.get(path)
        if item is None:
            self.send_bytes(404, b"Not found\n", "text/plain; charset=utf-8")
            return
        filename, content_type, cache = item
        try:
            body = (WEB / filename).read_bytes()
        except OSError:
            self.send_bytes(500, b"Dashboard asset unavailable\n", "text/plain; charset=utf-8")
            return
        self.send_bytes(200, body, content_type, cache)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.log_date_time_string(), fmt % args), flush=True)



# --- Tiny pet Telegram bot (optional; enabled only when BOT_TOKEN is configured) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = Path(os.environ.get("PET_DB_PATH", str(ROOT / "data" / "pets.sqlite3"))).expanduser()
TELEGRAM_API = "https://api.telegram.org/bot" + BOT_TOKEN


def init_pet_db():
    """Create the database directory and schema on first run; keep existing data."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS pets (
                user_id INTEGER PRIMARY KEY,
                pet_name TEXT NOT NULL DEFAULT 'Бублик',
                food INTEGER NOT NULL DEFAULT 3,
                happiness INTEGER NOT NULL DEFAULT 3,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()
    print("Pet database ready at %s" % DB_PATH, flush=True)


def pet_db_action(user_id, action=None):
    """Apply an action and return the current pet state. Each call uses a short-lived connection."""
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        db.row_factory = sqlite3.Row
        db.execute(
            "INSERT OR IGNORE INTO pets (user_id) VALUES (?)",
            (int(user_id),)
        )
        if action == "pet":
            db.execute("""
                UPDATE pets SET happiness = MIN(10, happiness + 1),
                    updated_at = CURRENT_TIMESTAMP WHERE user_id = ?
            """, (int(user_id),))
        elif action == "feed":
            db.execute("""
                UPDATE pets SET food = MIN(10, food + 1),
                    happiness = MIN(10, happiness + 1),
                    updated_at = CURRENT_TIMESTAMP WHERE user_id = ?
            """, (int(user_id),))
        db.commit()
        row = db.execute(
            "SELECT pet_name, food, happiness FROM pets WHERE user_id = ?",
            (int(user_id),)
        ).fetchone()
        return dict(row)


def safe_error_message(exc):
    """Return a useful error detail without ever printing the bot token."""
    message = str(exc) or type(exc).__name__
    if BOT_TOKEN:
        message = message.replace(BOT_TOKEN, "[REDACTED]")
    return message[:300]


def telegram_call(method, payload=None, timeout=40):
    """Call the Bot API; never log the token or the full API URL."""
    url = TELEGRAM_API + "/" + method
    data = json.dumps(payload or {}).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"},
                      method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Include Telegram's status/description, but never the request URL (it contains the token).
        try:
            body = exc.read().decode("utf-8", errors="replace")
            details = json.loads(body).get("description", body[:200])
        except (ValueError, OSError):
            details = "no response details"
        raise RuntimeError("%s returned HTTP %s: %s" %
                           (method, exc.code, details)) from exc
    except URLError as exc:
        # Include the network-level reason (DNS, TLS, connection reset, etc.)
        # without ever logging the API URL, which contains the bot token.
        reason = getattr(exc, "reason", None)
        detail = "%s: %s" % (type(reason).__name__, reason) if reason is not None else "network error"
        raise RuntimeError("%s request failed: %s" % (method, detail[:180])) from exc
    except (TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError("%s request failed: %s" % (method, type(exc).__name__)) from exc
    if not result.get("ok"):
        # Telegram's response description is useful but avoid echoing credentials.
        raise RuntimeError("%s returned an API error: %s" %
                           (method, result.get("description", "unknown error")))
    return result.get("result")


def pet_keyboard():
    return {"inline_keyboard": [[
        {"text": "🐾 Погладить", "callback_data": "pet"},
        {"text": "🍖 Покормить", "callback_data": "feed"}
    ]]}


PET_REACTIONS = {
    "pet": [
        "🥰 Бублик довольно виляет хвостиком!",
        "🐾 Бублик подставил ушко: почеши ещё!",
        "💖 Бублик прижался к твоей руке.",
        "🐶 Гав-гав! Кажется, Бублик счастлив!",
        "✨ Бублик перевернулся на спинку и просит ещё ласки!"
    ],
    "feed": [
        "😋 Ням-ням! Бублик уплетает угощение!",
        "🍖 Бублик радостно подпрыгнул от запаха еды!",
        "🐕 Бублик всё съел и довольно облизывается.",
        "❤️ Бублик благодарно смотрит на тебя.",
        "🌟 Вкусняшка пришлась Бублику по душе!"
    ]
}


def pet_message(state, greeting=False, reaction=None):
    title = "🐶 Знакомься, %s!" % state["pet_name"] if greeting else "🐶 %s" % state["pet_name"]
    reaction_line = (reaction + "\n\n") if reaction else ""
    return (
        title + "\n\n"
        + reaction_line
        + "🍖 Сытость: %s/10\n" % state["food"]
        + "💖 Настроение: %s/10\n\n" % state["happiness"]
        + "Нажимай кнопки, чтобы заботиться о своём питомце."
    )


def handle_pet_update(update):
    callback = update.get("callback_query")
    if callback:
        data = callback.get("data", "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        user = callback.get("from") or {}
        user_id = user.get("id")
        if not user_id or data not in ("pet", "feed"):
            telegram_call("answerCallbackQuery", {"callback_query_id": callback["id"]})
            return
        # Acknowledge the tap quickly, but don't let a transient Telegram/network
        # failure prevent the actual pet action from being applied.
        try:
            telegram_call(
                "answerCallbackQuery",
                {"callback_query_id": callback["id"]},
                timeout=8
            )
        except RuntimeError as exc:
            print("Pet bot callback acknowledgement failed: %s; continuing action." %
                  safe_error_message(exc), flush=True)
        state = pet_db_action(user_id, data)
        reaction = random.choice(PET_REACTIONS[data])
        if chat.get("id") is not None and message.get("message_id") is not None:
            try:
                telegram_call("editMessageText", {
                    "chat_id": chat["id"],
                    "message_id": message["message_id"],
                    "text": pet_message(state, reaction=reaction),
                    "reply_markup": pet_keyboard()
                })
            except RuntimeError as exc:
                # Telegram returns this when the rendered text/keyboard did not change.
                if "message is not modified" not in str(exc).lower():
                    raise
        return

    message = update.get("message") or update.get("edited_message") or {}
    text_value = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    if not chat.get("id") or not user.get("id"):
        return
    if text_value.startswith("/start") or text_value.startswith("/newpet"):
        state = pet_db_action(user["id"])
        telegram_call("sendMessage", {
            "chat_id": chat["id"],
            "text": pet_message(state, greeting=True),
            "reply_markup": pet_keyboard()
        })
    elif text_value.startswith("/help"):
        telegram_call("sendMessage", {
            "chat_id": chat["id"],
            "text": "Команды:\n/start — познакомиться с питомцем\n/help — помощь\n\nУхаживай за питомцем кнопками под сообщением."
        })


def bot_polling_loop():
    """Long-poll updates in a background thread so the web dashboard stays available."""
    print("Telegram pet bot polling thread started.", flush=True)
    offset = None
    backoff = 2
    while True:
        try:
            payload = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            updates = telegram_call("getUpdates", payload, timeout=40) or []
            backoff = 2
            for update in updates:
                offset = max(offset or 0, int(update.get("update_id", 0)) + 1)
                try:
                    handle_pet_update(update)
                except (RuntimeError, sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
                    print("Pet bot update failed (%s): %s." %
                          (type(exc).__name__, safe_error_message(exc)), flush=True)
        except Exception as exc:
            print("Pet bot polling error (%s): %s; retrying in %ss." %
                  (type(exc).__name__, safe_error_message(exc), backoff), flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def start_pet_bot():
    if not BOT_TOKEN:
        print("BOT_TOKEN is not set; Telegram bot is disabled. Web dashboard remains active.", flush=True)
        return
    try:
        init_pet_db()
    except (sqlite3.Error, OSError) as exc:
        # Don't take down the dashboard if storage is unavailable.
        print("Pet database initialization failed (%s): %s; bot is disabled for this run." %
              (type(exc).__name__, safe_error_message(exc)), flush=True)
        return
    thread = threading.Thread(target=bot_polling_loop, name="telegram-pet-bot", daemon=True)
    thread.start()


def main():
    start_pet_bot()
    raw_port = os.environ.get("PORT", "8080").strip()
    try:
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("PORT must be an integer between 1 and 65535")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print("Tinybox (Python %s) listening on 0.0.0.0:%s" %
          (platform.python_version(), port), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
