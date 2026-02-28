# TG Tablet Agent — Docs

An always-on Telegram bot running on an Android tablet (Termux + Termux:Boot).  
It chats (Gemini primary, OpenAI fallback) and can run a small set of **whitelisted tools** (“hands”) for device context + external data.

---

This bot is different from standard inference with ChatGPT/Gemini because it can:

- **Read device context** (battery, Wi-Fi SSID/IP/RSSI/link speed) from the tablet via Termux:API.
- **Use authenticated APIs** (e.g., GitHub) with *your* tokens.
- Pull **real-time, changing info** (TTC arrivals, soccer fixtures/tables) from explicit tools you control.
- Stay safe by design:
  - allowlist (only your Telegram user id)
  - safe mode (disables external tools)
  - rate limits
  - logs + bounded outputs
  - no arbitrary shell execution

---

## Requirements

### On Android
- Termux (recommended from F-Droid)
- Termux:Boot (from F-Droid)
- Termux:API (from F-Droid) + Termux package `termux-api`

### In Termux
```sh
pkg update -y
pkg install -y python git termux-api wget unzip
pip install -U pip
pip install python-telegram-bot==20.* requests gtfs-realtime-bindings protobuf
```

## Instructions: How to Use the Bot (Operator Guide)

This section is meant to be read end-to-end once. After that, you’ll use it as a reference.

---

### 0) Mental model (how to “think” when using the bot)

There are **three ways** to interact:

1) **Normal chat (no tools)**  
   Just message the bot like a normal assistant.  
   Use this for: explanations, brainstorming, general Q&A.

2) **Direct commands (deterministic tools)**  
   You call a tool explicitly: `/wifi`, `/ttc 12345`, `/soccer pl table`.  
   Use this for: facts that must be correct *right now* (device status, TTC arrivals, tables).

3) **Tool-assisted Q&A (`/ask`)**  
   You ask in natural language and the bot chooses **at most one** tool, runs it, then summarizes.  
   Use this when you *don’t want to remember commands* or want a quick summary.

> Key rule: `/ask` uses **one tool max**. If you need multiple steps, use direct commands.

---

### 1) First-time “smoke test” (prove everything works)

After starting the bot, send these in Telegram:

1) `/status`  
   Confirms uptime, models, safe mode, and basic device info.

2) `/wifi`  
   Should show SSID/IP/RSSI/link speed.  
   If it says Termux:API missing, install the Termux:API Android app + `pkg install termux-api`.

3) `/battery`  
   Confirms battery metrics. Same Termux:API requirement.

4) `/help`  
   Confirms command list is available.

5) (Optional) `/ping 1.1.1.1`  
   Confirms network connectivity from the device.

---

### 2) Safe mode (IMPORTANT)

Safe mode is your kill-switch for external data + “ops” features.

- `/safe on`  
  External tools are blocked. Device tools still work.
- `/safe off`  
  External tools are allowed.

**Recommended habit:**
- Keep safe mode **ON** most of the time.
- Turn safe mode **OFF** only when you need:
  - TTC / soccer / weather / stock / GitHub (external APIs)
  - restart operations (if you enabled them)
- Turn safe mode back **ON** afterwards.

Check current state:
- `/status` (shows Safe mode: ON/OFF)

---

### 3) Command reference (complete)

#### Core
- `/start`  
  Shows quick intro + hints.

- `/help`  
  Lists all supported commands/tools.

- `/status`  
  Health summary: uptime, device, safe mode, Wi-Fi summary, battery summary, models.

- `/whoami`  
  Shows your Telegram numeric user id (useful for allowlisting).

- `/clear`  
  Clears in-RAM chat history (resets conversational context).

- `/log [n]`  
  Shows last `n` in-memory log entries (default 20).  
  Example:
  - `/log`
  - `/log 80`

- `/ask <question>`  
  Lets the model pick **one** tool and then summarize results.  
  Examples:
  - `/ask what’s my wifi strength right now`
  - `/ask next buses at bloor and bathurst`
  - `/ask premier league table`
  - `/ask check my github account stats`

> If `/ask` chooses the wrong tool, use the direct command yourself (below).

---

#### Device tools (local)
These work even in safe mode.

- `/wifi`  
  Shows Wi-Fi SSID, IP, signal strength (RSSI), link speed (if available).

- `/battery`  
  Shows battery percentage, charging status, temperature, health.

- `/ip`  
  Shows IP address fallback (even if Termux:API is missing).

- `/ping <host>`  
  Pings a host 3 times (quick network debug).  
  Examples:
  - `/ping 1.1.1.1`
  - `/ping api.telegram.org`

---

#### TTC tools (external; require safe mode OFF)
TTC has three levels: stop-id, intersection/landmark, and direction/corridor selection.

**A) Stop ID (fastest, always reliable)**
- `/ttc <stop_id>`  
  Example:
  - `/ttc 12345`  
  Output:
  - next 5 arrivals with minutes to arrival + route/trip identifiers (depending on feed detail)

**B) Intersection / landmark**
- `/ttc "<intersection or landmark>"`  
  Examples:
  - `/ttc "Bloor & Bathurst"`
  - `/ttc "Yorkdale Mall"`  
  Output:
  - geocode result (where it thinks you mean)
  - nearest stops (top few)
  - next arrivals for the top stop(s)

> Requires `ttc_stops.json` generated from TTC Surface GTFS (see setup section).

**C) Direction + road corridor / destination**
Use this when you want: “Which side/direction do I stand on?”

