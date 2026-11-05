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
import io
import csv
import hmac
import hashlib
import urllib.request
from urllib.parse import parse_qsl, parse_qs, urlsplit
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from telegram import (
    Update, BotCommand, ReplyKeyboardMarkup, InlineKeyboardButton,
    InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo,
)
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
# Mini ilova manzili (HTTPS bo'lishi shart). Bo'sh bo'lsa, Render manzilidan olinadi.
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip() or (
    SELF_URL.rstrip("/") + "/app" if SELF_URL else "")

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
BTN_APP = "📊 Ilova"
KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_TODAY, BTN_MONTH], [BTN_ARCHIVE, BTN_UNDO], [BTN_APP, BTN_HELP]],
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
        c.execute(text(
            f"CREATE TABLE IF NOT EXISTS income (id {ident}, user_id BIGINT NOT NULL, "
            f"amount BIGINT NOT NULL, note VARCHAR(300), created_at TIMESTAMP NOT NULL)"))
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS budgets (user_id BIGINT NOT NULL, "
            "category VARCHAR(50) NOT NULL, amount BIGINT NOT NULL, PRIMARY KEY (user_id, category))"))
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS settings (user_id BIGINT PRIMARY KEY, remind INTEGER NOT NULL)"))


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


def delete_by_id(user_id, exp_id):
    with engine.begin() as c:
        r = c.execute(text("DELETE FROM expenses WHERE id=:i AND user_id=:u"),
                      {"i": exp_id, "u": user_id})
    return r.rowcount > 0


def month_bounds(offset):
    """offset=0 shu oy, -1 oldingi oy ..."""
    n = now_local()
    y, m = n.year, n.month + offset
    while m < 1:
        m += 12
        y -= 1
    start = dt.datetime(y, m, 1)
    end = dt.datetime(y + (m == 12), m % 12 + 1, 1)
    return start, end


def sum_range(user_id, start, end):
    with engine.connect() as c:
        return int(c.execute(text(
            "SELECT COALESCE(SUM(amount),0) FROM expenses "
            "WHERE user_id=:u AND created_at>=:s AND created_at<:e"),
            {"u": user_id, "s": start, "e": end}).scalar() or 0)


def fetch_income(user_id, start, end):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT id, amount, note, created_at FROM income "
            "WHERE user_id=:u AND created_at>=:s AND created_at<:e ORDER BY created_at"),
            {"u": user_id, "s": start, "e": end}).fetchall()


def add_income(user_id, items):
    ts = now_local()
    with engine.begin() as c:
        for it in items:
            c.execute(text("INSERT INTO income (user_id, amount, note, created_at) "
                           "VALUES (:u,:a,:n,:t)"),
                      {"u": user_id, "a": int(it["amount"]), "n": it["note"][:300], "t": ts})


def delete_row(user_id, row_id, kind="e"):
    table = "income" if kind == "i" else "expenses"
    with engine.begin() as c:
        r = c.execute(text(f"DELETE FROM {table} WHERE id=:i AND user_id=:u"),
                      {"i": row_id, "u": user_id})
    return r.rowcount > 0


def update_expense(user_id, row_id, amount, category, note):
    with engine.begin() as c:
        r = c.execute(text(
            "UPDATE expenses SET amount=:a, category=:c, note=:n WHERE id=:i AND user_id=:u"),
            {"a": amount, "c": category, "n": note[:300], "i": row_id, "u": user_id})
    return r.rowcount > 0


def get_budgets(user_id):
    with engine.connect() as c:
        return {r[0]: int(r[1]) for r in c.execute(text(
            "SELECT category, amount FROM budgets WHERE user_id=:u"), {"u": user_id})}


def set_budgets(user_id, data):
    with engine.begin() as c:
        for cat, amount in data.items():
            c.execute(text("DELETE FROM budgets WHERE user_id=:u AND category=:c"),
                      {"u": user_id, "c": cat})
            if amount > 0:
                c.execute(text("INSERT INTO budgets (user_id, category, amount) VALUES (:u,:c,:a)"),
                          {"u": user_id, "c": cat, "a": amount})


