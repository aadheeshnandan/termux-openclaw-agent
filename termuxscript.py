import os
import time
import json
import re
import math
import platform
import subprocess
from dataclasses import dataclass
from collections import deque, defaultdict
from typing import Callable, Dict, Any, Tuple, Optional, List

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# GTFS-RT protobuf bindings
from google.transit import gtfs_realtime_pb2


# =============================================================================
# Config
# =============================================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

# Gemini primary
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# OpenAI fallback
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")

# Optional integrations
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
FOOTBALL_DATA_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN", "")

# Admin PIN for dangerous ops (/restart). Strongly recommended.
AGENT_ADMIN_PIN = os.environ.get("AGENT_ADMIN_PIN", "")

# Safe mode: blocks external tools + process ops when ON
SAFE_MODE = os.environ.get("SAFE_MODE", "0").strip().lower() in ("1", "true", "on", "yes")

START_TIME = time.time()

# Chat memory (short, RAM)
HISTORY: List[Dict[str, str]] = []
MAX_TURNS = 8

# Logs
LOG_PATH = os.environ.get("LOG_PATH", "bot.log")
LOG_RING = deque(maxlen=300)

TG_MAX = 3800

# Rate limits
LAST_HIT_TS = defaultdict(lambda: 0.0)  # uid -> ts
HITS_HOURLY = defaultdict(lambda: deque())  # uid -> deque[timestamps]
COOLDOWN_SECONDS = 1.2
HOURLY_LIMIT = 120

# TTC endpoints (official GTFS-RT base)
TTC_GTFSRT_BASE = "https://bustime.ttc.ca/gtfsrt"

# Simple Rubik lookup (2-look OLL/PLL starter set — extend later if you want)
RUBIK = {
    "2look_oll_edges": "F R U R' U' F' (orient edges)",
    "sune": "R U R' U R U2 R'",
    "antisune": "R U2 R' U' R U' R'",
    "2look_pll_ua": "R U' R U R U R U' R' U' R2",
    "2look_pll_ub": "R2 U R U R' U' R' U' R' U R'",
}


# =============================================================================
# Utilities
# =============================================================================

def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str):
    line = f"[{_now_str()}] {msg}"
    LOG_RING.appendleft(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def run(cmd: str, timeout_s: float = 4.0) -> Tuple[str, str]:
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, text=True, timeout=timeout_s)
        return out.strip(), ""
    except subprocess.CalledProcessError as e:
        return "", (e.output or str(e))[:800]
    except Exception as e:
        return "", str(e)[:800]

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    u = update.effective_user
    return bool(u and u.id == ALLOWED_USER_ID)

def rate_limit_ok(update: Update) -> Tuple[bool, str]:
    u = update.effective_user
    uid = u.id if u else 0
    now = time.time()

    if now - LAST_HIT_TS[uid] < COOLDOWN_SECONDS:
        return False, "⏳ Cooldown — try again in a second."

    dq = HITS_HOURLY[uid]
    one_hour_ago = now - 3600
    while dq and dq[0] < one_hour_ago:
        dq.popleft()
    if len(dq) >= HOURLY_LIMIT:
        return False, "🚦 Hourly limit reached. Try later."

    LAST_HIT_TS[uid] = now
    dq.append(now)
    return True, ""

def tg_trim(s: str) -> str:
    if len(s) > TG_MAX:
        return s[:TG_MAX] + "\n\n[truncated]"
    return s

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    # bearing from point1 to point2 (0=N, 90=E)
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    brng = math.degrees(math.atan2(y, x))
    return (brng + 360) % 360

def bearing_to_cardinal(b: float) -> str:
    # 4-way
    if b >= 315 or b < 45:
        return "N"
    if 45 <= b < 135:
        return "E"
    if 135 <= b < 225:
        return "S"
    return "W"


# =============================================================================
# LLM calls
# =============================================================================

def gemini_generate(prompt: str) -> str:
    if not GEMINI_API_KEY:
        return "❌ GEMINI_API_KEY not set."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}

    contents = []
    for turn in HISTORY[-MAX_TURNS:]:
        role = "user" if turn["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": turn["text"]}]})
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    r = requests.post(url, headers=headers, json={"contents": contents}, timeout=60)
    if r.status_code != 200:
        return f"❌ Gemini HTTP {r.status_code}: {r.text[:400]}"

    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return f"❌ Gemini parse error: {str(data)[:400]}"

