"""
ЧМ-2026 | Бот для ставок на 1/8 финала
=======================================
Установка:
  pip install python-telegram-bot==20.7

Запуск:
  1. Создайте бота через @BotFather в Telegram → получите TOKEN
  2. Узнайте свой Telegram user_id (например через @userinfobot) → вставьте в ADMIN_IDS
  3. python wc2026_bot.py

Команды пользователя:
  /start      — приветствие
  /predict    — внести прогноз (пошаговый диалог)
  /mypred     — посмотреть свои прогнозы
  /table      — турнирная таблица

Команды админа:
  /setresult  — ввести результат матча
  /results    — посмотреть все введённые результаты
"""

import logging
import sqlite3
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters
)

# ─────────────────────────────────────────────
# НАСТРОЙКИ — измените здесь
# ─────────────────────────────────────────────
import os
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "0").split(",")]  # задаётся через переменные окружения
DB_FILE   = "wc2026.db"
# ─────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

MATCHES = [
    {"id": 1, "date": "05.07", "home": "Парагвай",               "away": "Франция"},
    {"id": 2, "date": "05.07", "home": "Канада",                  "away": "Марокко"},
    {"id": 3, "date": "06.07", "home": "Норвегия",                "away": "Бразилия"},
    {"id": 4, "date": "06.07", "home": "Мексика",                 "away": "Англия"},
    {"id": 5, "date": "07.07", "home": "Бельгия",                 "away": "США"},
    {"id": 6, "date": "07.07", "home": "Испания/Австрия*",        "away": "Португалия/Хорватия*"},
    {"id": 7, "date": "08.07", "home": "Аргентина/Кабо-Верде*",  "away": "Австралия/Египет*"},
    {"id": 8, "date": "08.07", "home": "Швейцария/Алжир*",       "away": "Колумбия/Гана*"},
]

# ConversationHandler states
SELECT_MATCH, ENTER_SCORE, CONFIRM = range(3)
ADMIN_SELECT, ADMIN_SCORE = range(10, 12)


