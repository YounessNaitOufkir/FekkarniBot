import os
import json
import threading
import asyncio
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytz
from dotenv import load_dotenv

# Telegram libraries
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Google Sheets libraries
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Gemini AI library
import google.generativeai as genai

# --- 1. CONFIGURATION & LOGINS ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SHEET_NAME = os.getenv("SHEET_NAME")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel("gemini-2.5-flash")

# Connect to Google Sheets
print("Connecting to Google Sheets...")
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open(SHEET_NAME).worksheet("Tasks")
print("Connected successfully!")

# Set our home timezone
LOCAL_TIMEZONE = pytz.timezone("Africa/Casablanca")


# --- 2. RENDER DUMMY WEB SERVER WITH HEAD CHECK SUPPORT ---
def run_dummy_server():
    """Starts a lightweight web server to satisfy Render's health checks."""
    class DummyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Fekkarni Bot is active and running smoothly 24/7.")
            
        def do_HEAD(self):
            # Render sends HEAD requests to check application vitality
            self.send_response(200)
            self.end_headers()
            
    # Render assigns a dynamic port. We default to 10000 if running locally.
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting background web server on port {port}...")
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

# Start the dummy server immediately in a background thread
threading.Thread(target=run_dummy_server, daemon=True).start()


# --- 3. AI PARSING BRAIN ---
def parse_task_with_ai(user_text: str) -> dict:
    now_local = datetime.now(LOCAL_TIMEZONE)
    current_date_str = now_local.strftime("%Y-%m-%d")
    current_time_str = now_local.strftime("%H:%M")
    current_day_name = now_local.strftime("%A")

    system_prompt = f"""
    You are a precise data extraction engine for a personal task manager bot.
    The current local date is {current_date_str}, the current time is {current_time_str}, and today is {current_day_name}.
    The user lives in Casablanca, Morocco.
    
    Analyze the incoming user message which may be in English, French, Arabic, or Moroccan Darija (or a mix).
    Extract the following data fields and return them strictly as a JSON object:
    - task_name: A clear, professional title of what needs to be done (translate to English or French for consistency).
    - date: The target date for the reminder in YYYY-MM-DD format. Calculate relative dates like 'tomorrow', 'next Friday', 'ghadan' based on the context.
    - time: The target time for the reminder in 24-hour HH:MM format.
    - duration: The estimated duration mentioned (e.g., '2h', '30m'). If not mentioned, default to 'Unknown'.
    - recurrence: If the task repeats, set this value to 'Daily', 'Weekly', 'Monthly'. If it is a one-time task, set it to 'None'.

    Output ONLY a valid raw JSON object. Do not wrap it in markdown block quotes.
    """
    
    response = ai_model.generate_content(
        contents=f"User Message: {user_text}\n\nContext Instructions:\n{system_prompt}",
        generation_config={"response_mime_type": "application/json"}
    )
    return json.loads(response.text)


# --- 4. NATIVE ASYNC SCHEDULER ENGINE ---
async def check_and_send_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Runs automatically every 60 seconds inside Telegram's native async loop."""
    try:
        now_local = datetime.now(LOCAL_TIMEZONE)
        today_str = now_local.strftime("%Y-%m-%d")
        time_str = now_local.strftime("%H:%M")
        
        all_records = sheet.get_all_records()
        
        for index, row in enumerate(all_records, start=2):
            if str(row['Status']).strip() == "Active":
                # Check if the task date and time match right now
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


# --- 5. BUTTON CLICK (CALLBACK) LOGIC ---
async def handle_button_clicks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data_parts = query.data.split("_")
    action = data_parts[0]
    task_id = data_parts[1]
    row_index = int(data_parts[2])
    
    if str(sheet.cell(row_index, 1).value) != str(task_id):
        await query.edit_message_text("❌ Synchronization error. The dashboard layout changed.")
        return

    if action == "done":
        recurrence = sheet.cell(row_index, 7).value
        if recurrence and recurrence != "None":
            current_date_val = datetime.strptime(sheet.cell(row_index, 3).value, "%Y-%m-%d")
            if recurrence == "Daily":
                next_date = current_date_val + timedelta(days=1)
            elif recurrence == "Weekly":
                next_date = current_date_val + timedelta(weeks=1)
            elif recurrence == "Monthly":
                next_date = current_date_val + timedelta(days=30)
                
            sheet.update_cell(row_index, 3, next_date.strftime("%Y-%m-%d"))
            sheet.update_cell(row_index, 6, "Active")
            await query.edit_message_text(f"✅ Completed! Rescheduled for {next_date.strftime('%Y-%m-%d')}.")
        else:
            sheet.update_cell(row_index, 6, "Completed")
            await query.edit_message_text("✅ Task marked as Completed in your dashboard!")
            
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


