import os
import time
import platform
import subprocess
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ===== Config =====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))  # your numeric TG id

# Gemini (primary)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# OpenAI (optional fallback)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")

START_TIME = time.time()

# Keep short chat history (in RAM; resets on restart)
HISTORY = []  # list of {"role": "user"/"model", "text": "..."}
MAX_TURNS = 8  # keep last 8 turns

def run(cmd: str) -> str:
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, text=True, timeout=3)
        return out.strip()
    except Exception:
        return ""

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    u = update.effective_user
    return bool(u and u.id == ALLOWED_USER_ID)

def gemini_generate(prompt: str) -> str:
    if not GEMINI_API_KEY:
        return "❌ GEMINI_API_KEY not set."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
    }
    # Build multi-turn contents from HISTORY + new prompt
    contents = []
    for turn in HISTORY[-MAX_TURNS:]:
        role = "user" if turn["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": turn["text"]}]})
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    body = {"contents": contents}

    r = requests.post(url, headers=headers, json=body, timeout=60)
    if r.status_code != 200:
        return f"❌ Gemini HTTP {r.status_code}: {r.text[:500]}"

    data = r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text
    except Exception:
        return f"❌ Gemini parse error: {str(data)[:500]}"

def openai_responses(prompt: str) -> str:
    if not OPENAI_API_KEY:
        return "❌ OPENAI_API_KEY not set (fallback unavailable)."

    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENAI_MODEL,
        "input": prompt,
    }
    r = requests.post(url, headers=headers, json=body, timeout=60)
    if r.status_code != 200:
        return f"❌ OpenAI HTTP {r.status_code}: {r.text[:500]}"

    data = r.json()
    # Responses API returns output items; easiest is to read output_text if present
    if "output_text" in data and data["output_text"]:
        return data["output_text"]
    # Fallback parse (in case schema differs)
    try:
        out = data["output"][0]["content"][0]["text"]
        return out
    except Exception:
        return f"❌ OpenAI parse error: {str(data)[:500]}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "✅ Tablet agent online (Gemini primary).\n\n"
        "Commands:\n"
        "• /status\n"
        "• /whoami\n"
        "• /clear\n"
        "Just message me to chat."
    )

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    u = update.effective_user
    await update.message.reply_text(f"Your user_id: {u.id if u else 'unknown'}")

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    HISTORY.clear()
    await update.message.reply_text("🧹 Cleared conversation memory (RAM).")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    uptime = int(time.time() - START_TIME)
    ip = run("ip addr show wlan0 2>/dev/null | grep 'inet ' | awk '{print $2}' | head -n 1")
    battery = run("termux-battery-status 2>/dev/null | head -c 500")
    await update.message.reply_text(
        f"🟢 OK\n"
        f"Uptime: {uptime}s\n"
        f"Device: {platform.platform()}\n"
        f"Wi-Fi IP: {ip or 'unknown'}\n"
        f"Battery: {battery or 'termux-api not installed'}\n"
        f"Gemini model: {GEMINI_MODEL}\n"
        f"OpenAI fallback: {'enabled' if OPENAI_API_KEY else 'disabled'}"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    prompt = (update.message.text or "").strip()
    if not prompt:
        return

    await update.message.reply_text("…thinking…")

    # Gemini primary
    reply = gemini_generate(prompt)

    # If Gemini fails, optionally fall back to OpenAI
    if reply.startswith("❌") and OPENAI_API_KEY:
        reply = openai_responses(prompt)

    # Update history only if we got a normal reply
    if not reply.startswith("❌"):
        HISTORY.append({"role": "user", "text": prompt})
        HISTORY.append({"role": "model", "text": reply})
        # trim
        if len(HISTORY) > 2 * MAX_TURNS:
            HISTORY[:] = HISTORY[-2 * MAX_TURNS :]

    # Telegram length guard
    if len(reply) > 3800:
        reply = reply[:3800] + "\n\n[truncated]"
    await update.message.reply_text(reply)

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first.")
    if ALLOWED_USER_ID == 0:
        print("WARNING: ALLOWED_USER_ID not set. Anyone can message your bot.")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
