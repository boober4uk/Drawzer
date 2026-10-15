"""Drawzo — совместное рисование в Telegram Mini App.
Один процесс: aiogram (бот) + aiohttp (раздача Mini App и WebSocket комнат)."""
import asyncio, hashlib, hmac, json, os, random, re, secrets, shutil, sqlite3, time, zlib
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web, WSMsgType
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                           CallbackQuery, MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo)
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8080").rstrip("/")
BOT_USERNAME = os.getenv("BOT_USERNAME", "DrawzoBot")
APP_SHORT_NAME = os.getenv("APP_SHORT_NAME", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
DONATE_STARS = int(os.getenv("DONATE_STARS", "100"))
PORT = int(os.getenv("PORT", "3000"))
DEV_MODE = os.getenv("DEV_MODE") == "1"
ALLOW_GUESTS = os.getenv("ALLOW_GUESTS", "1") != "0"      # вход из обычного браузера (гость с сохранением сессии)
STATIC = Path(__file__).parent / "public"

W, H = 1600, 1000                       # размер холста (совпадает с клиентом)
MAX_PLAYERS, MAX_SPECS, MAX_OPS, MAX_ROOMS, EMPTY_TTL = 8, 100, 6000, 500, 600
SHAPES = {"line", "arrow", "rect", "ellipse", "tri"}
ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # без похожих символов (0/O, 1/I)

# ───────────── премиум (донат Stars) ─────────────
db = sqlite3.connect("data/drawzo.db", check_same_thread=False)
db.execute("create table if not exists premium(uid integer primary key, since integer)")
def is_premium(uid): return db.execute("select 1 from premium where uid=?", (uid,)).fetchone() is not None
def set_premium(uid): db.execute("insert or ignore into premium values(?,?)", (uid, int(time.time()))); db.commit()

# ───────────── роли и баны ─────────────
db.execute("create table if not exists staff(uid integer primary key, role text)")
db.execute("create table if not exists bans(uid integer primary key)")
db.execute("create table if not exists app_state(key text primary key, value text not null)")
db.commit()

def _load_maintenance():
    default = {"phase": "off", "deadline": None, "started": None, "reason": "Плановые технические работы"}
    row = db.execute("select value from app_state where key='maintenance'").fetchone()
    if row:
        try:
            data = json.loads(row[0])
            if data.get("phase") in ("off", "warning", "active"):
                default.update({k: data.get(k, default[k]) for k in default})
        except (ValueError, TypeError, AttributeError):
            pass
    return default

MAINTENANCE = _load_maintenance()
MAINTENANCE_TASK = None

def is_mod(uid): return uid in ADMIN_IDS or db.execute("select 1 from staff where uid=? and role='mod'", (uid,)).fetchone() is not None
def is_banned(uid): return db.execute("select 1 from bans where uid=?", (uid,)).fetchone() is not None
def del_premium(uid): db.execute("delete from premium where uid=?", (uid,)); db.commit()

# ───────────── сохранения и ежедневное состязание ─────────────
db.execute("create table if not exists projects(id integer primary key autoincrement, uid integer, title text, ops blob, preview text, updated integer)")
db.execute("create table if not exists daily_entries(day text, uid integer, name text, img text, ts integer, primary key(day, uid))")
db.execute("create table if not exists daily_votes(day text, voter integer, target integer, primary key(day, voter))")
try: db.execute("alter table daily_entries add column ops blob")      # проект работы — чтобы её можно было изменить
except sqlite3.OperationalError: pass
db.execute("create table if not exists visits(uid integer primary key, day text)")
try: db.execute("alter table projects add column inf integer default 0")   # бесконечный ли холст у проекта
except sqlite3.OperationalError: pass   # когда человек заходил в последний раз
db.execute("create index if not exists idx_projects_uid_updated on projects(uid, updated desc)")
db.execute("create index if not exists idx_daily_entries_day_ts on daily_entries(day, ts)")
db.execute("create index if not exists idx_daily_votes_day_target on daily_votes(day, target)")
db.commit()

PROMPTS = ["Котик в космосе", "Дом на дереве", "Робот, который любит цветы", "Дождливый город", "Дракон-вегетарианец", "Подводный велосипед",
           "Чайник-путешественник", "Облако с характером", "Зимняя сказка", "Любимая еда", "Маяк в тумане", "Динозавр на скейте",
           "Волшебный лес", "Корабль из бумаги", "Утро в горах", "Смешной монстр", "Остров в небе", "Лето на колёсах", "Сова-библиотекарь",
           "Город будущего", "Пингвин на пляже", "Самый уютный дом", "Космический повар", "Гигантская улитка", "Ночной карнавал",
           "Мост между мирами", "Грибной дождь", "Лиса и луна", "Пират-неудачник", "Мой талисман"]

WORDS = [
    "кот", "собака", "слон", "жираф", "пингвин", "сова", "краб", "медуза",
    "улитка", "лиса", "заяц", "бабочка", "лошадь", "корова", "свинья", "овца",
    "коза", "курица", "петух", "утка", "гусь", "лебедь", "попугай", "орёл",
    "воробей", "ворона", "тигр", "лев", "медведь", "волк", "обезьяна", "зебра",
    "носорог", "бегемот", "верблюд", "кенгуру", "коала", "панда", "белка", "ёжик",
    "мышь", "крыса", "хомяк", "кролик", "олень", "лось", "крокодил", "змея",
    "черепаха", "лягушка", "акула", "кит", "дельфин", "осьминог", "морская звезда", "рыба",
    "паук", "муравей", "пчела", "божья коровка", "комар", "муха", "стрекоза", "жук",
    "червяк", "летучая мышь", "фламинго", "страус", "павлин", "тюлень", "морж", "ленивец",
    "бобр", "енот", "динозавр", "дракон", "единорог", "цыплёнок", "котёнок", "щенок",
    "попугайчик", "сурок", "хорёк", "барсук", "лама", "гиена", "колибри", "дятел",
    "пеликан", "сердце", "рак", "скат", "морской конёк", "мотылёк", "гусеница", "скорпион",
    "яблоко", "банан", "арбуз", "виноград", "клубника", "вишня", "лимон", "апельсин",
    "груша", "ананас", "морковка", "помидор", "огурец", "картошка", "лук", "кукуруза",
    "гриб", "хлеб", "сыр", "пицца", "бутерброд", "гамбургер", "хот-дог", "торт",
    "пончик", "печенье", "мороженое", "конфета", "шоколад", "яйцо", "молоко", "суп",
    "блины", "пельмени", "чай", "кофе", "сок", "попкорн", "сосиска", "бублик",
    "пирожок", "мёд", "орех", "тыква", "баклажан", "перец", "редиска", "капуста",
    "брокколи", "кекс", "вафля", "чеснок", "персик", "слива", "арахис", "лимонад",
    "сахарная вата", "яичница", "спагетти", "суши", "тако", "пирог", "леденец", "жвачка",
    "дом", "машина", "часы", "зонт", "ключ", "корона", "телефон", "лампа",
    "свеча", "очки", "книга", "ручка", "карандаш", "ножницы", "молоток", "пила",
    "лопата", "ведро", "чайник", "кастрюля", "ложка", "вилка", "нож", "тарелка",
    "чашка", "стул", "стол", "кровать", "диван", "шкаф", "окно", "дверь",
    "зеркало", "телевизор", "компьютер", "клавиатура", "наушники", "фотоаппарат", "гитара", "пианино",
    "барабан", "скрипка", "труба", "микрофон", "рюкзак", "чемодан", "сумка", "шапка",
    "шарф", "перчатка", "носок", "ботинок", "футболка", "платье", "галстук", "кольцо",
    "бусы", "расчёска", "зубная щётка", "мыло", "полотенце", "подушка", "одеяло", "коврик",
    "лестница", "подарок", "воздушный шар", "флаг", "колокольчик", "песочные часы", "компас", "карта",
    "глобус", "лупа", "весы", "магнит", "батарейка", "лампочка", "розетка", "пылесос",
    "утюг", "холодильник", "плита", "швабра", "веник", "кисточка", "мольберт", "конверт",
    "будильник", "календарь", "фонарик", "бинокль", "телескоп", "микроскоп", "парашют", "воздушный змей",
    "ёлочная игрушка", "фейерверк", "карусель", "колесо обозрения", "качели", "горка", "батут", "удочка",
    "якорь", "сундук", "бумеранг", "пазл", "кубик", "мячик", "кукла", "робот-игрушка",
    "плюшевый мишка", "юла", "шашки", "шахматы", "наручные часы", "кошелёк", "монета", "ножницы для бумаги",
    "открытка", "шпион", "лего", "свисток", "медаль", "кубок", "мишень", "лук со стрелами",
    "щит", "меч", "палатка", "спальный мешок", "костёр", "термос", "бутылка", "банка",
    "коробка", "мусорное ведро", "лейка", "грабли", "тачка", "самолёт", "корабль", "велосипед",
    "ракета", "поезд", "лодка", "автобус", "трактор", "вертолёт", "мотоцикл", "самокат",
    "скейтборд", "подводная лодка", "такси", "пожарная машина", "скорая помощь", "паровоз", "трамвай", "экскаватор",
    "санки", "лыжи", "коньки", "летающая тарелка", "дирижабль", "парусник", "яхта", "грузовик",
    "воздушный шар с корзиной", "снегоход", "тележка", "солнце", "луна", "звезда", "облако", "радуга",
    "гора", "вулкан", "остров", "пляж", "море", "река", "озеро", "водопад",
    "пещера", "лес", "дерево", "цветок", "роза", "подсолнух", "ромашка", "тюльпан",
    "кактус", "пальма", "трава", "камень", "снежинка", "снеговик", "молния", "торнадо",
    "дождь", "ветер", "огонь", "планета", "комета", "галактика", "замок", "мост",
    "маяк", "мельница", "башня", "пирамида", "церковь", "ферма", "город", "деревня",
    "космос", "шалаш", "иглу", "айсберг", "пустыня", "джунгли", "болото", "поле",
    "туман", "сосулька", "лужа", "ёлка", "дуб", "берёза", "листок", "жёлудь",
    "шишка", "куст", "мох", "космонавт", "привидение", "робот", "пират", "ковбой",
    "рыцарь", "принцесса", "король", "волшебник", "ведьма", "ниндзя", "клоун", "повар",
    "врач", "пожарный", "полицейский", "художник", "фокусник", "супергерой", "русалка", "вампир",
    "зомби", "инопланетянин", "снежный человек", "ангел", "фея", "гном", "великан", "Дед Мороз",
    "скелет", "мумия", "пират с попугаем", "водолаз", "лётчик", "садовник", "строитель", "барабанщик",
    "танцор", "спортсмен", "балерина", "дирижёр", "фотограф", "пастух", "рыбак", "футбол",
    "баскетбол", "теннис", "хоккей", "гантели", "боулинг", "бильярд", "дартс", "йога",
    "плавание", "серфинг", "сноуборд", "лыжник", "велогонка", "прыжок с парашютом", "фокус", "сон",
    "смех", "дождливый день", "день рождения", "праздник", "свадьба", "пикник", "поход", "каникулы",
    "зарядка", "танец", "песня", "мороженое в рожке", "селфи", "подарок на ёлку"]

RECENT = {"w": deque(maxlen=80), "t": deque(maxlen=20)}   # что выпадало недавно (во всех комнатах)

def pick(r, key, pool):
    """«Мешок»: слова выдаются в случайном порядке без повторов, пока не кончатся все; недавние слова из других игр откладываются."""
    bag = r.bags.setdefault(key, [])
    if not bag:
        bag.extend(random.sample(pool, len(pool)))
        if len(bag) > 1 and bag[-1] == r.last.get(key): bag[0], bag[-1] = bag[-1], bag[0]
    for _ in range(len(bag)):
        w = bag.pop()
        if w not in RECENT[key]: break
        bag.insert(0, w)
    r.last[key] = w; RECENT[key].append(w)
    return w

def norm(t): return re.sub(r"[^a-zа-я0-9]", "", str(t).lower().replace("ё", "е"))

def tg_name(user):   # ник из Telegram — его нельзя менять
    n = re.sub(r"[^\w .\-]", "", str(user.get("first_name") or "")).strip()[:16]
    if len(n) < 2: n = re.sub(r"[^\w.\-]", "", str(user.get("username") or ""))[:16]
    return n if len(n) >= 2 else f"Игрок{int(user['id']) % 1000}"

def toint(v):
    try: return int(v)
    except Exception: return None

def daily_info():
    d = datetime.now(timezone.utc).date()
    end = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)
    return {"day": d.isoformat(), "prompt": PROMPTS[d.toordinal() % len(PROMPTS)], "ends": int(end.timestamp() * 1000)}