def openai_responses(prompt: str) -> str:
    if not OPENAI_API_KEY:
        return "❌ OPENAI_API_KEY not set (fallback unavailable)."

    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    body = {"model": OPENAI_MODEL, "input": prompt}

    r = requests.post(url, headers=headers, json=body, timeout=60)
    if r.status_code != 200:
        return f"❌ OpenAI HTTP {r.status_code}: {r.text[:400]}"

    data = r.json()
    if data.get("output_text"):
        return data["output_text"]

    try:
        return data["output"][0]["content"][0]["text"]
    except Exception:
        return f"❌ OpenAI parse error: {str(data)[:400]}"

def llm(prompt: str) -> str:
    out = gemini_generate(prompt)
    if out.startswith("❌") and OPENAI_API_KEY:
        out = openai_responses(prompt)
    return out


# =============================================================================
# Tools framework
# =============================================================================

@dataclass
class Tool:
    name: str
    help: str
    is_external: bool
    handler: Callable[[str], Dict[str, Any]]
    needs_env: Tuple[str, ...] = ()

TOOLS: Dict[str, Tool] = {}

def tool_allowed(t: Tool) -> Tuple[bool, str]:
    if SAFE_MODE and t.is_external:
        return False, "🛡️ Safe mode is ON. External tools are disabled."
    for k in t.needs_env:
        if not os.environ.get(k, ""):
            return False, f"🔑 Missing env: {k}"
    return True, ""

def render_json(obj: Dict[str, Any]) -> str:
    s = json.dumps(obj, indent=2, ensure_ascii=False)
    return tg_trim(f"```json\n{s}\n```")

def summarize_with_llm(user_q: str, tool_name: str, tool_result: Dict[str, Any]) -> str:
    prompt = (
        "Summarize the tool output for a Telegram user. Be compact and practical.\n\n"
        f"User:\n{user_q}\n\nTool: {tool_name}\n"
        f"JSON:\n{json.dumps(tool_result, ensure_ascii=False)}\n\n"
        "Return plain text (no code fences)."
    )
    return llm(prompt)

def plan_one_tool(user_text: str) -> Dict[str, Any]:
    tool_list = "\n".join([f"- {t.name}: {t.help}" for t in TOOLS.values()])
    prompt = (
        "Pick at most ONE tool to answer the user's request.\n"
        "Output ONLY valid JSON.\n\n"
        f"TOOLS:\n{tool_list}\n\n"
        "Rules:\n"
        "- If tool needed: {\"mode\":\"tool\",\"tool\":\"<name>\",\"args\":\"<string>\"}\n"
        "- Else: {\"mode\":\"final\",\"text\":\"<answer>\"}\n"
        "- Prefer device tools for device questions.\n"
        "- Prefer /ttc for transit questions, /soccer for fixtures/tables.\n\n"
        f"USER:\n{user_text}\n"
    )
    raw = llm(prompt)
    try:
        j = json.loads(raw)
        if j.get("mode") in ("tool", "final"):
            return j
    except Exception:
        pass
    return {"mode": "final", "text": raw}


# =============================================================================
# Device tools
# =============================================================================

def tool_battery(_: str) -> Dict[str, Any]:
    out, err = run("termux-battery-status", timeout_s=3)
    if not out:
        return {"ok": False, "error": "termux-battery-status failed", "detail": err or "Termux:API not installed?"}
    try:
        return {"ok": True, "battery": json.loads(out)}
    except Exception:
        return {"ok": True, "battery_raw": out[:800]}

def tool_wifi(_: str) -> Dict[str, Any]:
    out, err = run("termux-wifi-connectioninfo", timeout_s=3)
    if not out:
        ip, _ = run("ip addr show wlan0 2>/dev/null | grep 'inet ' | awk '{print $2}' | head -n 1")
        return {"ok": False, "error": "termux-wifi-connectioninfo failed", "ip": ip or "unknown", "detail": err or "Termux:API not installed?"}
    try:
        return {"ok": True, "wifi": json.loads(out)}
    except Exception:
        return {"ok": True, "wifi_raw": out[:800]}

def tool_ip(_: str) -> Dict[str, Any]:
    ip, _ = run("ip addr show wlan0 2>/dev/null | grep 'inet ' | awk '{print $2}' | head -n 1")
    return {"ok": True, "ip": ip or "unknown"}

def tool_ping(arg: str) -> Dict[str, Any]:
    host = (arg or "").strip()
    if not host:
        return {"ok": False, "error": "usage: /ping <host>"}
    if not re.fullmatch(r"[A-Za-z0-9\.\-]{1,253}", host):
        return {"ok": False, "error": "invalid host"}
    out, err = run(f"ping -c 3 -W 2 {host}", timeout_s=8)
    if not out:
        return {"ok": False, "error": "ping failed", "detail": err}
    return {"ok": True, "result": "\n".join(out.splitlines()[-6:])}