def get_remind(user_id):
    with engine.connect() as c:
        v = c.execute(text("SELECT remind FROM settings WHERE user_id=:u"), {"u": user_id}).scalar()
    return True if v is None else bool(v)


def set_remind(user_id, on):
    with engine.begin() as c:
        c.execute(text("DELETE FROM settings WHERE user_id=:u"), {"u": user_id})
        c.execute(text("INSERT INTO settings (user_id, remind) VALUES (:u,:r)"),
                  {"u": user_id, "r": 1 if on else 0})


def users_to_remind():
    """Oxirgi 30 kunda xarajat kiritgan, lekin bugun kiritmagan va eslatmani o'chirmagan."""
    today = now_local().replace(hour=0, minute=0, second=0)
    with engine.connect() as c:
        return [r[0] for r in c.execute(text(
            "SELECT DISTINCT user_id FROM expenses WHERE created_at>=:since "
            "AND user_id NOT IN (SELECT user_id FROM settings WHERE remind=0) "
            "AND user_id NOT IN (SELECT user_id FROM expenses WHERE created_at>=:t)"),
            {"since": today - dt.timedelta(days=30), "t": today})]


def budget_warning(user_id, items):
    """Yangi xarajatdan keyin limit holati haqida ogohlantirish matni (yoki None)."""
    try:
        b = get_budgets(user_id)
        if not b:
            return None
        s, e = month_bounds(0)
        rows = fetch_range(user_id, s, e)
        total = sum(r[1] for r in rows)
        by = {}
        for r in rows:
            by[r[2]] = by.get(r[2], 0) + r[1]
        msgs = []
        for k, lim in b.items():
            spent = total if k == "*" else by.get(k, 0)
            touched = k == "*" or any(i["category"] == k for i in items)
            if touched and spent >= lim * 0.8:
                name = "Umumiy" if k == "*" else k
                msgs.append(f"{name}: {fmt(spent)} / {fmt(lim)} so'm" + (" ❗" if spent >= lim else ""))
        return ("⚠️ Byudjet: " + "; ".join(msgs)) if msgs else None
    except Exception:
        return None


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


