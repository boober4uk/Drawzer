"""Drawzo — совместное рисование в Telegram Mini App.
Один процесс: aiogram (бот) + aiohttp (раздача Mini App и WebSocket комнат)."""
import asyncio, hashlib, hmac, json, os, re, secrets, sqlite3, time, zlib
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web, WSMsgType
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                           MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo)
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000").rstrip("/")
BOT_USERNAME = os.getenv("BOT_USERNAME", "DrawzoBot")
APP_SHORT_NAME = os.getenv("APP_SHORT_NAME", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
DONATE_STARS = int(os.getenv("DONATE_STARS", "100"))
PORT = 8000
DEV_MODE = os.getenv("DEV_MODE") == "1"
STATIC = Path(__file__).parent / "public"

W, H = 1600, 1000                       # размер холста (совпадает с клиентом)
MAX_PLAYERS, MAX_SPECS, MAX_OPS, MAX_ROOMS, EMPTY_TTL = 8, 100, 6000, 500, 600
SHAPES = {"line", "arrow", "rect", "ellipse", "tri"}
ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # без похожих символов (0/O, 1/I)

# ───────────── премиум (донат Stars) ─────────────
db = sqlite3.connect("drawzo.db", check_same_thread=False)
db.execute("create table if not exists premium(uid integer primary key, since integer)")
def is_premium(uid): return db.execute("select 1 from premium where uid=?", (uid,)).fetchone() is not None
def set_premium(uid): db.execute("insert or ignore into premium values(?,?)", (uid, int(time.time()))); db.commit()

# ───────────── проверка Telegram initData ─────────────
def check_init_data(init):
    if DEV_MODE and init.startswith("dev:"):
        n = init[4:][:20] or "guest"
        return {"id": zlib.crc32(n.encode()), "first_name": n}
    try:
        data = dict(parse_qsl(init, strict_parsing=True))
        h = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), h): return None
        if time.time() - int(data.get("auth_date", 0)) > 86400: return None
        return json.loads(data["user"])
    except Exception:
        return None

# ───────────── комнаты ─────────────
ROOMS, WATCH = {}, {}      # код игрока -> Room, код наблюдателя -> Room

def gen_code():
    while True:
        c = "".join(secrets.choice(ALPHA) for _ in range(6))
        if c not in ROOMS and c not in WATCH: return c

class Room:
    def __init__(s, owner):
        s.owner = owner
        s.code = gen_code(); ROOMS[s.code] = s
        s.watch = gen_code(); WATCH[s.watch] = s
        s.conn = {}                       # uid -> (ws, name)
        s.roles = {owner: "owner"}        # uid -> owner|player|spectator
        s.muted, s.banned = set(), set()
        s.log, s.idx, s.hidden = [], {}, set()   # журнал операций (источник истины)
        s.undo, s.redo, s.n, s.timer = {}, {}, 0, None
        s.idle, s.prem = set(), set()      # спящие игроки / игроки с Drawzo+
    def nid(s): s.n += 1; return f"o{s.n}"
    def online(s, *roles): return sum(1 for u in s.conn if s.roles.get(u) in roles)
    def can_draw(s, u): return s.roles.get(u) in ("owner", "player") and u not in s.muted
    def members(s): return [{"uid": u, "name": n, "role": s.roles[u], "muted": u in s.muted,
                 "idle": u in s.idle, "prem": u in s.prem} for u, (_, n) in s.conn.items()]
    def add(s, op):
        s.log.append(op); s.idx[op["id"]] = op
        s.undo.setdefault(op["uid"], []).append(op["id"]); s.redo[op["uid"]] = []
    def destroy(s): ROOMS.pop(s.code, None); WATCH.pop(s.watch, None)

async def send(ws, m):
    try: await ws.send_str(json.dumps(m, separators=(",", ":")))
    except Exception: pass

async def bcast(r, m, skip=None):
    await asyncio.gather(*(send(w, m) for u, (w, _) in list(r.conn.items()) if u != skip))