# =============================================================================
# Weather + Stock (no-key quick wins)
# =============================================================================

def tool_weather(arg: str) -> Dict[str, Any]:
    city = (arg or "").strip()
    if not city:
        return {"ok": False, "error": "usage: /weather <city>"}

    geo = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "en", "format": "json"},
        timeout=20,
    )
    if geo.status_code != 200:
        return {"ok": False, "error": f"geocode HTTP {geo.status_code}"}
    gj = geo.json()
    if not gj.get("results"):
        return {"ok": False, "error": "city not found"}

    r0 = gj["results"][0]
    lat, lon = r0["latitude"], r0["longitude"]

    fc = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "timezone": "auto",
        },
        timeout=20,
    )
    if fc.status_code != 200:
        return {"ok": False, "error": f"forecast HTTP {fc.status_code}"}
    return {"ok": True, "place": {"name": r0.get("name"), "country": r0.get("country")}, "forecast": fc.json()}

def tool_stock(arg: str) -> Dict[str, Any]:
    sym = (arg or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "usage: /stock <symbol>"}
    if not re.fullmatch(r"[A-Z0-9\.\-]{1,12}", sym):
        return {"ok": False, "error": "invalid symbol"}

    def fetch(s):
        return requests.get(
            "https://stooq.com/q/l/",
            params={"s": s.lower(), "f": "sd2t2ohlcv", "h": "", "e": "csv"},
            timeout=20,
        )

    r = fetch(sym)
    if r.status_code != 200 or "N/D" in r.text:
        r2 = fetch(sym + ".US")
        if r2.status_code == 200 and "N/D" not in r2.text:
            r = r2

    if r.status_code != 200:
        return {"ok": False, "error": f"stooq HTTP {r.status_code}"}

    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return {"ok": False, "error": "no data"}
    header = lines[0].split(",")
    vals = lines[1].split(",")
    return {"ok": True, "symbol": sym, "data": dict(zip(header, vals))}


# =============================================================================
# GitHub (authenticated)
# =============================================================================

def tool_github(arg: str) -> Dict[str, Any]:
    if not GITHUB_TOKEN:
        return {"ok": False, "error": "GITHUB_TOKEN not set"}

    a = (arg or "").strip()
    if not a:
        return {"ok": False, "error": "usage: /gh me | /gh repo owner/repo"}

    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

    if a == "me":
        r = requests.get("https://api.github.com/user", headers=headers, timeout=20)
        if r.status_code != 200:
            return {"ok": False, "error": f"github HTTP {r.status_code}", "detail": r.text[:300]}
        u = r.json()
        return {"ok": True, "me": {"login": u.get("login"), "name": u.get("name"), "public_repos": u.get("public_repos"), "followers": u.get("followers")}}

    m = re.match(r"repo\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$", a)
    if not m:
        return {"ok": False, "error": "usage: /gh repo owner/repo"}
    repo = m.group(1)

    r = requests.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=20)
    if r.status_code != 200:
        return {"ok": False, "error": f"github HTTP {r.status_code}", "detail": r.text[:300]}
    j = r.json()
    return {"ok": True, "repo": repo, "info": {"description": j.get("description"), "stars": j.get("stargazers_count"), "forks": j.get("forks_count"), "open_issues": j.get("open_issues_count"), "default_branch": j.get("default_branch"), "updated_at": j.get("updated_at")}}


# =============================================================================
# Soccer (football-data.org) — cheap & reliable fixtures/results/tables
# =============================================================================

COMP_CODES = {
    "pl": "PL",   # Premier League
    "cl": "CL",   # Champions League
    "pd": "PD",   # La Liga
    "wc": "WC",   # World Cup
}

def fd_get(path: str, params: Optional[Dict[str, str]] = None) -> requests.Response:
    headers = {"X-Auth-Token": FOOTBALL_DATA_TOKEN}
    return requests.get(f"https://api.football-data.org/v4{path}", headers=headers, params=params or {}, timeout=30)

