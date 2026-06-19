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
ai_model = genai.GenerativeModel("gemini-2.5-flash")

# Connect to Google Sheets
print("Connecting to Google Sheets...")
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open(SHEET_NAME).worksheet("Tasks")
print("Connected successfully!")

# Set local timezone
LOCAL_TIMEZONE = pytz.timezone("Africa/Casablanca")


# Background web server to keep Render from putting the app to sleep
def run_dummy_server():
    class DummyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Fekkarni Bot is active and running smoothly 24/7.")
            
        def do_HEAD(self):
            # Render pings HEAD to check if the app is alive
            self.send_response(200)
            self.end_headers()
            
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting background web server on port {port}...")
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()


# Helper to extract task details and intent using Gemini
async def parse_task_with_ai(user_text: str) -> dict:
    now_local = datetime.now(LOCAL_TIMEZONE)
    current_date_str = now_local.strftime("%Y-%m-%d")
    current_time_str = now_local.strftime("%H:%M")
    current_day_name = now_local.strftime("%A")

    system_prompt = f"""
    You are a precise data extraction engine for a personal task manager bot.
    The current local date is {current_date_str}, the current time is {current_time_str}, and today is {current_day_name}.
    
    Analyze the incoming user message. Extract the following data fields and return them strictly as a JSON object:
    - intent: Use "create" if they are adding a new task. Use "complete" if finishing a task. Use "cancel" if stopping a task.
    - task_name: A clear title of the task to create, complete, or cancel.
    - date: The target date for the reminder in YYYY-MM-DD format. Default to 'Unknown'.
    - time: The target time in 24-hour HH:MM format. CRITICAL RULE: If the user provides a time range (e.g., "between 5 and 6" or "around 5:30 and 6:30"), you MUST randomly pick ONE specific minute inside that range (e.g., "17:42") and output ONLY that specific time. Never output a range.
    - duration: The estimated duration mentioned. Default to 'Unknown'.
    - recurrence: 'Daily', 'Weekly', 'Monthly', or 'None'.

    Output ONLY a valid raw JSON object. Do not wrap it in markdown block quotes.
    """
    
    response = await ai_model.generate_content_async(
        contents=f"User Message: {user_text}\n\nContext Instructions:\n{system_prompt}",
        generation_config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)


