import os
import re
import json
import html
import asyncio
import logging
import threading
import urllib.request
import datetime as dt
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from telegram import (
    Update, BotCommand, ReplyKeyboardMarkup,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("xarajat")

BOT_TOKEN = os.environ["BOT_TOKEN"]
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TZ = ZoneInfo("Asia/Tashkent")
HTML = ParseMode.HTML

# ---------- Baza (Postgres yoki SQLite) ----------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///xarajat.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
IS_PG = engine.dialect.name == "postgresql"


def q(sql: str, **params):
    with engine.begin() as c:
        res = c.execute(text(sql), params)
        return [dict(r._mapping) for r in res] if res.returns_rows else []


def init_db():
    pk = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    q(f"""CREATE TABLE IF NOT EXISTS expenses(
        id {pk}, user_id BIGINT, amount BIGINT, note TEXT,
        category TEXT, day TEXT, created_at TEXT)""")
    q("CREATE TABLE IF NOT EXISTS users(user_id BIGINT PRIMARY KEY)")


def add_user(uid: int):
    q("INSERT INTO users(user_id) VALUES(:u) ON CONFLICT(user_id) DO NOTHING", u=uid)


# ---------- Yordamchi ----------
CAT = {
    "ovqat": "🍽", "transport": "🚕", "o'qish": "📚", "kommunal": "💡",
    "kiyim": "👕", "salomatlik": "💊", "ko'ngilochar": "🎮", "boshqa": "📦",
}
MONTHS = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul",
          "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]

PROMPT = """Foydalanuvchi o'zbek tilida xarajatlarini yozadi yoki aytadi.
Barcha xarajatlarni ajratib, FAQAT JSON massiv qaytar:
[{"amount": so'mda butun son, "note": qisqa izoh (bosh harf bilan), "category": ovqat|transport|o'qish|kommunal|kiyim|salomatlik|ko'ngilochar|boshqa}]
"15 ming" = 15000, "ikki yarim ming" = 2500, "1 million" = 1000000.
Xarajat bo'lmasa bo'sh massiv [] qaytar."""


def today() -> str:
    return dt.datetime.now(TZ).date().isoformat()


def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def bar(pct: int) -> str:
    f = round(pct / 10)
    return "▓" * f + "░" * (10 - f)


def month_title(ym: str) -> str:
    y, m = ym.split("-")
    return f"{MONTHS[int(m) - 1].capitalize()} {y}"


def E(s) -> str:
    return html.escape(str(s))


# ---------- Ko'rinishlar (matn + tugmalar) ----------
def day_view(uid: int, day: str):
    rows = q("SELECT id, amount, note, category FROM expenses "
             "WHERE user_id=:u AND day=:d ORDER BY id", u=uid, d=day)
    title = "Bugun" if day == today() else day
    if not rows:
        return (f"📅 <b>{title}</b>\n\nHozircha xarajat yo'q.\n"
                "Yozing yoki 🎤 ovozli xabar yuboring.", None)
    lines = [f"📅 <b>{title}</b>\n"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {CAT.get(r['category'], '📦')} {E(r['note'])} — <b>{fmt(r['amount'])}</b>")
    total = sum(r["amount"] for r in rows)
    lines.append(f"\n💰 <b>Jami: {fmt(total)} so'm</b>")
    lines.append("\n<i>O'chirish uchun raqamni bosing:</i>")
    btns = [InlineKeyboardButton(f"🗑 {i}", callback_data=f"del:{r['id']}")
            for i, r in enumerate(rows, 1)]
    kb = [btns[i:i + 5] for i in range(0, len(btns), 5)]
    kb.append([InlineKeyboardButton("🗓 Shu oy hisoboti", callback_data="month")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


def month_view(uid: int, ym: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", ym):
        return archive_view(uid)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("🗄 Arxivga qaytish", callback_data="arch")]])
    title = month_title(ym)
    rows = q("SELECT category, amount, day FROM expenses WHERE user_id=:u AND day LIKE :p",
             u=uid, p=ym + "%")
    if not rows:
        return f"🗓 <b>{title}</b>\n\nBu oyda xarajat yo'q.", back
    total = sum(r["amount"] for r in rows)
    by_cat, by_day = defaultdict(int), defaultdict(int)
    for r in rows:
        by_cat[r["category"]] += r["amount"]
        by_day[r["day"]] += r["amount"]
    lines = [f"🗓 <b>{title}</b>", "", f"💰 <b>Jami: {fmt(total)} so'm</b>", "",
             "<b>Kategoriyalar:</b>"]
    for cat, s in sorted(by_cat.items(), key=lambda x: -x[1]):
        pct = s * 100 // total
        lines.append(f"{CAT.get(cat, '📦')} {cat.capitalize()} — {fmt(s)}\n{bar(pct)} {pct}%")
    top_day, top_sum = max(by_day.items(), key=lambda x: x[1])
    lines += ["",
              f"📊 O'rtacha kuniga: <b>{fmt(total // len(by_day))}</b> so'm",
              f"🔥 Eng qimmat kun: {int(top_day[8:])}-sana — {fmt(top_sum)} so'm",
              f"🧾 Yozuvlar soni: {len(rows)}"]
    return "\n".join(lines), back


def archive_view(uid: int):
    rows = q("SELECT substr(day,1,7) AS ym, SUM(amount) AS s FROM expenses "
             "WHERE user_id=:u GROUP BY substr(day,1,7) ORDER BY ym DESC LIMIT 24", u=uid)
    if not rows:
        return "🗄 <b>Arxiv</b>\n\nHozircha bo'sh. Birinchi xarajatingizni yozing!", None
    kb = [[InlineKeyboardButton(f"🗓 {month_title(r['ym'])} — {fmt(r['s'])}",
                                callback_data=f"m:{r['ym']}")] for r in rows]
    return "🗄 <b>Arxiv</b>\n\nOyni tanlang:", InlineKeyboardMarkup(kb)


# ---------- AI ----------
def extract(content):
    r = client.models.generate_content(
        model=MODEL, contents=[PROMPT, content],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    try:
        data = json.loads(r.text)
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ---------- Buyruqlar ----------
MENU = ReplyKeyboardMarkup(
    [["📅 Bugun", "🗓 Shu oy"], ["🗄 Arxiv", "↩️ Oxirgisini o'chirish"], ["ℹ️ Yordam"]],
    resize_keyboard=True,
)

HELP = (
    "💸 <b>Kunlik xarajatlarim</b>\n\n"
    "Xarajatingizni oddiy so'z bilan yozing yoki 🎤 ovoz yuboring:\n"
    "• <i>non 4000</i>\n"
    "• <i>taksi 15 ming, tushlik 32 ming</i>\n\n"
    "Bot o'zi summa va kategoriyani aniqlaydi.\n"
    "Har kuni soat 22:00 da kunlik hisobot, oyning 1-kuni esa o'tgan oyning "
    "to'liq hisoboti avtomatik keladi.\n\n"
    "Pastdagi tugmalardan foydalaning 👇"
)


async def reply(update: Update, text_: str, kb=None):
    await update.effective_message.reply_text(text_, parse_mode=HTML, reply_markup=kb)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_user.id)
    await update.message.reply_text(HELP, parse_mode=HTML, reply_markup=MENU)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, HELP, MENU)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t, kb = day_view(update.effective_user.id, today())
    await reply(update, t, kb)


async def cmd_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t, kb = month_view(update.effective_user.id, today()[:7])
    await reply(update, t, kb)


async def cmd_archive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t, kb = archive_view(update.effective_user.id)
    await reply(update, t, kb)


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    r = q("SELECT id, amount, note FROM expenses WHERE user_id=:u ORDER BY id DESC LIMIT 1", u=uid)
    if not r:
        await reply(update, "O'chirish uchun yozuv yo'q.")
        return
    q("DELETE FROM expenses WHERE id=:i AND user_id=:u", i=r[0]["id"], u=uid)
    await reply(update, f"🗑 O'chirildi: {E(r[0]['note'])} — {fmt(r[0]['amount'])} so'm")


ROUTES = {
    "📅 Bugun": cmd_today, "🗓 Shu oy": cmd_month, "🗄 Arxiv": cmd_archive,
    "↩️ Oxirgisini o'chirish": cmd_undo, "ℹ️ Yordam": cmd_help,
}


# ---------- Xarajat qabul qilish ----------
async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    uid = update.effective_user.id
    add_user(uid)
    if m.text and m.text.strip() in ROUTES:
        await ROUTES[m.text.strip()](update, ctx)
        return

    await m.chat.send_action("typing")
    try:
        if m.voice:
            f = await m.voice.get_file()
            data = bytes(await f.download_as_bytearray())
            content = types.Part.from_bytes(data=data, mime_type="audio/ogg")
        else:
            content = m.text
        items = await asyncio.to_thread(extract, content)
    except Exception:
        log.exception("AI xatosi")
        await reply(update, "⏳ AI hozir band. Birozdan keyin qayta yuboring.")
        return

    clean = []
    for i in items:
        if isinstance(i, dict) and isinstance(i.get("amount"), (int, float)) and i["amount"] > 0:
            cat = i.get("category", "boshqa")
            clean.append({
                "amount": int(i["amount"]),
                "note": (str(i.get("note", "")).strip()[:100] or "Xarajat"),
                "category": cat if cat in CAT else "boshqa",
            })
    if not clean:
        await reply(update, "🤔 Xarajatni tushunmadim.\nMasalan: <i>non 4000</i> yoki <i>taksi 15 ming</i>")
        return

    now = dt.datetime.now(TZ)
    ids = []
    with engine.begin() as c:
        for i in clean:
            r = c.execute(text(
                "INSERT INTO expenses(user_id,amount,note,category,day,created_at) "
                "VALUES(:u,:a,:n,:c,:d,:t) RETURNING id"),
                {"u": uid, "a": i["amount"], "n": i["note"], "c": i["category"],
                 "d": now.date().isoformat(), "t": now.isoformat()})
            ids.append(r.scalar())
    total = q("SELECT SUM(amount) AS s FROM expenses WHERE user_id=:u AND day=:d",
              u=uid, d=now.date().isoformat())[0]["s"]

    added = "\n".join(f"{CAT[i['category']]} {E(i['note'])} — <b>{fmt(i['amount'])}</b>" for i in clean)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Bekor qilish", callback_data=f"undo:{min(ids)}-{max(ids)}"),
        InlineKeyboardButton("📅 Bugungi ro'yxat", callback_data="today"),
    ]])
    await reply(update, f"✅ <b>Saqlandi</b>\n\n{added}\n\n💰 Bugun jami: <b>{fmt(total)}</b> so'm", kb)


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    await qy.answer()
    uid = qy.from_user.id
    d = qy.data or ""
    if d == "today":
        t, kb = day_view(uid, today())
    elif d == "month":
        t, kb = month_view(uid, today()[:7])
    elif d == "arch":
        t, kb = archive_view(uid)
    elif d.startswith("m:"):
        t, kb = month_view(uid, d[2:])
    elif d.startswith("del:"):
        rid = int(d[4:])
        r = q("SELECT day FROM expenses WHERE id=:i AND user_id=:u", i=rid, u=uid)
        day = r[0]["day"] if r else today()
        q("DELETE FROM expenses WHERE id=:i AND user_id=:u", i=rid, u=uid)
        t, kb = day_view(uid, day)
    elif d.startswith("undo:"):
        a, b = d[5:].split("-")
        q("DELETE FROM expenses WHERE user_id=:u AND id BETWEEN :a AND :b", u=uid, a=int(a), b=int(b))
        t, kb = "↩️ <b>Bekor qilindi.</b>", None
    else:
        return
    try:
        await qy.edit_message_text(t, parse_mode=HTML, reply_markup=kb)
    except BadRequest:
        pass