async def cmd_app(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not WEBAPP_URL.startswith("https://"):
        await reply(update, "Mini ilova hali sozlanmagan: WEBAPP_URL (https) kiritilmagan.")
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Ilovani ochish", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await update.effective_message.reply_text("Xarajatlaringiz — grafik va ro'yxat bilan:", reply_markup=kb)


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
               BTN_UNDO: do_undo, BTN_HELP: cmd_help, BTN_APP: cmd_app}
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
        warn = await asyncio.to_thread(budget_warning, uid, items)
        await reply(update, confirm_text(items) + ("\n\n" + html.escape(warn) if warn else ""))
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
MINIAPP_HTML = r'''<!DOCTYPE html>
<html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<title>Xarajatlarim</title>
<style>
:root{--bg:var(--tg-theme-bg-color,#fff);--tx:var(--tg-theme-text-color,#111);--hint:var(--tg-theme-hint-color,#888);--card:var(--tg-theme-secondary-bg-color,#f2f2f7);--btn:var(--tg-theme-button-color,#2481cc);--btx:var(--tg-theme-button-text-color,#fff)}
*{box-sizing:border-box}body{margin:0;padding:12px 12px 40px;background:var(--bg);color:var(--tx);font:15px/1.35 -apple-system,system-ui,sans-serif}
.card{background:var(--card);border-radius:14px;padding:12px;margin-bottom:12px}
.nav{display:flex;align-items:center;justify-content:space-between;font-weight:600}
.nav button{background:none;border:0;color:var(--btn);font-size:22px;padding:4px 12px}.nav button:disabled{opacity:.25}
.total{text-align:center;font-size:26px;font-weight:700;margin-top:6px}.sub{text-align:center;color:var(--hint);font-size:13px}
.g{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px;font-size:13px}.g div{background:var(--bg);border-radius:10px;padding:8px}.g small{color:var(--hint);display:block}
.up{color:#e5484d}.dn{color:#2ea66b}
h4{margin:0 0 8px;font-size:13px;color:var(--hint);text-transform:uppercase}
.chart{display:flex;gap:14px;align-items:center}
.donut{width:96px;height:96px;border-radius:50%;flex:none;position:relative}.donut:after{content:"";position:absolute;inset:22px;background:var(--card);border-radius:50%}
.leg{flex:1;font-size:13px}.leg div{display:flex;justify-content:space-between;gap:8px;padding:2px 0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.bars{display:flex;align-items:flex-end;gap:2px;height:70px}.bars i{flex:1;background:var(--btn);border-radius:2px 2px 0 0;min-height:1px;opacity:.85}
.ax{display:flex;justify-content:space-between;color:var(--hint);font-size:11px;margin-top:2px}
.pb{height:8px;border-radius:4px;background:var(--bg);overflow:hidden;margin:3px 0 8px}.pb i{display:block;height:100%;background:#2ea66b}
.pl{display:flex;justify-content:space-between;font-size:13px}
.add,.flt{display:flex;gap:8px}
input,select{min-width:0;padding:10px;border-radius:10px;border:1px solid var(--hint);background:var(--bg);color:var(--tx);font-size:15px}
.add input{flex:1}.flt input{flex:1}
button.p{padding:10px 16px;border:0;border-radius:10px;background:var(--btn);color:var(--btx);font-weight:600}
button.s{padding:9px 12px;border:0;border-radius:10px;background:var(--bg);color:var(--tx);font-size:13px}
.day{color:var(--hint);font-size:12px;margin:10px 0 4px;text-transform:uppercase;display:flex;justify-content:space-between}
.row{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid rgba(128,128,128,.15)}
.row .n{flex:1;min-width:0}.row small{color:var(--hint);display:block}.row b{white-space:nowrap}.row .x{background:none;border:0;color:#e5484d;font-size:18px;padding:2px 6px}
.msg{text-align:center;color:var(--hint);padding:16px}
.md{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:flex-end;z-index:9}.md[hidden]{display:none}
.box{background:var(--bg);width:100%;border-radius:16px 16px 0 0;padding:14px;max-height:85vh;overflow:auto}
.box label{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:6px 0;font-size:14px}.box input,.box select{width:55%}
.ft{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:13px}
</style></head><body>
<div class="card">
  <div class="nav"><button id="prev">‹</button><span id="lbl">…</span><button id="next" disabled>›</button></div>
  <div class="total" id="total">0</div><div class="sub" id="sub">so'm</div>
  <div class="g" id="g"></div>
</div>
<div class="card add"><select id="kind"><option value="e">−</option><option value="i">+</option></select><input id="inp" placeholder="non 5000, taksi 15 ming" enterkeyhint="send"><button class="p" id="go">OK</button></div>
<div class="card"><h4>Kategoriyalar</h4><div class="chart" id="chart"></div></div>
<div class="card"><h4>Kunlik xarajat</h4><div class="bars" id="bars"></div><div class="ax"><span>1</span><span id="dmax"></span></div></div>
<div class="card"><h4>Byudjet</h4><div id="bud"></div><button class="s" id="bset">🎯 Byudjetni sozlash</button></div>
<div class="card"><h4>Eng katta xarajatlar</h4><div id="top"></div></div>
<div class="flt"><input id="q" placeholder="🔍 Qidirish"><select id="fc"></select></div>
<div id="list"></div>
<div class="card ft" style="margin-top:12px"><button class="s" id="ex1">📥 Shu oy CSV</button><button class="s" id="ex2">📥 Hammasi CSV</button>
<label style="margin-left:auto"><input type="checkbox" id="rm" style="width:auto"> 🔔 Eslatma 21:00</label></div>
<div class="md" id="md" hidden><div class="box" id="bx"></div></div>
<script>
const tg=window.Telegram.WebApp;tg.ready();tg.expand();
const CATS=["Oziq-ovqat","Transport","Uy-joy","Kiyim","Salomatlik","Ko'ngil ochar","Aloqa","Ta'lim","Boshqa"];
const COL={"Oziq-ovqat":"#f5a623","Transport":"#4a90e2","Uy-joy":"#7b61ff","Kiyim":"#e8559b","Salomatlik":"#2ecc71","Ko'ngil ochar":"#ff6b6b","Aloqa":"#17b8c4","Ta'lim":"#9b8a2b","Boshqa":"#9aa0a6","Daromad":"#2ea66b"};
const $=id=>document.getElementById(id);let off=0,D=null;
const fmt=n=>String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g," ");
const esc=s=>{const d=document.createElement("div");d.textContent=s;return d.innerHTML};
const num=s=>parseInt(String(s).replace(/\D/g,""))||0;
const opts=(l,sel)=>l.map(c=>`<option${c===sel?" selected":""}>${esc(c)}</option>`).join("");
$("fc").innerHTML='<option value="">Hammasi</option>'+opts([...CATS,"Daromad"]);
async function api(p,d){const r=await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({initData:tg.initData,...d})});if(!r.ok)throw new Error(r.status);return r.json()}
async function load(){try{D=await api("/api/month",{offset:off});render()}catch(e){$("list").innerHTML='<div class="msg">Yuklab bo\'lmadi. Ilovani bot ichidagi tugma orqali oching.</div>'}}
function render(){
  const d=D,T=d.total,by={};d.rows.forEach(r=>by[r.category]=(by[r.category]||0)+r.amount);
  const cats=Object.entries(by).sort((a,b)=>b[1]-a[1]);
  $("lbl").textContent=d.label;$("total").textContent=fmt(T);$("next").disabled=off>=0;$("rm").checked=d.remind;
  const avg=T/Math.max(1,d.elapsed),pct=d.prev?(T-d.prev)/d.prev*100:null,bal=d.incTotal-T;
  $("g").innerHTML=(d.incTotal?`<div><small>Daromad</small><b class="dn">+${fmt(d.incTotal)}</b></div><div><small>Balans</small><b class="${bal<0?"up":"dn"}">${bal<0?"−":"+"}${fmt(Math.abs(bal))}</b></div>`:"")+
   `<div><small>O'rtacha / kun</small><b>${fmt(avg)}</b></div>`+(off==0?`<div><small>Oy oxirigacha prognoz</small><b>${fmt(avg*d.days)}</b></div>`:"")+
   (pct!==null?`<div style="grid-column:1/-1"><small>O'tgan oyga (${fmt(d.prev)}) nisbatan</small><b class="${pct>0?"up":"dn"}">${pct>0?"+":""}${pct.toFixed(0)}%</b></div>`:"");
  let acc=0;const seg=cats.map(([c,v])=>{const s=acc/T*100;acc+=v;return `${COL[c]} ${s}% ${acc/T*100}%`});
  $("chart").innerHTML=cats.length?`<div class="donut" style="background:conic-gradient(${seg.join(",")})"></div><div class="leg">${cats.map(([c,v])=>`<div><span><i class="dot" style="background:${COL[c]}"></i>${esc(c)}</span><b>${fmt(v)}</b></div>`).join("")}</div>`:'<div class="msg">Ma\'lumot yo\'q</div>';
  const dy=Array(d.days).fill(0);d.rows.forEach(r=>dy[+r.t.slice(8,10)-1]+=r.amount);const mx=Math.max(1,...dy);
  $("bars").innerHTML=dy.map((v,i)=>`<i title="${i+1}: ${fmt(v)}" style="height:${v/mx*100}%"></i>`).join("");$("dmax").textContent=d.days;
  const bk=["*",...CATS].filter(k=>d.budgets[k]);
  $("bud").innerHTML=bk.length?bk.map(k=>{const sp=k=="*"?T:(by[k]||0),L=d.budgets[k],p=sp/L*100;
   return `<div class="pl"><span>${k=="*"?"Umumiy":esc(k)}</span><span>${fmt(sp)} / ${fmt(L)}</span></div><div class="pb"><i style="width:${Math.min(100,p)}%;background:${p>=100?"#e5484d":p>=80?"#f5a623":"#2ea66b"}"></i></div>`}).join(""):'<div class="sub" style="margin-bottom:8px">Limit belgilanmagan</div>';
  const tp=[...d.rows].sort((a,b)=>b.amount-a.amount).slice(0,3);
  $("top").innerHTML=tp.length?tp.map((r,i)=>`<div class="pl" style="padding:3px 0"><span>${i+1}. ${esc(r.note)}</span><b>${fmt(r.amount)}</b></div>`).join(""):'<div class="msg">—</div>';
  list();
}
function list(){
  const q=$("q").value.trim().toLowerCase(),fc=$("fc").value;
  const all=[...D.rows.map(r=>({...r,k:"e"})),...D.inc.map(r=>({...r,k:"i",category:"Daromad"}))].sort((a,b)=>b.t<a.t?-1:1)
   .filter(r=>(!fc||r.category==fc)&&(!q||(r.note+" "+r.category).toLowerCase().includes(q)));
  if(!all.length){$("list").innerHTML='<div class="msg">Hech narsa topilmadi</div>';return}
  const days={};all.forEach(r=>(days[r.t.slice(0,10)]??=[]).push(r));
  $("list").innerHTML=Object.entries(days).map(([k,rs])=>`<div class="day"><span>${k.slice(8)}.${k.slice(5,7)}</span><span>${fmt(rs.reduce((s,r)=>s+(r.k=="i"?0:r.amount),0))}</span></div>`+
   rs.map(r=>`<div class="row"><div class="n" data-id="${r.id}" data-k="${r.k}">${esc(r.note)}<small>${esc(r.category)} · ${r.t.slice(11,16)}${r.k=="e"?" · ✏️":""}</small></div><b class="${r.k=="i"?"dn":""}">${r.k=="i"?"+":""}${fmt(r.amount)}</b><button class="x" data-id="${r.id}" data-k="${r.k}">✕</button></div>`).join("")).join("");
}
function modal(h){$("bx").innerHTML=h;$("md").hidden=false}
function closeM(){$("md").hidden=true}
$("md").onclick=e=>{if(e.target.id=="md")closeM()};
$("list").onclick=e=>{const t=e.target,id=+t.dataset.id;if(!id)return;
  if(t.classList.contains("x"))return tg.showConfirm("O'chirilsinmi?",async ok=>{if(!ok)return;try{await api("/api/delete",{id,kind:t.dataset.k});load()}catch(_){}});
  if(t.dataset.k=="e"){const r=D.rows.find(x=>x.id==id);
   modal(`<h4>Xarajatni tahrirlash</h4><label>Summa<input id="ea" inputmode="numeric" value="${r.amount}"></label><label>Izoh<input id="en" value="${esc(r.note).replace(/"/g,"&quot;")}"></label><label>Kategoriya<select id="ec">${opts(CATS,r.category)}</select></label><div style="display:flex;gap:8px;margin-top:10px"><button class="p" style="flex:1" id="sv">Saqlash</button><button class="s" onclick="closeM()">Bekor</button></div>`);
   $("sv").onclick=async()=>{try{const d=await api("/api/edit",{id,amount:num($("ea").value),note:$("en").value,category:$("ec").value});if(d.error)return tg.showAlert(d.error);closeM();load()}catch(_){tg.showAlert("Xatolik")}}}};
$("bset").onclick=()=>{modal(`<h4>Oylik limit (so'm, bo'sh = yo'q)</h4>`+["*",...CATS].map(k=>`<label>${k=="*"?"Umumiy":k}<input inputmode="numeric" data-k="${k}" value="${D.budgets[k]||""}"></label>`).join("")+`<div style="display:flex;gap:8px;margin-top:10px"><button class="p" style="flex:1" id="sb">Saqlash</button><button class="s" onclick="closeM()">Bekor</button></div>`);
  $("sb").onclick=async()=>{const b={};document.querySelectorAll("#bx input[data-k]").forEach(i=>b[i.dataset.k]=num(i.value));try{await api("/api/budget",{budgets:b});closeM();load()}catch(_){tg.showAlert("Xatolik")}}};
$("prev").onclick=()=>{off--;load()};$("next").onclick=()=>{if(off<0){off++;load()}};
$("q").oninput=list;$("fc").onchange=list;
$("rm").onchange=async e=>{try{await api("/api/remind",{on:e.target.checked})}catch(_){}};
function exp(m){const u=location.origin+"/export?d="+encodeURIComponent(tg.initData)+"&m="+m;
  if(tg.isVersionAtLeast&&tg.isVersionAtLeast("8.0")&&tg.downloadFile)tg.downloadFile({url:u,file_name:"xarajatlar.csv"});else tg.openLink(u)}
$("ex1").onclick=()=>exp(off);$("ex2").onclick=()=>exp("all");
async function add(){const t=$("inp").value.trim();if(!t)return;
  try{const d=await api("/api/add",{text:t,kind:$("kind").value});if(d.error)return tg.showAlert(d.error);
   $("inp").value="";tg.HapticFeedback&&tg.HapticFeedback.notificationOccurred("success");if(d.warn)tg.showAlert(d.warn);off=0;load()}
  catch(_){tg.showAlert("Xatolik, qayta urinib ko'ring")}}
$("go").onclick=add;$("inp").onkeydown=e=>{if(e.key==="Enter")add()};
load();
</script></body></html>
'''