def tool_soccer(arg: str) -> Dict[str, Any]:
    """
    /soccer <pl|cl|pd|wc> <fixtures|results|table>
    """
    if not FOOTBALL_DATA_TOKEN:
        return {"ok": False, "error": "FOOTBALL_DATA_TOKEN not set"}

    parts = (arg or "").strip().lower().split()
    if len(parts) < 2:
        return {"ok": False, "error": "usage: /soccer <pl|cl|pd|wc> <fixtures|results|table>"}

    comp = COMP_CODES.get(parts[0])
    mode = parts[1]
    if not comp:
        return {"ok": False, "error": "unknown competition (use pl/cl/pd/wc)"}

    if mode == "table":
        r = fd_get(f"/competitions/{comp}/standings")
        if r.status_code != 200:
            return {"ok": False, "error": f"football-data HTTP {r.status_code}", "detail": r.text[:300]}
        return {"ok": True, "competition": comp, "standings": r.json()}

    # fixtures / results are both matches — we’ll filter by status
    r = fd_get(f"/competitions/{comp}/matches")
    if r.status_code != 200:
        return {"ok": False, "error": f"football-data HTTP {r.status_code}", "detail": r.text[:300]}
    data = r.json()
    matches = data.get("matches", [])

    if mode == "fixtures":
        # upcoming: SCHEDULED / TIMED
        matches = [m for m in matches if (m.get("status") in ("SCHEDULED", "TIMED"))]
        matches = matches[:20]
        return {"ok": True, "competition": comp, "fixtures": matches}

    if mode == "results":
        # finished: FINISHED
        matches = [m for m in matches if (m.get("status") == "FINISHED")]
        matches = matches[:20]
        return {"ok": True, "competition": comp, "results": matches}

    return {"ok": False, "error": "mode must be fixtures|results|table"}


# =============================================================================
# Rubik
# =============================================================================

def tool_rubik(arg: str) -> Dict[str, Any]:
    key = (arg or "").strip().lower().replace(" ", "_")
    if not key:
        return {"ok": True, "cases": sorted(RUBIK.keys())}
    if key in RUBIK:
        return {"ok": True, "case": key, "alg": RUBIK[key]}
    # fuzzy contains
    hits = [k for k in RUBIK.keys() if key in k]
    return {"ok": False, "error": "case not found", "suggestions": hits[:10]}


# =============================================================================
# TTC (GTFS-RT) — stop ID + intersection + direction/corridor heuristic
# =============================================================================

# Cache geocode + stop searches
GEO_CACHE = {}  # query -> (lat, lon, display)
STOP_CACHE_TS = 0.0
STOPS = []  # list of dict: {"stop_id","stop_name","stop_lat","stop_lon"}

def load_stops_static():
    """
    Minimal approach:
    - You provide a local stops file (CSV-like) OR we try to keep it simple and not bundle full TTC static GTFS parsing here.
    Practical compromise:
    - If you drop a file at ~/tg-agent/ttc_stops.json, we load it.
      Format: [{"stop_id":"...", "stop_name":"...", "stop_lat":43.6, "stop_lon":-79.4}, ...]
    """
    global STOPS, STOP_CACHE_TS
    path = os.path.join(os.path.dirname(__file__), "ttc_stops.json")
    try:
        st = os.stat(path)
        if st.st_mtime <= STOP_CACHE_TS and STOPS:
            return
        with open(path, "r", encoding="utf-8") as f:
            STOPS = json.load(f)
        STOP_CACHE_TS = st.st_mtime
        log(f"TTC: loaded {len(STOPS)} stops from ttc_stops.json")
    except Exception as e:
        # No stops file — intersection mode will still geocode, but won’t find stops.
        STOPS = []
        log(f"TTC: stops file not loaded ({e})")

def geocode_nominatim(q: str) -> Optional[Tuple[float, float, str]]:
    q = q.strip()
    if not q:
        return None
    if q in GEO_CACHE:
        return GEO_CACHE[q]
    # Add Toronto bias if user doesn't specify
    if "toronto" not in q.lower():
        q2 = q + ", Toronto, Ontario"
    else:
        q2 = q
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": q2, "format": "json", "limit": 1},
        headers={"User-Agent": "tg-tablet-agent/1.0"},
        timeout=25,
    )
    if r.status_code != 200:
        return None
    arr = r.json()
    if not arr:
        return None
    lat = float(arr[0]["lat"])
    lon = float(arr[0]["lon"])
    disp = arr[0].get("display_name", q2)
    GEO_CACHE[q] = (lat, lon, disp)
    return GEO_CACHE[q]

def fetch_gtfsrt_trip_updates() -> gtfs_realtime_pb2.FeedMessage:
    url = f"{TTC_GTFSRT_BASE}/trips"
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    return feed