def daily_payload(uid):
    info = daily_info()
    rows = db.execute("select e.uid, e.name, e.img, (select count(*) from daily_votes v where v.day=e.day and v.target=e.uid) c "
                      "from daily_entries e where e.day=? order by c desc, e.ts asc limit 30", (info["day"],)).fetchall()
    voted = db.execute("select target from daily_votes where day=? and voter=?", (info["day"], uid)).fetchone()
    mine = db.execute("select 1 from daily_entries where day=? and uid=?", (info["day"], uid)).fetchone() is not None
    return {"t": "daily", **info, "me": uid, "mine": mine, "voted": voted[0] if voted else None,
            "entries": [{"uid": r[0], "name": r[1], "img": r[2], "votes": r[3]} for r in rows]}

def recap_for(uid):
    """Если человек был вчера (или раньше) — показать лучшие работы завершившегося состязания."""
    today = daily_info()["day"]
    row = db.execute("select day from visits where uid=?", (uid,)).fetchone()
    db.execute("insert or replace into visits values(?,?)", (uid, today)); db.commit()
    if not row or row[0] >= today: return None
    d = db.execute("select day from daily_entries where day>=? and day<? group by day order by day desc limit 1", (row[0], today)).fetchone()
    if not d: return None
    rows = db.execute("select e.uid, e.name, e.img, (select count(*) from daily_votes v where v.day=e.day and v.target=e.uid) c "
                      "from daily_entries e where e.day=? order by c desc, e.ts asc limit 3", (d[0],)).fetchall()
    rows = [r for r in rows if r[3] > 0]
    if not rows: return None
    return {"day": d[0], "prompt": PROMPTS[date.fromisoformat(d[0]).toordinal() % len(PROMPTS)], "me": uid,
            "items": [{"rank": i + 1, "uid": r[0], "name": r[1], "img": r[2], "votes": r[3]} for i, r in enumerate(rows)]}

def fill_room(r, blob):   # загрузить сохранённые операции и последние 20 отменяемых действий
    r.log = json.loads(zlib.decompress(blob)); r.idx = {o["id"]: o for o in r.log}
    r.undo.clear(); r.redo.clear()
    for op in r.log:
        uid, oid = op.get("uid"), op.get("id")
        if uid is None or oid is None: continue
        stack = r.undo.setdefault(uid, []); stack.append(oid)
        if len(stack) > 20: del stack[:-20]
    r.n = max([int(o["id"][1:]) for o in r.log if re.fullmatch(r"o\d+", str(o["id"]))] or [0])
    r.t0 = time.time() - max([o.get("te", o.get("ts", 0)) for o in r.log] or [0]) / 1000

def list_saves(uid):
    return [{"id": r[0], "title": r[1], "preview": r[2], "updated": r[3], "inf": bool(r[4])} for r in
            db.execute("select id, title, preview, updated, inf from projects where uid=? order by updated desc", (uid,)).fetchall()]

