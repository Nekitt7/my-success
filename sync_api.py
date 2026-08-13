"""
================================================================================
  СЕРВЕР СИНХРОНИЗАЦИИ  —  мост между ботом и Mini App
================================================================================

  Читает ту же самую diary.db, что и bot_local.py, и отдаёт данные приложению.
  Бота останавливать не нужно: запускай в СОСЕДНЕМ окне терминала.

  ЗАПУСК:
      pip install aiohttp
      python sync_api.py

  ВАЖНО: вставь тот же токен, что и в боте — им проверяется подпись Telegram,
  чтобы чужой человек не смог прочитать твои цели.

================================================================================
"""

TOKEN = "СЮДА_ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER"   # тот же, что в bot_local.py
PORT = 8080
DB_FILE = "diary.db"                        # лежит рядом с этим файлом

# Нейросеть для кнопки "Предложить план" в приложении.
# Тот же ключ, что в bot_local.py.
GEMINI_API_KEY = ""
GEMINI_MODEL = "gemini-2.0-flash"

# ==============================================================================

import datetime as dt
import hashlib
import re
import hmac
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qsl

try:
    from aiohttp import web
except ImportError:
    print("\n  Нет библиотеки aiohttp. Выполни:\n\n      pip install aiohttp\n")
    sys.exit(1)

DB_PATH = Path(__file__).resolve().parent / DB_FILE


# ---------- нейросеть ----------

SYSTEM_PROMPT = (
    "Ты составляешь план достижения цели за заданное число дней. "
    "Верни ТОЛЬКО JSON вида {\"plan\":[{\"step\":\"текст\",\"days\":число}]}. "
    "Правила: от 5 до 10 шагов; сумма days равна заданному сроку; "
    "step — одно короткое действие на русском, до 60 символов, без нумерации; "
    "шаги идут от простого к сложному; на рутинные повторяющиеся действия "
    "отводи больше дней, на разовые подготовительные — один-два дня. "
    "Никаких пояснений вне JSON."
)


def distribute(raw_days, target):
    """Подгоняет длительности шагов так, чтобы в сумме вышло ровно target дней."""
    if not raw_days:
        return []
    n = len(raw_days)
    if n >= target:
        return [1] * n
    total = sum(max(1, d) for d in raw_days) or n
    out = [max(1, round(max(1, d) * target / total)) for d in raw_days]
    guard = 0
    while sum(out) != target and guard < 10000:
        diff = target - sum(out)
        i = guard % n
        if diff > 0:
            out[i] += 1
        elif out[i] > 1:
            out[i] -= 1
        guard += 1
    return out


async def ask_gemini(goal, target_days, about="", avoid=None):
    """Возвращает список [{'step':..., 'days':...}] или None."""
    if not GEMINI_API_KEY.strip():
        return None

    prompt = f"Цель: {goal}\nСрок: {target_days} дней"
    if about:
        prompt += f"\nО человеке: {about}"
    if avoid:
        prompt += "\n\nЭтот вариант не подошёл, предложи заметно другой:\n" + "\n".join(avoid)

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"},
    }

    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload,
                                 headers={"x-goog-api-key": GEMINI_API_KEY.strip()}) as r:
                if r.status != 200:
                    print("  Gemini", r.status, (await r.text())[:200])
                    return None
                data = await r.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        m = re.search(r"\{.*\}", raw, re.S)
        parsed = json.loads(m.group(0) if m else raw)
        plan = parsed.get("plan") or parsed.get("steps") or []

        texts, days = [], []
        for item in plan:
            if isinstance(item, dict):
                txt = str(item.get("step") or item.get("text") or "").strip()
                d = item.get("days", 1)
            else:
                txt, d = str(item).strip(), 1
            txt = re.sub(r"^\s*\d+[.)]\s*", "", txt).strip('"«» ')
            if len(txt) > 2:
                texts.append(txt[:120])
                try:
                    days.append(max(1, int(d)))
                except (TypeError, ValueError):
                    days.append(1)
        if not texts:
            return None
        texts, days = texts[:10], days[:10]
        return [{"step": a, "days": b} for a, b in zip(texts, distribute(days, target_days))]
    except Exception as e:
        print("  Ошибка Gemini:", e)
        return None