# ══════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                user_id   INTEGER,
                username  TEXT,
                match_id  INTEGER,
                home_goal INTEGER,
                away_goal INTEGER,
                PRIMARY KEY (user_id, match_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS results (
                match_id  INTEGER PRIMARY KEY,
                home_goal INTEGER,
                away_goal INTEGER
            )
        """)

def save_prediction(user_id, username, match_id, h, a):
    with db() as c:
        c.execute("""
            INSERT INTO predictions (user_id, username, match_id, home_goal, away_goal)
            VALUES (?,?,?,?,?)
            ON CONFLICT(user_id, match_id) DO UPDATE SET
                home_goal=excluded.home_goal,
                away_goal=excluded.away_goal,
                username=excluded.username
        """, (user_id, username, match_id, h, a))

def get_predictions(user_id):
    with db() as c:
        return c.execute(
            "SELECT match_id, home_goal, away_goal FROM predictions WHERE user_id=? ORDER BY match_id",
            (user_id,)
        ).fetchall()

def save_result(match_id, h, a):
    with db() as c:
        c.execute("""
            INSERT INTO results (match_id, home_goal, away_goal) VALUES (?,?,?)
            ON CONFLICT(match_id) DO UPDATE SET home_goal=excluded.home_goal, away_goal=excluded.away_goal
        """, (match_id, h, a))

def get_results():
    with db() as c:
        return {r["match_id"]: r for r in c.execute("SELECT * FROM results").fetchall()}

def get_all_predictions():
    with db() as c:
        return c.execute(
            "SELECT user_id, username, match_id, home_goal, away_goal FROM predictions ORDER BY user_id, match_id"
        ).fetchall()


# ══════════════════════════════════════════════
# SCORING
# ══════════════════════════════════════════════
def calc_points(ph, pa, ah, aa):
    """3 — точный счёт | 2 — разница | 1 — победитель | 0 — промах"""
    if ph == ah and pa == aa:
        return 3
    if (ph - pa) == (ah - aa):
        return 2
    def winner(h, a):
        return 1 if h > a else (-1 if a > h else 0)
    if winner(ph, pa) == winner(ah, aa):
        return 1
    return 0

def leaderboard():
    results = get_results()
    all_preds = get_all_predictions()

    users = {}
    for row in all_preds:
        uid = row["user_id"]
        if uid not in users:
            users[uid] = {"username": row["username"], "total": 0, "detail": {}}
        mid = row["match_id"]
        if mid in results:
            r = results[mid]
            pts = calc_points(row["home_goal"], row["away_goal"], r["home_goal"], r["away_goal"])
            users[uid]["total"] += pts
            users[uid]["detail"][mid] = pts
        else:
            users[uid]["detail"][mid] = None   # ещё не сыграно

    return sorted(users.values(), key=lambda x: x["total"], reverse=True)


# ══════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════
def match_line(m):
    return f"М{m['id']} | {m['date']} | {m['home']} — {m['away']}"

def is_admin(user_id):
    return user_id in ADMIN_IDS

def pts_emoji(p):
    return {3: "🟢", 2: "🔵", 1: "🟡", 0: "🔴"}.get(p, "⚪")


# ══════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    admin_hint = "\n🔑 <b>Вы — администратор</b>: /setresult /results" if is_admin(update.effective_user.id) else ""
    await update.message.reply_html(
        f"🏆 <b>ЧМ-2026 | Прогнозы 1/8 финала</b>\n\n"
        f"Привет, {name}! Делай ставки на счёт каждого матча.\n\n"
        f"<b>Баллы:</b>\n"
        f"🟢 3 — точный счёт\n"
        f"🔵 2 — верная разница голов\n"
        f"🟡 1 — верный победитель\n"
        f"🔴 0 — промах\n\n"
        f"<b>Команды:</b>\n"
        f"/predict — внести прогноз\n"
        f"/mypred  — мои прогнозы\n"
        f"/table   — турнирная таблица"
        f"{admin_hint}"
    )


# ══════════════════════════════════════════════
# /predict — пошаговый диалог
# ══════════════════════════════════════════════
async def cmd_predict(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["pred_idx"] = 0
    ctx.user_data["pending"] = {}
    return await ask_match(update, ctx)

async def ask_match(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    idx = ctx.user_data.get("pred_idx", 0)
    if idx >= len(MATCHES):
        return await finish_prediction(update, ctx)
    m = MATCHES[idx]
    msg = (
        f"⚽ <b>Матч {m['id']}/8</b> — {m['date']}\n"
        f"<b>{m['home']} — {m['away']}</b>\n\n"
        f"Введите счёт через двоеточие, например <code>2:1</code>\n"
        f"или /skip чтобы пропустить этот матч"
    )
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_match")]]
    if isinstance(update, Update) and update.message:
        await update.message.reply_html(msg, reply_markup=InlineKeyboardMarkup(keyboard))
    elif isinstance(update, Update) and update.callback_query:
        await update.callback_query.message.reply_html(msg, reply_markup=InlineKeyboardMarkup(keyboard))
    return ENTER_SCORE

async def handle_score_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    idx  = ctx.user_data.get("pred_idx", 0)
    m    = MATCHES[idx]

    if ":" not in text:
        await update.message.reply_text("❌ Формат: 2:1 (цифра:цифра)")
        return ENTER_SCORE

    parts = text.split(":")
    try:
        h, a = int(parts[0].strip()), int(parts[1].strip())
        if h < 0 or a < 0 or h > 20 or a > 20:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Введите корректный счёт, например 1:0")
        return ENTER_SCORE

    ctx.user_data["pending"][m["id"]] = (h, a)
    await update.message.reply_html(
        f"✅ <b>М{m['id']}</b> {m['home']} <b>{h}:{a}</b> {m['away']}"
    )

    ctx.user_data["pred_idx"] = idx + 1
    return await ask_match(update, ctx)

async def skip_match(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["pred_idx"] = ctx.user_data.get("pred_idx", 0) + 1
    return await ask_match(update, ctx)

async def finish_prediction(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pending = ctx.user_data.get("pending", {})
    if not pending:
        msg_text = "😕 Вы не ввели ни одного прогноза."
        if update.message:
            await update.message.reply_text(msg_text)
        return ConversationHandler.END

    user     = update.effective_user
    username = user.username or user.first_name

    for mid, (h, a) in pending.items():
        save_prediction(user.id, username, mid, h, a)

    lines = []
    for m in MATCHES:
        if m["id"] in pending:
            h, a = pending[m["id"]]
            lines.append(f"М{m['id']}: {m['home']} <b>{h}:{a}</b> {m['away']}")

    reply = f"🎉 <b>Прогноз сохранён!</b> ({len(pending)}/8 матчей)\n\n" + "\n".join(lines)
    target = update.message or update.callback_query.message
    await target.reply_html(reply)
    return ConversationHandler.END

async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["pred_idx"] = ctx.user_data.get("pred_idx", 0) + 1
    return await ask_match(update, ctx)

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Ввод прогноза отменён. /predict — начать снова.")
    return ConversationHandler.END


# ══════════════════════════════════════════════
# /mypred
# ══════════════════════════════════════════════
async def cmd_mypred(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    preds = {r["match_id"]: r for r in get_predictions(uid)}
    results = get_results()

    if not preds:
        await update.message.reply_text("У вас пока нет прогнозов. /predict — ввести.")
        return

    lines = ["<b>Ваши прогнозы:</b>\n"]
    total = 0
    for m in MATCHES:
        mid = m["id"]
        if mid in preds:
            p   = preds[mid]
            ph, pa = p["home_goal"], p["away_goal"]
            if mid in results:
                r  = results[mid]
                pt = calc_points(ph, pa, r["home_goal"], r["away_goal"])
                total += pt
                lines.append(f"{pts_emoji(pt)} М{mid}: {m['home']} <b>{ph}:{pa}</b> {m['away']}  [{pt} оч.]")
            else:
                lines.append(f"⚪ М{mid}: {m['home']} <b>{ph}:{pa}</b> {m['away']}")
        else:
            lines.append(f"➖ М{mid}: {m['home']} — {m['away']}  (нет прогноза)")

    if results:
        lines.append(f"\n🏅 <b>Итого: {total} очков</b>")

    await update.message.reply_html("\n".join(lines))


# ══════════════════════════════════════════════
# /table — турнирная таблица
# ══════════════════════════════════════════════
async def cmd_table(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lb = leaderboard()
    results = get_results()

    if not lb:
        await update.message.reply_text("Пока никто не сделал прогнозы. /predict — первым!")
        return

    medals  = ["🥇", "🥈", "🥉"]
    lines   = ["<b>🏆 Турнирная таблица</b>\n"]

    for i, entry in enumerate(lb):
        medal = medals[i] if i < 3 else f"{i+1}."
        name  = entry["username"]
        pts   = entry["total"] if results else "—"
        # mini breakdown
        breakdown = ""
        if results:
            pts_list = [pts_emoji(v) for v in entry["detail"].values() if v is not None]
            breakdown = " " + "".join(pts_list)
        lines.append(f"{medal} <b>{name}</b>{breakdown}  — <b>{pts} оч.</b>")

    if not results:
        lines.append("\n<i>Очки появятся после ввода результатов администратором.</i>")

    await update.message.reply_html("\n".join(lines))


# ══════════════════════════════════════════════
# ADMIN: /setresult
# ══════════════════════════════════════════════
async def cmd_setresult(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только для администратора.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(f"М{m['id']} | {m['date']} | {m['home']} — {m['away']}", callback_data=f"ar_{m['id']}")]
        for m in MATCHES
    ]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="ar_cancel")])
    await update.message.reply_text(
        "Выберите матч для ввода результата:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_SELECT

async def admin_select_match(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    data = update.callback_query.data
    if data == "ar_cancel":
        await update.callback_query.message.edit_text("Отменено.")
        return ConversationHandler.END

    mid = int(data.split("_")[1])
    ctx.user_data["admin_match"] = mid
    m = next(x for x in MATCHES if x["id"] == mid)
    await update.callback_query.message.edit_text(
        f"⚽ <b>{m['home']} — {m['away']}</b>\n\nВведите итоговый счёт (например <code>2:0</code>):",
        parse_mode="HTML"
    )
    return ADMIN_SCORE

async def admin_enter_score(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mid  = ctx.user_data.get("admin_match")
    m    = next(x for x in MATCHES if x["id"] == mid)

    if ":" not in text:
        await update.message.reply_text("❌ Формат: 2:1")
        return ADMIN_SCORE
    try:
        h, a = int(text.split(":")[0]), int(text.split(":")[1])
    except:
        await update.message.reply_text("❌ Введите корректный счёт")
        return ADMIN_SCORE

    save_result(mid, h, a)

    # пересчитать и показать итог
    lb = leaderboard()
    lines = [f"✅ Результат М{mid}: <b>{m['home']} {h}:{a} {m['away']}</b> сохранён!\n\n<b>Текущая таблица:</b>\n"]
    medals = ["🥇","🥈","🥉"]
    for i, e in enumerate(lb):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} {e['username']} — {e['total']} оч.")

    await update.message.reply_html("\n".join(lines))
    return ConversationHandler.END

async def cmd_results(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Только для администратора.")
        return
    results = get_results()
    if not results:
        await update.message.reply_text("Результаты ещё не введены. /setresult")
        return
    lines = ["<b>Введённые результаты:</b>\n"]
    for m in MATCHES:
        if m["id"] in results:
            r = results[m["id"]]
            lines.append(f"✅ М{m['id']}: {m['home']} <b>{r['home_goal']}:{r['away_goal']}</b> {m['away']}")
        else:
            lines.append(f"⏳ М{m['id']}: {m['home']} — {m['away']} (не введён)")
    await update.message.reply_html("\n".join(lines))


# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    predict_conv = ConversationHandler(
        entry_points=[CommandHandler("predict", cmd_predict)],
        states={
            ENTER_SCORE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_score_input),
                CommandHandler("skip", cmd_skip),
                CallbackQueryHandler(skip_match, pattern="^skip_match$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("setresult", cmd_setresult)],
        states={
            ADMIN_SELECT: [CallbackQueryHandler(admin_select_match, pattern="^ar_")],
            ADMIN_SCORE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_enter_score)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("mypred",  cmd_mypred))
    app.add_handler(CommandHandler("table",   cmd_table))
    app.add_handler(CommandHandler("results", cmd_results))
    app.add_handler(predict_conv)
    app.add_handler(admin_conv)

    log.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