# ───────────── проверка Telegram initData ─────────────
def check_init_data(init):
    if DEV_MODE and init.startswith("dev:"):
        n = init[4:][:20] or "guest"
        return {"id": zlib.crc32(n.encode()), "first_name": n}
    if init.startswith("guest:") and ALLOW_GUESTS:   # браузер: личный токен хранится в localStorage, id отрицательный (не пересекается с Telegram)
        tok = init[6:]
        if not re.fullmatch(r"[A-Za-z0-9_\-]{20,64}", tok): return None
        return {"id": -(int(hashlib.sha256(tok.encode()).hexdigest()[:12], 16) % 10**11) - 1, "first_name": "Гость"}
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
    def __init__(s, owner, daily=None):
        s.owner = owner
        s.code = gen_code(); ROOMS[s.code] = s
        s.watch = gen_code(); WATCH[s.watch] = s
        s.conn = {}                       # uid -> (ws, name)
        s.roles = {owner: "owner"}        # uid -> owner|player|spectator
        s.muted, s.banned = set(), set()
        s.log, s.idx, s.hidden = [], {}, set()   # журнал операций (источник истины)
        s.undo, s.redo, s.n, s.timer = {}, {}, 0, None
        s.t0 = time.time()                 # начало комнаты — от него считается время операций (для реплеев)
        s.idle, s.prem = set(), set()      # спящие игроки / игроки с Drawzo+
        s.daily, s.pid = daily, None       # задание дня (личная комната) / id сохранения
        s.day = daily_info()["day"] if daily else None
        s.title, s.pub, s.open_draw, s.owner_name = "", None, True, ""   # публичная комната: название / токен в списке / можно ли рисовать чужим
        s.xfer, s.trail = None, {}                                       # перенос владельца / след курсора игроков (Drawzo+)
        s.inf, s.areas = False, {}   # бесконечный холст / области реплея игроков
        s.bags, s.last = {}, {}     # «мешки» слов и тем: без повторов
        s.game, s.gtask, s.ptitle = None, None, ""                       # мини-игра / задача игры / название сохранения
    def nid(s): s.n += 1; return f"o{s.n}"
    def now(s): return int((time.time() - s.t0) * 1000)
    def online(s, *roles): return sum(1 for u in s.conn if s.roles.get(u) in roles)
    def can_draw(s, u):
        if s.roles.get(u) not in ("owner", "player") or u in s.muted: return False
        g = s.game
        if not g: return True
        if g.get("g") == "speed": return g.get("phase") == "draw" and u in g.get("zones", {})   # «Скоростной рисунок»: все, но каждый в своей полосе
        return g.get("phase") == "draw" and g.get("drawer") == u                               # в остальных играх рисует только тот, чей ход
    def members(s): return [{"uid": u, "name": n, "role": s.roles[u], "muted": u in s.muted,
                 "idle": u in s.idle, "prem": u in s.prem} for u, (_, n) in s.conn.items()]
    def add(s, op):
        op.setdefault("ts", s.now()); s.log.append(op); s.idx[op["id"]] = op
        # История отмены ограничена последними 20 действиями на игрока.
        stack = s.undo.setdefault(op["uid"], [])
        stack.append(op["id"])
        if len(stack) > 20: del stack[:-20]
        s.redo[op["uid"]] = []
    def destroy(s):
        ROOMS.pop(s.code, None); WATCH.pop(s.watch, None); s.pub = None
        if s.gtask: s.gtask.cancel()
        if s.xfer: s.xfer.cancel()

async def send(ws, m):
    try: await ws.send_str(json.dumps(m, separators=(",", ":")))
    except Exception: pass

async def bcast(r, m, skip=None):
    await asyncio.gather(*(send(w, m) for u, (w, _) in list(r.conn.items()) if u != skip))

def maintenance_payload():
    return {"t": "maintenance", "phase": MAINTENANCE["phase"],
            "deadline": MAINTENANCE.get("deadline"), "reason": MAINTENANCE.get("reason") or "Плановые технические работы"}

def persist_maintenance():
    payload = {k: MAINTENANCE.get(k) for k in ("phase", "deadline", "started", "reason")}
    db.execute("insert or replace into app_state(key,value) values('maintenance',?)", (json.dumps(payload, ensure_ascii=False),))
    db.commit()

async def publish_maintenance():
    msg = maintenance_payload()
    await asyncio.gather(*(bcast(r, msg) for r in list(ROOMS.values())))

async def _activate_maintenance_at(deadline):
    global MAINTENANCE_TASK
    try:
        await asyncio.sleep(max(0, float(deadline) - time.time()))
        if MAINTENANCE["phase"] != "warning" or MAINTENANCE.get("deadline") != deadline:
            return
        MAINTENANCE.update(phase="active", deadline=None, started=time.time())
        persist_maintenance()
        await publish_maintenance()
    except asyncio.CancelledError:
        raise
    finally:
        if MAINTENANCE_TASK is asyncio.current_task(): MAINTENANCE_TASK = None

async def maintenance_start(reason="Плановые технические работы"):
    global MAINTENANCE_TASK
    if MAINTENANCE_TASK:
        MAINTENANCE_TASK.cancel()
    reason = re.sub(r"[\x00-\x1f]", "", str(reason or "")).strip()[:120] or "Плановые технические работы"
    deadline = time.time() + 180
    MAINTENANCE.update(phase="warning", deadline=deadline, started=None, reason=reason)
    persist_maintenance()
    MAINTENANCE_TASK = asyncio.create_task(_activate_maintenance_at(deadline))
    await publish_maintenance()

async def maintenance_finish():
    global MAINTENANCE_TASK
    if MAINTENANCE_TASK:
        MAINTENANCE_TASK.cancel(); MAINTENANCE_TASK = None
    MAINTENANCE.update(phase="off", deadline=None, started=None)
    persist_maintenance()
    await publish_maintenance()

async def join(r, uid, name, ws, spectate, auto=False):
    if uid in r.banned: return await send(ws, {"t": "error", "e": "Вас удалили из этой комнаты"})
    dup = lambda nm: any(u != uid and n.lower() == nm.lower() for u, (_, n) in r.conn.items())
    if auto:   # ник из Telegram менять нельзя — при совпадении добавляем номер
        base, i = name, 2
        while dup(name): name = f"{base[:13]} {i}"; i += 1
    elif dup(name):
        return await send(ws, {"t": "error", "c": "nick", "e": f"Ник «{name}» уже занят в этой комнате. Выберите другой"})
    if uid == r.owner:
        r.owner_name = name
        if r.xfer: r.xfer.cancel(); r.xfer = None   # владелец вернулся — передача отменяется
    role = r.roles.get(uid) or ("spectator" if spectate else "player")
    if r.daily and role == "player":
        return await send(ws, {"t": "error", "e": "Это личная комната задания — зайдите по коду зрителя"})
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
    await send(ws, {"t": "joined", "code": r.code if role != "spectator" else r.watch, "age": r.now(), "watch": r.watch if role == "owner" else None, "role": role,
                    "you": uid, "members": r.members(), "log": r.log, "hidden": list(r.hidden),
                    "daily": r.daily, "pid": r.pid if role == "owner" else None,
                    "title": r.title, "public": bool(r.pub), "open_draw": r.open_draw, "game": game_pub(r),
                    "trails": {str(u): c for u, c in r.trail.items() if u in r.conn},
                    "inf": r.inf, "areas": {str(u): a for u, a in r.areas.items()},
                    "ptitle": r.ptitle if role == "owner" else None})
    await bcast(r, {"t": "members", "members": r.members()}, skip=uid)
    g = r.game
    if g and g["g"] == "guess" and g.get("phase") == "draw" and g.get("drawer") == uid: await send(ws, {"t": "word", "word": g["word"]})
    return True

