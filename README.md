# Termux OpenClaw Agent

A lightweight Telegram bot server for Android/Termux setup that can:

- chat via Gemini (with OpenAI fallback),
- run phone/network checks (battery, Wi-Fi, IP, ping),
- fetch external (local weather, GitHub information, futbol fixtures),
- and pull TTC transit arrivals (including intersection-based lookups).
- all with a lot better accuracy than traditional llm interaction
- a lot more efficient than directly interacting with these tools or apis


## Info

`termuxscript.py` is a single-file Telegram assistant with:

- command handlers (`/status`, `/wifi`, `/battery`, `/ttc`, etc.),
- short in-memory chat history,
- basic rate limiting,
- optional allowlist lock (`ALLOWED_USER_ID`),
- and safe mode controls for riskier operations.

## Requirements

- Python 3.10+ (3.11 recommended)
- A Telegram bot token (from BotFather)
- Termux + Termux:API app (for device-level commands)
- Python packages:
  - `python-telegram-bot`
  - `requests`
  - `protobuf`
  - `gtfs-realtime-bindings`

## Setup (quick)

1. Clone this repo and enter it.
2. Create a virtual env and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install python-telegram-bot requests protobuf gtfs-realtime-bindings
```

3. Export env vars (minimum):

```bash
export TELEGRAM_BOT_TOKEN="your_bot_token"
export ALLOWED_USER_ID="123456789"
export GEMINI_API_KEY="your_gemini_key"
```

4. Optional env vars:

```bash
export GEMINI_MODEL="gemini-2.5-flash"
export OPENAI_API_KEY="your_openai_key"          # fallback only
export OPENAI_MODEL="gpt-5-mini"
export GITHUB_TOKEN="ghp_..."
export FOOTBALL_DATA_TOKEN="..."
export AGENT_ADMIN_PIN="set_a_strong_pin"
export SAFE_MODE="0"                              # 1=true/on, 0=false/off
export LOG_PATH="bot.log"
```

5. Run:

```bash
python3 termuxscript.py
```

## Allowlist (important)

The bot can be locked to one Telegram account via `ALLOWED_USER_ID`.

- If `ALLOWED_USER_ID` is set to your Telegram user ID, only you can use the bot.
- If `ALLOWED_USER_ID=0` or unset, anyone who finds your bot can message it.

Get your user ID quickly by running the bot and sending `/whoami` from your account.

## Safe mode + risky ops

- `SAFE_MODE=1` blocks external tools (weather/stock/GitHub/soccer/TTC).
- `/safe on|off` can toggle safe mode while running.
- `/restart <PIN>` only works when:
  - safe mode is OFF,
  - and `AGENT_ADMIN_PIN` is configured + correct.

## TTC notes

`/ttc` works with a stop ID directly.  
For intersection-style lookups, add `ttc_stops.json` next to `termuxscript.py`:

```json
[
  {
    "stop_id": "1234",
    "stop_name": "Bathurst St at Bloor St West",
    "stop_lat": 43.665,
    "stop_lon": -79.411
  }
]
```

Without that file, intersection geocoding still works, but stop matching won’t.

## Useful commands

- `/start` - intro + command groups
- `/help` - full command list
- `/status` - uptime, device, Wi-Fi, battery, model info
- `/ask <question>` - model picks one tool and summarizes
- `/wifi`, `/battery`, `/ip`, `/ping 1.1.1.1`
- `/weather Toronto`, `/stock AAPL`
- `/gh me`, `/gh repo owner/repo`
- `/soccer pl fixtures|results|table`
- `/ttc 12345`, `/ttc "Bloor & Bathurst"`
- `/ttcgo "Bloor & Bathurst" N on "Bathurst"`

## Troubleshooting

- **`termux-*` commands fail**: install/open Termux:API app and grant permissions.
- **No Telegram response**: verify `TELEGRAM_BOT_TOKEN`, then check `bot.log`.
- **Import errors**: ensure dependencies are installed in the active venv.
- **TTC intersection mode empty**: add a valid `ttc_stops.json`.
