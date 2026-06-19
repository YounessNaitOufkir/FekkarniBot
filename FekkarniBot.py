import os
import json
import threading
import asyncio
import random
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytz
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import google.generativeai as genai

# Load env variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SHEET_NAME = os.getenv("SHEET_NAME")

# Init Gemini
genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel("gemini-3.1-flash-lite")

# Connect to Google Sheets
print("Connecting to Google Sheets...")
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open(SHEET_NAME).worksheet("Tasks")
print("Connected successfully!")

# Set local timezone
LOCAL_TIMEZONE = pytz.timezone("Africa/Casablanca")

# --- SECURITY SANITIZER ---
def sanitize_error(error_msg: Exception) -> str:
    safe_text = str(error_msg)
    if GEMINI_API_KEY and GEMINI_API_KEY in safe_text:
        safe_text = safe_text.replace(GEMINI_API_KEY, "********[REDACTED_API_KEY]********")
    if TELEGRAM_TOKEN and TELEGRAM_TOKEN in safe_text:
        safe_text = safe_text.replace(TELEGRAM_TOKEN, "********[REDACTED_BOT_TOKEN]********")
    return safe_text

# --- DUMMY SERVER ---
def run_dummy_server():
    class DummyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Fekkarni Bot is active and running smoothly 24/7.")
            
        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()
            
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting background web server on port {port}...")
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()


# --- AI PARSING ENGINE ---
async def parse_task_with_ai(user_text: str, draft_context: str = "") -> list:
    now_local = datetime.now(LOCAL_TIMEZONE)
    current_date_str = now_local.strftime("%Y-%m-%d")
    current_time_str = now_local.strftime("%H:%M")
    current_day_name = now_local.strftime("%A")

    system_prompt = f"""
    You are a precise data extraction engine for a personal task manager bot.
    The current local date is {current_date_str}, the current time is {current_time_str}, and today is {current_day_name}.
    {draft_context}
    
    Analyze the incoming user message. The user might mention MULTIPLE tasks.
    Extract the data fields and return them strictly as a JSON ARRAY of objects, even if there is only one task.
    Format exactly like this:
    [
      {{
        "intent": "create", "complete", or "cancel",
        "task_name": "A clear title",
        "date": "YYYY-MM-DD",
        "time": "HH:MM",
        "duration": "Unknown",
        "recurrence": "None",
        "needs_clarification": false
      }}
    ]

    CRITICAL RULES:
    1. If a time range is given, pick ONE specific minute.
    2. If the user does NOT specify a date, output "Unknown".
    3. If the user does NOT specify a time, output "Unknown".
    4. If intent is "create" and date or time is missing, set "needs_clarification" to true.
    5. If intent is "complete" or "cancel", extract ONLY the core identifying keywords for the `task_name`. Completely remove filler words like "task", "reminder", "the", "my".
    
    Output ONLY a valid raw JSON array. Do not wrap it in markdown block quotes.
    """
    
    response = await ai_model.generate_content_async(
        contents=f"User Message: {user_text}\n\nContext Instructions:\n{system_prompt}",
        generation_config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)