# ---------- Avtomatik hisobotlar ----------
def all_users():
    return [r["user_id"] for r in q("SELECT user_id FROM users")]


async def daily_job(ctx: ContextTypes.DEFAULT_TYPE):
    for uid in all_users():
        try:
            t, _ = day_view(uid, today())
            if "Hozircha xarajat yo'q" in t:
                t = "🌙 Bugun xarajat kiritilmadi.\nUnutgan bo'lsangiz, hozir yozing 📝"
            else:
                t = "🌙 <b>Kun yakuni</b>\n\n" + t.split("\n\n<i>")[0]
            await ctx.bot.send_message(uid, t, parse_mode=HTML)
        except Exception:
            log.exception("Kunlik hisobot yuborilmadi")


async def monthly_job(ctx: ContextTypes.DEFAULT_TYPE):
    now = dt.datetime.now(TZ)
    if now.day != 1:
        return
    prev = (now.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    for uid in all_users():
        try:
            t, _ = month_view(uid, prev)
            if "xarajat yo'q" not in t:
                await ctx.bot.send_message(uid, "📬 <b>Oylik hisobot tayyor!</b>\n\n" + t, parse_mode=HTML)
        except Exception:
            log.exception("Oylik hisobot yuborilmadi")


# ---------- Render uchun: web server + o'zini uyg'oq tutish ----------
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


def run_http():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), Ping).serve_forever()


async def self_ping(ctx: ContextTypes.DEFAULT_TYPE):
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    try:
        await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=15).read())
    except Exception:
        log.warning("self-ping muvaffaqiyatsiz")


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("bugun", "Bugungi xarajatlar"),
        BotCommand("oy", "Shu oy hisoboti"),
        BotCommand("arxiv", "Oylar arxivi"),
        BotCommand("ochir", "Oxirgisini o'chirish"),
        BotCommand("yordam", "Yordam"),
    ])


def main():
    init_db()
    threading.Thread(target=run_http, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("yordam", cmd_help))
    app.add_handler(CommandHandler("bugun", cmd_today))
    app.add_handler(CommandHandler("oy", cmd_month))
    app.add_handler(CommandHandler("arxiv", cmd_archive))
    app.add_handler(CommandHandler("ochir", cmd_undo))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.VOICE | (filters.TEXT & ~filters.COMMAND), handle))
    jq = app.job_queue
    jq.run_daily(daily_job, time=dt.time(22, 0, tzinfo=TZ))
    jq.run_daily(monthly_job, time=dt.time(9, 0, tzinfo=TZ))
    jq.run_repeating(self_ping, interval=600, first=60)
    app.run_polling()


if __name__ == "__main__":
    main()