async def handle_plan(request):
    body = await request.json()
    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))
    if not GEMINI_API_KEY.strip():
        return cors(web.json_response({"error": "no key"}, status=503))

    goal = (body.get("goal") or "").strip()[:200]
    days = max(1, min(365, int(body.get("days") or 30)))
    about = (body.get("about") or "").strip()[:400]
    avoid = body.get("avoid") or None
    if not goal:
        return cors(web.json_response({"error": "no goal"}, status=400))

    plan = await ask_gemini(goal, days, about, avoid)
    if not plan:
        return cors(web.json_response({"error": "ai failed"}, status=502))
    print(f"  ✨ сгенерировал план для {uid}: {len(plan)} шагов")
    return cors(web.json_response({"plan": plan}))


# ---------- таблицы для профилей и дуэлей ----------

def ensure_tables():
    """Свои таблицы, чтобы не трогать схему бота."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                nick    TEXT,
                name    TEXT,
                photo   TEXT,
                wins    INTEGER DEFAULT 0,
                seen    TEXT
            );
            CREATE TABLE IF NOT EXISTS duels (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id  INTEGER NOT NULL,
                to_id    INTEGER NOT NULL,
                status   TEXT DEFAULT 'pending',   -- pending | active | declined
                created  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_profiles_nick ON profiles(nick);
            CREATE INDEX IF NOT EXISTS idx_duels_to ON duels(to_id, status);
            CREATE INDEX IF NOT EXISTS idx_duels_from ON duels(from_id, status);
        """)
        conn.commit()
    finally:
        conn.close()


def best_progress(uid: int) -> int:
    """Процент по самой продвинутой незакрытой цели — им и меряются в дуэли."""
    data = read_user(uid)
    if not data or not data.get("goals"):
        return 0
    best = 0
    for g in data["goals"]:
        total = sum(max(1, s["days"]) for s in g["steps"]) or 1
        done = sum((s["days"] if s["done"] else s["progress"]) for s in g["steps"])
        best = max(best, min(100, round(done / total * 100)))
    return best


# ---------- нейросеть ----------

SYSTEM_PROMPT = (
    "Ты составляешь план достижения цели за заданное число дней. "
    "Верни ТОЛЬКО JSON вида {\"plan\":[{\"step\":\"текст\",\"days\":число}]}. "
    "Правила: от 5 до 10 шагов; сумма days равна заданному сроку; "
    "step — одно короткое действие на русском, до 60 символов, без нумерации; "
    "шаги идут от простого к сложному; на рутинные повторяющиеся действия "
    "отводи больше дней, на разовые подготовительные — один-два дня. "
    "Никаких пояснений вне JSON."
)


def distribute(raw_days, target):
    """Подгоняет длительности шагов так, чтобы в сумме вышло ровно target дней."""
    if not raw_days:
        return []
    n = len(raw_days)
    if n >= target:
        return [1] * n
    total = sum(max(1, d) for d in raw_days) or n
    out = [max(1, round(max(1, d) * target / total)) for d in raw_days]
    guard = 0
    while sum(out) != target and guard < 10000:
        diff = target - sum(out)
        i = guard % n
        if diff > 0:
            out[i] += 1
        elif out[i] > 1:
            out[i] -= 1
        guard += 1
    return out


async def ask_gemini(goal, target_days, about="", avoid=None):
    """Возвращает список [{'step':..., 'days':...}] или None."""
    if not GEMINI_API_KEY.strip():
        return None

    prompt = f"Цель: {goal}\nСрок: {target_days} дней"
    if about:
        prompt += f"\nО человеке: {about}"
    if avoid:
        prompt += "\n\nЭтот вариант не подошёл, предложи заметно другой:\n" + "\n".join(avoid)

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"},
    }

    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload,
                                 headers={"x-goog-api-key": GEMINI_API_KEY.strip()}) as r:
                if r.status != 200:
                    print("  Gemini", r.status, (await r.text())[:200])
                    return None
                data = await r.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        m = re.search(r"\{.*\}", raw, re.S)
        parsed = json.loads(m.group(0) if m else raw)
        plan = parsed.get("plan") or parsed.get("steps") or []

        texts, days = [], []
        for item in plan:
            if isinstance(item, dict):
                txt = str(item.get("step") or item.get("text") or "").strip()
                d = item.get("days", 1)
            else:
                txt, d = str(item).strip(), 1
            txt = re.sub(r"^\s*\d+[.)]\s*", "", txt).strip('"«» ')
            if len(txt) > 2:
                texts.append(txt[:120])
                try:
                    days.append(max(1, int(d)))
                except (TypeError, ValueError):
                    days.append(1)
        if not texts:
            return None
        texts, days = texts[:10], days[:10]
        return [{"step": a, "days": b} for a, b in zip(texts, distribute(days, target_days))]
    except Exception as e:
        print("  Ошибка Gemini:", e)
        return None