- `/ttcgo "<intersection>" <N|S|E|W> on "<road>"`  
  Examples:
  - `/ttcgo "Bloor & Bathurst" N on "Bathurst"`
  - `/ttcgo "Danforth & Pape" W on "Danforth"`

- `/ttcgo "<intersection>" toward "<destination>"`  
  Examples:
  - `/ttcgo "Yonge & Eglinton" toward "Union Station"`
  - `/ttcgo "Bloor & Spadina" toward "High Park"`

Output:
- inferred travel direction (N/S/E/W) if using `toward`
- candidate nearby stops
- selected “best” stop(s) given direction + corridor match
- next 5 arrivals for those selected stops

> `/ttcgo` is not a full route planner. It’s a “pick correct stop platform/direction and show arrivals” assistant.

---

#### Soccer (football-data.org) (external; require safe mode OFF)
Format:
- `/soccer <pl|cl|pd|wc> <fixtures|results|table>`

Competition codes:
- `pl` = Premier League  
- `cl` = Champions League  
- `pd` = La Liga  
- `wc` = World Cup  

Examples:
- `/soccer pl fixtures`  
  Upcoming matches (next chunk returned by API)

- `/soccer pl results`  
  Recent finished matches

- `/soccer cl table`  
  Standings (group/overall depending on API structure)

Notes:
- Requires `FOOTBALL_DATA_TOKEN` in `.env`.
- Output may be raw JSON; use `/ask` for a nicer summary.

---

#### Weather (external; require safe mode OFF)
- `/weather <city>`  
  Examples:
  - `/weather Toronto`
  - `/weather Mississauga`

Returns forecast JSON.  
For a summary:
- `/ask what’s the weather in toronto today`

---

#### Stock (external; require safe mode OFF)
- `/stock <symbol>`  
  Examples:
  - `/stock AAPL`
  - `/stock TSLA`

Returns quote data as JSON.

---

#### GitHub (external; require safe mode OFF)
Requires `GITHUB_TOKEN`.

- `/gh me`  
  Returns your account info (login/name/public repos/followers).

- `/gh repo owner/repo`  
  Example:
  - `/gh repo torvalds/linux`  
  Returns metadata like stars, forks, open issues, updated_at, etc.

---

#### Rubik (local)
- `/rubik`  
  Lists available keys/cases.

- `/rubik <case>`  
  Examples:
  - `/rubik sune`
  - `/rubik antisune`
  - `/rubik 2look_pll_ua`

---

#### Ops / reliability commands (local, but restricted)
These exist to recover the always-on agent without touching the tablet.

- `/ps`  
  Shows whether `python bot.py` is running (process snapshot).

- `/tail [n]`  
  Tails the bot log file (`bot.log`).  
  Examples:
  - `/tail`
  - `/tail 120`

- `/restart <PIN>`  
  Restarts the bot process using a fixed script/flow.  
  Requirements:
  - safe mode must be OFF (`/safe off`)
  - `AGENT_ADMIN_PIN` must be set in `.env`
  - you must provide the correct PIN
  Example flow:
  1) `/safe off`
  2) `/restart <yourPIN>`
  3) `/ps`
  4) `/tail 80`
  5) `/safe on`

> There is no arbitrary command execution. Only whitelisted ops exist.

---

### 4) Recommended usage workflows (how you’ll actually use it)

#### Workflow A — “What’s happening with the tablet?”
1) `/status`
2) `/wifi`
3) `/battery`
4) If issues: `/ping 1.1.1.1` then `/ping api.telegram.org`

#### Workflow B — “Catch a bus now”
1) If you know stop number: `/ttc 12345`
2) If you only know intersection: `/ttc "Bloor & Bathurst"`
3) If you want the correct side/direction:
   - `/ttcgo "Bloor & Bathurst" N on "Bathurst"`  
   OR  
   - `/ttcgo "Yonge & Eglinton" toward "Union Station"`

#### Workflow C — “Quick soccer update”
1) `/soccer pl fixtures` or `/soccer pl table`
2) If you want a clean summary:
   - `/ask summarize premier league table in 5 lines`

#### Workflow D — “Check GitHub quickly”
1) `/gh me`
2) `/gh repo owner/repo`
3) Optional summarization:
   - `/ask tell me what’s notable about this repo’s activity`

#### Workflow E — “Recover the bot without touching the tablet”
1) `/ps`
2) `/tail 120`
3) `/safe off`
4) `/restart <PIN>`
5) `/ps` + `/tail 120`
6) `/safe on`

---

### 5) “How should I choose between /ask and direct commands?”
Use **direct commands** when:
- You need precise output now (TTC arrivals, device stats).
- You’re debugging.
- You want transparency (raw JSON).

Use **/ask** when:
- You want a short summary.
- You don’t remember the command name.
- You want the tool output interpreted (e.g., “is my Wi-Fi weak?”).

---

### 6) What to do if something looks wrong

- If a command returns nothing:
  - check `/status`
  - check `/tail 120`

- If a tool says blocked:
  - `/status` → verify safe mode
  - `/safe off`

- If Termux:API tools fail:
  - confirm Termux:API app installed
  - `pkg install termux-api`
  - run:
    - `termux-battery-status`
    - `termux-wifi-connectioninfo`

- If TTC intersection mode says stops not loaded:
  - confirm `~/tg-agent/ttc_stops.json` exists
  - regenerate using the TTC setup steps

---