def verify_init_data(init_data, max_age=86400):
    """Telegram initData imzosini tekshiradi. To'g'ri bo'lsa user_id, aks holda None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > max_age:
            return None
        return int(json.loads(pairs["user"])["id"])
    except Exception:
        return None


def _csv_safe(v):
    v = str(v)
    return "'" + v if v[:1] in "=+-@" else v


class Ping(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        sp = urlsplit(self.path)
        if sp.path == "/app":
            return self._send(200, MINIAPP_HTML.encode(), "text/html; charset=utf-8")
        if sp.path == "/export":
            q = parse_qs(sp.query)
            uid = verify_init_data((q.get("d") or [""])[0])
            if not uid:
                return self._send(401, b"auth")
            try:
                m = (q.get("m") or ["0"])[0]
                s, e = ((dt.datetime(2000, 1, 1), dt.datetime(2100, 1, 1)) if m == "all"
                        else month_bounds(max(-36, min(0, int(m)))))
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["Sana", "Turi", "Kategoriya", "Izoh", "Summa"])
                data = [(to_dt(r[4]), "Xarajat", r[2], r[3], r[1]) for r in fetch_range(uid, s, e)]
                data += [(to_dt(r[3]), "Daromad", "Daromad", r[2], r[1]) for r in fetch_income(uid, s, e)]
                for t, kind, cat, note, amt in sorted(data, key=lambda x: x[0]):
                    w.writerow([t.strftime("%Y-%m-%d %H:%M"), kind, _csv_safe(cat), _csv_safe(note), amt])
                body = ("\ufeff" + buf.getvalue()).encode("utf-8")
                return self._send(200, body, "text/csv; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="xarajatlar.csv"'})
            except Exception as ex:
                log.error("Eksport xatosi: %s", ex)
                return self._send(500, b"error")
        self._send(200, b"ok")

    do_HEAD = do_GET

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 20_000:
                raise ValueError
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"error": "bad request"})
        uid = verify_init_data(body.get("initData", ""))
        if not uid:
            return self._json(401, {"error": "auth"})
        path = urlsplit(self.path).path
        try:
            if path == "/api/month":
                off = max(-36, min(0, int(body.get("offset", 0))))
                s, e = month_bounds(off)
                ps, pe = month_bounds(off - 1)
                rows = fetch_range(uid, s, e)
                inc = fetch_income(uid, s, e)
                return self._json(200, {
                    "label": f"{MONTHS[s.month]} {s.year}",
                    "total": sum(r[1] for r in rows),
                    "prev": sum_range(uid, ps, pe),
                    "days": (e - s).days,
                    "elapsed": now_local().day if off == 0 else (e - s).days,
                    "rows": [{"id": r[0], "amount": r[1], "category": r[2], "note": r[3],
                              "t": to_dt(r[4]).isoformat()} for r in rows],
                    "inc": [{"id": r[0], "amount": r[1], "note": r[2],
                             "t": to_dt(r[3]).isoformat()} for r in inc],
                    "incTotal": sum(r[1] for r in inc),
                    "budgets": get_budgets(uid),
                    "remind": get_remind(uid),
                })
            if path == "/api/add":
                items = parse_local(str(body.get("text", ""))[:500])
                if not items:
                    return self._json(200, {"error": "Tushunmadim. Masalan: non 5000"})
                if body.get("kind") == "i":
                    add_income(uid, items)
                    return self._json(200, {"ok": True})
                add_expenses(uid, items)
                return self._json(200, {"ok": True, "warn": budget_warning(uid, items)})
            if path == "/api/delete":
                ok = delete_row(uid, int(body.get("id", 0)), body.get("kind", "e"))
                return self._json(200, {"ok": ok})
            if path == "/api/edit":
                amount = int(body.get("amount", 0))
                cat = body.get("category")
                if not (0 < amount <= MAX_AMOUNT) or cat not in CATEGORIES:
                    return self._json(200, {"error": "Summa yoki kategoriya noto'g'ri"})
                note = str(body.get("note") or "Xarajat").strip() or "Xarajat"
                return self._json(200, {"ok": update_expense(uid, int(body["id"]), amount, cat, note)})
            if path == "/api/budget":
                data = {k: max(0, min(MAX_AMOUNT, int(v))) for k, v in (body.get("budgets") or {}).items()
                        if k == "*" or k in CATEGORIES}
                set_budgets(uid, data)
                return self._json(200, {"ok": True})
            if path == "/api/remind":
                set_remind(uid, bool(body.get("on")))
                return self._json(200, {"ok": True})
        except Exception as e:
            log.error("API xatosi %s: %s", path, e)
            return self._json(500, {"error": "server"})
        self._json(404, {"error": "not found"})

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


async def daily_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        users = await asyncio.to_thread(users_to_remind)
    except Exception as e:
        log.warning("Eslatma ro'yxati olinmadi: %s", e)
        return
    kb = None
    if WEBAPP_URL.startswith("https://"):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Ilovani ochish", web_app=WebAppInfo(url=WEBAPP_URL))]])
    for uid in users:
        try:
            await ctx.bot.send_message(
                uid, "🔔 Bugun hali xarajat kiritmadingiz. Yozing: <code>non 5000</code>",
                parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            pass
        await asyncio.sleep(0.05)


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Boshlash"),
        BotCommand("help", "Yordam"),
        BotCommand("app", "Mini ilovani ochish"),
        BotCommand("ai", "AI holatini tekshirish"),
    ])
    if WEBAPP_URL.startswith("https://"):
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="📊 Ilova", web_app=WebAppInfo(url=WEBAPP_URL)))
        except Exception as e:
            log.warning("Menu tugmasini o'rnatib bo'lmadi: %s", e)
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
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    if app.job_queue:
        app.job_queue.run_repeating(self_ping, interval=480, first=60)
        app.job_queue.run_daily(daily_reminder, time=dt.time(21, 0, tzinfo=TZ))
    app.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