async def leave(r, uid, ws):
    c = r.conn.get(uid)
    if not c or c[0] is not ws: return
    del r.conn[uid]; r.idle.discard(uid); r.prem.discard(uid); r.trail.pop(uid, None)
    if uid == r.owner and not r.daily:   # владелец вышел — через 12 с владельцем становится случайный игрок
        if r.xfer: r.xfer.cancel()
        r.xfer = asyncio.get_running_loop().call_later(12, lambda: asyncio.create_task(transfer_owner(r)))
    await bcast(r, {"t": "members", "members": r.members()})
    await game_check(r)
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
        c = r.conn.get(t)
        if c: await send(c[0], {"t": "code", "code": r.code})   # повышенному до игрока — код игрока
    await bcast(r, {"t": "members", "members": r.members()})

def num(v, lo, hi):
    return max(lo, min(hi, v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
def color(c): return c if isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c) else "#000000"
def bounds(r): return (-16000, 16000, -16000, 16000) if r.inf else (-50, W + 50, -50, H + 50)

def clean_pts(p, b=(-50, W + 50, -50, H + 50)):
    if not isinstance(p, list) or not p or len(p) > 200: return None
    out = []
    for q in p:
        if not (isinstance(q, list) and len(q) == 2): return None
        x, y = num(q[0], b[0], b[1]), num(q[1], b[2], b[3])
        if x is None or y is None: return None
        out.append([round(x, 1), round(y, 1)])
    return out

# ───────────── мини-игры: «Угадай рисунок» (2–8) и «Эстафета» (1–8) ─────────────
def game_pub(r):
    g = r.game
    if not g: return None
    out = {k: g[k] for k in ("g", "phase", "drawer", "turn", "turns", "scores", "theme", "hint", "names", "zones") if k in g}
    if "votes" in g: out["voted"] = list(g["votes"])
    if g.get("phase") in ("draw", "vote"): out["left"] = max(0, g["ends"] - int(time.time() * 1000))
    if g["g"] == "guess" and g.get("phase") in ("reveal", "end"): out["word"] = g.get("word")
    return out

async def game_push(r): await bcast(r, {"t": "game", "game": game_pub(r)})
def players_online(r): return [u for u in r.conn if r.roles.get(u) in ("owner", "player")]

def room_reset(r):
    r.log.clear(); r.idx.clear(); r.hidden.clear(); r.undo.clear(); r.redo.clear(); r.t0 = time.time()

async def game_stop(r, note=None):
    if r.gtask and r.gtask is not asyncio.current_task(): r.gtask.cancel()
    r.gtask = None; r.game = None
    await game_push(r)
    if note: await bcast(r, {"t": "chat", "sys": 1, "text": note})

async def game_check(r):
    g = r.game
    if not g: return
    on = players_online(r)
    if g["g"] == "guess" and len(on) < 2: return await game_stop(r, "Игра остановлена: нужно минимум 2 игрока")
    if g["g"] in ("relay", "speed") and not on: return await game_stop(r)
    if g["g"] != "speed" and g.get("drawer") not in on and g.get("ev"): g["ev"].set()   # рисующий ушёл — ход сразу заканчивается

async def game_start(r, kind):
    if r.game and r.game.get("phase") == "end": await game_stop(r)   # «Играть снова»
    if r.game: return "Игра уже идёт"
    if r.inf: return "Мини-игры доступны только на обычном холсте"
    if r.daily: return "В комнате задания игры недоступны"
    if kind not in ("guess", "relay", "speed"): return "Неизвестная игра"
    on = players_online(r)
    if kind == "guess" and len(on) < 2: return "Для «Угадай рисунок» нужно минимум 2 игрока"
    room_reset(r); await bcast(r, {"t": "reset"})
    order = on[:]; random.shuffle(order); solo = len(order) == 1
    r.game = {"g": kind, "phase": "start", "order": order, "turn": 0, "turns": 1 if kind == "speed" else len(order) * (2 if kind == "relay" and not solo else 1),
              "scores": {u: 0 for u in order}, "names": {u: r.conn[u][1] for u in order}, "ev": asyncio.Event(),
              "turn_len": 60 if (solo or kind in ("guess", "speed")) else 20, "guessed": set(), "drawer": None}
    r.gtask = asyncio.create_task(run_game(r))

async def run_game(r):
    g = r.game
    try:
        if g["g"] == "speed": return await run_speed(r)
        if g["g"] == "relay": g["theme"] = pick(r, "t", PROMPTS)
        while g["turn"] < g["turns"]:
            drawer = g["order"][g["turn"] % len(g["order"])]
            if drawer not in players_online(r): g["turn"] += 1; continue
            g.update(ev=asyncio.Event(), drawer=drawer, phase="draw", guessed=set(), ends=int(time.time() * 1000) + g["turn_len"] * 1000)
            if g["g"] == "guess":
                word = pick(r, "w", WORDS); g["word"] = word; g["hint"] = "".join(" " if c == " " else "_" for c in word)
                room_reset(r); await bcast(r, {"t": "reset"})
            await game_push(r)
            if g["g"] == "guess":   # слово шлём ПОСЛЕ состояния игры — иначе в первом раунде клиент его терял
                c = r.conn.get(drawer)
                if c: await send(c[0], {"t": "word", "word": word})
            try: await asyncio.wait_for(g["ev"].wait(), g["turn_len"] / 2)
            except asyncio.TimeoutError:
                if g["g"] == "guess": g["hint"] = g["word"][0] + g["hint"][1:]; await game_push(r)   # подсказка — первая буква
                try: await asyncio.wait_for(g["ev"].wait(), g["turn_len"] / 2)
                except asyncio.TimeoutError: pass
            if g["g"] == "guess": g["phase"] = "reveal"; await game_push(r); await asyncio.sleep(5)
            g["turn"] += 1
        g["phase"], g["drawer"] = "end", None
        await game_push(r); await asyncio.sleep(120); await game_stop(r)   # итоги висят, пока владелец не нажмёт «Играть снова» / «Остановить»
    except asyncio.CancelledError:
        pass

def zone_of(r, uid):
    g = r.game
    return g["zones"].get(uid) if g and g["g"] == "speed" and g.get("phase") == "draw" else None

async def run_speed(r):   # «Скоростной рисунок»: у каждого своя полоса, 60 с на тему, потом голосование
    g = r.game
    try:
        order = g["order"]; n = len(order); zw = W / n
        g["theme"] = pick(r, "t", PROMPTS); g["zones"] = {u: [round(i * zw), round((i + 1) * zw)] for i, u in enumerate(order)}
        g.update(phase="draw", drawer=None, ends=int(time.time() * 1000) + g["turn_len"] * 1000)
        await game_push(r); await asyncio.sleep(g["turn_len"])
        if n > 1:
            g.update(phase="vote", ends=int(time.time() * 1000) + 20000, votes={}, ev=asyncio.Event()); await game_push(r)
            try: await asyncio.wait_for(g["ev"].wait(), 20)
            except asyncio.TimeoutError: pass
            for t in g["votes"].values(): g["scores"][t] = g["scores"].get(t, 0) + 1
        g["phase"] = "end"; await game_push(r); await asyncio.sleep(120); await game_stop(r)
    except asyncio.CancelledError:
        pass

async def transfer_owner(r):
    r.xfer = None
    if r.owner in r.conn: return
    cands = [u for u in r.conn if r.roles.get(u) == "player"]
    if not cands: return
    old, new = r.owner, random.choice(cands)
    r.roles[old], r.roles[new], r.owner = "player", "owner", new
    r.owner_name = r.conn[new][1]
    await bcast(r, {"t": "members", "members": r.members()})
    await bcast(r, {"t": "notice", "text": f"{r.owner_name} теперь владелец комнаты"})
    await send(r.conn[new][0], {"t": "owner", "code": r.code, "watch": r.watch})

TRAIL_SHAPES = {"circle", "ring", "star", "heart", "square", "triangle", "spark"}
def clean_trail(c):   # след курсора — косметика Drawzo+
    return {"on": bool(c.get("on", True)), "color": color(c.get("color")), "speed": int(num(c.get("speed"), 1, 10) or 5),
            "size": int(num(c.get("size"), 4, 40) or 14), "shape": c.get("shape") if c.get("shape") in TRAIL_SHAPES else "circle"}

def clean_nick(v):
    n = re.sub(r"\s+", " ", str(v or "")).strip()
    return n if re.fullmatch(r"[\w .\-]{2,16}", n) else ""

async def wake(r, uid):
    if uid in r.idle:
        r.idle.discard(uid); await bcast(r, {"t": "members", "members": r.members()})

async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=2 * 1024 * 1024)
    await ws.prepare(req)
    user, room, uid, name, locked = None, None, 0, "", False
    maintenance_notified = False
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT: continue
            try: d = json.loads(msg.data); t = d.get("t")
            except Exception: continue
            if user is None:
                user = check_init_data(str(d.get("init", ""))) if t == "auth" else None
                if not user: await send(ws, {"t": "error", "e": "Ошибка авторизации Telegram"}); break
                init_s = str(d.get("init", "")); locked = not (init_s.startswith("dev:") or init_s.startswith("guest:"))   # зашли из Telegram — ник берётся из Telegram и не меняется
                uid = int(user["id"]); name = tg_name(user) if locked else str(user.get("first_name") or user.get("username") or "Гость")[:20]
                if is_banned(uid): await send(ws, {"t": "error", "e": "Доступ ограничен администратором"}); break
                prem = is_premium(uid)
                await send(ws, {"t": "hello", "premium": prem, "maxColors": 10 if prem else 5,
                                "bot": BOT_USERNAME, "app": APP_SHORT_NAME, "stars": DONATE_STARS, "daily": daily_info(),
                                "nick": name if locked else None, "locked": locked,
                                "uid": uid, "guest": uid < 0, "mod": is_mod(uid)})
                await send(ws, maintenance_payload())
                continue

            # Во время активных техработ сервер не принимает команды приложения,
            # даже если пользователь оставил WebSocket открытым заранее.
            if MAINTENANCE["phase"] == "active":
                if not maintenance_notified:
                    await send(ws, maintenance_payload())
                    maintenance_notified = True
                continue

            if uid < 0 and (t.startswith("daily") or (t == "create" and d.get("daily"))):   # ежедневка — только в Telegram
                await send(ws, {"t": "error", "e": "Перейдите в Telegram для полной версии"}); continue
            if t in ("create", "join"):
                if room: await leave(room, uid, ws); room = None
                if t == "create":
                    if len(ROOMS) >= MAX_ROOMS: await send(ws, {"t": "error", "e": "Сервер перегружен, попробуйте позже"}); continue
                    r, spec = Room(uid, daily_info()["prompt"] if d.get("daily") else None), False
                    r.inf = d.get("canvas") == "inf" and not d.get("daily")   # бесконечный холст
                else:
                    code = str(d.get("code", "")).strip().upper()
                    r, spec = ROOMS.get(code), False
                    if not r: r, spec = WATCH.get(code), True
                    if not r: await send(ws, {"t": "error", "e": "Комната не найдена"}); continue
                if await join(r, uid, name if locked else (clean_nick(d.get("nick")) or name), ws, spec, locked): room = r
                continue
            if t == "join_public":   # вход в публичную комнату из списка
                r = next((x for x in ROOMS.values() if x.pub and x.pub == str(d.get("id"))), None)
                if not r: await send(ws, {"t": "error", "e": "Комната больше не публичная"}); continue
                if room: await leave(room, uid, ws); room = None
                spec = not (d.get("as") == "player" and r.open_draw)
                if await join(r, uid, name if locked else (clean_nick(d.get("nick")) or name), ws, spec, locked): room = r
                continue
            if t == "public_list":
                items = [{"id": x.pub, "title": x.title or f"Комната {x.owner_name or 'друга'}", "owner": x.owner_name or "—",
                          "players": x.online("owner", "player"), "specs": x.online("spectator"),
                          "draw": x.open_draw and x.online("owner", "player") < MAX_PLAYERS, "game": x.game["g"] if x.game else None, "inf": x.inf}
                         for x in list(ROOMS.values()) if x.pub and uid not in x.banned and x.conn]
                items.sort(key=lambda i: -i["players"])
                await send(ws, {"t": "public", "rooms": items[:30]}); continue
            if t == "visit":   # открыл приложение — вдруг пропустил итоги состязания
                rc = recap_for(uid) if uid > 0 else None
                if rc: await send(ws, {"t": "recap", **rc})
                continue
            if t == "rename":
                pid, title = toint(d.get("id")), re.sub(r"[\x00-\x1f]", "", str(d.get("title") or "")).strip()[:40]
                if pid is not None and title: db.execute("update projects set title=? where id=? and uid=?", (title, pid, uid)); db.commit()
                await send(ws, {"t": "saves", "items": list_saves(uid)}); continue
            if t == "daily_edit":   # вернуться к своей сданной работе, а не рисовать с нуля
                info = daily_info()
                row = db.execute("select ops from daily_entries where day=? and uid=?", (info["day"], uid)).fetchone()
                if not row or not row[0]: await send(ws, {"t": "error", "e": "Не нашёл вашу работу за сегодня"}); continue
                if len(ROOMS) >= MAX_ROOMS: await send(ws, {"t": "error", "e": "Сервер перегружен, попробуйте позже"}); continue
                if room: await leave(room, uid, ws); room = None
                r = Room(uid, info["prompt"]); fill_room(r, row[0])
                if await join(r, uid, name if locked else (clean_nick(d.get("nick")) or name), ws, False, locked): room = r
                else: r.destroy()
                continue
            if t == "daily_del":   # модератор убирает неподобающую работу
                target = toint(d.get("uid"))
                if not is_mod(uid): await send(ws, {"t": "error", "e": "Только для модераторов"}); continue
                if target is not None:
                    day = daily_info()["day"]
                    db.execute("delete from daily_entries where day=? and uid=?", (day, target)); db.execute("delete from daily_votes where day=? and target=?", (day, target)); db.commit()
                await send(ws, daily_payload(uid)); continue
            if t == "saves":
                await send(ws, {"t": "saves", "items": list_saves(uid)}); continue
            if t == "delsave":
                pid = toint(d.get("id"))
                if pid is not None: db.execute("delete from projects where id=? and uid=?", (pid, uid)); db.commit()
                await send(ws, {"t": "saves", "items": list_saves(uid)}); continue
            if t == "daily":
                await send(ws, daily_payload(uid)); continue
            if t == "daily_vote":
                target, day = toint(d.get("uid")), daily_info()["day"]
                cur = db.execute("select target from daily_votes where day=? and voter=?", (day, uid)).fetchone()
                if target is not None and target != uid:
                    if cur and cur[0] == target:   # повторное нажатие — лайк убирается
                        db.execute("delete from daily_votes where day=? and voter=?", (day, uid)); db.commit()
                    elif db.execute("select 1 from daily_entries where day=? and uid=?", (day, target)).fetchone():
                        db.execute("insert or replace into daily_votes values(?,?,?)", (day, uid, target)); db.commit()   # лайк переносится
                await send(ws, daily_payload(uid)); continue
            if t == "replay_get":   # данные для реплея сохранённого проекта
                pid = toint(d.get("id"))
                row = db.execute("select ops, title, inf from projects where id=? and uid=?", (pid, uid)).fetchone() if pid is not None else None
                if not row: await send(ws, {"t": "error", "e": "Сохранение не найдено"}); continue
                await send(ws, {"t": "replay", "title": row[1], "inf": bool(row[2]), "ops": json.loads(zlib.decompress(row[0]))}); continue
            if t == "load":   # открыть сохранённый проект в новой комнате
                pid = toint(d.get("id"))
                row = db.execute("select ops, title, inf from projects where id=? and uid=?", (pid, uid)).fetchone() if pid is not None else None
                if not row: await send(ws, {"t": "error", "e": "Сохранение не найдено"}); continue
                if len(ROOMS) >= MAX_ROOMS: await send(ws, {"t": "error", "e": "Сервер перегружен, попробуйте позже"}); continue
                if room: await leave(room, uid, ws); room = None
                r = Room(uid); r.pid = pid; r.ptitle = row[1]; r.inf = bool(row[2])
                fill_room(r, row[0])   # последние 20 действий доступны для отмены и в загруженном проекте
                if await join(r, uid, name if locked else (clean_nick(d.get("nick")) or name), ws, False, locked): room = r
                else: r.destroy()
                continue
            if room is None: continue
            if t in ("pts", "op", "undo", "redo", "cur", "end"): await wake(room, uid)   # любое действие будит игрока

            if t == "pts" and room.can_draw(uid):
                pts, layer = clean_pts(d.get("pts"), bounds(room)), d.get("layer")
                if pts is None or layer not in (0, 1, 2): continue
                z = zone_of(room, uid)
                if z: pts = [[min(max(p[0], z[0]), z[1]), p[1]] for p in pts]   # в «Скоростном рисунке» нельзя выходить за свою полосу
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
                op["pts"].extend(pts); op["te"] = room.now()
                await bcast(room, {"t": "op", "op": op} if first else {"t": "pts", "id": key, "pts": pts}, skip=uid)

            elif t == "op" and room.can_draw(uid):
                o, k = d.get("op") or {}, (d.get("op") or {}).get("k")
                if o.get("layer") not in (0, 1, 2) or len(room.log) >= MAX_OPS: continue
                op = {"id": room.nid(), "uid": uid, "k": k, "layer": o["layer"]}
                if k == "fill":
                    if room.inf or (room.game and room.game["g"] == "speed"): continue   # заливка — только на обычном холсте и не в скоростной игре
                    x, y = num(o.get("x"), 0, W - 1), num(o.get("y"), 0, H - 1)
                    if x is None or y is None: continue
                    op.update(x=int(x), y=int(y), color=color(o.get("color")), alpha=num(o.get("alpha"), .05, 1) or 1)
                elif k == "img":
                    if room.daily or room.game: continue   # состязание и игры — без фото
                    data = o.get("data")
                    if not (isinstance(data, str) and data.startswith("data:image/") and len(data) < 600_000): continue
                    rng = (-16000, 16000) if room.inf else (-W, 2 * W)
                    box = [num(o.get(f), *rng) for f in ("x", "y", "w", "h")]
                    if None in box: continue
                    op.update(data=data, x=box[0], y=box[1], w=box[2], h=box[3], alpha=num(o.get("alpha"), .05, 1) or 1)
                elif k == "shape":
                    b = bounds(room); z = zone_of(room, uid)
                    box = [num(o.get(f), b[0], b[1]) if f[0] == "x" else num(o.get(f), b[2], b[3]) for f in ("x1", "y1", "x2", "y2")]
                    if z and None not in box: box[0], box[2] = [min(max(v, z[0]), z[1]) for v in (box[0], box[2])]
                    if o.get("s") not in SHAPES or None in box: continue
                    op.update(s=o["s"], x1=box[0], y1=box[1], x2=box[2], y2=box[3], color=color(o.get("color")),
                              size=num(o.get("size"), 1, 80) or 6, alpha=num(o.get("alpha"), .05, 1) or 1, fill=bool(o.get("fill")))
                elif k == "imgset":   # перенос / масштаб / прозрачность уже вставленной картинки
                    ref = room.idx.get(o.get("ref"))
                    rng = (-16000, 16000) if room.inf else (-W, 2 * W)
                    box = [num(o.get(f), *rng) for f in ("x", "y", "w", "h")]
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
                    i = st.pop(); room.hidden.discard(i)
                    undo_stack = room.undo.setdefault(uid, []); undo_stack.append(i)
                    if len(undo_stack) > 20: del undo_stack[:-20]
                    await bcast(room, {"t": "show", "id": i})
            elif t == "save" and room.owner == uid:
                prev = d.get("preview"); prev = prev if isinstance(prev, str) and prev.startswith("data:image/") and len(prev) < 150_000 else ""
                blob = zlib.compress(json.dumps([o for o in room.log if o["id"] not in room.hidden], separators=(",", ":")).encode(), 6)
                title, now = str(d.get("title") or "Рисунок")[:40], int(time.time())
                if len(blob) > 6_000_000: await send(ws, {"t": "error", "e": "Рисунок слишком большой для сохранения"}); continue
                if room.pid and db.execute("select 1 from projects where id=? and uid=?", (room.pid, uid)).fetchone():
                    db.execute("update projects set title=?, ops=?, preview=?, updated=?, inf=? where id=?", (title, blob, prev, now, int(room.inf), room.pid))
                else:
                    limit = 20 if is_premium(uid) else 10
                    if db.execute("select count(*) from projects where uid=?", (uid,)).fetchone()[0] >= limit:
                        await send(ws, {"t": "error", "e": f"Лимит сохранений ({limit}). Удалите старые во вкладке «Сохранения»"}); continue
                    room.pid = db.execute("insert into projects(uid, title, ops, preview, updated, inf) values(?,?,?,?,?,?)", (uid, title, blob, prev, now, int(room.inf))).lastrowid
                room.ptitle = title; db.commit(); await send(ws, {"t": "saved", "title": title})
            elif t == "daily_submit" and room.owner == uid and room.daily:
                img = d.get("img")
                if not (isinstance(img, str) and img.startswith("data:image/") and len(img) < 200_000): continue
                if room.day != daily_info()["day"]: await send(ws, {"t": "error", "e": "Задание уже закончилось"}); continue
                ops = zlib.compress(json.dumps([o for o in room.log if o["id"] not in room.hidden], separators=(",", ":")).encode(), 6)
                db.execute("insert or replace into daily_entries(day, uid, name, img, ts, ops) values(?,?,?,?,?,?)", (room.day, uid, room.conn[uid][1], img, int(time.time()), ops))
                db.commit(); await send(ws, {"t": "daily_ok"})
            elif t == "area" and room.inf and room.roles.get(uid) in ("owner", "player"):   # область реплея — прямоугольник 8:5
                if d.get("clear"): room.areas.pop(uid, None)
                else:
                    x, y, w = num(d.get("x"), -16000, 16000), num(d.get("y"), -16000, 16000), num(d.get("w"), 160, 8000)
                    if None in (x, y, w): continue
                    room.areas[uid] = {"x": round(x), "y": round(y), "w": round(w), "h": round(w * 0.625), "name": room.conn[uid][1]}
                await bcast(room, {"t": "areas", "areas": {str(u): a for u, a in room.areas.items()}})
            elif t == "gvote":   # «Скоростной рисунок»: голос за лучший рисунок
                g, tgt = room.game, toint(d.get("uid"))
                if g and g["g"] == "speed" and g.get("phase") == "vote" and uid in g["order"] and tgt in g["order"] and tgt != uid:
                    g["votes"][uid] = tgt; await game_push(room)
                    if len(g["votes"]) >= len([u for u in players_online(room) if u in g["order"]]): g["ev"].set()
            elif t == "trail" and is_premium(uid):
                cfg = clean_trail(d)
                if cfg["on"]: room.trail[uid] = cfg
                else: room.trail.pop(uid, None)
                await bcast(room, {"t": "trail", "uid": uid, "cfg": cfg}, skip=uid)
            elif t == "room_set" and room.owner == uid:
                ttl = re.sub(r"\s+", " ", str(d.get("title") or "")).strip()[:30]
                if re.fullmatch(r"[\w .,!?:()\-]{0,30}", ttl): room.title = ttl
                want = bool(d.get("public")) and not room.daily
                if want and not room.pub: room.pub = secrets.token_urlsafe(6).replace("-", "a").replace("_", "b")
                if not want: room.pub = None
                room.open_draw = bool(d.get("open_draw", True))
                await bcast(room, {"t": "room", "title": room.title, "public": bool(room.pub), "open_draw": room.open_draw})
            elif t == "game_start" and room.owner == uid:
                err = await game_start(room, d.get("g"))
                if err: await send(ws, {"t": "error", "e": err})
            elif t == "game_stop" and room.owner == uid: await game_stop(room, "Владелец остановил игру")
            elif t == "guess" and room.game:
                g, text = room.game, str(d.get("text", ""))[:40].strip(); nm = room.conn[uid][1]
                if not text: continue
                can = g["g"] == "guess" and g.get("phase") == "draw" and uid != g["drawer"] and room.roles.get(uid) in ("owner", "player") and uid not in g["guessed"]
                if can and norm(text) == norm(g["word"]):
                    left = max(0, g["ends"] - int(time.time() * 1000)) // 1000
                    g["guessed"].add(uid); g["scores"][uid] = g["scores"].get(uid, 0) + 50 + left
                    g["scores"][g["drawer"]] = g["scores"].get(g["drawer"], 0) + 20
                    await bcast(room, {"t": "chat", "sys": 1, "text": f"{nm} угадал(а) слово!"}); await game_push(room)
                    if len(g["guessed"]) >= len([u for u in players_online(room) if u != g["drawer"]]): g["ev"].set()
                else: await bcast(room, {"t": "chat", "name": nm, "text": text})
            elif t == "idle" and room.can_draw(uid):
                if d.get("v"):
                    if uid not in room.idle: room.idle.add(uid); await bcast(room, {"t": "members", "members": room.members()})
                else: await wake(room, uid)
            elif t == "end" and room.can_draw(uid):
                key = f"{uid}:{str(d.get('sid'))[:24]}"
                if key in room.idx: await bcast(room, {"t": "end", "id": key}, skip=uid)
            elif t == "cur" and room.can_draw(uid):
                b = bounds(room); x, y = num(d.get("x"), b[0], b[1]), num(d.get("y"), b[2], b[3])
                if x is not None and y is not None:   # спящим курсоры не шлём — экономим трафик
                    m = {"t": "cur", "uid": uid, "x": x, "y": y}
                    await asyncio.gather(*(send(w, m) for u, (w, _) in list(room.conn.items()) if u != uid and u not in room.idle))
            elif t == "admin" and room.owner == uid:
                await do_admin(room, d)
    finally:
        if room: await leave(room, uid, ws)
    return ws

