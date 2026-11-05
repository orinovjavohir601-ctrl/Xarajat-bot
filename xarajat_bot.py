"""
Xarajatlarim boti (v2, bardoshli versiya)

Asosiy yaxshilanishlar:
  * Gemini modelini o'zi topadi (nom eskirsa ham bot yiqilmaydi)
  * 503/429/404 bo'lsa: boshqa modelga o'tadi, qayta urinadi
  * AI butunlay band bo'lsa: xabarni xotirada ushlab, orqa fonda qayta urinadi
    va natijani o'zi yuboradi (foydalanuvchi qayta yubormaydi)
  * Tokenni logga yozmaydi (httpx logi o'chirilgan)
  * "5000 kofe" -> 5 000 000 bo'lib ketadigan xato tuzatilgan
  * 409 Conflict (ikkita nusxa) aniq xabar bilan logga yoziladi
  * /ai buyrug'i: AI holatini tekshirish
"""
import os
import re
import sys
import json
import html
import time
import random
import asyncio
import logging
import threading
import urllib.request
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from telegram import Update, BotCommand, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters,
)
from google import genai
from google.genai import types

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx URL ni (ichida bot tokeni bor) logga yozmasligi uchun
for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("xarajat")

# ---------------- Sozlamalar ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip().strip("\"'")
if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
    sys.exit(
        "BOT_TOKEN noto'g'ri. U 'raqamlar:harflar' ko'rinishida bo'lishi kerak "
        "(BotFather bergan to'liq token, ':' belgisi bilan)."
    )

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip().strip("\"'")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Tashkent"))
PORT = int(os.environ.get("PORT", "10000"))
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
MAX_AMOUNT = 10_000_000_000

# Ixtiyoriy: GEMINI_MODELS="model1,model2" (bo'lmasa, o'zi topadi)
PREFERRED = [
    m.strip().removeprefix("models/")
    for m in os.getenv("GEMINI_MODELS", os.getenv("GEMINI_MODEL", "")).split(",")
    if m.strip()
]
FALLBACK_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]

client = (
    genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=15_000))
    if GEMINI_API_KEY else None
)

CATEGORIES = [
    "Oziq-ovqat", "Transport", "Uy-joy", "Kiyim", "Salomatlik",
    "Ko'ngil ochar", "Aloqa", "Ta'lim", "Boshqa",
]
KEYWORDS = {
    "Oziq-ovqat": ["non", "go'sht", "gosht", "sut", "ovqat", "osh", "o'sh", "plov", "choy", "kofe",
                   "bozor", "market", "meva", "sabzavot", "restoran", "kafe", "tushlik",
                   "nonushta", "kechki", "somsa", "lavash", "burger", "pitsa", "suv", "ichimlik",
                   "gorilla", "energetik", "cola", "kola", "pepsi", "fanta", "shirinlik",
                   "tort", "shashlik", "manti", "chuchvara", "kabob", "kebab", "dumbul", "tuxum",
                   "yog'", "guruch", "shakar", "olma", "banan"],
    "Transport": ["taksi", "benzin", "yoqilg'i", "metro", "avtobus", "yo'l", "mashina", "gaz", "parkovka"],
    "Uy-joy": ["ijara", "kommunal", "svet", "elektr", "gaz pul", "uy", "remont", "internet"],
    "Kiyim": ["kiyim", "oyoq kiyim", "krossovka", "ko'ylak", "shim", "kurtka"],
    "Salomatlik": ["dori", "apteka", "shifokor", "doktor", "klinika", "tish"],
    "Ko'ngil ochar": ["kino", "o'yin", "konsert", "sayohat", "dam olish"],
    "Aloqa": ["telefon", "paket", "aloqa", "mobil", "tarif"],
    "Ta'lim": ["kurs", "kitob", "o'qish", "maktab", "universitet", "dars"],
}

BTN_TODAY = "📅 Bugun"
BTN_MONTH = "🗓 Shu oy"
BTN_ARCHIVE = "🗄 Arxiv"
BTN_UNDO = "↩️ Oxirgisini o'chirish"
BTN_HELP = "ℹ️ Yordam"
KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_TODAY, BTN_MONTH], [BTN_ARCHIVE, BTN_UNDO], [BTN_HELP]],
    resize_keyboard=True,
)