# --- 6. NATURAL TEXT HANDLER ---
async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = str(update.effective_chat.id)
    
    # Save Chat ID securely
    if os.getenv("CHAT_ID") != chat_id:
        with open(".env", "a") as env_file:
            env_file.write(f"\nCHAT_ID={chat_id}")
        os.environ["CHAT_ID"] = chat_id

    # Check if we are waiting for a custom delay input
    if 'pending_delay_row' in context.user_data:
        row_index = context.user_data.pop('pending_delay_row')
        msg_id = context.user_data.pop('pending_delay_msg_id')
        
        ai_prompt = f"Extract the numeric duration in total minutes from this text: '{user_text}'. Respond with ONLY an integer number."
        ai_res = ai_model.generate_content(ai_prompt).text.strip()
        
        try:
            minutes_to_add = int(ai_res)
            now_local = datetime.now(LOCAL_TIMEZONE)
            new_target = now_local + timedelta(minutes=minutes_to_add)
            
            sheet.update_cell(row_index, 3, new_target.strftime("%Y-%m-%d"))
            sheet.update_cell(row_index, 4, new_target.strftime("%H:%M"))
            sheet.update_cell(row_index, 6, "Active")
            
            await update.message.reply_text(f"🔄 Custom delay set! Moved to {new_target.strftime('%H:%M')}.")
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except:
            await update.message.reply_text("Could not parse time window. Please try again with a simple entry like '45m'.")
        return

    # Standard task parsing
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        task_data = parse_task_with_ai(user_text)
        task_id = datetime.now(LOCAL_TIMEZONE).strftime("%M%S")
        
        task_name = task_data.get("task_name", "Untitled Task")
        target_date = task_data.get("date", "Unknown")
        target_time = task_data.get("time", "Unknown")
        duration = task_data.get("duration", "Unknown")
        recurrence = task_data.get("recurrence", "None")
        
        row_to_add = [task_id, task_name, target_date, target_time, duration, "Active", recurrence]
        sheet.append_row(row_to_add)
        
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

# --- ASYNC VOICE NOTE HANDLER ---
async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    msg_id = update.message.message_id
    temp_file_path = f"voice_{msg_id}.ogg"
    
    # Save Chat ID securely just in case this is the first message
    if os.getenv("CHAT_ID") != chat_id:
        with open(".env", "a") as env_file:
            env_file.write(f"\nCHAT_ID={chat_id}")
        os.environ["CHAT_ID"] = chat_id

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    
try:
        # 1. Download the voice note from Telegram
        voice_file_info = await context.bot.get_file(update.message.voice.file_id)
        await voice_file_info.download_to_drive(temp_file_path)
        
        # 2. Read the audio bytes directly into memory (Bypasses the broken File API!)
        with open(temp_file_path, "rb") as f:
            audio_bytes = f.read()
            
        audio_part = {
            "mime_type": "audio/ogg",
            "data": audio_bytes
        }
        
        # 3. Process the audio with AI directly
        now_local = datetime.now(LOCAL_TIMEZONE)
        current_date_str = now_local.strftime("%Y-%m-%d")
        current_time_str = now_local.strftime("%H:%M")
        current_day_name = now_local.strftime("%A")

        system_prompt = f"""
        You are a precise data extraction engine for a personal task manager bot.
        The current local date is {current_date_str}, the current time is {current_time_str}, and today is {current_day_name}.
        
        Listen to this audio message. The user lives in Casablanca, Morocco, so the audio may be in English, French, Arabic, or Moroccan Darija.
        Extract the following data fields and return them strictly as a JSON object:
        - task_name: A clear, professional title of what needs to be done.
        - date: The target date for the reminder in YYYY-MM-DD format.
        - time: The target time for the reminder in 24-hour HH:MM format.
        - duration: The estimated duration mentioned. Default to 'Unknown'.
        - recurrence: 'Daily', 'Weekly', 'Monthly', or 'None'.

        Output ONLY a valid raw JSON object. Do not wrap it in markdown block quotes.
        """
        
        # Pass the memory data directly instead of an uploaded file link
        response = await ai_model.generate_content_async(
            contents=[audio_part, system_prompt],
            generation_config={"response_mime_type": "application/json"}
        )
        
        task_data = json.loads(response.text)
        
        # 4. Save to Google Sheets
        task_id_str = datetime.now(LOCAL_TIMEZONE).strftime("%M%S")
        task_name = task_data.get("task_name", "Untitled Task")
        target_date = task_data.get("date", "Unknown")
        target_time = task_data.get("time", "Unknown")
        duration = task_data.get("duration", "Unknown")
        recurrence = task_data.get("recurrence", "None")
        
        row_to_add = [task_id_str, task_name, target_date, target_time, duration, "Active", recurrence]
        await asyncio.to_thread(sheet.append_row, row_to_add)
        
        # 5. Send Confirmation
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
        # Adding flush=True forces Render to print this immediately
        print(f"Voice parse error: {e}", flush=True) 
        
        # Send the exact Python error directly back to Telegram
        error_message = (
            f"❌ Sorry, I had trouble understanding that voice note.\n\n"
            f"🛠️ **Debug Info for Youness:**\n`{str(e)}`"
        )
        await update.message.reply_text(error_message, parse_mode="Markdown")
        
    finally:
        # 7. Delete the local temporary file from Render
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)

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

# --- 7. ENGINE RUNNER ---
def main():
    # Application builder with reinforced timeouts for cloud deployment stability
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    
    # Calculate exactly how many seconds until the clock hits the next :00 mark
    current_time = datetime.now()
    seconds_until_perfect_minute = 60 - current_time.second
    
    # Initialize JobQueue securely now that dependencies are explicitly packed
    if app.job_queue:
        app.job_queue.run_repeating(check_and_send_reminders, interval=60, first=seconds_until_perfect_minute)
        print("JobQueue successfully verified and clock synchronized.")
    else:
        print("⚠️ Warning: JobQueue initialization delayed.")

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button_clicks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    
    print("Bot is fully initialized and preparing to poll...")
    app.run_polling()

if __name__ == '__main__':
    main()
