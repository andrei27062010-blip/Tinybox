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
    """Create character storage and migrate existing single-pet data once."""
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
        db.execute("""
            CREATE TABLE IF NOT EXISTS pet_characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                character_type TEXT NOT NULL DEFAULT 'dog',
                pet_name TEXT NOT NULL,
                food INTEGER NOT NULL DEFAULT 3,
                happiness INTEGER NOT NULL DEFAULT 3,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_pet_characters_user
            ON pet_characters(user_id, id)
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS pet_preferences (
                user_id INTEGER PRIMARY KEY,
                active_pet_id INTEGER
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS pet_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        migrated = db.execute(
            "SELECT value FROM pet_meta WHERE key = 'legacy_pets_migrated'"
        ).fetchone()
        if not migrated:
            legacy_rows = db.execute(
                "SELECT user_id, pet_name, food, happiness, created_at, updated_at FROM pets"
            ).fetchall()
            for row in legacy_rows:
                cur = db.execute("""
                    INSERT INTO pet_characters
                        (user_id, character_type, pet_name, food, happiness, created_at, updated_at)
                    VALUES (?, 'dog', ?, ?, ?, ?, ?)
                """, row)
                db.execute("""
                    INSERT OR IGNORE INTO pet_preferences(user_id, active_pet_id)
                    VALUES (?, ?)
                """, (row[0], cur.lastrowid))
            db.execute(
                "INSERT INTO pet_meta(key, value) VALUES ('legacy_pets_migrated', '1')"
            )
        db.commit()
    print("Pet database ready at %s" % DB_PATH, flush=True)


CHARACTER_TYPES = {
    "dog": {"emoji": "🐶", "default_name": "Бублик", "label": "Собачка"},
    "cat": {"emoji": "🐱", "default_name": "Плюша", "label": "Кошечка"},
    "fox": {"emoji": "🦊", "default_name": "Рыжик", "label": "Лисёнок"},
}


def pet_list(user_id):
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT id, character_type, pet_name, food, happiness FROM pet_characters "
            "WHERE user_id = ? ORDER BY id", (int(user_id),)
        ).fetchall()]


def pet_get_active(user_id, create_if_missing=True):
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("""
            SELECT c.id, c.user_id, c.character_type, c.pet_name, c.food, c.happiness
            FROM pet_preferences p
            JOIN pet_characters c ON c.id = p.active_pet_id AND c.user_id = p.user_id
            WHERE p.user_id = ?
        """, (int(user_id),)).fetchone()
        if row:
            return dict(row)
    if create_if_missing:
        return pet_create(user_id, "dog")
    return None


def pet_create(user_id, character_type="dog", pet_name=None):
    if character_type not in CHARACTER_TYPES:
        character_type = "dog"
    name = (pet_name or CHARACTER_TYPES[character_type]["default_name"]).strip()[:24]
    if not name:
        name = CHARACTER_TYPES[character_type]["default_name"]
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        cur = db.execute("""
            INSERT INTO pet_characters(user_id, character_type, pet_name)
            VALUES (?, ?, ?)
        """, (int(user_id), character_type, name))
        pet_id = cur.lastrowid
        db.execute("""
            INSERT INTO pet_preferences(user_id, active_pet_id) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET active_pet_id = excluded.active_pet_id
        """, (int(user_id), pet_id))
        db.commit()
    return pet_get_active(user_id, create_if_missing=False)


def pet_select(user_id, pet_id):
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        row = db.execute(
            "SELECT id FROM pet_characters WHERE id = ? AND user_id = ?",
            (int(pet_id), int(user_id))
        ).fetchone()
        if not row:
            return None
        db.execute("""
            INSERT INTO pet_preferences(user_id, active_pet_id) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET active_pet_id = excluded.active_pet_id
        """, (int(user_id), int(pet_id)))
        db.commit()
    return pet_get_active(user_id, create_if_missing=False)


def pet_rename(user_id, pet_name):
    name = " ".join((pet_name or "").split())[:24]
    if not name:
        return None
    state = pet_get_active(user_id)
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        db.execute("""
            UPDATE pet_characters SET pet_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ?
        """, (name, state["id"], int(user_id)))
        db.commit()
    return pet_get_active(user_id, create_if_missing=False)


def pet_delete(user_id, pet_id=None):
    state = pet_get_active(user_id, create_if_missing=False)
    if not state:
        return None
    target_id = int(pet_id or state["id"])
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        row = db.execute(
            "SELECT id FROM pet_characters WHERE id = ? AND user_id = ?",
            (target_id, int(user_id))
        ).fetchone()
        if not row:
            return None
        db.execute("DELETE FROM pet_characters WHERE id = ? AND user_id = ?",
                   (target_id, int(user_id)))
        if state["id"] == target_id:
            next_row = db.execute(
                "SELECT id FROM pet_characters WHERE user_id = ? ORDER BY id LIMIT 1",
                (int(user_id),)
            ).fetchone()
            if next_row:
                db.execute("""
                    INSERT INTO pet_preferences(user_id, active_pet_id) VALUES (?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET active_pet_id = excluded.active_pet_id
                """, (int(user_id), next_row[0]))
            else:
                db.execute("DELETE FROM pet_preferences WHERE user_id = ?", (int(user_id),))
        db.commit()
    return pet_get_active(user_id, create_if_missing=False)


def pet_db_action(user_id, action=None):
    """Apply an action to the active character and return its state."""
    state = pet_get_active(user_id)
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        if action == "pet":
            db.execute("""
                UPDATE pet_characters SET happiness = MIN(10, happiness + 1),
                    updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?
            """, (state["id"], int(user_id)))
        elif action == "feed":
            db.execute("""
                UPDATE pet_characters SET food = MIN(10, food + 1),
                    happiness = MIN(10, happiness + 1),
                    updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?
            """, (state["id"], int(user_id)))
        db.commit()
    return pet_get_active(user_id, create_if_missing=False)

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


PET_REACTIONS = {
    "dog": {
        "pet": [
            "🥰 {name} радостно виляет хвостом!",
            "🐾 {name} подставляет ушко: почеши ещё!",
            "💖 {name} прижимается к твоей руке.",
            "🐶 Гав-гав! {name} счастлив(а) рядом с тобой!",
            "✨ {name} переворачивается на спинку за лаской!"
        ],
        "feed": [
            "😋 Ням-ням! {name} с аппетитом уплетает угощение!",
            "🍖 {name} подпрыгивает от запаха еды!",
            "🐕 {name} всё съел(а) и довольно облизывается.",
            "❤️ {name} благодарно смотрит на тебя.",
            "🌟 Вкусняшка пришлась {name} по душе!"
        ]
    },
    "cat": {
        "pet": [
            "😽 {name} довольно мурлычет и щурится.",
            "🐈 {name} трётся головой о твою ладонь.",
            "💗 {name} разрешает погладить себя ещё немного.",
            "✨ {name} важно подставляет пушистую спинку.",
            "😻 Мур-мур! Кажется, ты заслужил(а) кошачью любовь."
        ],
        "feed": [
            "🐟 {name} внимательно проверяет миску — одобрено!",
            "😋 {name} съедает угощение и умывается.",
            "🐾 {name} довольно мурлычет у полной миски.",
            "💖 {name} делает вид, что корм был именно так себе… но всё съедает.",
            "🥣 Миска пуста, а {name} уже смотрит на тебя с надеждой."
        ]
    },
    "fox": {
        "pet": [
            "🦊 {name} хитро улыбается и машет пушистым хвостом.",
            "✨ {name} подпрыгивает от неожиданной ласки.",
            "🍂 {name} доверчиво прижимается к тебе.",
            "💛 {name} довольно щурится на солнышке.",
            "🐾 {name} кружится вокруг тебя, приглашая играть."
        ],
        "feed": [
            "🍓 {name} ловко хватает угощение и прячет самое вкусное!",
            "🦊 {name} проверяет еду на хитрость — и съедает всё.",
            "😋 {name} довольно облизывается: отличный улов!",
            "🌰 {name} припрятывает кусочек на потом.",
            "❤️ {name} благодарит тебя лукавым взглядом."
        ]
    }
}


def pet_keyboard():
    return {"inline_keyboard": [
        [
            {"text": "🐾 Погладить", "callback_data": "pet"},
            {"text": "🍖 Покормить", "callback_data": "feed"}
        ],
        [
            {"text": "🔄 Выбрать персонажа", "callback_data": "list"},
            {"text": "➕ Создать", "callback_data": "new"}
        ],
        [
            {"text": "✏️ Переименовать", "callback_data": "rename_help"},
            {"text": "🗑 Удалить", "callback_data": "delete_prompt"}
        ]
    ]}


def character_keyboard():
    return {"inline_keyboard": [
        [
            {"text": "%s %s" % (item["emoji"], item["label"]),
             "callback_data": "create:" + kind}
        ]
        for kind, item in CHARACTER_TYPES.items()
    ] + [[{"text": "↩️ Назад", "callback_data": "back"}]]}


def pet_list_keyboard(user_id):
    characters = pet_list(user_id)
    if not characters:
        return character_keyboard()
    rows = []
    active = pet_get_active(user_id, create_if_missing=False)
    for state in characters:
        info = CHARACTER_TYPES.get(state["character_type"], CHARACTER_TYPES["dog"])
        marker = "✓ " if active and active["id"] == state["id"] else ""
        rows.append([{
            "text": "%s%s %s" % (marker, info["emoji"], state["pet_name"]),
            "callback_data": "choose:%s" % state["id"]
        }])
    rows.append([{"text": "➕ Создать персонажа", "callback_data": "new"}])
    rows.append([{"text": "↩️ Назад", "callback_data": "back"}])
    return {"inline_keyboard": rows}


def delete_keyboard(state):
    return {"inline_keyboard": [[
        {"text": "🗑 Да, удалить", "callback_data": "delete_yes:%s" % state["id"]},
        {"text": "Отмена", "callback_data": "delete_no"}
    ]]}


def pet_message(state, greeting=False, reaction=None):
    info = CHARACTER_TYPES.get(state.get("character_type"), CHARACTER_TYPES["dog"])
    title = "%s Знакомься, %s!" % (info["emoji"], state["pet_name"]) if greeting else "%s %s" % (info["emoji"], state["pet_name"])
    personalities = {
        "dog": "Я люблю ласку, вкусняшки и быть рядом с тобой.",
        "cat": "Я независимая натура, но ласку и вкусняшки ценю.",
        "fox": "Я любопытный хитрец: давай играть и искать приключения!"
    }
    reaction_line = (reaction + "\n\n") if reaction else ""
    return (
        title + "\n\n"
        + reaction_line
        + personalities.get(state.get("character_type"), personalities["dog"]) + "\n\n"
        + "🍖 Сытость: %s/10\n" % state["food"]
        + "💖 Настроение: %s/10\n\n" % state["happiness"]
        + "Выбирай действие или управляй своими персонажами кнопками ниже."
    )


def send_pet_card(chat_id, state, greeting=False, reaction=None):
    telegram_call("sendMessage", {
        "chat_id": chat_id,
        "text": pet_message(state, greeting=greeting, reaction=reaction),
        "reply_markup": pet_keyboard()
    })


def handle_pet_callback(callback):
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    user = callback.get("from") or {}
    user_id = user.get("id")
    callback_id = callback.get("id")
    if callback_id:
        try:
            telegram_call("answerCallbackQuery", {"callback_query_id": callback_id}, timeout=8)
        except RuntimeError as exc:
            print("Pet bot callback acknowledgement failed: %s; continuing." %
                  safe_error_message(exc), flush=True)

    if not user_id:
        return
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if data == "pet" or data == "feed":
        state = pet_db_action(user_id, data)
        reactions = PET_REACTIONS.get(state["character_type"], PET_REACTIONS["dog"])
        reaction = random.choice(reactions[data]).format(name=state["pet_name"])
        if chat_id is not None and message_id is not None:
            try:
                telegram_call("editMessageText", {
                    "chat_id": chat_id, "message_id": message_id,
                    "text": pet_message(state, reaction=reaction),
                    "reply_markup": pet_keyboard()
                }, timeout=12)
            except RuntimeError as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
        return

    if data == "new":
        if chat_id is not None and message_id is not None:
            telegram_call("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": "Кого создадим? Выбери тип персонажа:",
                "reply_markup": character_keyboard()
            }, timeout=12)
        return

    if data.startswith("create:"):
        kind = data.split(":", 1)[1]
        if kind not in CHARACTER_TYPES:
            return
        state = pet_create(user_id, kind)
        if chat_id is not None and message_id is not None:
            telegram_call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": pet_message(state, greeting=True) + "\n\nЧтобы дать имя, отправь /rename НовоеИмя",
                "reply_markup": pet_keyboard()
            }, timeout=12)
        return

    if data == "list":
        characters = pet_list(user_id)
        text = "Твои персонажи — выбери активного:" if characters else "У тебя пока нет персонажей. Создадим первого?"
        if chat_id is not None and message_id is not None:
            telegram_call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": text,
                "reply_markup": pet_list_keyboard(user_id)
            }, timeout=12)
        return

    if data.startswith("choose:"):
        try:
            pet_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        state = pet_select(user_id, pet_id)
        if state and chat_id is not None and message_id is not None:
            telegram_call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": "Теперь активный персонаж:\n\n" + pet_message(state),
                "reply_markup": pet_keyboard()
            }, timeout=12)
        return

    if data == "rename_help":
        state = pet_get_active(user_id, create_if_missing=False)
        text = ("Отправь команду /rename НовоеИмя\n"
                "Имя может содержать пробелы и быть длиной до 24 символов.")
        if not state:
            text = "Сначала создай персонажа кнопкой «Создать»."
        if chat_id is not None:
            telegram_call("sendMessage", {"chat_id": chat_id, "text": text}, timeout=12)
        return

    if data == "delete_prompt":
        state = pet_get_active(user_id, create_if_missing=False)
        if not state:
            text = "У тебя пока нет персонажей для удаления."
            keyboard = character_keyboard()
        else:
            text = "Точно удалить персонажа «%s»? Это действие нельзя отменить." % state["pet_name"]
            keyboard = delete_keyboard(state)
        if chat_id is not None and message_id is not None:
            telegram_call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": text, "reply_markup": keyboard
            }, timeout=12)
        return

    if data.startswith("delete_yes:"):
        try:
            pet_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        state = pet_delete(user_id, pet_id)
        if not state:
            state = pet_get_active(user_id, create_if_missing=False)
        text = ("Персонаж удалён.\n\n" + pet_message(state)) if state else (
            "Персонаж удалён. У тебя больше нет питомцев — создай нового!"
        )
        keyboard = pet_keyboard() if state else character_keyboard()
        if chat_id is not None and message_id is not None:
            telegram_call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": text, "reply_markup": keyboard
            }, timeout=12)
        return

    if data == "delete_no":
        state = pet_get_active(user_id, create_if_missing=False)
        if chat_id is not None and message_id is not None:
            telegram_call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": pet_message(state) if state else "Удаление отменено. Создай персонажа, когда будешь готов.",
                "reply_markup": pet_keyboard() if state else character_keyboard()
            }, timeout=12)
        return

    if data == "back":
        state = pet_get_active(user_id, create_if_missing=False)
        if chat_id is not None and message_id is not None:
            telegram_call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": pet_message(state) if state else "Давай создадим твоего первого персонажа!",
                "reply_markup": pet_keyboard() if state else character_keyboard()
            }, timeout=12)


def handle_pet_update(update):
    callback = update.get("callback_query")
    if callback:
        handle_pet_callback(callback)
        return

    message = update.get("message") or update.get("edited_message") or {}
    text_value = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    if not chat.get("id") or not user.get("id"):
        return

    command = text_value.split()[0].split("@", 1)[0].lower() if text_value else ""
    args = text_value.split(maxsplit=1)[1].strip() if len(text_value.split(maxsplit=1)) > 1 else ""

    if command == "/start":
        state = pet_get_active(user["id"], create_if_missing=False)
        if not state:
            state = pet_create(user["id"], "dog")
            send_pet_card(chat["id"], state, greeting=True)
            telegram_call("sendMessage", {
                "chat_id": chat["id"],
                "text": "Это твой первый персонаж! Можешь создать котика или лисёнка кнопкой «Создать», а имя изменить командой /rename НовоеИмя."
            })
        else:
            send_pet_card(chat["id"], state, greeting=True)
    elif command == "/newpet":
        if args:
            pieces = args.split(maxsplit=1)
            kind = pieces[0].lower()
            aliases = {"dog": "dog", "собака": "dog", "пёс": "dog",
                       "cat": "cat", "кот": "cat", "кошка": "cat",
                       "fox": "fox", "лиса": "fox", "лис": "fox"}
            kind = aliases.get(kind, kind)
            if kind not in CHARACTER_TYPES:
                telegram_call("sendMessage", {
                    "chat_id": chat["id"],
                    "text": "Типы персонажей: dog (собака), cat (кот), fox (лис). Например: /newpet cat Плюша"
                })
                return
            name = pieces[1] if len(pieces) > 1 else None
            state = pet_create(user["id"], kind, name)
            send_pet_card(chat["id"], state, greeting=True)
        else:
            telegram_call("sendMessage", {
                "chat_id": chat["id"], "text": "Кого создадим? Выбери тип персонажа:",
                "reply_markup": character_keyboard()
            })
    elif command in ("/choose", "/mypets"):
        characters = pet_list(user["id"])
        telegram_call("sendMessage", {
            "chat_id": chat["id"],
            "text": "Твои персонажи — выбери активного:" if characters else "У тебя пока нет персонажей. Создадим первого?",
            "reply_markup": pet_list_keyboard(user["id"])
        })
    elif command == "/rename":
        if not args:
            telegram_call("sendMessage", {
                "chat_id": chat["id"], "text": "Использование: /rename Новое имя"
            })
            return
        state = pet_rename(user["id"], args)
        if state:
            send_pet_card(chat["id"], state)
        else:
            telegram_call("sendMessage", {
                "chat_id": chat["id"], "text": "Сначала создай персонажа командой /newpet."
            })
    elif command == "/delete":
        state = pet_get_active(user["id"], create_if_missing=False)
        if state:
            telegram_call("sendMessage", {
                "chat_id": chat["id"],
                "text": "Точно удалить персонажа «%s»?" % state["pet_name"],
                "reply_markup": delete_keyboard(state)
            })
        else:
            telegram_call("sendMessage", {
                "chat_id": chat["id"], "text": "У тебя пока нет персонажей."
            })
    elif command == "/help":
        telegram_call("sendMessage", {
            "chat_id": chat["id"],
            "text": (
                "Команды:\n/start — открыть активного персонажа\n"
                "/newpet — создать персонажа\n/newpet cat Плюша — создать кота с именем\n"
                "/mypets — список и выбор персонажа\n/rename НовоеИмя — переименовать активного\n"
                "/delete — удалить активного персонажа\n/help — помощь\n\n"
                "У собаки, кота и лисёнка свои реплики. Прогресс и имя каждого персонажа сохраняются отдельно."
            )
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