async def join(r, uid, name, ws, spectate):
    if uid in r.banned: return await send(ws, {"t": "error", "e": "Вас удалили из этой комнаты"})
    if any(u != uid and n.lower() == name.lower() for u, (_, n) in r.conn.items()):
        return await send(ws, {"t": "error", "c": "nick", "e": f"Ник «{name}» уже занят в этой комнате. Выберите другой"})
    role = r.roles.get(uid) or ("spectator" if spectate else "player")
    n = lambda *rs: sum(1 for u in r.conn if u != uid and r.roles.get(u) in rs)
    if role in ("owner", "player") and n("owner", "player") >= MAX_PLAYERS:
        return await send(ws, {"t": "error", "e": "Комната заполнена (8/8). Зайдите по коду наблюдателя"})
    if role == "spectator" and n("spectator") >= MAX_SPECS:
        return await send(ws, {"t": "error", "e": "Достигнут лимит наблюдателей (100)"})
    r.roles[uid] = role
    r.idle.discard(uid); (r.prem.add if is_premium(uid) else r.prem.discard)(uid)
    old = r.conn.get(uid); r.conn[uid] = (ws, name)
    if old and old[0] is not ws: asyncio.create_task(old[0].close())
    if r.timer: r.timer.cancel(); r.timer = None
    await send(ws, {"t": "joined", "code": r.code, "watch": r.watch if role == "owner" else None, "role": role,
                    "you": uid, "members": r.members(), "log": r.log, "hidden": list(r.hidden)})
    await bcast(r, {"t": "members", "members": r.members()}, skip=uid)
    return True

async def leave(r, uid, ws):
    c = r.conn.get(uid)
    if not c or c[0] is not ws: return
    del r.conn[uid]; r.idle.discard(uid); r.prem.discard(uid)
    await bcast(r, {"t": "members", "members": r.members()})
    if not r.conn: r.timer = asyncio.get_running_loop().call_later(EMPTY_TTL, r.destroy)

async def close_room(r):
    await bcast(r, {"t": "closed"})
    for w, _ in list(r.conn.values()): await w.close()
    r.conn.clear(); r.destroy()

async def do_admin(r, d):
    a = d.get("a")
    if a == "close": return await close_room(r)
    if a == "clear":
        l = d.get("layer")
        if l in (0, 1, 2):
            op = {"id": r.nid(), "uid": r.owner, "k": "clear", "layer": l}
            r.add(op); await bcast(r, {"t": "op", "op": op})
        return
    try: t = int(d.get("uid"))
    except Exception: return
    if t == r.owner: return
    if a == "kick":
        r.banned.add(t); c = r.conn.pop(t, None)
        if c: await send(c[0], {"t": "kicked"}); await c[0].close()
    elif a == "mute": r.muted.add(t)
    elif a == "unmute": r.muted.discard(t)
    elif a == "spectator":
        if r.online("spectator") >= MAX_SPECS: return
        r.roles[t] = "spectator"; r.idle.discard(t)
    elif a == "player":
        if r.online("owner", "player") >= MAX_PLAYERS:
            c = r.conn.get(r.owner); return await send(c[0], {"t": "error", "e": "Уже 8 игроков"}) if c else None
        r.roles[t] = "player"
    await bcast(r, {"t": "members", "members": r.members()})