HELP_TEXT = (
    "<b>Xarajatlarim boti</b>\n\n"
    "Xarajatni oddiy yozing yoki 🎤 ovozli xabar yuboring:\n"
    "• <code>non 5000</code>\n"
    "• <code>taksi 15 ming, tushlik 32000</code>\n"
    "• <code>5000 suv</code>\n\n"
    "Pastdagi tugmalar:\n"
    "📅 Bugun, 🗓 Shu oy, 🗄 Arxiv (oldingi oylar), ↩️ Oxirgisini o'chirish.\n\n"
    "AI band bo'lsa, xabaringiz saqlanadi va natija o'zi yuboriladi."
)

# ---------------- Baza ----------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///xarajat.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)


def init_db():
    if DATABASE_URL.startswith("sqlite") and os.getenv("RENDER"):
        log.warning("DIQQAT: SQLite Render'da har deployda o'chadi. "
                    "Doimiy saqlash uchun DATABASE_URL (Postgres) kiriting.")
    ident = "SERIAL PRIMARY KEY" if "postgresql" in DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with engine.begin() as c:
        c.execute(text(
            f"CREATE TABLE IF NOT EXISTS expenses ("
            f"id {ident}, user_id BIGINT NOT NULL, amount BIGINT NOT NULL, "
            f"category VARCHAR(50), note VARCHAR(300), created_at TIMESTAMP NOT NULL)"
        ))
        c.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_exp_user_time ON expenses (user_id, created_at)"
        ))


def now_local():
    return dt.datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def add_expenses(user_id, items):
    """Saqlaydi va yangi qatorlar id sini qaytaradi (kategoriyani keyin yangilash uchun)."""
    ts = now_local()
    with engine.begin() as c:
        last_before = c.execute(text(
            "SELECT COALESCE(MAX(id), 0) FROM expenses WHERE user_id=:u"), {"u": user_id}).scalar()
        for it in items:
            c.execute(text(
                "INSERT INTO expenses (user_id, amount, category, note, created_at) "
                "VALUES (:u, :a, :c, :n, :t)"),
                {"u": user_id, "a": int(it["amount"]), "c": it["category"],
                 "n": it["note"][:300], "t": ts})
        ids = [r[0] for r in c.execute(text(
            "SELECT id FROM expenses WHERE user_id=:u AND id>:b ORDER BY id"),
            {"u": user_id, "b": last_before}).fetchall()]
    return ids


def set_categories(updates):
    """updates: [(id, category), ...]"""
    with engine.begin() as c:
        for i, cat in updates:
            c.execute(text("UPDATE expenses SET category=:c WHERE id=:i"), {"c": cat, "i": i})


def fetch_range(user_id, start, end):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT id, amount, category, note, created_at FROM expenses "
            "WHERE user_id=:u AND created_at>=:s AND created_at<:e ORDER BY created_at"),
            {"u": user_id, "s": start, "e": end}).fetchall()


def fetch_all(user_id):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT amount, created_at FROM expenses WHERE user_id=:u"),
            {"u": user_id}).fetchall()


def delete_last(user_id):
    with engine.begin() as c:
        row = c.execute(text(
            "SELECT id, amount, category, note FROM expenses WHERE user_id=:u "
            "ORDER BY created_at DESC, id DESC LIMIT 1"), {"u": user_id}).fetchone()
        if not row:
            return None
        c.execute(text("DELETE FROM expenses WHERE id=:i"), {"i": row[0]})
    return row


# ---------------- Yordamchi ----------------
def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def to_dt(v):
    if isinstance(v, dt.datetime):
        return v
    return dt.datetime.fromisoformat(str(v))


def guess_category(note):
    # turli apostrof belgilarini (ʻ ’ ` ‘) oddiy ' ga keltiramiz: o'sh / oʻsh
    low = re.sub(r"[ʻʼ’‘`´]", "'", note.lower())
    tokens = set(re.findall(r"[\w']+", low))
    for cat, words in KEYWORDS.items():
        # qisqa so'zlar ("osh", "non", "uy") faqat butun so'z bo'lsa mos keladi
        if any((w in tokens) if len(w) <= 3 else (w in low) for w in words):
            return cat
    return "Boshqa"