async def handle_plan(request):
    body = await request.json()
    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))
    if not GEMINI_API_KEY.strip():
        return cors(web.json_response({"error": "no key"}, status=503))

    goal = (body.get("goal") or "").strip()[:200]
    days = max(1, min(365, int(body.get("days") or 30)))
    about = (body.get("about") or "").strip()[:400]
    avoid = body.get("avoid") or None
    if not goal:
        return cors(web.json_response({"error": "no goal"}, status=400))

    plan = await ask_gemini(goal, days, about, avoid)
    if not plan:
        return cors(web.json_response({"error": "ai failed"}, status=502))
    print(f"  ✨ сгенерировал план для {uid}: {len(plan)} шагов")
    return cors(web.json_response({"plan": plan}))


# ---------- таблицы для профилей и дуэлей ----------

def ensure_social_tables():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id    INTEGER PRIMARY KEY,
                nick       TEXT,
                name       TEXT,
                photo      TEXT,
                goals_done INTEGER DEFAULT 0,
                updated_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_nick ON profiles(nick);

            CREATE TABLE IF NOT EXISTS duels (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                a_id    INTEGER NOT NULL,
                b_id    INTEGER NOT NULL,
                status  TEXT DEFAULT 'pending',
                created TEXT
            );
        """)
        conn.commit()
    finally:
        conn.close()


def upsert_profile(uid, init_user, goals_done):
    """Профиль обновляется при каждом заходе — так люди находятся по нику."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO profiles (user_id,nick,name,photo,goals_done,updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET nick=excluded.nick, name=excluded.name, "
            "photo=excluded.photo, goals_done=excluded.goals_done, updated_at=excluded.updated_at",
            (uid, (init_user.get("username") or "").lower(),
             init_user.get("first_name") or "", init_user.get("photo_url") or "",
             goals_done, dt.datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()


# ---------- проверка подписи Telegram ----------

def check_init_data(init_data: str):
    """
    Telegram подписывает данные пользователя. Проверяем подпись —
    так мы точно знаем, что запрос от настоящего владельца аккаунта.
    Возвращает user_id или None.
    """
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got_hash = pairs.pop("hash", None)
        if not got_hash:
            return None

        check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            return None

        user = json.loads(pairs.get("user", "{}"))
        return user.get("id")
    except Exception:
        return None


def init_user_obj(init_data: str) -> dict:
    """Достаёт имя, ник и аватар из подписанных данных Telegram."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return {}


# ---------- чтение базы ----------

def read_user(uid: int):
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        u = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u:
            return None

        goals = []
        seq = 0
        for g in conn.execute(
            "SELECT * FROM goals WHERE user_id=? AND archived=0 ORDER BY id", (uid,)
        ).fetchall():
            steps = []
            for s in conn.execute(
                "SELECT * FROM steps WHERE goal_id=? ORDER BY pos,id", (g["id"],)
            ).fetchall():
                steps.append({
                    "id": 100000 + s["id"],
                    "text": s["text"],
                    "days": max(1, s["days"] or 1),
                    "progress": s["progress"] or 0,
                    "done": bool(s["done_date"]),
                })
                seq = max(seq, 100000 + s["id"])
            goals.append({
                "id": g["id"],
                "title": g["title"],
                "targetDays": g["target_days"] or 30,
                "created": g["created_at"] or dt.date.today().isoformat(),
                "steps": steps,
            })
            seq = max(seq, g["id"])

        days = {}
        for r in conn.execute("SELECT day,kind FROM checkins WHERE user_id=?", (uid,)).fetchall():
            days[r["day"]] = r["kind"] or "done"

        return {
            "name": u["name"] or "",
            "streak": u["streak"] or 0,
            "best": u["best_streak"] or 0,
            "freezes": u["freezes"] if u["freezes"] is not None else 1,
            "lastDone": u["last_done"],
            "days": days,
            "goals": goals,
            "seq": seq,
        }
    finally:
        conn.close()


def write_back(uid: int, state: dict):
    """Отметки, сделанные в приложении, возвращаются в базу бота."""
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
            return

        for day, kind in (state.get("days") or {}).items():
            conn.execute("INSERT OR IGNORE INTO checkins (user_id,day,kind) VALUES (?,?,?)",
                         (uid, day, kind if kind in ("done", "freeze") else "done"))

        conn.execute("UPDATE users SET streak=?, best_streak=?, last_done=?, freezes=? WHERE user_id=?",
                     (int(state.get("streak") or 0),
                      int(state.get("best") or 0),
                      state.get("lastDone"),
                      int(state.get("freezes") if state.get("freezes") is not None else 1),
                      uid))

        for g in state.get("goals") or []:
            for s in g.get("steps") or []:
                sid = s.get("id", 0)
                if sid < 100000:          # цель создана в приложении, в базе бота её нет
                    continue
                conn.execute(
                    "UPDATE steps SET progress=?, done_date=CASE WHEN ? THEN COALESCE(done_date, ?) ELSE NULL END WHERE id=?",
                    (int(s.get("progress") or 0), 1 if s.get("done") else 0,
                     dt.date.today().isoformat(), sid - 100000))
        conn.commit()
    finally:
        conn.close()


# ---------- маршруты ----------

def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


async def handle_options(request):
    return cors(web.Response(status=204))


async def handle_state(request):
    try:
        body = await request.json()
    except Exception:
        return cors(web.json_response({"error": "bad json"}, status=400))

    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))

    data = read_user(uid)
    if not data:
        data = {"goals": [], "note": "нет данных в боте"}

    # профиль обновляем на каждом заходе — иначе людей не найти по нику
    user_obj = init_user_obj(body.get("initData", ""))
    done_count = sum(1 for g in data.get("goals", [])
                     if g.get("steps") and all(s.get("done") for s in g["steps"]))
    try:
        upsert_profile(uid, user_obj, done_count)
        data["invites"], data["duels"] = social_for(uid)
    except Exception as e:
        print("  профили недоступны:", e)

    print(f"  → отдал данные пользователю {uid}: целей {len(data.get('goals', []))}")
    return cors(web.json_response(data))


async def handle_save(request):
    try:
        body = await request.json()
    except Exception:
        return cors(web.json_response({"error": "bad json"}, status=400))

    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))

    write_back(uid, body.get("state") or {})
    print(f"  ← принял отметки от пользователя {uid}")
    return cors(web.json_response({"ok": True}))


async def handle_profile(request):
    """Приложение представляется серверу — иначе человека нельзя найти по нику."""
    try:
        body = await request.json()
    except Exception:
        return cors(web.json_response({"error": "bad json"}, status=400))
    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))

    p = body.get("profile") or {}
    nick = (p.get("nick") or "").lstrip("@").strip().lower()[:32]
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO profiles (user_id,nick,name,photo,wins,seen) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET nick=excluded.nick, name=excluded.name, "
            "photo=excluded.photo, seen=excluded.seen",
            (uid, nick, (p.get("name") or "")[:64], (p.get("photo") or "")[:300],
             0, dt.datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()
    return cors(web.json_response({"ok": True}))


async def handle_find(request):
    try:
        body = await request.json()
    except Exception:
        return cors(web.json_response({"error": "bad json"}, status=400))
    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))

    nick = (body.get("nick") or "").lstrip("@").strip().lower()[:32]
    if not nick:
        return cors(web.json_response({"user": None}))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM profiles WHERE nick=? AND user_id<>?", (nick, uid)).fetchone()
    finally:
        conn.close()
    if not r:
        return cors(web.json_response({"user": None}))

    # наружу отдаём только то, что человек сам показал в Telegram
    return cors(web.json_response({"user": {
        "nick": r["nick"], "name": r["name"], "photo": r["photo"],
        "wins": count_wins(r["user_id"]),
    }}))


def count_wins(uid: int) -> int:
    """Сколько целей человек довёл до конца."""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT g.id FROM goals g WHERE g.user_id=? AND g.archived=0 "
            "AND NOT EXISTS (SELECT 1 FROM steps s WHERE s.goal_id=g.id AND s.done_date IS NULL) "
            "AND EXISTS (SELECT 1 FROM steps s WHERE s.goal_id=g.id)", (uid,)).fetchall()
        return len(rows)
    finally:
        conn.close()


async def handle_invite(request):
    try:
        body = await request.json()
    except Exception:
        return cors(web.json_response({"error": "bad json"}, status=400))
    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))

    nick = (body.get("nick") or "").lstrip("@").strip().lower()[:32]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT user_id FROM profiles WHERE nick=?", (nick,)).fetchone()
        if not r or r["user_id"] == uid:
            return cors(web.json_response({"error": "not found"}, status=404))
        target = r["user_id"]
        dup = conn.execute(
            "SELECT 1 FROM duels WHERE status IN ('pending','active') AND "
            "((from_id=? AND to_id=?) OR (from_id=? AND to_id=?))",
            (uid, target, target, uid)).fetchone()
        if dup:
            return cors(web.json_response({"ok": True, "note": "already"}))
        conn.execute("INSERT INTO duels (from_id,to_id,status,created) VALUES (?,?,'pending',?)",
                     (uid, target, dt.date.today().isoformat()))
        conn.commit()
    finally:
        conn.close()
    print(f"  ⚔ {uid} вызвал @{nick}")
    return cors(web.json_response({"ok": True}))


async def handle_respond(request):
    try:
        body = await request.json()
    except Exception:
        return cors(web.json_response({"error": "bad json"}, status=400))
    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))

    did = int(body.get("id") or 0)
    ok = bool(body.get("ok"))
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("UPDATE duels SET status=? WHERE id=? AND to_id=? AND status='pending'",
                     ("active" if ok else "declined", did, uid))
        conn.commit()
    finally:
        conn.close()
    return cors(web.json_response({"ok": True}))


async def handle_duels(request):
    try:
        body = await request.json()
    except Exception:
        return cors(web.json_response({"error": "bad json"}, status=400))
    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        invites = []
        for r in conn.execute(
            "SELECT d.id, p.nick, p.name, p.photo FROM duels d "
            "LEFT JOIN profiles p ON p.user_id=d.from_id "
            "WHERE d.to_id=? AND d.status='pending'", (uid,)).fetchall():
            invites.append({"id": r["id"], "nick": r["nick"] or "?",
                            "name": r["name"] or "", "photo": r["photo"] or ""})

        mine = best_progress(uid)
        duels_out = []
        for r in conn.execute(
            "SELECT d.id, d.from_id, d.to_id FROM duels d WHERE d.status='active' "
            "AND (d.from_id=? OR d.to_id=?)", (uid, uid)).fetchall():
            other = r["to_id"] if r["from_id"] == uid else r["from_id"]
            p = conn.execute("SELECT nick,name FROM profiles WHERE user_id=?", (other,)).fetchone()
            duels_out.append({
                "id": r["id"],
                "rivalNick": (p["nick"] if p else "") or "?",
                "rivalName": (p["name"] if p else "") or "",
                "mePct": mine,
                "rivalPct": best_progress(other),
            })
    finally:
        conn.close()
    return cors(web.json_response({"invites": invites, "duels": duels_out}))


# ---------- профили и дуэли ----------

async def handle_find(request):
    """Поиск человека по нику Telegram."""
    body = await request.json()
    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))

    nick = (body.get("nick") or "").strip().lstrip("@").lower()
    if not nick:
        return cors(web.json_response({}))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(
            "SELECT user_id,nick,name,photo,goals_done FROM profiles WHERE nick=? AND user_id<>?",
            (nick, uid)).fetchone()
    finally:
        conn.close()

    if not r:
        return cors(web.json_response({}))
    return cors(web.json_response({
        "nick": r["nick"], "name": r["name"], "photo": r["photo"],
        "goalsDone": r["goals_done"] or 0}))


async def handle_invite(request):
    """Отправка приглашения на дуэль."""
    body = await request.json()
    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))

    nick = (body.get("nick") or "").strip().lstrip("@").lower()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT user_id FROM profiles WHERE nick=?", (nick,)).fetchone()
        if not r or r["user_id"] == uid:
            return cors(web.json_response({"error": "not found"}, status=404))
        target = r["user_id"]
        exists = conn.execute(
            "SELECT 1 FROM duels WHERE ((a_id=? AND b_id=?) OR (a_id=? AND b_id=?)) AND status<>'declined'",
            (uid, target, target, uid)).fetchone()
        if not exists:
            conn.execute("INSERT INTO duels (a_id,b_id,status,created) VALUES (?,?,?,?)",
                         (uid, target, "pending", dt.date.today().isoformat()))
            conn.commit()
    finally:
        conn.close()
    print(f"  ⚔ {uid} пригласил @{nick}")
    return cors(web.json_response({"ok": True}))


async def handle_invite_reply(request):
    """Приём или отклонение приглашения."""
    body = await request.json()
    uid = check_init_data(body.get("initData", ""))
    if not uid:
        return cors(web.json_response({"error": "unauthorized"}, status=401))

    nick = (body.get("nick") or "").strip().lstrip("@").lower()
    accept = bool(body.get("accept"))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT user_id FROM profiles WHERE nick=?", (nick,)).fetchone()
        if not r:
            return cors(web.json_response({"error": "not found"}, status=404))
        conn.execute("UPDATE duels SET status=? WHERE a_id=? AND b_id=? AND status='pending'",
                     ("active" if accept else "declined", r["user_id"], uid))
        conn.commit()
    finally:
        conn.close()
    return cors(web.json_response({"ok": True}))


def social_for(uid):
    """Входящие приглашения и активные дуэли с процентом прогресса соперника."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        invites = []
        for r in conn.execute(
            "SELECT p.nick,p.name,p.photo,p.goals_done FROM duels d "
            "JOIN profiles p ON p.user_id=d.a_id WHERE d.b_id=? AND d.status='pending'", (uid,)):
            invites.append({"nick": r["nick"], "name": r["name"],
                            "photo": r["photo"], "goalsDone": r["goals_done"] or 0})

        duels = []
        for r in conn.execute(
            "SELECT CASE WHEN d.a_id=? THEN d.b_id ELSE d.a_id END AS other "
            "FROM duels d WHERE (d.a_id=? OR d.b_id=?) AND d.status='active'", (uid, uid, uid)):
            other = r["other"]
            p = conn.execute("SELECT nick,name,photo FROM profiles WHERE user_id=?", (other,)).fetchone()
            duels.append({"nick": p["nick"] if p else "", "name": p["name"] if p else "",
                          "photo": p["photo"] if p else "",
                          "mine": progress_pct(conn, uid), "theirs": progress_pct(conn, other)})
        return invites, duels
    finally:
        conn.close()


def progress_pct(conn, uid):
    """Средний процент выполнения активных целей — им и меряются в дуэли."""
    rows = conn.execute(
        "SELECT s.days,s.progress,s.done_date FROM steps s "
        "JOIN goals g ON g.id=s.goal_id WHERE g.user_id=? AND g.archived=0", (uid,)).fetchall()
    if not rows:
        return 0
    total = sum(max(1, r["days"] or 1) for r in rows)
    done = sum((r["days"] if r["done_date"] else (r["progress"] or 0)) for r in rows)
    return min(100, round(done / total * 100)) if total else 0


async def handle_ping(request):
    return cors(web.json_response({"ok": True, "db": DB_PATH.exists()}))


def main():
    if TOKEN.startswith("СЮДА") or len(TOKEN) < 20:
        print("\n" + "=" * 70)
        print("  Не вставлен токен. Открой sync_api.py и впиши в строку TOKEN")
        print("  тот же токен, что стоит в bot_local.py.")
        print("=" * 70 + "\n")
        return

    if not DB_PATH.exists():
        print(f"\n  Файла {DB_PATH.name} нет рядом со скриптом.")
        print("  Положи sync_api.py в ту же папку, где лежит бот, и запусти бота хотя бы раз.\n")
        return

    ensure_tables()

    ensure_social_tables()

    app = web.Application()
    app.router.add_post("/api/state", handle_state)
    app.router.add_post("/api/save", handle_save)
    app.router.add_post("/api/plan", handle_plan)
    app.router.add_post("/api/find", handle_find)
    app.router.add_post("/api/invite", handle_invite)
    app.router.add_post("/api/invite-reply", handle_invite_reply)
    app.router.add_post("/api/profile", handle_profile)
    app.router.add_post("/api/plan", handle_plan)
    app.router.add_post("/api/find", handle_find)
    app.router.add_post("/api/invite", handle_invite)
    app.router.add_post("/api/respond", handle_respond)
    app.router.add_post("/api/duels", handle_duels)
    app.router.add_get("/api/ping", handle_ping)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", handle_options)

    print(f"\n  База: {DB_PATH}")
    print(f"  Нейросеть: {'Gemini ' + GEMINI_MODEL if GEMINI_API_KEY.strip() else 'ключ не задан'}")
    print(f"  Сервер синхронизации слушает порт {PORT}")
    print(f"  Проверка: http://localhost:{PORT}/api/ping")
    print("  Останов: Ctrl+C\n")
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Сервер остановлен.\n")