def num(v, lo, hi):
    return max(lo, min(hi, v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
def color(c): return c if isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c) else "#000000"
def clean_pts(p):
    if not isinstance(p, list) or not p or len(p) > 200: return None
    out = []
    for q in p:
        if not (isinstance(q, list) and len(q) == 2): return None
        x, y = num(q[0], -50, W + 50), num(q[1], -50, H + 50)
        if x is None or y is None: return None
        out.append([round(x, 1), round(y, 1)])
    return out

def clean_nick(v):
    n = re.sub(r"\s+", " ", str(v or "")).strip()
    return n if re.fullmatch(r"[\w .\-]{2,16}", n) else ""

async def wake(r, uid):
    if uid in r.idle:
        r.idle.discard(uid); await bcast(r, {"t": "members", "members": r.members()})

async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=2 * 1024 * 1024)
    await ws.prepare(req)
    user, room, uid, name = None, None, 0, ""
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT: continue
            try: d = json.loads(msg.data); t = d.get("t")
            except Exception: continue
            if user is None:
                user = check_init_data(str(d.get("init", ""))) if t == "auth" else None
                if not user: await send(ws, {"t": "error", "e": "Ошибка авторизации Telegram"}); break
                uid = int(user["id"]); name = str(user.get("first_name") or user.get("username") or "Гость")[:20]
                prem = is_premium(uid)
                await send(ws, {"t": "hello", "premium": prem, "maxColors": 10 if prem else 5,
                                "bot": BOT_USERNAME, "app": APP_SHORT_NAME, "stars": DONATE_STARS})
                continue

            if t in ("create", "join"):
                if room: await leave(room, uid, ws); room = None
                if t == "create":
                    if len(ROOMS) >= MAX_ROOMS: await send(ws, {"t": "error", "e": "Сервер перегружен, попробуйте позже"}); continue
                    r, spec = Room(uid), False
                else:
                    code = str(d.get("code", "")).strip().upper()
                    r, spec = ROOMS.get(code), False
                    if not r: r, spec = WATCH.get(code), True
                    if not r: await send(ws, {"t": "error", "e": "Комната не найдена"}); continue
                if await join(r, uid, clean_nick(d.get("nick")) or name, ws, spec): room = r
                continue
            if room is None: continue
            if t in ("pts", "op", "undo", "redo", "cur", "end"): await wake(room, uid)   # любое действие будит игрока

            if t == "pts" and room.can_draw(uid):
                pts, layer = clean_pts(d.get("pts")), d.get("layer")
                if pts is None or layer not in (0, 1, 2): continue
                key = f"{uid}:{str(d.get('sid'))[:24]}"
                op, first = room.idx.get(key), False
                if op is None:
                    if len(room.log) >= MAX_OPS: await send(ws, {"t": "error", "e": "Холст переполнен"}); continue
                    op = {"id": key, "uid": uid, "k": "stroke", "layer": layer,
                          "tool": "erase" if d.get("tool") == "erase" else "brush",
                          "color": color(d.get("color")), "size": num(d.get("size"), 1, 80) or 6,
                          "alpha": num(d.get("alpha"), .05, 1) or 1, "pts": []}
                    room.add(op); first = True
                if len(op["pts"]) > 20000: continue
                op["pts"].extend(pts)
                await bcast(room, {"t": "op", "op": op} if first else {"t": "pts", "id": key, "pts": pts}, skip=uid)

            elif t == "op" and room.can_draw(uid):
                o, k = d.get("op") or {}, (d.get("op") or {}).get("k")
                if o.get("layer") not in (0, 1, 2) or len(room.log) >= MAX_OPS: continue
                op = {"id": room.nid(), "uid": uid, "k": k, "layer": o["layer"]}
                if k == "fill":
                    x, y = num(o.get("x"), 0, W - 1), num(o.get("y"), 0, H - 1)
                    if x is None or y is None: continue
                    op.update(x=int(x), y=int(y), color=color(o.get("color")), alpha=num(o.get("alpha"), .05, 1) or 1)
                elif k == "img":
                    data = o.get("data")
                    if not (isinstance(data, str) and data.startswith("data:image/") and len(data) < 600_000): continue
                    box = [num(o.get(f), -W, 2 * W) for f in ("x", "y", "w", "h")]
                    if None in box: continue
                    op.update(data=data, x=box[0], y=box[1], w=box[2], h=box[3], alpha=num(o.get("alpha"), .05, 1) or 1)
                elif k == "shape":
                    box = [num(o.get(f), -50, (W if f[0] == "x" else H) + 50) for f in ("x1", "y1", "x2", "y2")]
                    if o.get("s") not in SHAPES or None in box: continue
                    op.update(s=o["s"], x1=box[0], y1=box[1], x2=box[2], y2=box[3], color=color(o.get("color")),
                              size=num(o.get("size"), 1, 80) or 6, alpha=num(o.get("alpha"), .05, 1) or 1, fill=bool(o.get("fill")))
                elif k == "imgset":   # перенос / масштаб / прозрачность уже вставленной картинки
                    ref = room.idx.get(o.get("ref"))
                    box = [num(o.get(f), -W, 2 * W) for f in ("x", "y", "w", "h")]
                    if not ref or ref["k"] != "img" or None in box or box[2] < 8 or box[3] < 8: continue
                    op.update(ref=ref["id"], x=box[0], y=box[1], w=box[2], h=box[3], alpha=num(o.get("alpha"), .05, 1) or 1)
                else: continue
                room.add(op); await bcast(room, {"t": "op", "op": op})

            elif t == "undo" and room.can_draw(uid):
                st = room.undo.get(uid, [])
                while st:
                    i = st.pop()
                    if i in room.hidden: continue
                    room.hidden.add(i); room.redo.setdefault(uid, []).append(i)
                    await bcast(room, {"t": "hide", "id": i}); break
            elif t == "redo" and room.can_draw(uid):
                st = room.redo.get(uid, [])
                if st:
                    i = st.pop(); room.hidden.discard(i); room.undo.setdefault(uid, []).append(i)
                    await bcast(room, {"t": "show", "id": i})
            elif t == "idle" and room.can_draw(uid):
                if d.get("v"):
                    if uid not in room.idle: room.idle.add(uid); await bcast(room, {"t": "members", "members": room.members()})
                else: await wake(room, uid)
            elif t == "end" and room.can_draw(uid):
                key = f"{uid}:{str(d.get('sid'))[:24]}"
                if key in room.idx: await bcast(room, {"t": "end", "id": key}, skip=uid)
            elif t == "cur" and room.can_draw(uid):
                x, y = num(d.get("x"), 0, W), num(d.get("y"), 0, H)
                if x is not None and y is not None:   # спящим курсоры не шлём — экономим трафик
                    m = {"t": "cur", "uid": uid, "x": x, "y": y}
                    await asyncio.gather(*(send(w, m) for u, (w, _) in list(room.conn.items()) if u != uid and u not in room.idle))
            elif t == "admin" and room.owner == uid:
                await do_admin(room, d)
    finally:
        if room: await leave(room, uid, ws)
    return ws