# birlik so'zdan keyin harf kelsa ("kofe") birlik hisoblanmaydi
NUM_RE = re.compile(
    r"(\d{1,3}(?:[ .,]\d{3})+|\d+(?:[.,]\d+)?)(?:\s*(ming|mln|million|k|so'm|som|sum)(?![A-Za-z'’]))?",
    re.I,
)


def _to_number(s):
    """'5 000' -> 5000, '1.500' -> 1500, '1.5' -> 1.5"""
    if re.fullmatch(r"\d{1,3}(?:[ .,]\d{3})+", s):
        return int(re.sub(r"[ .,]", "", s))
    return float(s.replace(",", "."))


def parse_local(raw):
    """AI ishlamasa ham matnni tushunadigan oddiy parser."""
    items = []
    for part in re.split(r"(?<!\d),|,(?!\d)|[\n;]+|\s+va\s+|\s+и\s+", raw):
        part = part.strip()
        best = None  # (birlik_bormi, summa, match)
        for m in NUM_RE.finditer(part):
            num = _to_number(m.group(1))
            unit = (m.group(2) or "").lower()
            if unit in ("ming", "k"):
                num *= 1000
            elif unit in ("mln", "million"):
                num *= 1_000_000
            num = int(round(num))
            cand = (1 if unit in ("ming", "k", "mln", "million") else 0, num, m)
            if best is None or cand[:2] > best[:2]:
                best = cand
        if not best:
            continue
        _, num, m = best
        note = (part[:m.start()] + " " + part[m.end():]).strip(" -:.")
        note = re.sub(r"\s+", " ", note) or "Xarajat"
        if 0 < num <= MAX_AMOUNT:
            items.append({"amount": num, "category": guess_category(note), "note": note})
    return items


def clean_items(data):
    out = []
    for it in (data or []):
        if not isinstance(it, dict):
            continue
        try:
            amount = int(float(str(it.get("amount")).replace(" ", "")))
        except Exception:
            continue
        if amount <= 0 or amount > MAX_AMOUNT:
            continue
        cat = it.get("category") if it.get("category") in CATEGORIES else "Boshqa"
        note = str(it.get("note") or "Xarajat").strip() or "Xarajat"
        out.append({"amount": amount, "category": cat, "note": note})
    return out


# ---------------- AI: model topish + qayta urinish ----------------
PROMPT = (
    "Foydalanuvchi o'zbek tilida (ba'zan rus aralash) xarajat haqida yozgan yoki aytgan. "
    "Barcha xarajatlarni ajrat. Faqat JSON qaytar, boshqa matn yo'q:\n"
    '{"items":[{"amount":<so\'mda butun son>,"category":"<' + "|".join(CATEGORIES) + '>",'
    '"note":"<qisqa nom>"}]}\n'
    "'ming'=1000, 'mln'/'million'=1000000. Xarajat bo'lmasa items bo'sh bo'lsin.\n"
    "O'zbekcha sonlar: bir=1, ikki=2, uch=3, to'rt=4, besh=5, olti=6, yetti=7, sakkiz=8, "
    "to'qqiz=9, o'n=10, yigirma=20, o'ttiz=30, qirq=40, ellik=50, oltmish=60, yetmish=70, "
    "sakson=80, to'qson=90, yuz=100, ming=1000. "
    "Masalan: 'o'n besh ming'=15000, 'ikki yuz ellik ming'=250000, 'ikki yarim ming'=2500, "
    "'besh yuz'=500 (so'm), 'bir million'=1000000.\n"
    "note — mahsulot yoki xizmat nomi (bosh harf bilan, o'zbek lotin yozuvida). "
    "Bir xabarda bir nechta xarajat bo'lsa, har birini alohida yoz."
)
GEN_CFG = types.GenerateContentConfig(response_mime_type="application/json", temperature=0)

_BAD_WORDS = ("image", "tts", "live", "audio", "embed", "robotics", "computer",
              "vision", "aqa", "imagen", "veo", "gemma", "learnlm")
_cache = {"ts": 0.0, "list": []}
_dead = {}  # model -> qaysi vaqtgacha o'tkazib yuboriladi
_lock = threading.Lock()
AI_SEM = asyncio.Semaphore(4)


