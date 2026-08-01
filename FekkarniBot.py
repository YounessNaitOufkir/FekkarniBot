import os
import json
import uuid
import asyncio
import random
import logging
import tempfile
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.request
import urllib.parse

import pytz
import sqlite3
import csv
import io
from dotenv import load_dotenv
from dateutil.relativedelta import relativedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)

import google.generativeai as genai

# ── CONFIGURATION ──────────────────────────────────────────────
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_PATH = os.getenv("DB_PATH", "fekkarni.db")
DEFAULT_TIMEZONE = pytz.timezone(os.getenv("DEFAULT_TIMEZONE", "Africa/Casablanca"))
MODEL_NAME = "models/gemini-3.1-flash-lite"
MAX_MESSAGE_LENGTH = 1000

# ── LOGGING ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fekkarni")

# ── SERVICES ───────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
# A vanilla instance for simple text-extraction tasks (like custom delays)
ai_model_simple = genai.GenerativeModel(MODEL_NAME)


# ── SECURITY SANITIZER ────────────────────────────────────────
def sanitize_error(error_msg: Exception) -> str:
    safe = str(error_msg)
    if GEMINI_API_KEY and GEMINI_API_KEY in safe:
        safe = safe.replace(GEMINI_API_KEY, "[REDACTED_KEY]")
    if TELEGRAM_TOKEN and TELEGRAM_TOKEN in safe:
        safe = safe.replace(TELEGRAM_TOKEN, "[REDACTED_TOKEN]")
    return safe


# ── TASK-ID GENERATOR ─────────────────────────────────────────
def generate_task_id() -> str:
    return uuid.uuid4().hex[:10]


# ── DUMMY KEEP-ALIVE SERVER ───────────────────────────────────
def run_dummy_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Fekkarni Bot is running.")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, fmt, *args):
            pass  # suppress noisy HTTP logs

    port = int(os.environ.get("PORT", 10000))
    logger.info("Keep-alive server on port %s", port)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()


# ── SQLITE LOCAL DATABASE LAYER (OPTION 2) ─────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT PRIMARY KEY,
                first_name TEXT,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                chat_id TEXT,
                task_name TEXT,
                target_date TEXT,
                target_time TEXT,
                duration TEXT,
                status TEXT,
                recurrence TEXT,
                created_at TEXT,
                last_reminded_at TEXT DEFAULT '',
                priority TEXT DEFAULT 'Normal'
            )
        """)
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN last_reminded_at TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN priority TEXT DEFAULT 'Normal'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN username TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    logger.info("SQLite database initialized at %s", DB_PATH)


def _ensure_user_sync(chat_id: str, first_name: str, username: str = ""):
    with sqlite3.connect(DB_PATH) as conn:
        now_str = datetime.now(DEFAULT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, first_name, created_at, username) VALUES (?, ?, ?, ?)",
            (str(chat_id), str(first_name), now_str, str(username)),
        )
        conn.execute(
            "UPDATE users SET first_name = ?, username = ? WHERE chat_id = ?",
            (str(first_name), str(username), str(chat_id)),
        )
        conn.commit()


async def ensure_user(chat_id: str, first_name: str, username: str = ""):
    await asyncio.to_thread(_ensure_user_sync, str(chat_id), str(first_name), str(username))


def _add_task_sync(chat_id, first_name, task_id, task_name, target_date, target_time, duration, status, recurrence, priority, username):
    with sqlite3.connect(DB_PATH) as conn:
        now_str = datetime.now(DEFAULT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, first_name, created_at, username) VALUES (?, ?, ?, ?)",
            (str(chat_id), str(first_name), now_str, str(username)),
        )
        conn.execute(
            """
            INSERT INTO tasks (task_id, chat_id, task_name, target_date, target_time, duration, status, recurrence, priority, created_at, last_reminded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (
                str(task_id),
                str(chat_id),
                str(task_name),
                str(target_date),
                str(target_time),
                str(duration),
                str(status),
                str(recurrence),
                str(priority),
                now_str,
            ),
        )
        conn.commit()


