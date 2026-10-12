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
RENAME_PENDING = set()
CREATE_PENDING = {}


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
                food INTEGER NOT NULL DEFAULT 75,
                happiness INTEGER NOT NULL DEFAULT 70,
                water INTEGER NOT NULL DEFAULT 70,
                energy INTEGER NOT NULL DEFAULT 80,
                cleanliness INTEGER NOT NULL DEFAULT 75,
                level INTEGER NOT NULL DEFAULT 1,
                xp INTEGER NOT NULL DEFAULT 0,
                last_needs_update INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate columns on existing databases without deleting saved pets.
        existing_columns = {row[1] for row in db.execute("PRAGMA table_info(pet_characters)")}
        old_scale = "water" not in existing_columns
        for column, definition in (
            ("water", "INTEGER NOT NULL DEFAULT 70"),
            ("energy", "INTEGER NOT NULL DEFAULT 80"),
            ("cleanliness", "INTEGER NOT NULL DEFAULT 75"),
            ("level", "INTEGER NOT NULL DEFAULT 1"),
            ("xp", "INTEGER NOT NULL DEFAULT 0"),
            ("last_needs_update", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in existing_columns:
                db.execute("ALTER TABLE pet_characters ADD COLUMN %s %s" % (column, definition))
        if old_scale:
            db.execute("UPDATE pet_characters SET food = MIN(100, food * 10), happiness = MIN(100, happiness * 10)")
        db.execute("UPDATE pet_characters SET last_needs_update = CAST(strftime('%s','now') AS INTEGER) WHERE last_needs_update = 0")
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
            CREATE TABLE IF NOT EXISTS pet_action_cooldowns (
                pet_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                last_at INTEGER NOT NULL,
                PRIMARY KEY (pet_id, action)
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
                    VALUES (?, 'dog', ?, MIN(100, ? * 10), MIN(100, ? * 10), ?, ?)
                """, row)
                db.execute("""
                    INSERT OR IGNORE INTO pet_preferences(user_id, active_pet_id)
                    VALUES (?, ?)
                """, (row[0], cur.lastrowid))
            db.execute(
                "INSERT INTO pet_meta(key, value) VALUES ('legacy_pets_migrated', '1')"
            )
        # Include newly migrated legacy characters in the timestamp initialization.
        db.execute("UPDATE pet_characters SET last_needs_update = CAST(strftime('%s','now') AS INTEGER) WHERE last_needs_update = 0")
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
            "SELECT id, character_type, pet_name, food, water, energy, cleanliness, happiness, level, xp FROM pet_characters "
            "WHERE user_id = ? ORDER BY id", (int(user_id),)
        ).fetchall()]


def pet_get_active(user_id, create_if_missing=True):
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("""
            SELECT c.*
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
    name = " ".join((pet_name or "").split())[:24]
    if not name:
        return None
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        if db.execute("SELECT 1 FROM pet_characters WHERE user_id = ? LIMIT 1",
                      (int(user_id),)).fetchone():
            return None
        cur = db.execute("""
            INSERT INTO pet_characters
                (user_id, character_type, pet_name, food, water, energy, cleanliness,
                 happiness, level, xp, last_needs_update)
            VALUES (?, ?, ?, 75, 70, 80, 75, 70, 1, 0, ?)
        """, (int(user_id), character_type, name, int(time.time())))
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


def pet_apply_needs_decay(user_id):
    """Randomly nudge needs down over time, with a one-day absence cap."""
    state = pet_get_active(user_id, create_if_missing=False)
    if not state:
        return None
    now = int(time.time())
    last = int(state.get("last_needs_update") or now)
    raw_elapsed = max(0, now - last)
    elapsed = min(raw_elapsed, 24 * 3600)
    full_hours = elapsed // 3600
    if full_hours:
        losses = {"food": 0, "water": 0, "happiness": 0, "cleanliness": 0}
        for _ in range(int(full_hours)):
            if random.random() < 0.35:
                need = random.choice(tuple(losses))
                losses[need] += random.randint(1, 3)
        next_update = now if raw_elapsed > 24 * 3600 else last + full_hours * 3600
        with sqlite3.connect(str(DB_PATH), timeout=10) as db:
            db.execute("""
                UPDATE pet_characters
                SET food = MAX(0, food - ?),
                    water = MAX(0, water - ?),
                    happiness = MAX(0, happiness - ?),
                    cleanliness = MAX(0, cleanliness - ?),
                    last_needs_update = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
            """, (losses["food"], losses["water"], losses["happiness"],
                  losses["cleanliness"], next_update, state["id"], int(user_id)))
            db.commit()
    elif raw_elapsed > 24 * 3600:
        with sqlite3.connect(str(DB_PATH), timeout=10) as db:
            db.execute("UPDATE pet_characters SET last_needs_update = ? WHERE id = ? AND user_id = ?",
                       (now, state["id"], int(user_id)))
            db.commit()
    return pet_get_active(user_id, create_if_missing=False)


ACTION_COOLDOWN_SECONDS = 20 * 60


def pet_action_desire(state, action):
    food = int(state.get("food", 0))
    water = int(state.get("water", 0))
    energy = int(state.get("energy", 0))
    clean = int(state.get("cleanliness", 0))
    mood = int(state.get("happiness", 0))
    if action == "feed" and food <= 65:
        return "Я проголодался — самое время перекусить."
    if action == "water" and water <= 60:
        return "Хочу пить. Принеси, пожалуйста, воды."
    if action == "sleep" and energy <= 55:
        return "Я устал и хочу отдохнуть."
    if action == "walk" and energy < 10:
        return None
    if action == "walk" and energy >= 10 and mood <= 75:
        return "Хочу размяться и исследовать мир!"
    if action == "play" and mood <= 70 and energy >= 25:
        return "Мне скучно! Давай поиграем."
    if action == "wash" and clean <= 60:
        return "Кажется, пора помыться."
    if action == "pet" and mood <= 75:
        return "Мне не хватает внимания и ласки."
    return None


def pet_desires_text(state):
    desires = []
    for action, label in (
        ("feed", "поесть"), ("water", "попить"), ("sleep", "поспать"),
        ("walk", "погулять"), ("play", "поиграть"), ("wash", "помыться"),
        ("pet", "получить ласку")
    ):
        if pet_action_desire(state, action):
            desires.append(label)
    if not desires:
        return "💭 Желания: сейчас я доволен и не прошу ничего особенного."
    return "💭 Хочу: " + ", ".join(desires) + "."


def pet_db_action(user_id, action=None):
    """Apply an activity, update needs and grant experience."""
    state = pet_apply_needs_decay(user_id) or pet_get_active(user_id, create_if_missing=False)
    if not state:
        return None, "Сначала создай персонажа."
    if action == "walk" and int(state.get("energy", 0)) < 10:
        return state, "Я слишком устал для прогулки. Сначала нужно восстановить хотя бы 10 энергии."
    desire = pet_action_desire(state, action)
    if not desire:
        return state, "Сейчас мне этого не хочется.\\n\\n" + pet_desires_text(state)
    now = int(time.time())
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        previous = db.execute(
            "SELECT last_at FROM pet_action_cooldowns WHERE pet_id = ? AND action = ?",
            (state["id"], action)
        ).fetchone()
        if previous and now - int(previous[0]) < ACTION_COOLDOWN_SECONDS:
            remaining = ACTION_COOLDOWN_SECONDS - (now - int(previous[0]))
            minutes = max(1, (remaining + 59) // 60)
            return state, "Мы уже занимались этим недавно. Попробуй снова примерно через %s мин.\\n\\n%s" % (minutes, pet_desires_text(state))
    effects = {
        "pet": {"happiness": 8, "xp": 4},
        "feed": {"food": 25, "happiness": 2, "xp": 6},
        "water": {"water": 30, "xp": 5},
        "sleep": {"energy": 45, "happiness": 2, "xp": 5},
        "walk": {"happiness": 12, "energy": -18, "cleanliness": -8, "xp": 8},
        "wash": {"cleanliness": 35, "happiness": -2, "xp": 4},
        "play": {"happiness": 15, "energy": -15, "xp": 7},
    }.get(action)
    if not effects:
        return state, None
    assignments = []
    params = []
    for column, amount in effects.items():
        if column == "xp":
            continue
        if amount >= 0:
            assignments.append("%s = MIN(100, %s + ?)" % (column, column))
        else:
            assignments.append("%s = MAX(0, %s - ?)" % (column, column))
        params.append(abs(amount))
    xp_gain = effects.get("xp", 0)
    assignments.extend(["xp = xp + ?", "last_needs_update = ?", "updated_at = CURRENT_TIMESTAMP"])
    params.extend([xp_gain, now, state["id"], int(user_id)])
    with sqlite3.connect(str(DB_PATH), timeout=10) as db:
        current = db.execute(
            "SELECT energy FROM pet_characters WHERE id = ? AND user_id = ?",
            (state["id"], int(user_id))
        ).fetchone()
        if not current:
            return None, None
        if action == "walk" and int(current[0]) < 10:
            return state, "Я слишком устал для прогулки. Сначала нужно восстановить энергию."
        db.execute("UPDATE pet_characters SET " + ", ".join(assignments) +
                   " WHERE id = ? AND user_id = ?", params)
        db.execute("""
            INSERT INTO pet_action_cooldowns(pet_id, action, last_at) VALUES (?, ?, ?)
            ON CONFLICT(pet_id, action) DO UPDATE SET last_at = excluded.last_at
        """, (state["id"], action, now))
        db.commit()
        level, xp = db.execute(
            "SELECT level, xp FROM pet_characters WHERE id = ? AND user_id = ?",
            (state["id"], int(user_id))
        ).fetchone()
        while xp >= level * 150:
            xp -= level * 150
            level += 1
        db.execute("UPDATE pet_characters SET level = ?, xp = ? WHERE id = ? AND user_id = ?",
                   (level, xp, state["id"], int(user_id)))
        db.commit()
    updated = pet_get_active(user_id, create_if_missing=False)
    reactions = {
        "pet": "радостно откликается на ласку",
        "feed": "довольно хрустит кормом",
        "water": "с удовольствием пьёт свежую воду",
        "sleep": "сворачивается клубочком и отдыхает",
        "walk": "возвращается с прогулки, полный впечатлений",
        "wash": "чистый и пушистый, хоть на выставку",
        "play": "весело носится после игры",
    }
    return updated, "%s %s!" % (updated["pet_name"], reactions.get(action, "доволен заботой"))


def telegram_edit_with_retry(payload, timeout=12, attempts=3):
    """Retry transient network failures when editing a Telegram message.

    Pet state is changed separately in SQLite, so retries must only repeat the
    display update, never the pet action itself.
    """
    for attempt in range(attempts):
        try:
            return telegram_call("editMessageText", payload, timeout=timeout)
        except RuntimeError as exc:
            message = str(exc).lower()
            transient = (
                "request failed" in message
                and any(term in message for term in (
                    "network is unreachable", "network is down",
                    "temporary failure", "timed out", "timeout",
                    "connection reset", "connection refused",
                    "connection aborted", "no route to host",
                    "name or service not known", "temporary name resolution",
                    "urlerror", "oserror"
                ))
            )
            if not transient or attempt + 1 >= attempts:
                raise
            delay = min(1 * (2 ** attempt), 4)
            print(
                "Telegram message edit hit a temporary network error; "
                "retrying in %ss (attempt %s/%s)." %
                (delay, attempt + 2, attempts),
                flush=True
            )
            time.sleep(delay)


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
            {"text": "💧 Вода", "callback_data": "water"},
            {"text": "😴 Сон", "callback_data": "sleep"}
        ],
        [
            {"text": "🚶 Прогулка", "callback_data": "walk"},
            {"text": "🎾 Играть", "callback_data": "play"}
        ],
        [
            {"text": "🫧 Помыть", "callback_data": "wash"},
            {"text": "🔄 Персонажи", "callback_data": "list"}
        ],
        [
            {"text": "➕ Создать", "callback_data": "new"},
            {"text": "✏️ Имя", "callback_data": "rename_help"},
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
        "dog": "Я люблю ласку, вкусняшки и прогулки.",
        "cat": "Я независимая натура, но ласку и уют ценю.",
        "fox": "Я любопытный хитрец: давай играть и искать приключения!"
    }
    reaction_line = (reaction + "\n\n") if reaction else ""
    level = int(state.get("level", 1))
    xp = int(state.get("xp", 0))
    return (
        title + "\n"
        + "⭐ Уровень: %s · Опыт: %s/%s\n\n" % (level, xp, level * 150)
        + reaction_line
        + personalities.get(state.get("character_type"), personalities["dog"]) + "\n\n"
        + "🍗 Сытость: %s/100\n" % state.get("food", 75)
        + "💧 Вода: %s/100\n" % state.get("water", 70)
        + "😊 Настроение: %s/100\n" % state.get("happiness", 70)
        + "⚡ Энергия: %s/100\n" % state.get("energy", 80)
        + "🫧 Чистота: %s/100\n\n" % state.get("cleanliness", 75)
        + pet_desires_text(state) + "\n\n"
        + "Каждое действие даёт опыт. Заботься обо мне, чтобы повышать уровень!"
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

    # Any other button press cancels an unfinished character creation, then
    # the clicked action continues normally.
    if int(user_id) in CREATE_PENDING:
        CREATE_PENDING.pop(int(user_id), None)
        if chat_id is not None:
            telegram_call("sendMessage", {
                "chat_id": chat_id,
                "text": "Создание персонажа отменено."
            }, timeout=12)

    if data in ("pet", "feed", "water", "sleep", "walk", "wash", "play"):
        state, reaction = pet_db_action(user_id, data)
        if not state:
            return
        if data in ("pet", "feed") and reaction and not reaction.startswith(("Сейчас", "Мы уже", "Я слишком")):
            reactions = PET_REACTIONS.get(state["character_type"], PET_REACTIONS["dog"])
            reaction = random.choice(reactions[data]).format(name=state["pet_name"])
        if chat_id is not None and message_id is not None:
            try:
                telegram_edit_with_retry({
                    "chat_id": chat_id, "message_id": message_id,
                    "text": pet_message(state, reaction=reaction),
                    "reply_markup": pet_keyboard()
                }, timeout=12)
            except RuntimeError as exc:
                if "message is not modified" in str(exc).lower():
                    return
                raise
        return

    if data == "new":
        existing = pet_get_active(user_id, create_if_missing=False)
        if existing:
            if chat_id is not None and message_id is not None:
                telegram_edit_with_retry({
                    "chat_id": chat_id, "message_id": message_id,
                    "text": "У тебя уже есть персонаж «%s». Новый персонаж заменит его, а старый будет удалён без возможности восстановления. Создаём нового?" % existing["pet_name"],
                    "reply_markup": {"inline_keyboard": [[
                        {"text": "🗑 Да, удалить и создать", "callback_data": "replace_yes"},
                        {"text": "↩️ Нет, оставить", "callback_data": "replace_no"}
                    ]]}
                }, timeout=12)
            return
        if chat_id is not None and message_id is not None:
            telegram_edit_with_retry({
                "chat_id": chat_id, "message_id": message_id,
                "text": "Кого создадим? Выбери тип персонажа:",
                "reply_markup": character_keyboard()
            }, timeout=12)
        return

    if data == "replace_no":
        state = pet_get_active(user_id, create_if_missing=False)
        if chat_id is not None and message_id is not None:
            telegram_edit_with_retry({
                "chat_id": chat_id, "message_id": message_id,
                "text": ("Хорошо, «%s» остаётся с тобой. Ничего не изменено." % state["pet_name"]) if state else "Хорошо, ничего не изменено.",
                "reply_markup": pet_keyboard() if state else character_keyboard()
            }, timeout=12)
        return

    if data == "replace_yes":
        with sqlite3.connect(str(DB_PATH), timeout=10) as db:
            db.execute("DELETE FROM pet_action_cooldowns WHERE pet_id IN (SELECT id FROM pet_characters WHERE user_id = ?)", (int(user_id),))
            db.execute("DELETE FROM pet_characters WHERE user_id = ?", (int(user_id),))
            db.execute("DELETE FROM pet_preferences WHERE user_id = ?", (int(user_id),))
            db.commit()
        if chat_id is not None and message_id is not None:
            telegram_edit_with_retry({
                "chat_id": chat_id, "message_id": message_id,
                "text": "Текущий персонаж удалён. Теперь выбери нового:",
                "reply_markup": character_keyboard()
            }, timeout=12)
        return

    if data.startswith("create:"):
        kind = data.split(":", 1)[1]
        if kind not in CHARACTER_TYPES:
            return
        existing = pet_get_active(user_id, create_if_missing=False)
        if existing:
            if chat_id is not None:
                telegram_call("sendMessage", {
                    "chat_id": chat_id,
                    "text": "У тебя уже есть персонаж. Нажми «Создать» и подтверди замену либо оставь текущего."
                }, timeout=12)
            return
        CREATE_PENDING[int(user_id)] = kind
        if chat_id is not None:
            telegram_call("sendMessage", {
                "chat_id": chat_id,
                "text": "Как назовём персонажа? Отправь имя сообщением (до 24 символов). Если нажмёшь другую кнопку или команду, создание отменится."
            }, timeout=12)
        return

    if data == "list":
        characters = pet_list(user_id)
        text = "Твои персонажи — выбери активного:" if characters else "У тебя пока нет персонажей. Создадим первого?"
        if chat_id is not None and message_id is not None:
            telegram_edit_with_retry({
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
            telegram_edit_with_retry({
                "chat_id": chat_id, "message_id": message_id,
                "text": "Теперь активный персонаж:\n\n" + pet_message(state),
                "reply_markup": pet_keyboard()
            }, timeout=12)
        return

    if data == "rename_help":
        state = pet_get_active(user_id, create_if_missing=False)
        text = ("Напиши новое имя следующим сообщением.\n"
                "Можно использовать пробелы, длина — до 24 символов.")
        if not state:
            text = "Сначала создай персонажа кнопкой «Создать»."
        else:
            RENAME_PENDING.add(int(user_id))
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
            telegram_edit_with_retry({
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
            telegram_edit_with_retry({
                "chat_id": chat_id, "message_id": message_id,
                "text": text, "reply_markup": keyboard
            }, timeout=12)
        return

    if data == "delete_no":
        state = pet_get_active(user_id, create_if_missing=False)
        if chat_id is not None and message_id is not None:
            telegram_edit_with_retry({
                "chat_id": chat_id, "message_id": message_id,
                "text": pet_message(state) if state else "Удаление отменено. Создай персонажа, когда будешь готов.",
                "reply_markup": pet_keyboard() if state else character_keyboard()
            }, timeout=12)
        return

    if data == "back":
        state = pet_get_active(user_id, create_if_missing=False)
        if chat_id is not None and message_id is not None:
            telegram_edit_with_retry({
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

    user_id = int(user["id"])
    command = text_value.split()[0].split("@", 1)[0].lower() if text_value else ""

    if command.startswith("/") and user_id in CREATE_PENDING:
        CREATE_PENDING.pop(user_id, None)
        telegram_call("sendMessage", {
            "chat_id": chat["id"],
            "text": "Создание персонажа отменено."
        }, timeout=12)

    if text_value and not text_value.startswith("/") and user_id in CREATE_PENDING:
        kind = CREATE_PENDING.pop(user_id)
        state = pet_create(user_id, kind, text_value)
        if state:
            telegram_call("sendMessage", {
                "chat_id": chat["id"],
                "text": "🎉 Знакомься! Твой персонаж создан. Забота о нём приносит опыт и помогает повышать уровень."
            }, timeout=12)
            send_pet_card(chat["id"], state, greeting=True)
        else:
            telegram_call("sendMessage", {
                "chat_id": chat["id"],
                "text": "Имя не должно быть пустым. Выбери «Создать» и попробуй ещё раз."
            }, timeout=12)
        return

    if command == "/start":
        state = pet_apply_needs_decay(user_id)
        if not state:
            telegram_call("sendMessage", {
                "chat_id": chat["id"],
                "text": "🐾 Добро пожаловать! Сначала выбери персонажа, а затем придумай ему имя."
            }, timeout=12)
            telegram_call("sendMessage", {
                "chat_id": chat["id"],
                "text": "Кого создадим?",
                "reply_markup": character_keyboard()
            }, timeout=12)
            return
        send_pet_card(chat["id"], state, greeting=True)
    elif command.startswith("/") and user_id in RENAME_PENDING:
        RENAME_PENDING.discard(user_id)
        telegram_call("sendMessage", {
            "chat_id": chat["id"],
            "text": "Переименование отменено."
        }, timeout=12)
        if command == "/start":
            state = pet_apply_needs_decay(user_id)
            if state:
                send_pet_card(chat["id"], state, greeting=True)
            else:
                telegram_call("sendMessage", {
                    "chat_id": chat["id"],
                    "text": "Сначала выбери персонажа и придумай ему имя.",
                    "reply_markup": character_keyboard()
                }, timeout=12)
    elif text_value and user_id in RENAME_PENDING and not text_value.startswith("/"):
        RENAME_PENDING.discard(user_id)
        state = pet_rename(user_id, text_value)
        if state:
            send_pet_card(chat["id"], state)
        else:
            telegram_call("sendMessage", {
                "chat_id": chat["id"],
                "text": "Имя не должно быть пустым. Нажми «Переименовать» и попробуй ещё раз."
            })
    elif text_value and user_id in RENAME_PENDING and text_value.startswith("/"):
        telegram_call("sendMessage", {
            "chat_id": chat["id"],
            "text": "Сначала отправь новое имя обычным сообщением или нажми /start."
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


def register_bot_commands():
    """Expose only the two intended commands in Telegram's command menu."""
    telegram_call("setMyCommands", {"commands": [
        {"command": "start", "description": "Открыть питомца"}
    ]}, timeout=12)


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
    try:
        register_bot_commands()
    except RuntimeError as exc:
        print("Pet bot command menu setup failed: %s" % safe_error_message(exc), flush=True)
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