class AIAuthError(Exception):
    pass


def _version_key(name):
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    ver = float(m.group(1)) if m else 0.0
    lite = 1 if "lite" in name else 0
    pre = 1 if ("preview" in name or "exp" in name) else 0
    return (-ver, lite, pre, name)


def _discover():
    names = set()
    for m in client.models.list():
        n = (m.name or "").removeprefix("models/")
        acts = getattr(m, "supported_actions", None) or []
        if acts and "generateContent" not in acts:
            continue
        if "flash" not in n or any(b in n for b in _BAD_WORDS):
            continue
        names.add(n)
    return sorted(names, key=_version_key)


def get_models(force=False):
    now = time.time()
    with _lock:
        if force or not _cache["list"] or now - _cache["ts"] > 6 * 3600:
            try:
                _cache["list"] = _discover()
                _cache["ts"] = now
                log.info("Topilgan Gemini modellari: %s", _cache["list"][:6])
            except Exception as e:
                log.warning("Model ro'yxatini olib bo'lmadi: %s", str(e)[:150])
                _cache["ts"] = now - 5 * 3600  # 1 soatdan keyin qayta urinadi
        order = list(dict.fromkeys(PREFERRED + _cache["list"][:4] + FALLBACK_MODELS))
        alive = [m for m in order if _dead.get(m, 0) < now]
        return alive or order


def _classify(e):
    s = str(e)
    code = getattr(e, "code", None)
    if code == 404 or "NOT_FOUND" in s:
        return "dead"
    if (code in (401, 403) or "PERMISSION_DENIED" in s
            or "API_KEY_INVALID" in s or "API key not valid" in s):
        return "auth"
    if code == 400:
        return "next"
    return "retry"


def _call_ai(contents, rounds=3, budget=None):
    """budget — umumiy soniya chegarasi: undan oshsa, yangi urinish boshlanmaydi."""
    if not client:
        raise RuntimeError("GEMINI_API_KEY kiritilmagan")
    last = None
    stop_at = time.monotonic() + budget if budget else None
    for rnd in range(rounds):
        for model in get_models():
            if stop_at and time.monotonic() > stop_at:
                raise last or TimeoutError("AI vaqt chegarasi tugadi")
            try:
                resp = client.models.generate_content(
                    model=model, contents=contents, config=GEN_CFG)
                if resp.text:
                    return resp.text
                last = RuntimeError("AI bo'sh javob berdi")
            except Exception as e:
                last = e
                kind = _classify(e)
                log.warning("AI %s (urinish %d): %s", model, rnd + 1, str(e)[:160])
                if kind == "auth":
                    raise AIAuthError(str(e))
                if kind == "dead":
                    _dead[model] = time.time() + 3600
                    _cache["ts"] = 0  # keyingi safar ro'yxatni yangilaydi
        if rnd < rounds - 1:
            if stop_at and time.monotonic() + 3 > stop_at:
                break
            time.sleep(min(2 ** (rnd + 1), 8) + random.random())
    raise last or RuntimeError("AI mavjud emas")


def _parse_json(raw):
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)
    return data.get("items", []) if isinstance(data, dict) else data


async def ai_from_text(raw, rounds=1, budget=20):
    """Ro'yxat (bo'sh bo'lishi mumkin) yoki None (AI ishlamadi)."""
    if not client:
        return None
    try:
        async with AI_SEM:
            out = await asyncio.to_thread(_call_ai, [PROMPT + "\n\nMatn: " + raw], rounds, budget)
        return clean_items(_parse_json(out))
    except Exception as e:
        log.error("AI matn xatosi: %s", str(e)[:200])
        return None


async def ai_from_voice(data: bytes, mime="audio/ogg", rounds=2, budget=25):
    if not client:
        return None
    try:
        part = types.Part.from_bytes(data=data, mime_type=mime)
        async with AI_SEM:
            out = await asyncio.to_thread(
                _call_ai, [PROMPT + "\n\nOvozli xabar:", part], rounds, budget)
        return clean_items(_parse_json(out))
    except Exception as e:
        log.error("AI ovoz xatosi: %s", str(e)[:200])
        return None