async def add_task(chat_id, first_name, task_id, task_name, target_date, target_time, duration, status="Active", recurrence="None", priority="Normal", username=""):
    await asyncio.to_thread(
        _add_task_sync, chat_id, first_name, task_id, task_name, target_date, target_time, duration, status, recurrence, priority, username
    )


def _get_active_tasks_sync(chat_id, today_str=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if today_str:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE chat_id = ? AND status = 'Active' AND target_date = ? ORDER BY target_date, target_time",
                (str(chat_id), str(today_str)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE chat_id = ? AND status = 'Active' ORDER BY target_date, target_time",
                (str(chat_id),),
            ).fetchall()
        return [dict(r) for r in rows]


async def get_active_tasks(chat_id, today_only=False):
    today_str = datetime.now(DEFAULT_TIMEZONE).strftime("%Y-%m-%d") if today_only else None
    return await asyncio.to_thread(_get_active_tasks_sync, str(chat_id), today_str)


def _get_all_tasks_sync(chat_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT task_id, task_name, target_date, target_time, duration, status, recurrence, created_at FROM tasks WHERE chat_id = ? ORDER BY target_date, target_time",
            (str(chat_id),),
        ).fetchall()
        return [dict(r) for r in rows]


async def get_all_tasks_for_user(chat_id):
    return await asyncio.to_thread(_get_all_tasks_sync, str(chat_id))


def _get_task_sync(task_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (str(task_id),)).fetchone()
        return dict(row) if row else None


async def get_task_by_id(task_id):
    return await asyncio.to_thread(_get_task_sync, str(task_id))


def _update_status_sync(task_id, new_status):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (str(new_status), str(task_id)))
        conn.commit()


async def update_task_status(task_id, new_status):
    await asyncio.to_thread(_update_status_sync, str(task_id), str(new_status))


def _update_schedule_sync(task_id, new_date, new_time, new_status):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE tasks SET target_date = ?, target_time = ?, status = ? WHERE task_id = ?",
            (str(new_date), str(new_time), str(new_status), str(task_id)),
        )
        conn.commit()


async def update_task_schedule(task_id, new_date, new_time, new_status="Active"):
    await asyncio.to_thread(_update_schedule_sync, str(task_id), str(new_date), str(new_time), str(new_status))


def _complete_or_cancel_sync(chat_id, task_name, intent):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        today_str = datetime.now(DEFAULT_TIMEZONE).strftime("%Y-%m-%d")
        new_st = "Completed" if intent == "complete" else "Cancelled"
        target_upper = task_name.strip().upper()

        if target_upper in ("ALL_TASKS", "TODAYS_TASKS"):
            if target_upper == "ALL_TASKS":
                rows = conn.execute(
                    "SELECT task_id, task_name FROM tasks WHERE chat_id = ? AND status = 'Active'",
                    (str(chat_id),),
                ).fetchall()
                if rows:
                    conn.execute("UPDATE tasks SET status = ? WHERE chat_id = ? AND status = 'Active'", (new_st, str(chat_id)))
                    conn.commit()
                return len(rows), None
            else:
                rows = conn.execute(
                    "SELECT task_id, task_name FROM tasks WHERE chat_id = ? AND status = 'Active' AND target_date = ?",
                    (str(chat_id), today_str),
                ).fetchall()
                if rows:
                    conn.execute(
                        "UPDATE tasks SET status = ? WHERE chat_id = ? AND status = 'Active' AND target_date = ?",
                        (new_st, str(chat_id), today_str),
                    )
                    conn.commit()
                return len(rows), None

        rows = conn.execute(
            "SELECT task_id, task_name FROM tasks WHERE chat_id = ? AND status = 'Active'",
            (str(chat_id),),
        ).fetchall()
        clean = task_name.lower()
        for w in ("task", "reminder", "the", "my", "all"):
            clean = clean.replace(w, "")
        clean = clean.strip()
        words = [w for w in clean.split() if len(w) > 2]

        matched_id = None
        matched_name = None
        for r in rows:
            sn = (r["task_name"] or "").lower()
            if clean in sn or (words and all(w in sn for w in words)):
                matched_id = r["task_id"]
                matched_name = r["task_name"]
                break

        if matched_id:
            conn.execute("UPDATE tasks SET status = ? WHERE task_id = ?", (new_st, str(matched_id)))
            conn.commit()
            return 1, matched_name
        return 0, None