def arrivals_for_stop(stop_id: str, limit: int = 5) -> Dict[str, Any]:
    stop_id = stop_id.strip()
    if not stop_id:
        return {"ok": False, "error": "missing stop_id"}

    feed = fetch_gtfsrt_trip_updates()
    now = int(time.time())
    hits = []

    for ent in feed.entity:
        if not ent.trip_update:
            continue
        tu = ent.trip_update
        route_id = tu.trip.route_id
        trip_id = tu.trip.trip_id

        for stu in tu.stop_time_update:
            if stu.stop_id != stop_id:
                continue
            # prefer arrival time, fallback departure
            t = None
            if stu.arrival and stu.arrival.time:
                t = int(stu.arrival.time)
            elif stu.departure and stu.departure.time:
                t = int(stu.departure.time)
            if not t:
                continue
            mins = max(0, int((t - now) / 60))
            heads = (stu.stop_id, route_id, trip_id, mins)
            hits.append(heads)

    hits.sort(key=lambda x: x[3])
    hits = hits[:limit]
    return {"ok": True, "stop_id": stop_id, "arrivals": [{"route": r, "trip": trip, "mins": mins} for (_, r, trip, mins) in hits]}

def find_nearby_stops(lat: float, lon: float, radius_m: float = 450.0, maxn: int = 6) -> List[Dict[str, Any]]:
    load_stops_static()
    if not STOPS:
        return []
    scored = []
    for s in STOPS:
        d = haversine_m(lat, lon, float(s["stop_lat"]), float(s["stop_lon"]))
        if d <= radius_m:
            scored.append((d, s))
    scored.sort(key=lambda x: x[0])
    return [{"distance_m": round(d, 1), **s} for d, s in scored[:maxn]]

def parse_ttcgo_args(s: str) -> Dict[str, str]:
    """
    /ttcgo "<intersection>" <N|S|E|W> on "<road>"
    /ttcgo "<intersection>" toward "<destination>"
    """
    out = {"intersection": "", "dir": "", "road": "", "toward": ""}
    # first quoted chunk = intersection
    m = re.search(r"\"([^\"]+)\"", s)
    if m:
        out["intersection"] = m.group(1).strip()
        rest = (s[m.end():] or "").strip()
    else:
        # fallback: first token chunk until dir keyword
        rest = s.strip()

    # toward "<dest>"
    m2 = re.search(r"toward\s+\"([^\"]+)\"", rest, flags=re.I)
    if m2:
        out["toward"] = m2.group(1).strip()

    # dir N/S/E/W
    m3 = re.search(r"\b(N|S|E|W|north|south|east|west)\b", rest, flags=re.I)
    if m3:
        d = m3.group(1).upper()
        out["dir"] = {"NORTH":"N","SOUTH":"S","EAST":"E","WEST":"W"}.get(d, d)

    # on "<road>"
    m4 = re.search(r"on\s+\"([^\"]+)\"", rest, flags=re.I)
    if m4:
        out["road"] = m4.group(1).strip()
    else:
        # unquoted "on Bathurst"
        m5 = re.search(r"on\s+([A-Za-z0-9\.\- ]{2,})$", rest, flags=re.I)
        if m5:
            out["road"] = m5.group(1).strip()

    return out

def choose_stop_by_dir_and_road(stops: List[Dict[str, Any]], desired_dir: str, road: str) -> List[Dict[str, Any]]:
    if not stops:
        return []
    desired_dir = (desired_dir or "").upper()
    road = (road or "").lower().strip()

    def dir_score(name: str) -> int:
        n = name.lower()
        # common suffixes in stop naming: "NB", "SB", "EB", "WB", "Northbound", etc.
        if not desired_dir:
            return 0
        if desired_dir == "N" and (" nb" in n or "northbound" in n):
            return -50
        if desired_dir == "S" and (" sb" in n or "southbound" in n):
            return -50
        if desired_dir == "E" and (" eb" in n or "eastbound" in n):
            return -50
        if desired_dir == "W" and (" wb" in n or "westbound" in n):
            return -50
        return 0

    def road_score(name: str) -> int:
        if not road:
            return 0
        return -30 if road in name.lower() else 0

    ranked = []
    for s in stops:
        name = s.get("stop_name", "")
        score = s.get("distance_m", 9999) + dir_score(name) + road_score(name)
        ranked.append((score, s))
    ranked.sort(key=lambda x: x[0])
    return [s for _, s in ranked[:3]]