async def refine_categories(ids, items):
    """Xabar allaqachon saqlangan. Noma'lum ('Boshqa') kategoriyalarni AI bilan jim yangilaydi."""
    try:
        notes = "; ".join(f"{it['note']} {it['amount']}" for it in items)
        ai = await ai_from_text(notes, rounds=2, budget=30)
        if not ai:
            return
        if len(ai) != len(items) or len(ids) != len(items):
            return  # tartib mos kelmasa, adashmaslik uchun tegmaymiz
        ups = []
        for i, (item, new) in enumerate(zip(items, ai)):
            if (item["category"] == "Boshqa" and new["category"] != "Boshqa"
                    and new["amount"] == item["amount"]):
                ups.append((ids[i], new["category"]))
        if ups:
            await asyncio.to_thread(set_categories, ups)
            log.info("Kategoriyalar yangilandi: %s", ups)
    except Exception as e:
        log.warning("Kategoriyani yangilab bo'lmadi: %s", str(e)[:150])


# ---------------- Hisobot ----------------
def build_report(title, rows):
    if not rows:
        return f"<b>{title}</b>\n\nHozircha xarajat yo'q. Yozing yoki 🎤 ovozli xabar yuboring."
    total = sum(r[1] for r in rows)
    by_cat = {}
    for r in rows:
        by_cat[r[2]] = by_cat.get(r[2], 0) + r[1]
    lines = [f"<b>{title}</b>", ""]
    for r in rows[-25:]:
        t = to_dt(r[4]).strftime("%d.%m %H:%M")
        lines.append(f"{t}  {html.escape(r[3])} — <b>{fmt(r[1])}</b>")
    if len(rows) > 25:
        lines.insert(2, f"<i>(oxirgi 25 ta ko'rsatildi, jami {len(rows)} ta)</i>")
    lines += ["", "<b>Kategoriyalar:</b>"]
    for cat, s in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"• {html.escape(cat)}: {fmt(s)}")
    lines += ["", f"<b>Jami: {fmt(total)} so'm</b>"]
    return "\n".join(lines)


async def reply(update: Update, text_, **kw):
    try:
        await update.effective_message.reply_text(
            text_, parse_mode=ParseMode.HTML, reply_markup=KEYBOARD, **kw)
    except Exception:
        try:
            await update.effective_message.reply_text(
                re.sub(r"<[^>]+>", "", text_), reply_markup=KEYBOARD)
        except Exception as e:
            log.error("Javob yuborilmadi: %s", e)


async def push(app: Application, chat_id, text_):
    """Foydalanuvchiga mustaqil xabar (orqa fondagi natijalar uchun)."""
    try:
        await app.bot.send_message(chat_id, text_, parse_mode=ParseMode.HTML,
                                   reply_markup=KEYBOARD)
    except Exception:
        try:
            await app.bot.send_message(chat_id, re.sub(r"<[^>]+>", "", text_),
                                       reply_markup=KEYBOARD)
        except Exception as e:
            log.error("Push yuborilmadi: %s", e)


# ---------------- Orqa fonda qayta urinish ----------------
_bg = set()
RETRY_DELAYS = [15, 30, 60, 90, 120, 180, 240]  # jami ~12 daqiqa


def schedule_retry(app, chat_id, user_id, make_call, prefix=""):
    """make_call() -> coroutine, ro'yxat yoki None qaytaradi."""
    if len(_bg) >= 30:
        return False

    async def runner():
        try:
            for d in RETRY_DELAYS:
                await asyncio.sleep(d)
                items = await make_call()
                if items is None:
                    continue
                if not items:
                    await push(app, chat_id, "Xabaringizdan xarajat topilmadi. Aniqroq yozing: «non 5000».")
                    return
                try:
                    await asyncio.to_thread(add_expenses, user_id, items)
                except Exception as e:
                    log.error("Bazaga yozishda xato (bg): %s", e)
                    await push(app, chat_id, "Saqlashda muammo bo'ldi, qayta yuboring.")
                    return
                await push(app, chat_id, prefix + confirm_text(items))
                return
            await push(app, chat_id,
                       "😔 AI uzoq vaqt band bo'ldi. Iltimos, matn bilan yozing: <code>non 5000</code>")
        except Exception as e:
            log.error("Orqa fon qayta urinish xatosi: %s", e)

    t = asyncio.create_task(runner())
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return True