async def complete_or_cancel_tasks_by_name(chat_id, task_name, intent):
    return await asyncio.to_thread(_complete_or_cancel_sync, str(chat_id), str(task_name), str(intent))


def _get_due_reminders_sync(today_str, time_str):
    remind_key = f"{today_str} {time_str}"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT t.task_id, t.chat_id, t.task_name, t.duration, t.priority, u.username
            FROM tasks t
            LEFT JOIN users u ON t.chat_id = u.chat_id
            WHERE t.status = 'Active' 
              AND t.target_date = ? 
              AND t.target_time = ? 
              AND (t.last_reminded_at IS NULL OR t.last_reminded_at != ?)
            """,
            (str(today_str), str(time_str), str(remind_key)),
        ).fetchall()
        return [dict(r) for r in rows]


async def get_due_reminders(today_str, time_str):
    return await asyncio.to_thread(_get_due_reminders_sync, str(today_str), str(time_str))


def _mark_reminded_sync(task_id, remind_key):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE tasks SET last_reminded_at = ? WHERE task_id = ?", (str(remind_key), str(task_id)))
        conn.commit()


async def mark_task_reminded(task_id, remind_key):
    await asyncio.to_thread(_mark_reminded_sync, str(task_id), str(remind_key))


# ── SHARED PROMPT BUILDER ─────────────────────────────────────
def build_system_prompt(draft_context: str = "") -> str:
    now = datetime.now(DEFAULT_TIMEZONE)
    d = now.strftime("%Y-%m-%d")
    t = now.strftime("%H:%M")
    day = now.strftime("%A")
    tmrw = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    return f"""You are a structured data extraction engine for "Fekkarni", a personal task manager.

# CONTEXT
- Date: {d} ({day})  Time: {t}  Timezone: Africa/Casablanca
{draft_context}

# OUTPUT SCHEMA  (return a JSON ARRAY of objects)
{{"intent":"create|complete|cancel","task_name":"string","date":"YYYY-MM-DD|Unknown","time":"HH:MM|Unknown","duration":"string|Unknown","recurrence":"None|Daily|Weekly|Monthly","priority":"Normal|Urgent","needs_clarification":false}}