def tool_ttc(arg: str) -> Dict[str, Any]:
    """
    /ttc <stop_id>
    /ttc "<intersection or landmark>"
    """
    a = (arg or "").strip()
    if not a:
        return {"ok": False, "error": "usage: /ttc <stop_id> OR /ttc \"Bloor & Bathurst\""}
    # stop id numeric?
    if re.fullmatch(r"\d{3,8}", a):
        return arrivals_for_stop(a, limit=5)

    # intersection/landmark
    g = geocode_nominatim(a)
    if not g:
        return {"ok": False, "error": "geocode failed"}
    lat, lon, disp = g
    nearby = find_nearby_stops(lat, lon)
    if not nearby:
        return {"ok": False, "error": "no stops loaded for intersection mode", "hint": "add ttc_stops.json in tg-agent directory", "geocode": {"lat": lat, "lon": lon, "display": disp}}

    # show arrivals for top 1-2 stops
    top = nearby[:2]
    results = []
    for s in top:
        res = arrivals_for_stop(str(s["stop_id"]), limit=5)
        results.append({"stop": s, "arrivals": res.get("arrivals", [])})
    return {"ok": True, "geocode": {"lat": lat, "lon": lon, "display": disp}, "stops": nearby[:6], "top_arrivals": results}

def tool_ttcgo(arg: str) -> Dict[str, Any]:
    """
    /ttcgo "<intersection>" N on "Bathurst"
    /ttcgo "<intersection>" toward "Union Station"
    """
    p = parse_ttcgo_args(arg or "")
    inter = p["intersection"]
    if not inter:
        return {"ok": False, "error": "usage: /ttcgo \"Bloor & Bathurst\" N on \"Bathurst\"  OR  /ttcgo \"Yonge & Eglinton\" toward \"Union Station\""}

    g1 = geocode_nominatim(inter)
    if not g1:
        return {"ok": False, "error": "intersection geocode failed"}
    lat1, lon1, disp1 = g1

    desired_dir = p["dir"]
    if p["toward"]:
        g2 = geocode_nominatim(p["toward"])
        if g2:
            lat2, lon2, _ = g2
            desired_dir = bearing_to_cardinal(bearing_deg(lat1, lon1, lat2, lon2))

    nearby = find_nearby_stops(lat1, lon1)
    if not nearby:
        return {"ok": False, "error": "no stops loaded for intersection mode", "hint": "add ttc_stops.json", "geocode": {"lat": lat1, "lon": lon1, "display": disp1}}

    chosen = choose_stop_by_dir_and_road(nearby, desired_dir, p["road"])
    out = []
    for s in chosen:
        arr = arrivals_for_stop(str(s["stop_id"]), limit=5).get("arrivals", [])
        out.append({"stop": s, "arrivals": arr})

    return {
        "ok": True,
        "intersection": {"query": inter, "display": disp1, "lat": lat1, "lon": lon1},
        "inferred_dir": desired_dir or "",
        "road_filter": p["road"] or "",
        "candidates": nearby[:6],
        "selected": out,
    }


# =============================================================================
# Ops tools (safe, whitelisted)
# =============================================================================

def tool_ps(_: str) -> Dict[str, Any]:
    out, err = run("ps aux | grep -E \"python.*bot\\.py\" | grep -v grep", timeout_s=5)
    if not out:
        return {"ok": False, "error": "not running (or ps failed)", "detail": err}
    return {"ok": True, "ps": out}

def tool_tail(arg: str) -> Dict[str, Any]:
    n = 50
    if arg.strip():
        try:
            n = max(1, min(200, int(arg.strip())))
        except Exception:
            n = 50
    out, err = run(f"tail -n {n} {LOG_PATH}", timeout_s=5)
    if not out:
        return {"ok": False, "error": "tail failed", "detail": err}
    return {"ok": True, "tail": out}

def tool_restart(arg: str) -> Dict[str, Any]:
    """
    Very restricted restart:
    - requires AGENT_ADMIN_PIN
    - safe mode must be OFF
    - runs fixed script ~/.termux/boot/start-agent.sh if present, else uses nohup python bot.py
    """
    if SAFE_MODE:
        return {"ok": False, "error": "safe mode is ON; restart disabled"}
    if not AGENT_ADMIN_PIN:
        return {"ok": False, "error": "AGENT_ADMIN_PIN not set; restart disabled"}
    pin = (arg or "").strip()
    if pin != AGENT_ADMIN_PIN:
        return {"ok": False, "error": "invalid PIN"}

    # kill old
    run("pkill -f \"python bot.py\"", timeout_s=6)

    script = os.path.expanduser("~/.termux/boot/start-agent.sh")
    if os.path.exists(script):
        out, err = run(script, timeout_s=8)
        return {"ok": True, "restart": "called start-agent.sh", "stdout": out[:300], "stderr": err[:300]}
    else:
        out, err = run(f"nohup python bot.py >> {LOG_PATH} 2>&1 &", timeout_s=6)
        return {"ok": True, "restart": "nohup python bot.py", "stdout": out[:300], "stderr": err[:300]}


# =============================================================================
# Register tools
# =============================================================================