# ---------------- Handlerlar ----------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, "Salom! 👋\n\n" + HELP_TEXT)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, HELP_TEXT)


async def cmd_ai(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """AI holatini tekshirish."""
    if not client:
        await reply(update, "❌ GEMINI_API_KEY kiritilmagan.")
        return
    models = await asyncio.to_thread(get_models)
    try:
        out = await asyncio.to_thread(_call_ai, ['{"items":[]} deb javob ber'], 1)
        status = "✅ AI ishlayapti"
    except AIAuthError:
        status = "❌ GEMINI_API_KEY noto'g'ri yoki ruxsat yo'q"
    except Exception as e:
        status = "⚠️ AI hozir javob bermayapti: " + html.escape(str(e)[:120])
    await reply(update, f"{status}\n\nModellar: <code>{html.escape(', '.join(models[:5]))}</code>")


async def show_today(update, ctx):
    uid = update.effective_user.id
    today = now_local().replace(hour=0, minute=0, second=0)
    rows = await asyncio.to_thread(fetch_range, uid, today, today + dt.timedelta(days=1))
    await reply(update, build_report("📅 Bugun", rows))


async def show_month(update, ctx):
    uid = update.effective_user.id
    n = now_local()
    start = n.replace(day=1, hour=0, minute=0, second=0)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    rows = await asyncio.to_thread(fetch_range, uid, start, end)
    await reply(update, build_report("🗓 Shu oy", rows))


MONTHS = ["", "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
          "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr"]


async def show_archive(update, ctx):
    uid = update.effective_user.id
    rows = await asyncio.to_thread(fetch_all, uid)
    if not rows:
        await reply(update, "<b>🗄 Arxiv</b>\n\nHozircha ma'lumot yo'q.")
        return
    agg = {}
    for amount, created in rows:
        d = to_dt(created)
        key = (d.year, d.month)
        agg[key] = agg.get(key, 0) + amount
    lines = ["<b>🗄 Arxiv (oylar bo'yicha)</b>", ""]
    for (y, m) in sorted(agg, reverse=True)[:12]:
        lines.append(f"{MONTHS[m]} {y}: <b>{fmt(agg[(y, m)])}</b> so'm")
    await reply(update, "\n".join(lines))


async def do_undo(update, ctx):
    row = await asyncio.to_thread(delete_last, update.effective_user.id)
    if not row:
        await reply(update, "O'chiriladigan xarajat yo'q.")
    else:
        await reply(update, f"🗑 O'chirildi: {html.escape(row[3])} — <b>{fmt(row[1])}</b>")


def confirm_text(items):
    lines = ["✅ <b>Qo'shildi:</b>"]
    for it in items:
        lines.append(f"• {html.escape(it['note'])} — <b>{fmt(it['amount'])}</b> ({html.escape(it['category'])})")
    if len(items) > 1:
        lines.append(f"\nJami: <b>{fmt(sum(i['amount'] for i in items))}</b> so'm")
    return "\n".join(lines)


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)