# --- BACKGROUND SCHEDULER ---
async def check_and_send_reminders(context: ContextTypes.DEFAULT_TYPE):
    try:
        now_local = datetime.now(LOCAL_TIMEZONE)
        today_str = now_local.strftime("%Y-%m-%d")
        time_str = now_local.strftime("%H:%M")
        
        all_records = await asyncio.to_thread(sheet.get_all_records)
        
        for index, row in enumerate(all_records, start=2):
            if str(row.get('Status', '')).strip() == "Active":
                if str(row.get('Date', '')) == today_str and str(row.get('Time', '')) == time_str:
                    
                    task_id = row['Task ID']
                    task_name = row['Task Name']
                    duration = row.get('Duration', 'Unknown')
                    
                    keyboard = [
                        [
                            InlineKeyboardButton("✅ Complete", callback_data=f"done_{task_id}"),
                            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")
                        ],
                        [
                            InlineKeyboardButton("⏳ Snooze 30m", callback_data=f"snooze_{task_id}"),
                            InlineKeyboardButton("🔄 Custom Delay", callback_data=f"delay_{task_id}")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    reminder_text = (
                        f"⏰ **REMINDER ALERT** ⏰\n\n"
                        f"📌 **Task:** {task_name}\n"
                        f"⏳ **Duration:** {duration}\n\n"
                        f"What would you like to do with this task?"
                    )
                    
                    chat_id = os.getenv("CHAT_ID")
                    if chat_id:
                        await context.bot.send_message(
                            chat_id=int(chat_id), 
                            text=reminder_text, 
                            reply_markup=reply_markup, 
                            parse_mode="Markdown"
                        )
                        print(f"✅ Fired reminder for task: {task_name}")
                        
    except Exception as e:
        print(f"Error checking scheduler: {e}")


# --- BUTTON CLICK HANDLER ---
async def handle_button_clicks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data_parts = query.data.split("_")
    action = data_parts[0]
    task_id = data_parts[1]
    
    col_ids = await asyncio.to_thread(sheet.col_values, 1)
    physical_row = None
    for i, val in enumerate(col_ids):
        if str(val).strip().lstrip('0') == str(task_id).lstrip('0'):
            physical_row = i + 1
            break
            
    if not physical_row:
        await query.edit_message_text("❌ Synchronization error. Task no longer exists in dashboard.")
        return

    if action == "done":
        success_messages = [
            "✅ Marked as done. Keep it up!",
            "✅ Excellent work! Task completed.",
            "✅ You're on a roll, Youness! Dashboard updated.",
            "✅ Boom! Another one off the list."
        ]
        encouragement = random.choice(success_messages)

        rec_cell = await asyncio.to_thread(sheet.cell, physical_row, 7)
        recurrence = rec_cell.value
        
        if recurrence and recurrence != "None":
            date_cell = await asyncio.to_thread(sheet.cell, physical_row, 3)
            current_date_val = datetime.strptime(date_cell.value, "%Y-%m-%d")
            
            if recurrence == "Daily":
                next_date = current_date_val + timedelta(days=1)
            elif recurrence == "Weekly":
                next_date = current_date_val + timedelta(weeks=1)
            elif recurrence == "Monthly":
                next_date = current_date_val + timedelta(days=30)
                
            await asyncio.to_thread(sheet.update_cell, physical_row, 3, next_date.strftime("%Y-%m-%d"))
            await asyncio.to_thread(sheet.update_cell, physical_row, 6, "Active")
            await query.edit_message_text(f"{encouragement} Rescheduled for {next_date.strftime('%Y-%m-%d')}.")
        else:
            await asyncio.to_thread(sheet.update_cell, physical_row, 6, "Completed")
            await query.edit_message_text(encouragement)
            
    elif action == "cancel":
        await asyncio.to_thread(sheet.update_cell, physical_row, 6, "Cancelled")
        await query.edit_message_text("❌ Task has been cancelled.")
        
    elif action == "snooze":
        now_local = datetime.now(LOCAL_TIMEZONE)
        new_time = now_local + timedelta(minutes=30)
        
        await asyncio.to_thread(sheet.update_cell, physical_row, 3, new_time.strftime("%Y-%m-%d"))
        await asyncio.to_thread(sheet.update_cell, physical_row, 4, new_time.strftime("%H:%M"))
        await asyncio.to_thread(sheet.update_cell, physical_row, 6, "Active")
        await query.edit_message_text(f"⏳ Task snoozed for 30 minutes. New target: {new_time.strftime('%H:%M')}")
        
    elif action == "delay":
        context.user_data['pending_delay_row'] = physical_row
        context.user_data['pending_delay_msg_id'] = query.message.message_id
        await query.message.reply_text("How many minutes or hours would you like to delay this? (e.g., type '45m' or '2 hours')")


# --- TEXT HANDLER ---
async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = str(update.effective_chat.id)
    
    if os.getenv("CHAT_ID") != chat_id:
        with open(".env", "a") as env_file:
            env_file.write(f"\nCHAT_ID={chat_id}")
        os.environ["CHAT_ID"] = chat_id

    if 'pending_delay_row' in context.user_data:
        physical_row = context.user_data.pop('pending_delay_row')
        msg_id = context.user_data.pop('pending_delay_msg_id')
        
        ai_prompt = f"Extract the numeric duration in total minutes from this text: '{user_text}'. Respond with ONLY an integer number."
        response = await ai_model.generate_content_async(ai_prompt)
        ai_res = response.text.strip()
        
        try:
            minutes_to_add = int(ai_res)
            now_local = datetime.now(LOCAL_TIMEZONE)
            new_target = now_local + timedelta(minutes=minutes_to_add)
            
            await asyncio.to_thread(sheet.update_cell, physical_row, 3, new_target.strftime("%Y-%m-%d"))
            await asyncio.to_thread(sheet.update_cell, physical_row, 4, new_target.strftime("%H:%M"))
            await asyncio.to_thread(sheet.update_cell, physical_row, 6, "Active")
            
            await update.message.reply_text(f"🔄 Custom delay set! Moved to {new_target.strftime('%H:%M')}.")
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except Exception as e:
            await update.message.reply_text("Could not parse time window. Please try again with a simple entry like '45m'.")
        return 

    draft_context = ""
    if 'draft_task_name' in context.user_data:
        draft_name = context.user_data.pop('draft_task_name')
        draft_context = f"\nCRITICAL: The user is clarifying the time for a previous task: '{draft_name}'. Keep this task name and extract the new date/time from the text."

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        task_list = await parse_task_with_ai(user_text, draft_context)
        if isinstance(task_list, dict):
            task_list = [task_list]
            
        response_messages = []
        for task_data in task_list:
            intent = task_data.get("intent", "create")
            task_name = task_data.get("task_name", "Untitled Task")
            
            if intent in ["complete", "cancel"]:
                names_col = await asyncio.to_thread(sheet.col_values, 2)
                status_col = await asyncio.to_thread(sheet.col_values, 6)
                
                # --- FUZZY SEARCH UPGRADE ---
                target_name_lower = task_name.lower()
                clean_target = target_name_lower.replace("task", "").replace("reminder", "").replace("the", "").replace("my", "").strip()
                target_words = [w for w in clean_target.split() if len(w) > 2] # Only search meaningful keywords
                
                found_row = None
                actual_name = ""
                
                for i in range(1, len(names_col)):
                    status = status_col[i] if i < len(status_col) else ""
                    sheet_name_lower = str(names_col[i]).lower()
                    
                    if str(status).strip() == "Active":
                        # Match if the exact phrase is found, OR if all the key words are found scattered in the name
                        if clean_target in sheet_name_lower or (target_words and all(w in sheet_name_lower for w in target_words)):
                            found_row = i + 1
                            actual_name = names_col[i]
                            break
                            
                if found_row:
                    new_status = "Completed" if intent == "complete" else "Cancelled"
                    await asyncio.to_thread(sheet.update_cell, found_row, 6, new_status)
                    response_messages.append(f"✅ Marked **{actual_name}** as {new_status}.")
                else:
                    response_messages.append(f"❌ Couldn't find active task matching '{task_name}'.")
                continue 
            
            task_id_str = datetime.now(LOCAL_TIMEZONE).strftime("%M%S%f")[:8]
            target_date = str(task_data.get("date", "Unknown"))
            target_time = str(task_data.get("time", "Unknown"))
            duration = task_data.get("duration", "Unknown")
            recurrence = task_data.get("recurrence", "None")
            needs_clarification = task_data.get("needs_clarification", False)
            
            is_missing = (
                needs_clarification == True or 
                target_date.lower() in ["unknown", "none", "null", ""] or 
                target_time.lower() in ["unknown", "none", "null", ""]
            )

            if is_missing:
                context.user_data['draft_task_name'] = task_name
                response_messages.append(f"📝 I noted: **{task_name}**\n\nBut you didn't specify when! What date and time would you like me to remind you?")
                continue
            
            row_to_add = [task_id_str, task_name, target_date, target_time, duration, "Active", recurrence]
            await asyncio.to_thread(sheet.append_row, row_to_add)
            
            response_messages.append(f"✅ **Saved:** {task_name} 📅 {target_date} 🕒 {target_time}")
            
        if response_messages:
            await update.message.reply_text("\n\n".join(response_messages), parse_mode="Markdown")

    except Exception as e:
        print(f"Parse error: {e}", flush=True)
        safe_error = sanitize_error(e) 
        error_message = (
            f"❌ Sorry, I had trouble parsing that task.\n\n"
            f"🛠️ **Debug Info:**\n`{safe_error}`"
        )
        await update.message.reply_text(error_message, parse_mode="Markdown")


# --- VOICE NOTE HANDLER ---
async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    msg_id = update.message.message_id
    temp_file_path = f"voice_{msg_id}.ogg"
    
    if os.getenv("CHAT_ID") != chat_id:
        with open(".env", "a") as env_file:
            env_file.write(f"\nCHAT_ID={chat_id}")
        os.environ["CHAT_ID"] = chat_id

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    
    try:
        voice_file_info = await context.bot.get_file(update.message.voice.file_id)
        await voice_file_info.download_to_drive(temp_file_path)
        
        with open(temp_file_path, "rb") as f:
            audio_bytes = f.read()
            
        audio_part = {
            "mime_type": "audio/ogg",
            "data": audio_bytes
        }
        
        now_local = datetime.now(LOCAL_TIMEZONE)
        current_date_str = now_local.strftime("%Y-%m-%d")
        current_time_str = now_local.strftime("%H:%M")
        current_day_name = now_local.strftime("%A")

        draft_context = ""
        if 'draft_task_name' in context.user_data:
            draft_name = context.user_data.pop('draft_task_name')
            draft_context = f"\nCRITICAL: The user is clarifying the time for a previous task: '{draft_name}'. Keep this task name and extract the new date/time from the audio."

        system_prompt = f"""
        You are a precise data extraction engine for a personal task manager bot.
        The current local date is {current_date_str}, the current time is {current_time_str}, and today is {current_day_name}.
        {draft_context}
        
        Analyze the incoming user message. The user might mention MULTIPLE tasks.
        Extract the data fields and return them strictly as a JSON ARRAY of objects.
        Format exactly like this:
        [
          {{
            "intent": "create", "complete", or "cancel",
            "task_name": "A clear title",
            "date": "YYYY-MM-DD",
            "time": "HH:MM",
            "duration": "Unknown",
            "recurrence": "None",
            "needs_clarification": false
          }}
        ]

        CRITICAL RULES:
        1. If a time range is given, pick ONE specific minute.
        2. If the user does NOT specify a date, output "Unknown".
        3. If the user does NOT specify a time, output "Unknown".
        4. If intent is "create" and date or time is missing, set "needs_clarification" to true.
        5. If intent is "complete" or "cancel", extract ONLY the core identifying keywords for the `task_name`. Completely remove filler words like "task", "reminder", "the", "my".
        
        Output ONLY a valid raw JSON array. Do not wrap it in markdown block quotes.
        """
        
        response = await ai_model.generate_content_async(
            contents=[audio_part, system_prompt],
            generation_config={"response_mime_type": "application/json"}
        )
        
        task_list = json.loads(response.text)
        
        if isinstance(task_list, dict):
            task_list = [task_list]
            
        response_messages = []
        
        for task_data in task_list:
            intent = task_data.get("intent", "create")
            task_name = task_data.get("task_name", "Untitled Task")
            
            if intent in ["complete", "cancel"]:
                names_col = await asyncio.to_thread(sheet.col_values, 2)
                status_col = await asyncio.to_thread(sheet.col_values, 6)
                
                # --- FUZZY SEARCH UPGRADE ---
                target_name_lower = task_name.lower()
                clean_target = target_name_lower.replace("task", "").replace("reminder", "").replace("the", "").replace("my", "").strip()
                target_words = [w for w in clean_target.split() if len(w) > 2]
                
                found_row = None
                actual_name = ""
                
                for i in range(1, len(names_col)):
                    status = status_col[i] if i < len(status_col) else ""
                    sheet_name_lower = str(names_col[i]).lower()
                    
                    if str(status).strip() == "Active":
                        if clean_target in sheet_name_lower or (target_words and all(w in sheet_name_lower for w in target_words)):
                            found_row = i + 1
                            actual_name = names_col[i]
                            break
                            
                if found_row:
                    new_status = "Completed" if intent == "complete" else "Cancelled"
                    await asyncio.to_thread(sheet.update_cell, found_row, 6, new_status)
                    response_messages.append(f"✅ Got it! I have marked **{actual_name}** as {new_status}.")
                else:
                    response_messages.append(f"❌ Couldn't find active task matching '{task_name}'.")
                continue
                
            task_id_str = datetime.now(LOCAL_TIMEZONE).strftime("%M%S%f")[:8]
            target_date = str(task_data.get("date", "Unknown"))
            target_time = str(task_data.get("time", "Unknown"))
            duration = task_data.get("duration", "Unknown")
            recurrence = task_data.get("recurrence", "None")
            needs_clarification = task_data.get("needs_clarification", False)

            is_missing = (
                needs_clarification == True or 
                target_date.lower() in ["unknown", "none", "null", ""] or 
                target_time.lower() in ["unknown", "none", "null", ""]
            )

            if is_missing:
                context.user_data['draft_task_name'] = task_name
                response_messages.append(f"🎙️ I noted: **{task_name}**\n\nBut you didn't specify when! What date and time would you like me to remind you?")
                continue
            
            row_to_add = [task_id_str, task_name, target_date, target_time, duration, "Active", recurrence]
            await asyncio.to_thread(sheet.append_row, row_to_add)
            
            response_messages.append(f"🎙️ **Voice Task Saved:** {task_name} 📅 {target_date} 🕒 {target_time}")
            
        if response_messages:
            await update.message.reply_text("\n\n".join(response_messages), parse_mode="Markdown")
        
    except Exception as e:
        print(f"Voice parse error: {e}", flush=True) 
        safe_error = sanitize_error(e)
        error_message = (
            f"❌ Sorry, I had trouble understanding that voice note.\n\n"
            f"🛠️ **Debug Info:**\n`{safe_error}`"
        )
        await update.message.reply_text(error_message, parse_mode="Markdown")
        
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)


# --- AGENDA & TODAY HANDLER ---
async def handle_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    command = update.message.text.split()[0].lower() # Detects if user typed /today or /agenda
    
    try:
        now_local = datetime.now(LOCAL_TIMEZONE)
        today_str = now_local.strftime("%Y-%m-%d")
        
        all_records = await asyncio.to_thread(sheet.get_all_records)
        
        # Filter: If /today, only show today's date. If /agenda, show ALL active tasks.
        if "today" in command:
            active_tasks = [
                row for row in all_records 
                if str(row.get('Status', '')).strip() == "Active" and str(row.get('Date', '')).strip() == today_str
            ]
            title = f"📅 **Your Tasks for Today ({today_str})**"
        else:
            active_tasks = [
                row for row in all_records 
                if str(row.get('Status', '')).strip() == "Active"
            ]
            title = "📋 **Your Full Agenda (All Active Tasks)**"
            
        if not active_tasks:
            await update.message.reply_text("🎉 You have no active tasks to show! Enjoy your time, Youness.")
            return
            
        # Sort by Date first, then Time
        active_tasks.sort(key=lambda x: (str(x.get('Date', '9999-12-31')), str(x.get('Time', '23:59'))))
        
        agenda_text = f"{title}\n\n"
        for t in active_tasks:
            date_val = t.get('Date', 'No Date')
            time_val = t.get('Time', 'No Time')
            name_val = t.get('Task Name', 'Untitled')
            
            # Formatting: Display date if viewing full agenda
            date_str = f"[{date_val}] " if "Full Agenda" in title else ""
            agenda_text += f"• {date_str}**{time_val}** - {name_val}\n"
            
        await update.message.reply_text(agenda_text, parse_mode="Markdown")
        
    except Exception as e:
        print(f"Agenda/Today error: {e}", flush=True)
        await update.message.reply_text("❌ Sorry, I had trouble fetching your tasks.")
        

# --- START COMMAND ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 **Welcome to Fekkarni!** Your personal AI memory assistant.\n\n"
        "I am here to make sure you never forget a task, appointment, or idea.\n\n"
        "**How to use me:**\n"
        "Just talk to me naturally! You can type or send a voice note.\n"
        "Try saying something like:\n"
        "• _'Remind me to call the client tomorrow at 10 AM'_\n"
        "• _'Every Friday at 4 PM remind me to check the budget'_\n\n"
        "Send me your first task right now to test it out!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


# --- ENGINE RUNNER ---
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
    
    current_time = datetime.now()
    seconds_until_perfect_minute = 60 - current_time.second
    
    if app.job_queue:
        app.job_queue.run_repeating(check_and_send_reminders, interval=60, first=seconds_until_perfect_minute)
        print("JobQueue successfully verified and clock synchronized.")
    else:
        print("⚠️ Warning: JobQueue initialization delayed.")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("agenda", handle_agenda)) 
    app.add_handler(CommandHandler("today", handle_agenda))  
    app.add_handler(CallbackQueryHandler(handle_button_clicks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    
    print("Bot is fully initialized and preparing to poll...")
    app.run_polling()

if __name__ == '__main__':
    main()