# ───────────── видео-реплеи: загрузка и раздача ─────────────
REPL = Path(__file__).parent / "replays"; REPL.mkdir(exist_ok=True)
LAST_UP = {}
REPLAY_TTL = 24 * 60 * 60  # временные ссылки на видео-реплеи живут сутки

def cleanup_replay_files(now=None):
    now = time.time() if now is None else now
    for f in REPL.iterdir():
        try:
            if f.is_file() and now - f.stat().st_mtime > REPLAY_TTL:
                f.unlink(missing_ok=True)
        except OSError:
            pass
    # Ограничитель загрузок тоже не должен накапливать старые uid бесконечно.
    for uid, ts in list(LAST_UP.items()):
        if now - ts > 3600: LAST_UP.pop(uid, None)

async def replay_cleanup_loop():
    while True:
        cleanup_replay_files()
        await asyncio.sleep(3600)

async def upload(req):
    if MAINTENANCE["phase"] == "active":
        raise web.HTTPServiceUnavailable(text="Технические работы. Попробуйте позже.")
    user = check_init_data(req.headers.get("X-Init", ""))
    if not user: raise web.HTTPForbidden()
    uid, now = int(user["id"]), time.time()
    if now - LAST_UP.get(uid, 0) < 15: raise web.HTTPTooManyRequests()
    LAST_UP[uid] = now
    data = await req.read()
    if len(data) < 1000: raise web.HTTPBadRequest()
    cleanup_replay_files(now)
    name = secrets.token_urlsafe(12).replace("-", "a").replace("_", "b") + (".mp4" if "mp4" in req.content_type else ".webm")
    path = REPL / name; path.write_bytes(data)
    if name.endswith(".webm") and shutil.which("ffmpeg"):   # Telegram-историям нужен mp4 (H.264)
        try:
            out = path.with_suffix(".mp4")
            proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", str(path), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                                        "-movflags", "+faststart", "-an", str(out),
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 90)
            if proc.returncode == 0 and out.exists(): path.unlink(missing_ok=True); name = out.name
        except Exception: pass
    return web.json_response({"url": f"{WEBAPP_URL}/r/{name}"})

