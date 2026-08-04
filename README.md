# 🧠 FekkarniBot (فكرني) — AI-Powered Telegram Memory & Voice Reminder Assistant

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Telegram Bot API](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4.svg?logo=telegram)](https://core.telegram.org/bots/api)
[![Google Gemini AI](https://img.shields.io/badge/AI-Google%20Gemini%203.1%20Flash%20Lite-orange.svg)](https://aistudio.google.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Deploy on Render](https://img.shields.io/badge/Deploy-Render-46E3B7.svg)](https://render.com/)

**FekkarniBot** (from Moroccan Arabic فكرني *"Remind me"*) is a state-of-the-art AI memory and task assistant built on Telegram. Using **Google Gemini 3.1 Flash Lite**, it understands text messages and voice notes in **English, Arabic, Darija, and French**, automatically parsing dates, times, durations, and priorities to keep your life organized.

---

## 🤖 How to Use on Telegram (No Coding Required)

If you are a regular user and want to use FekkarniBot right away:
1. Open **Telegram** on your phone or desktop.
2. Search for the bot by its username (or click your bot's Telegram link).
3. Send **/start** to begin!
4. **To receive Phone Call Reminders (Urgent Tasks):** Authorize free Telegram calls by clicking **[https://api2.callmebot.com/txt/auth.php](https://api2.callmebot.com/txt/auth.php)** and clicking **Start** (or message `@CallMeBot_API` on Telegram).
5. Simply type or send a voice note (e.g., *"Remind me tomorrow at 4 PM to call Sarah — Urgent"*).

---

## ✨ Features

- 🎙️ **Multimodal Voice & Text Input:** Send a voice note like *"Remind me tomorrow at 4 PM to call Sarah"* or *"فكرني نهار الجمعة مع 10 دالصباح نخلص الكرا"*—Gemini automatically extracts the task name, date, time, and priority.
- 🚨 **Urgent Voice Calls (via CallMeBot):** Mark any task as **Urgent** and FekkarniBot will dial your phone via Telegram Voice Call when the reminder is due!
- 📅 **Interactive Task Dashboard:** View your schedule with `/today` or `/agenda`. Mark tasks Done, Cancel, Snooze (30m), or Custom Delay right from inline Telegram buttons.
- 📊 **Complete CSV Export (`/export`):** Generate a full UTF-8 CSV spreadsheet of your complete task history on-the-fly to open in Microsoft Excel, Google Sheets, or Apple Numbers.
- 💬 **Conversational & Multi-task Parsing:** Create multiple tasks in one sentence, ask about your schedule, or say *"mark my call client task as completed"*.
- 🛠️ **Built-in Call Diagnostics (`/testcall`):** Test your CallMeBot voice call setup anytime and view server authorization/rate-limit responses directly in Telegram.

---

## 🛠️ Architecture

```
   ┌──────────────┐         ┌───────────────┐         ┌─────────────────────────┐
   │ Telegram App │ ──────> │ FekkarniBot   │ ──────> │ Google Gemini 3.1       │
   │ (Text/Voice) │ <────── │ (python-telegram│ <────── │ Flash Lite (AI Parser)  │
   └──────────────┘         └───────────────┘         └─────────────────────────┘
          ▲                         │
          │ (Urgent Voice Calls)    ├── (SQLite) ──> ./fekkarni.db (User Tasks)
          │                         │
   ┌──────────────┐                 ▼
   │ CallMeBot    │ <─────── Async Scheduler (30s Polling & Rate-Limit Alerts)
   └──────────────┘
```

---

## 🚀 Quickstart Guide

### 1. Prerequisites
- **Python 3.11+**
- A **Telegram Bot Token** from [@BotFather](https://t.me/BotFather)
- A **Google Gemini API Key** from [Google AI Studio](https://aistudio.google.com/)

### 2. Installation & Configuration

1. **Clone the repository:**
   ```bash
   git clone https://github.com/YounessNaitOufkir/FekkarniBot.git
   cd FekkarniBot
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables:**
   Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and insert your credentials:
   ```ini
   TELEGRAM_TOKEN=your_telegram_bot_token
   GEMINI_API_KEY=your_gemini_api_key
   DEFAULT_TIMEZONE=Africa/Casablanca
   ```

4. **Run FekkarniBot locally:**
   ```bash
   python FekkarniBot.py
   ```

---

## 📞 Enabling Urgent Voice Calls (CallMeBot Setup)

To receive **free Telegram voice calls** when a task is marked **Urgent**, authorize CallMeBot on your Telegram account (takes 5 seconds):

1. **One-Click Authorization:** Visit **[https://api2.callmebot.com/txt/auth.php](https://api2.callmebot.com/txt/auth.php)** and click **Start**.
2. *(Alternative):* Search for **`@CallMeBot_API`** in Telegram and send `/start`.
3. Test your call setup anytime by typing `/testcall` to FekkarniBot!

---

## 📖 Command Reference

| Command | Alias | Description |
| :--- | :--- | :--- |
| `/start` | — | Displays the welcome message and getting-started guide |
| `/today` | `/agenda today` | Shows all active tasks scheduled for today |
| `/agenda` | — | Shows your complete active task agenda across all dates |
| `/export` | `/mysheet`, `/sheet` | Downloads your full task history as a UTF-8 CSV spreadsheet |
| `/testcall` | `/test`, `/call` | Triggers a live test voice call and prints server diagnostic logs |
| `/help` | — | Shows full command and natural language tips |

---

## 🐳 Docker & Cloud Deployment

FekkarniBot includes a Dockerfile and is optimized for cloud platforms like **Render**, **Fly.io**, or **Railway**.

### Build and Run with Docker
```bash
docker build -t fekkarnibot .
docker run -d --env-file .env --name fekkarni fekkarnibot
```

### One-Click Deploy on Render (Free Tier)
1. Connect this repository to Render as a **Background Worker**.
2. Select **Docker** as the runtime environment.
3. Add your Environment Variables (`TELEGRAM_TOKEN`, `GEMINI_API_KEY`, `DEFAULT_TIMEZONE`) in the Render dashboard.

---

## 🔒 Security & Privacy Notice
- FekkarniBot stores task data locally in an SQLite database (`fekkarni.db`). Every query is strictly scoped by user `chat_id`.
- Ensure `.env` and `.db` files are excluded from version control (`.gitignore`).

---

## 📄 License
This project is licensed under the [MIT License](LICENSE).