# Background job running every 60s to check for due tasks
async def check_and_send_reminders(context: ContextTypes.DEFAULT_TYPE):
    try:
        now_local = datetime.now(LOCAL_TIMEZONE)
        today_str = now_local.strftime("%Y-%m-%d")
        time_str = now_local.strftime("%H:%M")
        
        all_records = sheet.get_all_records()
        
        for index, row in enumerate(all_records, start=2):
            if str(row['Status']).strip() == "Active":
                if str(row['Date']) == today_str and str(row['Time']) == time_str:
                    
                    task_id = row['Task ID']
                    task_name = row['Task Name']
                    duration = row['Duration']
                    
                    keyboard = [
                        [
                            InlineKeyboardButton("✅ Complete", callback_data=f"done_{task_id}_{index}"),
                            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}_{index}")
                        ],
                        [
                            InlineKeyboardButton("⏳ Snooze 30m", callback_data=f"snooze_{task_id}_{index}"),
                            InlineKeyboardButton("🔄 Custom Delay", callback_data=f"delay_{task_id}_{index}")
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


# Process inline button clicks (Done, Cancel, Snooze, Custom Delay)
async def handle_button_clicks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data_parts = query.data.split("_")
    action = data_parts[0]
    task_id = data_parts[1]
    row_index = int(data_parts[2])
    
    # Ensure sheet layout hasn't changed since the button was rendered
    if str(sheet.cell(row_index, 1).value) != str(task_id):
        await query.edit_message_text("❌ Synchronization error. The dashboard layout changed.")
        return

    if action == "done":
        success_messages = [
            "✅ Marked as done. Keep it up!",
            "✅ Excellent work! Task completed.",
            "✅ You're on a roll, Youness! Dashboard updated.",
            "✅ Boom! Another one off the list."
        ]
        encouragement = random.choice(success_messages)

        rec_cell = await asyncio.to_thread(sheet.cell, row_index, 7)
        recurrence = rec_cell.value
        
        # Reschedule if recurring, otherwise just mark completed
        if recurrence and recurrence != "None":
            date_cell = await asyncio.to_thread(sheet.cell, row_index, 3)
            current_date_val = datetime.strptime(date_cell.value, "%Y-%m-%d")
            
            if recurrence == "Daily":
                next_date = current_date_val + timedelta(days=1)
            elif recurrence == "Weekly":
                next_date = current_date_val + timedelta(weeks=1)
            elif recurrence == "Monthly":
                next_date = current_date_val + timedelta(days=30)
                
            await asyncio.to_thread(sheet.update_cell, row_index, 3, next_date.strftime("%Y-%m-%d"))
            await asyncio.to_thread(sheet.update_cell, row_index, 6, "Active")
            await query.edit_message_text(f"{encouragement} Rescheduled for {next_date.strftime('%Y-%m-%d')}.")
        else:
            await asyncio.to_thread(sheet.update_cell, row_index, 6, "Completed")
            await query.edit_message_text(encouragement)
            
    elif action == "cancel":
        sheet.update_cell(row_index, 6, "Cancelled")
        await query.edit_message_text("❌ Task has been cancelled.")
        
    elif action == "snooze":
        now_local = datetime.now(LOCAL_TIMEZONE)
        new_time = now_local + timedelta(minutes=30)
        
        sheet.update_cell(row_index, 3, new_time.strftime("%Y-%m-%d"))
        sheet.update_cell(row_index, 4, new_time.strftime("%H:%M"))
        sheet.update_cell(row_index, 6, "Active")
        await query.edit_message_text(f"⏳ Task snoozed for 30 minutes. New target: {new_time.strftime('%H:%M')}")
        
    elif action == "delay":
        context.user_data['pending_delay_row'] = row_index
        context.user_data['pending_delay_msg_id'] = query.message.message_id
        await query.message.reply_text("How many minutes or hours would you like to delay this? (e.g., type '45m' or '2 hours')")


# Handle standard text messages
async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = str(update.effective_chat.id)
    
    # Store chat ID for background jobs if not already saved
    if os.getenv("CHAT_ID") != chat_id:
        with open(".env", "a") as env_file:
            env_file.write(f"\nCHAT_ID={chat_id}")
        os.environ["CHAT_ID"] = chat_id

    # If waiting for a custom delay input
    if 'pending_delay_row' in context.user_data:
        row_index = context.user_data.pop('pending_delay_row')
        msg_id = context.user_data.pop('pending_delay_msg_id')
        
        ai_prompt = f"Extract the numeric duration in total minutes from this text: '{user_text}'. Respond with ONLY an integer number."
        response = await ai_model.generate_content_async(ai_prompt)
        ai_res = response.text.strip()
        
        try:
            minutes_to_add = int(ai_res)
            now_local = datetime.now(LOCAL_TIMEZONE)
            new_target = now_local + timedelta(minutes=minutes_to_add)
            
            await asyncio.to_thread(sheet.update_cell, row_index, 3, new_target.strftime("%Y-%m-%d"))
            await asyncio.to_thread(sheet.update_cell, row_index, 4, new_target.strftime("%H:%M"))
            await asyncio.to_thread(sheet.update_cell, row_index, 6, "Active")
            
            await update.message.reply_text(f"🔄 Custom delay set! Moved to {new_target.strftime('%H:%M')}.")
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except:
            await update.message.reply_text("Could not parse time window. Please try again with a simple entry like '45m'.")
        return

    # If following up on a task that was missing time/date
    if 'draft_task_name' in context.user_data:
        draft_name = context.user_data.pop('draft_task_name')
        user_text = f"The task is: '{draft_name}'. The date and time is: {user_text}"

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        task_data = await parse_task_with_ai(user_text)
        intent = task_data.get("intent", "create")
        task_name = task_data.get("task_name", "Untitled Task")
        
        # Handle updating existing tasks (complete/cancel)
        if intent in ["complete", "cancel"]:
            all_records = await asyncio.to_thread(sheet.get_all_records)
            target_name_lower = task_name.lower()
            found_row = None
            actual_name = ""
            
            for index, row in enumerate(all_records, start=2):
                if str(row['Status']).strip() == "Active" and target_name_lower in str(row['Task Name']).lower():
                    found_row = index
                    actual_name = row['Task Name']
                    break
                    
            if found_row:
                new_status = "Completed" if intent == "complete" else "Cancelled"
                await asyncio.to_thread(sheet.update_cell, found_row, 6, new_status)
                await update.message.reply_text(f"✅ Got it! I have marked **{actual_name}** as {new_status}.")
            else:
                await update.message.reply_text(f"❌ I couldn't find an active task matching '{task_name}' in your agenda.")
            return
            
        # Handle creating new tasks
        task_id = datetime.now(LOCAL_TIMEZONE).strftime("%M%S")
        target_date = task_data.get("date", "Unknown")
        target_time = task_data.get("time", "Unknown")
        duration = task_data.get("duration", "Unknown")
        recurrence = task_data.get("recurrence", "None")
        needs_clarification = task_data.get("needs_clarification", False)
        
        # Catch tasks missing scheduling info before saving to the sheet
        if needs_clarification or target_date == "Unknown" or target_time == "Unknown":
            context.user_data['draft_task_name'] = task_name
            await update.message.reply_text(f"📌 I noted: **{task_name}**\n\nBut you didn't specify when! What date and time would you like me to remind you?")
            return
        
        row_to_add = [task_id, task_name, target_date, target_time, duration, "Active", recurrence]
        await asyncio.to_thread(sheet.append_row, row_to_add)
        
        confirmation = (
            f"✅ **Task Saved!**\n\n"
            f"📌 **Task:** {task_name}\n"
            f"📅 **Date:** {target_date}\n"
            f"🕒 **Time:** {target_time}\n"
            f"⏳ **Duration:** {duration}\n"
            f"🔁 **Repeat:** {recurrence}\n"
        )
        await update.message.reply_text(confirmation, parse_mode="Markdown")
        
    except Exception as e:
        print(f"Parse error: {e}")
        await update.message.reply_text("❌ Sorry, I had trouble parsing that task.")


# Handle voice notes
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
        # Download audio from Telegram
        voice_file_info = await context.bot.get_file(update.message.voice.file_id)
        await voice_file_info.download_to_drive(temp_file_path)
        
        # Read directly into memory to bypass the buggy Gemini File API
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

        # Inject context if we are clarifying a previous prompt
        draft_context = ""
        if 'draft_task_name' in context.user_data:
            draft_name = context.user_data.pop('draft_task_name')
            draft_context = f"\nCRITICAL: The user is clarifying the time for a previous task: '{draft_name}'. Keep this task name and extract the new date/time from the audio."

        system_prompt = f"""
        You are a precise data extraction engine for a personal task manager bot.
        The current local date is {current_date_str}, the current time is {current_time_str}, and today is {current_day_name}.
    
        Analyze the incoming user message. Extract the following data fields and return them strictly as a JSON object:
        - intent: Use "create" if they are adding a new task. Use "complete" if finishing a task. Use "cancel" if stopping a task.
        - task_name: A clear title of the task to create, complete, or cancel.
        - date: The target date for the reminder in YYYY-MM-DD format. Default to 'Unknown'.
        - time: The target time in 24-hour HH:MM format. CRITICAL RULE: If the user provides a time range (e.g., "between 5 and 6" or "around 5:30 and 6:30"), you MUST randomly pick ONE specific minute inside that range (e.g., "17:42") and output ONLY that specific time. Never output a range.
        - duration: The estimated duration mentioned. Default to 'Unknown'.
        - recurrence: 'Daily', 'Weekly', 'Monthly', or 'None'.

        Output ONLY a valid raw JSON object. Do not wrap it in markdown block quotes.
        """
        
        response = await ai_model.generate_content_async(
            contents=[audio_part, system_prompt],
            generation_config={"response_mime_type": "application/json"}
        )
        
        task_data = json.loads(response.text)
        intent = task_data.get("intent", "create")
        task_name = task_data.get("task_name", "Untitled Task")
        
        # Handle updating existing tasks
        if intent in ["complete", "cancel"]:
            all_records = await asyncio.to_thread(sheet.get_all_records)
            target_name_lower = task_name.lower()
            found_row = None
            actual_name = ""
            
            for index, row in enumerate(all_records, start=2):
                if str(row['Status']).strip() == "Active" and target_name_lower in str(row['Task Name']).lower():
                    found_row = index
                    actual_name = row['Task Name']
                    break
                    
            if found_row:
                new_status = "Completed" if intent == "complete" else "Cancelled"
                await asyncio.to_thread(sheet.update_cell, found_row, 6, new_status)
                await update.message.reply_text(f"✅ Got it! I have marked **{actual_name}** as {new_status}.")
            else:
                await update.message.reply_text(f"❌ I couldn't find an active task matching '{task_name}' in your agenda.")
            return
            
        task_id_str = datetime.now(LOCAL_TIMEZONE).strftime("%M%S")
        target_date = task_data.get("date", "Unknown")
        target_time = task_data.get("time", "Unknown")
        duration = task_data.get("duration", "Unknown")
        recurrence = task_data.get("recurrence", "None")
        needs_clarification = task_data.get("needs_clarification", False)

        # Catch missing scheduling info
        if needs_clarification or target_date == "Unknown" or target_time == "Unknown":
            context.user_data['draft_task_name'] = task_name
            await update.message.reply_text(f"🎙️ I noted: **{task_name}**\n\nBut you didn't specify when! What date and time would you like me to remind you?")
            return
        
        row_to_add = [task_id_str, task_name, target_date, target_time, duration, "Active", recurrence]
        await asyncio.to_thread(sheet.append_row, row_to_add)
        
        confirmation = (
            f"🎙️ **Voice Task Saved!**\n\n"
            f"📌 **Task:** {task_name}\n"
            f"📅 **Date:** {target_date}\n"
            f"🕒 **Time:** {target_time}\n"
            f"⏳ **Duration:** {duration}\n"
            f"🔁 **Repeat:** {recurrence}\n"
        )
        await update.message.reply_text(confirmation, parse_mode="Markdown")
        
    except Exception as e:
        print(f"Voice parse error: {e}", flush=True) 
        error_message = (
            f"❌ Sorry, I had trouble understanding that voice note.\n\n"
            f"🛠️ **Debug Info:**\n`{str(e)}`"
        )
        await update.message.reply_text(error_message, parse_mode="Markdown")
        
    finally:
        # Clean up temp file to save server space
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)