# ───────────── твоя админка ─────────────
def admin_ok(v): return bool(ADMIN_KEY) and secrets.compare_digest(str(v or ""), ADMIN_KEY)

async def admin_page(req):
    if not admin_ok(req.query.get("key")): raise web.HTTPForbidden()
    rows = "".join(
        f"<tr><td>{r.code}</td><td>{r.watch}</td><td>{r.online('owner','player')}/8</td><td>{r.online('spectator')}</td>"
        f"<td>{len(r.log)}</td><td><form method=post action=/admin/close><input type=hidden name=key value='{ADMIN_KEY}'>"
        f"<input type=hidden name=code value={r.code}><button>Закрыть</button></form></td></tr>" for r in list(ROOMS.values()))
    n = db.execute("select count(*) from premium").fetchone()[0]
    return web.Response(content_type="text/html", text=f"<meta charset=utf-8><h2>Drawzo admin</h2>"
        f"<p>Комнат: {len(ROOMS)} · Premium: {n}</p><table border=1 cellpadding=6><tr><th>Игроки</th><th>Зрители</th>"
        f"<th>Играют</th><th>Смотрят</th><th>Операций</th><th></th></tr>{rows}</table>")

async def admin_close(req):
    d = await req.post()
    if not admin_ok(d.get("key")): raise web.HTTPForbidden()
    r = ROOMS.get(str(d.get("code")))
    if r: await close_room(r)
    raise web.HTTPFound(f"/admin?key={ADMIN_KEY}")

async def index(_):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

# ───────────── бот ─────────────
router = Router()

def app_kb(room=None):
    url = WEBAPP_URL + (f"/?room={room}" if room else "")
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎨 Открыть Drawzo", web_app=WebAppInfo(url=url))]])

@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    arg = (command.args or "").strip().upper()
    if arg == "DONATE": return await cmd_donate(m)
    room = arg if re.fullmatch(f"[{ALPHA}]{{6}}", arg) else None
    await m.answer("Drawzo — рисуй вместе с друзьями на одном холсте.\n"
                   "До 8 художников и до 100 зрителей в одной комнате.", reply_markup=app_kb(room))

@router.message(Command("donate"))
async def cmd_donate(m: Message):
    await m.answer_invoice(title="Drawzo+", description="Поддержать проект и получить 10 слотов для цветов",
                           payload="premium", currency="XTR", prices=[LabeledPrice(label="Drawzo+", amount=DONATE_STARS)])

@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery): await q.answer(ok=True)

@router.message(F.successful_payment)
async def paid(m: Message):
    set_premium(m.from_user.id)
    await m.answer("Спасибо! 💛 Drawzo+ активирован — перезапусти Mini App.")

@router.message(Command("stats"))
async def stats(m: Message):
    if m.from_user.id in ADMIN_IDS:
        n = db.execute("select count(*) from premium").fetchone()[0]
        await m.answer(f"Комнат: {len(ROOMS)}\nИгроков онлайн: {sum(r.online('owner','player') for r in ROOMS.values())}\nPremium: {n}")

async def main():
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler), web.get("/admin", admin_page),
                    web.post("/admin/close", admin_close), web.get("/health", lambda _: web.Response(text="ok")),
                    web.static("/static", STATIC)])
    runner = web.AppRunner(app); await runner.setup()
    print(f"Drawzo: http://localhost:{PORT}")
    if not BOT_TOKEN:
        print("BOT_TOKEN не задан — работает только веб-часть (DEV_MODE)"); await asyncio.Event().wait()
    bot = Bot(BOT_TOKEN.strip().strip("\"'")); dp = Dispatcher(); dp.include_router(router)
    try:
        print(f"Бот: @{(await bot.get_me()).username}")
        if WEBAPP_URL.startswith("https://"):
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Рисовать", web_app=WebAppInfo(url=WEBAPP_URL)))
        else:
            print("WEBAPP_URL не https — кнопки Mini App в Telegram работать не будут")
        await dp.start_polling(bot)
    except Exception as e:
        print("Ошибка бота:", e); await asyncio.Event().wait()   # веб-часть продолжит работать
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