async def replay_file(req):
    name = req.match_info["name"]
    if not re.fullmatch(r"[A-Za-z0-9]{10,24}\.(mp4|webm)", name) or not (REPL / name).exists(): raise web.HTTPNotFound()
    return web.FileResponse(REPL / name, headers={"Content-Type": "video/mp4" if name.endswith("mp4") else "video/webm", "Cache-Control": "public, max-age=3600"})

# ───────────── твоя админка ─────────────
def admin_ok(v): return bool(ADMIN_KEY) and secrets.compare_digest(str(v or ""), ADMIN_KEY)

async def admin_page(req):
    if not admin_ok(req.query.get("key")): raise web.HTTPForbidden()
    key = req.query.get("key", "")
    rows = "".join(
        f"<tr><td>{r.code}</td><td>{r.watch}</td><td>{r.online('owner','player')}/8</td><td>{r.online('spectator')}</td>"
        f"<td>{len(r.log)}</td><td><form method=post action=/admin/close><input type=hidden name=key value='{key}'>"
        f"<input type=hidden name=code value={r.code}><button>Закрыть</button></form></td></tr>" for r in list(ROOMS.values()))
    n = db.execute("select count(*) from premium").fetchone()[0]
    phase = MAINTENANCE["phase"]
    if phase == "warning":
        left = max(0, int((MAINTENANCE.get("deadline") or time.time()) - time.time()))
        phase_text = f"Предупреждение: запуск через {left // 60:02d}:{left % 60:02d}. Причина: {MAINTENANCE.get('reason')}"
        action_form = f"<form method=post action=/admin/maintenance><input type=hidden name=key value='{key}'><input type=hidden name=action value='cancel'><button>Отменить запуск</button></form>"
    elif phase == "active":
        phase_text = f"Техработы идут. Причина: {MAINTENANCE.get('reason')}"
        action_form = f"<form method=post action=/admin/maintenance><input type=hidden name=key value='{key}'><input type=hidden name=action value='finish'><button>Завершить техработы</button></form>"
    else:
        phase_text = "Техработы не запущены."
        action_form = f"<form method=post action=/admin/maintenance><input type=hidden name=key value='{key}'><input type=hidden name=action value='start'><label>Причина / сообщение пользователям: <input name=reason maxlength=120 value='Плановые технические работы'></label> <button>Предупредить и запустить через 3 минуты</button></form>"
    html = f"""<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
    <title>Drawzo — админка</title><style>body{{font:16px system-ui;max-width:1000px;margin:24px auto;padding:0 16px;background:#f5f6fa;color:#202234}}section{{padding:16px;border:1px solid #ccd0df;border-radius:12px;margin:18px 0;background:white}}button{{padding:8px 12px;cursor:pointer}}input{{padding:8px;max-width:100%}}table{{border-collapse:collapse;width:100%}}td,th{{padding:8px;border:1px solid #ccd0df;text-align:left}}.state{{font-weight:700}}</style>
    <h1>Drawzo admin</h1><p>Комнат: {len(ROOMS)} · Premium: {n}</p>
    <section><h2>Технические работы</h2><p class=state>{phase_text}</p>{action_form}
    <p>При запуске всем показывается предупреждение на 3 минуты. После отсчёта приложение блокируется. Перед перезапуском сервера проверьте, что нужные проекты сохранены: несохранённые комнаты хранятся в памяти процесса.</p></section>
    <section><h2>Комнаты</h2><table><tr><th>Код комнаты</th><th>Код зрителя</th><th>Игроки</th><th>Зрители</th><th>Операций</th><th></th></tr>{rows}</table></section></html>"""
    return web.Response(content_type="text/html", text=html)