# Fetch and format active tasks for the current day
async def handle_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        now_local = datetime.now(LOCAL_TIMEZONE)
        today_str = now_local.strftime("%Y-%m-%d")
        
        all_records = await asyncio.to_thread(sheet.get_all_records)
        
        # Filter active tasks matching today's date
        todays_tasks = [
            row for row in all_records 
            if str(row.get('Date', '')).strip() == today_str and str(row.get('Status', '')).strip() == "Active"
        ]
        
        if not todays_tasks:
            await update.message.reply_text("🎉 You have no remaining tasks for today! Enjoy your free time, Youness.")
            return
            
        # Sort chronologically
        todays_tasks.sort(key=lambda x: str(x.get('Time', '23:59')))
        
        agenda_text = f"📅 **Your Agenda for Today ({today_str})**\n\n"
        for t in todays_tasks:
            time_val = t.get('Time', 'Unknown')
            name_val = t.get('Task Name', 'Untitled')
            dur_val = t.get('Duration', '')
            dur_str = f" (⏳ {dur_val})" if dur_val and dur_val != 'Unknown' else ""
            
            agenda_text += f"• **{time_val}** - {name_val}{dur_str}\n"
            
        agenda_text += "\n_You've got this! Let me know if you need to add or change anything._"
        await update.message.reply_text(agenda_text, parse_mode="Markdown")
        
    except Exception as e:
        print(f"Agenda error: {e}", flush=True)
        await update.message.reply_text("❌ Sorry, I had trouble fetching your agenda from the database.")
        

# Onboarding message
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


# Setup bot connections and polling loop
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
    
    # Sync job queue with the top of the minute
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
