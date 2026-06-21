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

import pytz
import gspread
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
SHEET_NAME = os.getenv("SHEET_NAME")
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

gc = gspread.service_account(filename="credentials.json")
logger.info("Connected to Google Sheets with auto-refreshing credentials.")


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


# ── MULTI-TENANT TAB HELPER ───────────────────────────────────
async def get_user_sheet(chat_id: str, first_name: str):
    doc = await asyncio.to_thread(gc.open, SHEET_NAME)
    worksheets = await asyncio.to_thread(doc.worksheets)

    for ws in worksheets:
        if ws.title.endswith(f"_{chat_id}"):
            return ws

    safe = "".join(c for c in str(first_name) if c.isalnum()).strip() or "User"
    title = f"{safe}_{chat_id}"
    sheet = await asyncio.to_thread(doc.add_worksheet, title=title, rows=1000, cols=10)
    headers = ["Task ID", "Task Name", "Date", "Time", "Duration", "Status", "Recurrence"]
    await asyncio.to_thread(sheet.append_row, headers)
    logger.info("Created tab: %s", title)
    return sheet


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
{{"intent":"create|complete|cancel","task_name":"string","date":"YYYY-MM-DD|Unknown","time":"HH:MM|Unknown","duration":"string|Unknown","recurrence":"None|Daily|Weekly|Monthly","needs_clarification":false}}

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
async def process_parsed_tasks(task_list, update: Update, context: ContextTypes.DEFAULT_TYPE, user_sheet):
    if isinstance(task_list, dict):
        task_list = [task_list]

    msgs = []

    for td in task_list:
        intent = td.get("intent", "create")
        task_name = td.get("task_name", "Untitled Task")

        # ── COMPLETE / CANCEL ──────────────────────────────────
        if intent in ("complete", "cancel"):
            all_vals = await asyncio.to_thread(user_sheet.get_all_values)
            if len(all_vals) <= 1:
                msgs.append("❌ No tasks found in your dashboard.")
                continue

            hdr = all_vals[0]
            ni = hdr.index("Task Name") if "Task Name" in hdr else 1
            di = hdr.index("Date") if "Date" in hdr else 2
            si = hdr.index("Status") if "Status" in hdr else 5

            target_upper = task_name.strip().upper()
            found, names = [], []
            today_str = datetime.now(DEFAULT_TIMEZONE).strftime("%Y-%m-%d")

            if target_upper in ("ALL_TASKS", "TODAYS_TASKS"):
                for i in range(1, len(all_vals)):
                    r = all_vals[i]
                    st = r[si].strip() if si < len(r) else ""
                    dt = r[di].strip() if di < len(r) else ""
                    if st == "Active":
                        if target_upper == "ALL_TASKS" or dt == today_str:
                            found.append(i + 1)
            else:
                clean = task_name.lower()
                for w in ("task", "reminder", "the", "my", "all"):
                    clean = clean.replace(w, "")
                clean = clean.strip()
                words = [w for w in clean.split() if len(w) > 2]

                for i in range(1, len(all_vals)):
                    r = all_vals[i]
                    st = r[si].strip() if si < len(r) else ""
                    sn = (r[ni] if ni < len(r) else "").lower()
                    if st == "Active" and (clean in sn or (words and all(w in sn for w in words))):
                        found.append(i + 1)
                        names.append(r[ni])
                        break

            if found:
                new_st = "Completed" if intent == "complete" else "Cancelled"
                cells = [gspread.Cell(row, si + 1, new_st) for row in found]
                await asyncio.to_thread(user_sheet.update_cells, cells)

                if target_upper == "ALL_TASKS":
                    msgs.append(f"💥 **BOOM!** Marked all {len(found)} active tasks as {new_st}.")
                elif target_upper == "TODAYS_TASKS":
                    msgs.append(f"🧹 Swept up! Marked {len(found)} tasks for today as {new_st}.")
                else:
                    msgs.append(f"✅ Marked **{names[0]}** as {new_st}.")
            else:
                msgs.append(f"❌ Couldn't find any active tasks matching '{task_name}'.")
            continue

        # ── CREATE ─────────────────────────────────────────────
        task_id_str = generate_task_id() 
        target_date = str(td.get("date", "Unknown"))
        target_time = str(td.get("time", "Unknown"))
        duration = td.get("duration", "Unknown")
        recurrence = td.get("recurrence", "None")
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

        row = [task_id_str, task_name, target_date, target_time, duration, "Active", recurrence]
        await asyncio.to_thread(user_sheet.append_row, row)
        msgs.append(f"✅ **Saved:** {task_name} 📅 {target_date} 🕒 {target_time}")

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

    user_sheet = await get_user_sheet(chat_id, first_name)

    # Custom Delay flow uses the simple, un-prompted model
    if "pending_delay_row" in context.user_data:
        physical_row = context.user_data.pop("pending_delay_row")
        msg_id = context.user_data.pop("pending_delay_msg_id")
        prompt = f"Extract the numeric duration in total minutes from: '{user_text}'. Respond with ONLY an integer."
        resp = await ai_model_simple.generate_content_async(prompt)
        try:
            mins = int(resp.text.strip())
            new_t = datetime.now(DEFAULT_TIMEZONE) + timedelta(minutes=mins)
            await asyncio.to_thread(user_sheet.update_cell, physical_row, 3, new_t.strftime("%Y-%m-%d"))
            await asyncio.to_thread(user_sheet.update_cell, physical_row, 4, new_t.strftime("%H:%M"))
            await asyncio.to_thread(user_sheet.update_cell, physical_row, 6, "Active")
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
        await process_parsed_tasks(tasks, update, context, user_sheet)
    except Exception as e:
        logger.error("Parse error [%s]: %s", chat_id, e, exc_info=True)
        await update.message.reply_text("❌ Sorry, I couldn't process that. Please try rephrasing.")