TOOLS.update({
    "battery": Tool("battery", "Battery status (Termux:API)", is_external=False, handler=tool_battery),
    "wifi": Tool("wifi", "Wi-Fi info (SSID/IP/RSSI/link speed) (Termux:API)", is_external=False, handler=tool_wifi),
    "ip": Tool("ip", "Wi-Fi IP (fallback)", is_external=False, handler=tool_ip),
    "ping": Tool("ping", "Ping a host: /ping 1.1.1.1", is_external=False, handler=tool_ping),

    "weather": Tool("weather", "Weather by city (Open-Meteo, no key): /weather Toronto", is_external=True, handler=tool_weather),
    "stock": Tool("stock", "Stock quote (Stooq, no key): /stock AAPL", is_external=True, handler=tool_stock),

    "gh": Tool("gh", "GitHub: /gh me | /gh repo owner/repo (needs GITHUB_TOKEN)", is_external=True, handler=tool_github, needs_env=("GITHUB_TOKEN",)),
    "soccer": Tool("soccer", "Soccer: /soccer pl fixtures|results|table (needs FOOTBALL_DATA_TOKEN)", is_external=True, handler=tool_soccer, needs_env=("FOOTBALL_DATA_TOKEN",)),
    "rubik": Tool("rubik", "Rubik lookup: /rubik sune  (or /rubik to list)", is_external=False, handler=tool_rubik),

    "ttc": Tool("ttc", "TTC arrivals: /ttc 12345 OR /ttc \"Bloor & Bathurst\" (needs local ttc_stops.json for intersection mode)", is_external=True, handler=tool_ttc),
    "ttcgo": Tool("ttcgo", "TTC direction/corridor: /ttcgo \"Bloor & Bathurst\" N on \"Bathurst\"  OR  toward \"Union Station\"", is_external=True, handler=tool_ttcgo),

    "ps": Tool("ps", "Show whether bot is running", is_external=False, handler=tool_ps),
    "tail": Tool("tail", "Tail bot.log: /tail 50", is_external=False, handler=tool_tail),
    "restart": Tool("restart", "Restart bot (requires PIN): /restart <PIN> (safe mode must be OFF)", is_external=False, handler=tool_restart),
})


# =============================================================================
# Telegram handlers
# =============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "✅ Tablet agent online.\n\n"
        "Core: /help /status /log /safe on|off /clear /whoami\n"
        "Hands: /wifi /battery /ttc /ttcgo /gh /soccer /stock /weather\n"
        "Ops: /ps /tail /restart <PIN>\n"
        "Or just send a message to chat.\n"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    lines = [
        "🧭 Commands",
        "/status — health + device + models",
        "/help — show commands",
        "/whoami — show your Telegram user id",
        "/clear — clear RAM chat memory",
        "/log [n] — show last n log lines",
        "/safe on|off — safe mode toggle",
        "/ask <question> — model picks ONE tool and summarizes",
        "",
        "🛠️ Tools",
    ]
    for t in TOOLS.values():
        ext = "external" if t.is_external else "local"
        req = f" (needs {', '.join(t.needs_env)})" if t.needs_env else ""
        lines.append(f"/{t.name} — {t.help} [{ext}]{req}")
    await update.message.reply_text("\n".join(lines))

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

async def safe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SAFE_MODE
    if not is_allowed(update):
        return
    ok, msg = rate_limit_ok(update)
    if not ok:
        await update.message.reply_text(msg)
        return

    arg = " ".join(context.args).strip().lower()
    if arg in ("on", "1", "true", "yes"):
        SAFE_MODE = True
    elif arg in ("off", "0", "false", "no"):
        SAFE_MODE = False
    else:
        await update.message.reply_text(f"Safe mode is currently: {'ON' if SAFE_MODE else 'OFF'}\nUsage: /safe on|off")
        return
    log(f"SAFE_MODE set to {SAFE_MODE}")
    await update.message.reply_text(f"🛡️ Safe mode is now: {'ON' if SAFE_MODE else 'OFF'}")