async def admin_maintenance(req):
    d = await req.post()
    if not admin_ok(d.get("key")): raise web.HTTPForbidden()
    action = d.get("action")
    if action == "start":
        await maintenance_start(d.get("reason") or "Плановые технические работы")
    elif action in ("cancel", "finish"):
        await maintenance_finish()
    raise web.HTTPFound(f"/admin?key={req.query.get('key') or d.get('key')}")

async def maintenance_status(_):
    payload = maintenance_payload()
    payload.pop("t", None)
    return web.json_response(payload, headers={"Cache-Control": "no-store"})

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

# ───────────── /admin_panel ─────────────
PENDING = {}   # id админа -> ожидаемое действие (ждём ID пользователя)
ADM_ACTIONS = {"addmod": "Назначить модератором", "delmod": "Снять модератора", "addprem": "Выдать Drawzo+", "delprem": "Забрать Drawzo+",
               "ban": "Забанить", "unban": "Разбанить"}

def adm_kb():
    b = lambda t, a: InlineKeyboardButton(text=t, callback_data="adm:" + a)
    rows = [[b("Статистика", "stats"), b("Списки", "list")],
            [b(ADM_ACTIONS["addmod"], "addmod"), b(ADM_ACTIONS["delmod"], "delmod")],
            [b(ADM_ACTIONS["addprem"], "addprem"), b(ADM_ACTIONS["delprem"], "delprem")],
            [b(ADM_ACTIONS["ban"], "ban"), b(ADM_ACTIONS["unban"], "unban")]]
    if MAINTENANCE["phase"] == "off":
        rows.append([b("⚠️ Техработы через 3 минуты", "maint_start"), b("Статус техработ", "maint_status")])
    elif MAINTENANCE["phase"] == "warning":
        rows.append([b("Отменить запуск техработ", "maint_cancel"), b("Статус техработ", "maint_status")])
    else:
        rows.append([b("Завершить техработы", "maint_finish"), b("Статус техработ", "maint_status")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.message(Command("admin_panel"))
async def admin_panel(m: Message):
    if m.from_user.id not in ADMIN_IDS: return
    PENDING.pop(m.from_user.id, None)
    await m.answer("Панель администратора Drawzo.\nМодератор может удалять неподобающие работы в ежедневном состязании.\n"
                   "ID человека он видит в настройках приложения (шестерёнка).", reply_markup=adm_kb())

@router.message(Command("cancel"))
async def adm_cancel(m: Message):
    if PENDING.pop(m.from_user.id, None): await m.answer("Отменено")

@router.callback_query(F.data.startswith("adm:"))
async def adm_cb(q: CallbackQuery):
    if q.from_user.id not in ADMIN_IDS: return await q.answer()
    act = q.data[4:]
    if act == "stats":
        n = lambda sql: db.execute(sql).fetchone()[0]
        text = (f"Комнат: {len(ROOMS)}\nИгроков онлайн: {sum(r.online('owner', 'player') for r in ROOMS.values())}\nПубличных комнат: {sum(1 for r in ROOMS.values() if r.pub)}\n"
                f"Drawzo+: {n('select count(*) from premium')}\nМодераторов: {n('select count(*) from staff')}\nВ бане: {n('select count(*) from bans')}\n"
                f"Работ сегодня: {db.execute('select count(*) from daily_entries where day=?', (daily_info()['day'],)).fetchone()[0]}")
    elif act == "list":
        ids = lambda sql: ", ".join(str(r[0]) for r in db.execute(sql).fetchall()) or "—"
        text = f"Модераторы: {ids('select uid from staff')}\nDrawzo+: {ids('select uid from premium')}\nБан: {ids('select uid from bans')}"
    elif act == "maint_status":
        phase = MAINTENANCE["phase"]
        if phase == "warning":
            left = max(0, int((MAINTENANCE.get("deadline") or time.time()) - time.time()))
            text = f"Техработы начнутся через {left // 60:02d}:{left % 60:02d}. Причина: {MAINTENANCE.get('reason')}"
        elif phase == "active": text = f"Техработы сейчас идут. Причина: {MAINTENANCE.get('reason')}"
        else: text = "Техработы не запущены."
    elif act == "maint_start":
        if MAINTENANCE["phase"] == "off":
            await maintenance_start()
            text = "Предупреждение отправлено пользователям. Через 3 минуты приложение перейдёт в режим техработ. Отменить запуск можно этой же кнопкой в панели."
        else: text = "Техработы уже запланированы или идут. Сначала проверьте статус."
    elif act == "maint_cancel":
        if MAINTENANCE["phase"] == "warning":
            await maintenance_finish(); text = "Запуск техработ отменён."
        else: text = "Сейчас нет ожидающего запуска, который можно отменить."
    elif act == "maint_finish":
        if MAINTENANCE["phase"] == "active":
            await maintenance_finish(); text = "Техработы завершены. Пользователи смогут вернуться в приложение."
        else: text = "Техработы сейчас не идут."
    elif act in ADM_ACTIONS:
        PENDING[q.from_user.id] = act
        text = f"{ADM_ACTIONS[act]}. Отправьте ID пользователя (число) или перешлите сюда любое его сообщение. /cancel — отмена"
    else: return await q.answer()
    if act.startswith("maint_"):
        try: await q.message.edit_reply_markup(reply_markup=adm_kb())
        except Exception: pass
    await q.message.answer(text); await q.answer()

@router.message(lambda m: m.from_user is not None and m.from_user.id in PENDING)
async def adm_input(m: Message):
    act = PENDING.pop(m.from_user.id)
    t = (m.text or "").strip(); o = getattr(m, "forward_origin", None); fu = getattr(o, "sender_user", None)
    uid = int(t) if re.fullmatch(r"-?\d{1,15}", t) else (fu.id if fu else None)
    if uid is None: return await m.answer("Не вижу ID. Откройте /admin_panel и попробуйте ещё раз (если человек скрыл пересылку — введите число).")
    note = None
    if act == "addmod": db.execute("insert or replace into staff values(?, 'mod')", (uid,)); note = "Вас назначили модератором Drawzo: в ежедневном состязании у работ появилась кнопка удаления."
    elif act == "delmod": db.execute("delete from staff where uid=?", (uid,))
    elif act == "addprem": set_premium(uid); note = "Вам выдан Drawzo+! Перезапустите приложение, чтобы всё включилось."
    elif act == "delprem": del_premium(uid)
    elif act == "ban":
        db.execute("insert or replace into bans values(?)", (uid,))
        for r in list(ROOMS.values()):
            c = r.conn.get(uid)
            if c: await c[0].close()
    elif act == "unban": db.execute("delete from bans where uid=?", (uid,))
    db.commit()
    await m.answer(f"Готово: {ADM_ACTIONS[act]} — {uid}")
    if note:
        try: await m.bot.send_message(uid, note)
        except Exception: pass

async def main():
    app = web.Application(client_max_size=30 * 1024 * 1024)
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler), web.get("/admin", admin_page),
                    web.post("/admin/close", admin_close), web.post("/admin/maintenance", admin_maintenance),
                    web.get("/maintenance/status", maintenance_status), web.post("/upload", upload),
                    web.get("/r/{name}", replay_file), web.get("/health", lambda _: web.Response(text="ok")),
                    web.static("/static", STATIC)])
    asyncio.create_task(replay_cleanup_loop())
    global MAINTENANCE_TASK
    if MAINTENANCE["phase"] == "warning" and MAINTENANCE.get("deadline"):
        MAINTENANCE_TASK = asyncio.create_task(_activate_maintenance_at(MAINTENANCE["deadline"]))
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
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