# RULES
1. "Today"={d}. "Tomorrow"={tmrw}.
2. "Next [weekday]" = next occurrence AFTER today.
3. "In X hours/minutes" = compute from {t} on {d}; cross midnight → advance date.
4. Time range → pick the START.
5. Date only (no time) → time="Unknown", needs_clarification=true.
6. Time only (no date) → assume today; if already past, assume tomorrow.
7. NEVER invent dates/times not stated. Use "Unknown".
8. Default intent is "create". Use "complete"/"cancel" only when user explicitly says done/finished/complete/cancel/remove/delete.
9. For complete/cancel: task_name = core noun phrase only (strip filler words).
10. Distinct tasks → multiple objects.
11. "ALL tasks"/"everything" → one object with task_name "ALL_TASKS".
12. "today's tasks" → one object with task_name "TODAYS_TASKS".
13. User may write English, Arabic (Darija/MSA), or French. Keep original language in task_name.
14. Do NOT invent tasks not mentioned.
15. When unsure → intent "create", needs_clarification true.
16. If the user mentions "urgent", "important", "ASAP", "darori", "crucial", or "critical", set priority to "Urgent". Otherwise default priority to "Normal".
"""

# ── AI PARSER (UNIFIED FOR TEXT AND VOICE) ────────────────────
async def parse_task_with_ai(contents, draft_context: str = "") -> list:
    """Accepts either a text string OR an audio payload list."""
    model = genai.GenerativeModel(
        MODEL_NAME,
        system_instruction=build_system_prompt(draft_context),
    )
    resp = await model.generate_content_async(
        contents=contents,
        generation_config={"response_mime_type": "application/json"},
    )
    return json.loads(resp.text)


# ── CORE PROCESSING ENGINE ────────────────────────────────────
async def process_parsed_tasks(task_list, update: Update, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(task_list, dict):
        task_list = [task_list]

    chat_id = str(update.effective_user.id)
    first_name = str(update.effective_user.first_name)
    await ensure_user(chat_id, first_name)

    msgs = []

    for td in task_list:
        intent = td.get("intent", "create")
        task_name = td.get("task_name", "Untitled Task")

        # ── COMPLETE / CANCEL ──────────────────────────────────
        if intent in ("complete", "cancel"):
            count, matched_name = await complete_or_cancel_tasks_by_name(chat_id, task_name, intent)
            new_st = "Completed" if intent == "complete" else "Cancelled"
            target_upper = task_name.strip().upper()
            if count > 0:
                if target_upper == "ALL_TASKS":
                    msgs.append(f"💥 **BOOM!** Marked all {count} active tasks as {new_st}.")
                elif target_upper == "TODAYS_TASKS":
                    msgs.append(f"🧹 Swept up! Marked {count} tasks for today as {new_st}.")
                else:
                    msgs.append(f"✅ Marked **{matched_name}** as {new_st}.")
            else:
                msgs.append(f"❌ Couldn't find any active tasks matching '{task_name}'.")
            continue

        # ── CREATE ─────────────────────────────────────────────
        task_id_str = generate_task_id()
        target_date = str(td.get("date", "Unknown"))
        target_time = str(td.get("time", "Unknown"))
        duration = td.get("duration", "Unknown")
        recurrence = td.get("recurrence", "None")
        priority = td.get("priority", "Normal")
        nc = td.get("needs_clarification", False)

        is_missing = (
            str(nc).lower() in ("true", "1", "yes")
            or target_date.lower() in ("unknown", "none", "null", "")
            or target_time.lower() in ("unknown", "none", "null", "")
        )

        if is_missing:
            context.user_data["draft_task_name"] = task_name
            msgs.append(f"📝 I noted: **{task_name}**\n\nBut you didn't specify when! What date and time would you like?")
            continue

        username = ""
        if update and update.effective_user and update.effective_user.username:
            username = f"@{update.effective_user.username}"
        await add_task(
            chat_id, first_name, task_id_str, task_name, target_date, target_time, duration, "Active", recurrence, priority, username
        )
        prio_badge = " 🔥 **[URGENT]**" if priority and str(priority).lower() == "urgent" else ""
        msgs.append(f"✅ **Saved:** {task_name} 📅 {target_date} 🕒 {target_time}{prio_badge}")

    if msgs:
        await update.message.reply_text("\n\n".join(msgs), parse_mode="Markdown")


# ── TEXT MESSAGE HANDLER ───────────────────────────────────────
async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = str(update.effective_user.id)
    first_name = str(update.effective_user.first_name)

    if len(user_text) > MAX_MESSAGE_LENGTH:
        await update.message.reply_text("⚠️ Message too long — please keep it under 2000 characters.")
        return

    await ensure_user(chat_id, first_name)

    # Custom Delay flow uses the simple, un-prompted model
    if "pending_delay_task_id" in context.user_data:
        task_id = context.user_data.pop("pending_delay_task_id")
        msg_id = context.user_data.pop("pending_delay_msg_id")
        prompt = f"Extract the numeric duration in total minutes from: '{user_text}'. Respond with ONLY an integer."
        resp = await ai_model_simple.generate_content_async(prompt)
        try:
            mins = int(resp.text.strip())
            new_t = datetime.now(DEFAULT_TIMEZONE) + timedelta(minutes=mins)
            await update_task_schedule(task_id, new_t.strftime("%Y-%m-%d"), new_t.strftime("%H:%M"), "Active")
            await update.message.reply_text(f"🔄 Custom delay set! Moved to {new_t.strftime('%H:%M')}.")
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except Exception as e:
            logger.error("Delay parse error [%s]: %s", chat_id, e, exc_info=True)
            await update.message.reply_text("Could not parse that. Try something like '45m' or '2 hours'.")
        return

    draft_context = ""
    if "draft_task_name" in context.user_data:
        name = context.user_data.pop("draft_task_name")
        draft_context = f"\nCRITICAL: User is clarifying time for task '{name}'. Keep this name; extract only date/time."

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        tasks = await parse_task_with_ai(user_text, draft_context)
        await process_parsed_tasks(tasks, update, context)
    except Exception as e:
        logger.error("Parse error [%s]: %s", chat_id, e, exc_info=True)
        await update.message.reply_text("❌ Sorry, I couldn't process that. Please try rephrasing.")


# ── VOICE HANDLER ──────────────────────────────────────────────
async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    first_name = str(update.effective_user.first_name)
    msg_id = update.message.message_id
    tmp = os.path.join(tempfile.gettempdir(), f"voice_{chat_id}_{msg_id}.ogg")

    await ensure_user(chat_id, first_name)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        fi = await context.bot.get_file(update.message.voice.file_id)
        await fi.download_to_drive(tmp)
        with open(tmp, "rb") as f:
            audio_bytes = f.read()

        audio_part = {"mime_type": "audio/ogg", "data": audio_bytes}

        draft_context = ""
        if "draft_task_name" in context.user_data:
            name = context.user_data.pop("draft_task_name")
            draft_context = f"\nCRITICAL: User is clarifying time for task '{name}'. Keep this name; extract only date/time from audio."

        # Pass a list (audio payload + prompt context) to our unified AI Parser!
        contents = [audio_part, "Extract task data from this voice message."]
        tasks = await parse_task_with_ai(contents, draft_context)
        await process_parsed_tasks(tasks, update, context)

    except Exception as e:
        logger.error("Voice error [%s]: %s", chat_id, e, exc_info=True)
        await update.message.reply_text("❌ Couldn't understand that voice note. Please try again or type your task.")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


async def send_callmebot_call(username: str, task_name: str):
    if not username:
        logger.warning("No Telegram username available for CallMeBot call.")
        return
    un = username.strip().lstrip("@")
    url = (
        f"https://api.callmebot.com/start.php?user=%40{urllib.parse.quote(un)}"
        f"&text={urllib.parse.quote('Urgent reminder from Fekkarni: ' + task_name)}"
        f"&lang=en-US-Standard-C&rpt=2"
    )
    try:
        def _fetch():
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.read()
        await asyncio.to_thread(_fetch)
        logger.info("CallMeBot voice call initiated for @%s: %s", un, task_name)
    except Exception as e:
        logger.error("CallMeBot voice call failed for @%s: %s", un, e)


# ── SCHEDULER ──────────────────────────────────────────────────
async def check_and_send_reminders(context: ContextTypes.DEFAULT_TYPE):
    try:
        snapshot_now = datetime.now(DEFAULT_TIMEZONE)
        today_s = snapshot_now.strftime("%Y-%m-%d")
        time_s = snapshot_now.strftime("%H:%M")
        remind_key = f"{today_s} {time_s}"

        due_tasks = await get_due_reminders(today_s, time_s)
        for row in due_tasks:
            tid = row["task_id"]
            cid = row["chat_id"]
            tn = row["task_name"]
            dur = row.get("duration", "Unknown")
            priority = row.get("priority", "Normal")
            username = row.get("username", "")

            kb = [
                [InlineKeyboardButton("✅ Complete", callback_data=f"done_{tid}"),
                 InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{tid}")],
                [InlineKeyboardButton("⏳ Snooze 30m", callback_data=f"snooze_{tid}"),
                 InlineKeyboardButton("🔄 Custom Delay", callback_data=f"delay_{tid}")],
            ]

            if priority and str(priority).lower() == "urgent":
                txt = (
                    f"🚨 **URGENT REMINDER ALERT** 🚨\n\n"
                    f"📌 **Task:** {tn}\n"
                    f"⏳ **Duration:** {dur}\n"
                    f"🔥 **Priority:** URGENT\n\n"
                    f"📞 *Initiating Telegram Voice Call via CallMeBot...*\n"
                    f"*(Make sure you have authorized @CallMeBot_txtbot in Telegram to receive calls)*\n\n"
                    f"What would you like to do?"
                )
                if username:
                    asyncio.create_task(send_callmebot_call(username, tn))
            else:
                txt = (
                    f"⏰ **REMINDER ALERT** ⏰\n\n"
                    f"📌 **Task:** {tn}\n"
                    f"⏳ **Duration:** {dur}\n\n"
                    f"What would you like to do?"
                )
            try:
                await context.bot.send_message(
                    chat_id=int(cid), text=txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
                )
                await mark_task_reminded(tid, remind_key)
                logger.info("Fired reminder for user %s: %s (marked %s)", cid, tn, remind_key)
            except Exception as e:
                logger.error("Error sending reminder to %s: %s", cid, e)
    except Exception as e:
        logger.error("Scheduler error: %s", e, exc_info=True)


# ── BUTTON HANDLER ─────────────────────────────────────────────
async def handle_button_clicks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, task_id = query.data.split("_", 1)

    chat_id = str(update.effective_user.id)
    first_name = str(update.effective_user.first_name)
    await ensure_user(chat_id, first_name)

    task = await get_task_by_id(task_id)
    if not task:
        await query.edit_message_text("❌ Sync error — task no longer exists in dashboard.")
        return

    if action == "done":
        encouragement = random.choice([
            "✅ Marked as done. Keep it up!",
            "✅ Excellent work! Task completed.",
            "✅ You're on a roll! Dashboard updated.",
            "✅ Boom! Another one off the list.",
        ])
        recurrence = task.get("recurrence")

        if recurrence and recurrence != "None":
            cur_date_str = str(task.get("target_date", ""))
            try:
                cur_date = datetime.strptime(cur_date_str, "%Y-%m-%d")
            except Exception:
                cur_date = datetime.now(DEFAULT_TIMEZONE)

            if recurrence == "Daily":
                nxt = cur_date + timedelta(days=1)
            elif recurrence == "Weekly":
                nxt = cur_date + timedelta(weeks=1)
            elif recurrence == "Monthly":
                nxt = cur_date + relativedelta(months=1)
            else:
                nxt = cur_date + timedelta(days=1)

            await update_task_schedule(task_id, nxt.strftime("%Y-%m-%d"), str(task.get("target_time", "00:00")), "Active")
            await query.edit_message_text(f"{encouragement} Rescheduled for {nxt.strftime('%Y-%m-%d')}.")
        else:
            await update_task_status(task_id, "Completed")
            await query.edit_message_text(encouragement)

    elif action == "cancel":
        await update_task_status(task_id, "Cancelled")
        await query.edit_message_text("❌ Task has been cancelled.")

    elif action == "snooze":
        new_t = datetime.now(DEFAULT_TIMEZONE) + timedelta(minutes=30)
        await update_task_schedule(task_id, new_t.strftime("%Y-%m-%d"), new_t.strftime("%H:%M"), "Active")
        await query.edit_message_text(f"⏳ Snoozed 30 min. New target: {new_t.strftime('%H:%M')}")

    elif action == "delay":
        context.user_data["pending_delay_task_id"] = task_id
        context.user_data["pending_delay_msg_id"] = query.message.message_id
        await query.message.reply_text("How long to delay? (e.g. '45m' or '2 hours')")


# ── AGENDA / TODAY ─────────────────────────────────────────────
async def handle_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    command = update.message.text.split()[0].lower()
    chat_id = str(update.effective_user.id)
    first_name = str(update.effective_user.first_name)
    await ensure_user(chat_id, first_name)

    try:
        today_str = datetime.now(DEFAULT_TIMEZONE).strftime("%Y-%m-%d")

        if "today" in command:
            active = await get_active_tasks(chat_id, today_only=True)
            title = f"📅 **Your Tasks for Today ({today_str})**"
        else:
            active = await get_active_tasks(chat_id, today_only=False)
            title = "📋 **Your Full Agenda (All Active Tasks)**"

        if not active:
            await update.message.reply_text("🎉 No active tasks! Enjoy your time.")
            return

        txt = f"{title}\n\n"
        for t in active:
            d = t.get("target_date", "No Date")
            tm = t.get("target_time", "No Time")
            n = t.get("task_name", "Untitled")
            prefix = f"[{d}] " if "Full Agenda" in title else ""
            txt += f"• {prefix}**{tm}** - {n}\n"

        await update.message.reply_text(txt, parse_mode="Markdown")
    except Exception as e:
        logger.error("Agenda error [%s]: %s", chat_id, e, exc_info=True)
        await update.message.reply_text("❌ Sorry, couldn't fetch your tasks.")


# ── START COMMAND ──────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 **Welcome to Fekkarni, {name}!** Your personal AI memory assistant.\n\n"
        "I make sure you never forget a task, appointment, or idea.\n\n"
        "**How to use me:**\n"
        "Just talk to me naturally — type or send a voice note.\n"
        "• _'Remind me to call the client tomorrow at 10 AM'_\n"
        "• _'Every Friday at 4 PM remind me to check the budget'_\n\n"
        "**Commands:** /today · /agenda · /export · /help\n\n"
        "Send me your first task right now!",
        parse_mode="Markdown",
    )


# ── HELP COMMAND ───────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Fekkarni Commands**\n\n"
        "• /start — Welcome message\n"
        "• /today — Show today's tasks\n"
        "• /agenda — Show all active tasks\n"
        "• /export — Download your full task history as a CSV/Excel file\n"
        "• /help — This help message\n\n"
        "💡 **Tips:**\n"
        "• Type naturally or send a voice note to create tasks\n"
        "• Say _'complete [task name]'_ to mark tasks done\n"
        "• Say _'cancel all tasks'_ for bulk actions\n"
        "• 🌐 Supports English, العربية, and Français",
        parse_mode="Markdown",
    )


# ── EXPORT COMMAND ─────────────────────────────────────────────
async def handle_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    first_name = str(update.effective_user.first_name)
    await ensure_user(chat_id, first_name)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_document")

    try:
        tasks = await get_all_tasks_for_user(chat_id)
        if not tasks:
            await update.message.reply_text("❌ No tasks found in your database to export. Try creating a task first!")
            return

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Task ID", "Task Name", "Date", "Time", "Duration", "Status", "Recurrence", "Priority", "Created At"])
        for t in tasks:
            writer.writerow([
                t["task_id"],
                t["task_name"],
                t["target_date"],
                t["target_time"],
                t["duration"],
                t["status"],
                t["recurrence"],
                t.get("priority", "Normal"),
                t["created_at"],
            ])

        output.seek(0)
        file_bytes = io.BytesIO(output.getvalue().encode("utf-8-sig"))
        file_bytes.name = f"fekkarni_tasks_{chat_id}.csv"

        await update.message.reply_document(
            document=file_bytes,
            caption="📊 **Your Fekkarni Tasks Export**\n\nHere is your full task history in CSV format. You can open it in Microsoft Excel, Google Sheets, or Apple Numbers!",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Export error [%s]: %s", chat_id, e, exc_info=True)
        await update.message.reply_text("❌ Sorry, couldn't export your tasks right now.")


# ── ENGINE RUNNER ──────────────────────────────────────────────
def main():
    init_db()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    if app.job_queue:
        app.job_queue.run_repeating(check_and_send_reminders, interval=30, first=5)
        logger.info("JobQueue active — 30-second interval with duplicate protection.")
    else:
        logger.warning("JobQueue initialization delayed.")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))  
    app.add_handler(CommandHandler("agenda", handle_agenda))
    app.add_handler(CommandHandler("today", handle_agenda))
    app.add_handler(CommandHandler("export", handle_export))
    app.add_handler(CommandHandler("mysheet", handle_export))
    app.add_handler(CommandHandler("sheet", handle_export))
    app.add_handler(CallbackQueryHandler(handle_button_clicks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    logger.info("Bot fully initialized — starting polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