async def finish(update: Update, ack, text_):
    """Avval yuborilgan "eshityapman" xabarini natija bilan almashtiradi."""
    try:
        await ack.edit_text(text_, parse_mode=ParseMode.HTML)
    except Exception:
        await reply(update, text_)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = (update.effective_message.text or "").strip()
    actions = {BTN_TODAY: show_today, BTN_MONTH: show_month, BTN_ARCHIVE: show_archive,
               BTN_UNDO: do_undo, BTN_HELP: cmd_help}
    if raw in actions:
        await actions[raw](update, ctx)
        return

    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    items = parse_local(raw)
    if items:
        # Tez yo'l: AI ni kutmasdan darhol saqlaymiz va javob beramiz.
        try:
            ids = await asyncio.to_thread(add_expenses, uid, items)
        except Exception as e:
            log.error("Bazaga yozishda xato: %s", e)
            await reply(update, "Saqlashda muammo bo'ldi, bir daqiqadan keyin qayta yuboring.")
            return
        await reply(update, confirm_text(items))
        if client and any(i["category"] == "Boshqa" for i in items):
            _spawn(refine_categories(ids, items))  # kategoriyani orqa fonda aniqlaymiz
        return

    # Raqam topilmadi (masalan "non besh ming"): AI kerak
    ai = await ai_from_text(raw)
    if ai:
        try:
            await asyncio.to_thread(add_expenses, uid, ai)
        except Exception as e:
            log.error("Bazaga yozishda xato: %s", e)
            await reply(update, "Saqlashda muammo bo'ldi, bir daqiqadan keyin qayta yuboring.")
            return
        await reply(update, confirm_text(ai))
        return
    if ai is None and client and schedule_retry(
            ctx.application, chat_id, uid, lambda: ai_from_text(raw, budget=40)):
        await reply(update, "⏳ AI hozir band. Xabaringiz saqlandi, tayyor bo'lgach o'zim yuboraman.")
        return
    await reply(update, "Tushunmadim 🤔\nMasalan: <code>non 5000</code> yoki <code>taksi 15 ming</code>")


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    media = msg.voice or msg.audio
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    mime = getattr(media, "mime_type", None) or "audio/ogg"

    # Darhol javob: foydalanuvchi xabar qabul qilinganini ko'radi
    ack = await msg.reply_text("🎤 Eshityapman...", reply_markup=KEYBOARD)
    try:
        f = await media.get_file()
        data = bytes(await f.download_as_bytearray())
    except Exception as e:
        log.error("Ovoz yuklanmadi: %s", e)
        await finish(update, ack, "Ovozni yuklab bo'lmadi, qayta yuboring yoki matn yozing.")
        return
    log.info("Ovoz keldi: %d bayt, %s", len(data), mime)

    items = await ai_from_voice(data, mime)
    if items is None:
        ok = client and schedule_retry(
            ctx.application, chat_id, uid,
            lambda: ai_from_voice(data, mime, rounds=1, budget=40), prefix="🎤 ")
        if ok:
            await finish(update, ack, "⏳ AI hozir band. Ovozingiz saqlandi, o'zim qayta urinaman "
                                      "va natijani shu yerga yuboraman.")
        else:
            await finish(update, ack, "🎤 Ovozni hozir eshita olmadim. Iltimos, matn bilan yozing: "
                                      "<code>non 5000</code>")
        return
    if not items:
        await finish(update, ack, "Ovozdan xarajat topilmadi. Aniqroq ayting, masalan: «non besh ming».")
        return
    try:
        await asyncio.to_thread(add_expenses, uid, items)
    except Exception as e:
        log.error("Bazaga yozishda xato: %s", e)
        await finish(update, ack, "Saqlashda muammo bo'ldi, qayta yuboring.")
        return
    await finish(update, ack, "🎤 " + confirm_text(items))


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    if isinstance(err, Conflict):
        log.error("409 Conflict: bu bot boshqa joyda ham ishlayapti. "
                  "Ortiqcha nusxani (boshqa servis/kompyuter) to'xtating.")
        return
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Tarmoq xatosi: %s", err)
        return
    log.error("Kutilmagan xato", exc_info=err)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Kutilmagan xato bo'ldi, qayta urinib ko'ring.", reply_markup=KEYBOARD)
        except Exception:
            pass


# ---------------- Server (Render uchun) ----------------
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def start_server():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Ping)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("HTTP server %s portda", PORT)


async def self_ping(ctx: ContextTypes.DEFAULT_TYPE):
    if not SELF_URL:
        return
    try:
        await asyncio.to_thread(urllib.request.urlopen, SELF_URL, None, 15)
    except Exception as e:
        log.warning("self_ping: %s", e)


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Boshlash"),
        BotCommand("help", "Yordam"),
        BotCommand("ai", "AI holatini tekshirish"),
    ])
    if client:
        await asyncio.to_thread(get_models, True)


def main():
    init_db()
    start_server()
    app = (
        Application.builder().token(BOT_TOKEN)
        .concurrent_updates(True)          # bitta sekin so'rov boshqalarni to'xtatmaydi
        .get_updates_read_timeout(30)
        .post_init(post_init).build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ai", cmd_ai))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    if app.job_queue:
        app.job_queue.run_repeating(self_ping, interval=480, first=60)
    app.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