async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    n = 20
    if context.args:
        try:
            n = max(1, min(120, int(context.args[0])))
        except Exception:
            n = 20
    items = list(LOG_RING)[:n]
    if not items:
        await update.message.reply_text("No logs yet.")
        return
    await update.message.reply_text(tg_trim("\n".join(items)))

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    ok, msg = rate_limit_ok(update)
    if not ok:
        await update.message.reply_text(msg)
        return

    uptime = int(time.time() - START_TIME)

    wifi = tool_wifi("")
    batt = tool_battery("")
    ip = tool_ip("")

    wifi_line = "unknown"
    if wifi.get("ok") and "wifi" in wifi:
        w = wifi["wifi"]
        ssid = w.get("ssid") or "?"
        ipw = w.get("ip") or w.get("ip_address") or ip.get("ip", "?")
        rssi = w.get("rssi")
        link = w.get("link_speed_mbps") or w.get("linkSpeed") or w.get("link_speed")
        bits = [f"SSID={ssid}", f"IP={ipw}"]
        if rssi is not None:
            bits.append(f"RSSI={rssi}dBm")
        if link is not None:
            bits.append(f"Link={link}")
        wifi_line = " | ".join(bits)
    else:
        wifi_line = f"IP={ip.get('ip','unknown')} (no Termux:API?)"

    batt_line = "termux-api not installed"
    if batt.get("ok") and "battery" in batt:
        b = batt["battery"]
        batt_line = f"{b.get('percentage')}% | {b.get('status')} | {b.get('temperature')}°C"

    await update.message.reply_text(
        "🟢 OK\n"
        f"Uptime: {uptime}s\n"
        f"Device: {platform.platform()}\n"
        f"Safe mode: {'ON' if SAFE_MODE else 'OFF'}\n"
        f"Wi-Fi: {wifi_line}\n"
        f"Battery: {batt_line}\n"
        f"Gemini model: {GEMINI_MODEL}\n"
        f"OpenAI fallback: {'enabled' if OPENAI_API_KEY else 'disabled'}\n"
        f"Log: {LOG_PATH}"
    )

async def tool_command(update: Update, context: ContextTypes.DEFAULT_TYPE, tool_name: str):
    if not is_allowed(update):
        return
    ok, msg = rate_limit_ok(update)
    if not ok:
        await update.message.reply_text(msg)
        return

    tool = TOOLS.get(tool_name)
    if not tool:
        await update.message.reply_text("Unknown tool.")
        return

    allowed, why = tool_allowed(tool)
    if not allowed:
        await update.message.reply_text(why)
        return

    # process-control commands (restart) are extra sensitive: require safe mode OFF (already in tool_restart)
    arg = " ".join(context.args).strip()
    log(f"tool:{tool_name} arg='{arg}'")

    try:
        res = tool.handler(arg)
    except Exception as e:
        log(f"tool:{tool_name} ERROR {e}")
        res = {"ok": False, "error": f"exception: {str(e)[:200]}"}

    await update.message.reply_text(render_json(res), parse_mode="Markdown")

async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    ok, msg = rate_limit_ok(update)
    if not ok:
        await update.message.reply_text(msg)
        return

    user_q = " ".join(context.args).strip()
    if not user_q:
        await update.message.reply_text("Usage: /ask <question>")
        return

    await update.message.reply_text("…thinking…")

    plan = plan_one_tool(user_q)
    if plan.get("mode") == "final":
        await update.message.reply_text(tg_trim(plan.get("text", "")))
        return

    tname = (plan.get("tool") or "").strip()
    targs = (plan.get("args") or "").strip()
    tool = TOOLS.get(tname)
    if not tool:
        await update.message.reply_text("Planner selected an unknown tool. Try rephrasing.")
        return

    allowed, why = tool_allowed(tool)
    if not allowed:
        await update.message.reply_text(why)
        return

    log(f"ask->tool:{tname} args='{targs}' q='{user_q[:160]}'")
    try:
        result = tool.handler(targs)
    except Exception as e:
        log(f"ask tool:{tname} ERROR {e}")
        await update.message.reply_text(f"Tool failed: {e}")
        return

    summary = summarize_with_llm(user_q, tname, result)
    await update.message.reply_text(tg_trim(summary))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    ok, msg = rate_limit_ok(update)
    if not ok:
        await update.message.reply_text(msg)
        return

    prompt = (update.message.text or "").strip()
    if not prompt:
        return

    await update.message.reply_text("…thinking…")
    reply = llm(prompt)

    if not reply.startswith("❌"):
        HISTORY.append({"role": "user", "text": prompt})
        HISTORY.append({"role": "model", "text": reply})
        if len(HISTORY) > 2 * MAX_TURNS:
            HISTORY[:] = HISTORY[-2 * MAX_TURNS:]

    await update.message.reply_text(tg_trim(reply))


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first.")
    if ALLOWED_USER_ID == 0:
        print("WARNING: ALLOWED_USER_ID not set. Anyone can message your bot.")

    log("bot starting")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("log", log_cmd))
    app.add_handler(CommandHandler("safe", safe))
    app.add_handler(CommandHandler("ask", ask))

    for name in TOOLS.keys():
        async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE, n=name):
            return await tool_command(update, context, n)
        app.add_handler(CommandHandler(name, _handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