# ── VOICE HANDLER ──────────────────────────────────────────────
async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    first_name = str(update.effective_user.first_name)
    msg_id = update.message.message_id
    tmp = os.path.join(tempfile.gettempdir(), f"voice_{chat_id}_{msg_id}.ogg")

    user_sheet = await get_user_sheet(chat_id, first_name)
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
        await process_parsed_tasks(tasks, update, context, user_sheet)

    except Exception as e:
        logger.error("Voice error [%s]: %s", chat_id, e, exc_info=True)
        await update.message.reply_text("❌ Couldn't understand that voice note. Please try again or type your task.")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ── SCHEDULER ──────────────────────────────────────────────────
async def check_and_send_reminders(context: ContextTypes.DEFAULT_TYPE):
    try:
        # FROZEN SNAPSHOT: Secure the exact time immediately so it doesn't drift
        # while processing sheets for multiple colleagues.
        snapshot_now = datetime.now(DEFAULT_TIMEZONE)
        today_s = snapshot_now.strftime("%Y-%m-%d")
        time_s = snapshot_now.strftime("%H:%M")

        doc = await asyncio.to_thread(gc.open, SHEET_NAME)
        sheets = await asyncio.to_thread(doc.worksheets)

        for ws in sheets:
            if "_" not in ws.title:
                continue
            cid = ws.title.split("_")[-1]
            if not cid.isdigit():
                continue

            try:
                vals = await asyncio.to_thread(ws.get_all_values)
                if len(vals) <= 1:
                    continue
                hdr = vals[0]
                for row_vals in vals[1:]:
                    row = dict(zip(hdr, row_vals))
                    # Evaluate against the frozen snapshot!
                    if row.get("Status", "").strip() == "Active" and row.get("Date") == today_s and row.get("Time") == time_s:
                        tid = row["Task ID"]
                        tn = row["Task Name"]
                        dur = row.get("Duration", "Unknown")

                        kb = [
                            [InlineKeyboardButton("✅ Complete", callback_data=f"done_{tid}"),
                             InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{tid}")],
                            [InlineKeyboardButton("⏳ Snooze 30m", callback_data=f"snooze_{tid}"),
                             InlineKeyboardButton("🔄 Custom Delay", callback_data=f"delay_{tid}")],
                        ]
                        txt = f"⏰ **REMINDER ALERT** ⏰\n\n📌 **Task:** {tn}\n⏳ **Duration:** {dur}\n\nWhat would you like to do?"
                        await context.bot.send_message(chat_id=int(cid), text=txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
                        logger.info("Fired reminder for %s: %s", ws.title, tn)

            except gspread.exceptions.APIError as e:
                if "RATE_LIMIT" in str(e):
                    logger.warning("Rate-limited on sheet %s, skipping.", ws.title)
                else:
                    raise
    except Exception as e:
        logger.error("Scheduler error: %s", e, exc_info=True)


# ── BUTTON HANDLER ─────────────────────────────────────────────
async def handle_button_clicks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, task_id = query.data.split("_", 1)

    chat_id = str(update.effective_user.id)
    first_name = str(update.effective_user.first_name)
    user_sheet = await get_user_sheet(chat_id, first_name)

    col_ids = await asyncio.to_thread(user_sheet.col_values, 1)
    physical_row = None
    for i, val in enumerate(col_ids):
        if str(val).strip().lstrip("0") == str(task_id).lstrip("0"):
            physical_row = i + 1
            break

    if not physical_row:
        await query.edit_message_text("❌ Sync error — task no longer exists in dashboard.")
        return

    if action == "done":
        encouragement = random.choice([
            "✅ Marked as done. Keep it up!",
            "✅ Excellent work! Task completed.",
            "✅ You're on a roll! Dashboard updated.",
            "✅ Boom! Another one off the list.",
        ])
        rec_cell = await asyncio.to_thread(user_sheet.cell, physical_row, 7)
        recurrence = rec_cell.value

        if recurrence and recurrence != "None":
            date_cell = await asyncio.to_thread(user_sheet.cell, physical_row, 3)
            cur_date = datetime.strptime(date_cell.value, "%Y-%m-%d")

            if recurrence == "Daily":
                nxt = cur_date + timedelta(days=1)
            elif recurrence == "Weekly":
                nxt = cur_date + timedelta(weeks=1)
            elif recurrence == "Monthly":
                nxt = cur_date + relativedelta(months=1)
            else:
                nxt = cur_date + timedelta(days=1)

            await asyncio.to_thread(user_sheet.update_cell, physical_row, 3, nxt.strftime("%Y-%m-%d"))
            await asyncio.to_thread(user_sheet.update_cell, physical_row, 6, "Active")
            await query.edit_message_text(f"{encouragement} Rescheduled for {nxt.strftime('%Y-%m-%d')}.")
        else:
            await asyncio.to_thread(user_sheet.update_cell, physical_row, 6, "Completed")
            await query.edit_message_text(encouragement)

    elif action == "cancel":
        await asyncio.to_thread(user_sheet.update_cell, physical_row, 6, "Cancelled")
        await query.edit_message_text("❌ Task has been cancelled.")

    elif action == "snooze":
        new_t = datetime.now(DEFAULT_TIMEZONE) + timedelta(minutes=30)
        await asyncio.to_thread(user_sheet.update_cell, physical_row, 3, new_t.strftime("%Y-%m-%d"))
        await asyncio.to_thread(user_sheet.update_cell, physical_row, 4, new_t.strftime("%H:%M"))
        await asyncio.to_thread(user_sheet.update_cell, physical_row, 6, "Active")
        await query.edit_message_text(f"⏳ Snoozed 30 min. New target: {new_t.strftime('%H:%M')}")

    elif action == "delay":
        context.user_data["pending_delay_row"] = physical_row
        context.user_data["pending_delay_msg_id"] = query.message.message_id
        await query.message.reply_text("How long to delay? (e.g. '45m' or '2 hours')")


# ── AGENDA / TODAY ─────────────────────────────────────────────
async def handle_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    command = update.message.text.split()[0].lower()
    chat_id = str(update.effective_user.id)
    first_name = str(update.effective_user.first_name)
    user_sheet = await get_user_sheet(chat_id, first_name)

    try:
        today_str = datetime.now(DEFAULT_TIMEZONE).strftime("%Y-%m-%d")
        records = await asyncio.to_thread(user_sheet.get_all_records)

        if "today" in command:
            active = [r for r in records if r.get("Status", "").strip() == "Active" and str(r.get("Date", "")).strip() == today_str]
            title = f"📅 **Your Tasks for Today ({today_str})**"
        else:
            active = [r for r in records if r.get("Status", "").strip() == "Active"]
            title = "📋 **Your Full Agenda (All Active Tasks)**"

        if not active:
            await update.message.reply_text("🎉 No active tasks! Enjoy your time.")
            return

        active.sort(key=lambda x: (str(x.get("Date", "9999-12-31")), str(x.get("Time", "23:59"))))
        txt = f"{title}\n\n"
        for t in active:
            d = t.get("Date", "No Date")
            tm = t.get("Time", "No Time")
            n = t.get("Task Name", "Untitled")
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
        "**Commands:** /today · /agenda · /help\n\n"
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
        "• /help — This help message\n\n"
        "💡 **Tips:**\n"
        "• Type naturally or send a voice note to create tasks\n"
        "• Say _'complete [task name]'_ to mark tasks done\n"
        "• Say _'cancel all tasks'_ for bulk actions\n"
        "• 🌐 Supports English, العربية, and Français",
        parse_mode="Markdown",
    )


# ── ENGINE RUNNER ──────────────────────────────────────────────
def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    secs = 60 - datetime.now().second
    if app.job_queue:
        app.job_queue.run_repeating(check_and_send_reminders, interval=60, first=secs)
        logger.info("JobQueue active — clock-synchronized.")
    else:
        logger.warning("JobQueue initialization delayed.")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))  
    app.add_handler(CommandHandler("agenda", handle_agenda))
    app.add_handler(CommandHandler("today", handle_agenda))
    app.add_handler(CallbackQueryHandler(handle_button_clicks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    logger.info("Bot fully initialized — starting polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
