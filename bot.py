import discord
from discord.ext import commands
import requests
import traceback
import re
from datetime import datetime, timedelta, timezone
import asyncio
import os
from datetime import datetime

def to_unix(start_iso: str) -> int:
    dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    return int(dt.timestamp())

# ── cloudscraper (optional, needed for Cloudflare-protected sites) ────────────
try:
    import cloudscraper as _cs_mod
    _CS_AVAILABLE = True
except ImportError:
    _CS_AVAILABLE = False
    print("⚠️  cloudscraper not installed — Cloudflare-protected sites will fail")
    print("    Fix: add 'cloudscraper' to requirements.txt and redeploy")

# ── Constants ─────────────────────────────────────────────────────────────────
BOT_TOKEN             = os.getenv("TOKEN")
POLL_SECS             = int(os.getenv("POLL_SECONDS", "35"))
FOOTER_TAG            = "solve_id:"
FOOTER_FINAL_TAG      = "final_stats_id:"
FOOTER_UPCOMING_TAG   = "ctftime_id:"

# Upcoming auto-post config
UPCOMING_CHANNEL_ID   = os.getenv("UPCOMING_CHANNEL_ID", "").strip() or None
UPCOMING_POLL_MINUTES = int(os.getenv("UPCOMING_POLL_MINUTES", "10"))
UPCOMING_FETCH_COUNT  = int(os.getenv("UPCOMING_FETCH_COUNT", "10"))


# ── Config loader ─────────────────────────────────────────────────────────────
def load_ctfs():
    ctfs = {}
    for i in range(1, 10):
        ch = os.getenv(f"CHANNEL{i}_ID")
        if not ch:
            continue
        ctfs[ch.strip()] = {
            "ctf_name":   os.getenv(f"CTF{i}_NAME",  f"CTF{i}"),
            "ctf_url":    (os.getenv(f"CTF{i}_URL") or "").rstrip("/"),
            "ctfd_token": os.getenv(f"CTF{i}_TOKEN") or None,
            "team_id":    int(os.getenv(f"CTF{i}_TEAM", "0")),
            "end_time":   os.getenv(f"CTF{i}_END_TIME") or None,
        }
    return ctfs


CTF_CONFIGS = load_ctfs()
print("Loaded configs:", CTF_CONFIGS)

# Per-channel memory
channel_posted_ids:   dict[int, set[int]] = {}
channel_final_posted: set[int]            = set()

# Upcoming auto-post memory  (CTFtime event IDs already posted)
upcoming_posted_ids:  set[int]            = set()

# One cloudscraper session per origin (reused across calls)
_cf_sessions: dict[str, object] = {}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=";", intents=intents)


# ── HTTP layer (Cloudflare-aware) ─────────────────────────────────────────────

def _is_cf_block(r) -> bool:
    """Return True if response is a Cloudflare challenge/block."""
    ct = r.headers.get("Content-Type", "")
    if r.status_code in (403, 503) and "html" in ct:
        return True
    if "html" in ct:
        snip = r.text[:400]
        if any(x in snip for x in ("Just a moment", "__cf_chl_opt", "cf-browser-verification", "Cloudflare")):
            return True
    return False


def _cf_session(origin: str):
    if origin not in _cf_sessions:
        if _CS_AVAILABLE:
            _cf_sessions[origin] = _cs_mod.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
        else:
            _cf_sessions[origin] = None
    return _cf_sessions[origin]


def _api_get(ctf_url: str, token: str | None, path: str):
    """
    GET /api/v1{path} on ctf_url.

    Attempt order:
      1. Plain requests WITH token
      2. Plain requests WITHOUT token  ← if token caused 403, public access may work
      3. Cloudscraper WITH token       ← Cloudflare bypass
      4. Cloudscraper WITHOUT token    ← Cloudflare bypass + public access

    Returns the 'data' field of the JSON response, or None on all failures.
    """
    full_url     = f"{ctf_url}/api/v1{path}"
    base_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    auth_headers = {**base_headers, "Authorization": f"Token {token}"} if token else base_headers

    def _parse(r):
        try:
            return r.json().get("data")
        except Exception:
            return None

    # ── Attempt 1: plain requests WITH token ──────────────────────────────────
    got_403_with_token = False
    try:
        r = requests.get(full_url, headers=auth_headers, allow_redirects=False, timeout=15)
        if r.status_code == 200 and not _is_cf_block(r):
            return _parse(r)
        if _is_cf_block(r) or r.status_code in (301, 302, 307, 308):
            pass  # fall through to cloudscraper
        elif r.status_code == 403 and token:
            print(f"  ⚠️  [{path}] 403 with token — retrying without token (public access)")
            got_403_with_token = True
        else:
            print(f"  ⚠️  [{path}] HTTP {r.status_code}")
            return None
    except Exception as e:
        print(f"  ⚠️  [{path}] requests error: {e}")

    # ── Attempt 2: plain requests WITHOUT token (public access fallback) ──────
    if token and got_403_with_token:
        try:
            r2 = requests.get(full_url, headers=base_headers, allow_redirects=False, timeout=15)
            if r2.status_code == 200 and not _is_cf_block(r2):
                data = _parse(r2)
                if data is not None:
                    print(f"  ✅ [{path}] public access (no token) OK")
                    return data
            if not _is_cf_block(r2) and r2.status_code not in (301, 302, 307, 308, 403):
                print(f"  ⚠️  [{path}] public retry HTTP {r2.status_code}")
                return None
        except Exception as e:
            print(f"  ⚠️  [{path}] public retry error: {e}")

    # ── Attempt 3 & 4: cloudscraper (Cloudflare bypass) ──────────────────────
    if not _CS_AVAILABLE:
        print(f"  ❌ [{path}] Cloudflare blocked + cloudscraper missing")
        return None

    sess = _cf_session(ctf_url)
    if sess is None:
        return None

    for hdrs, label in [(auth_headers, "with token"), (base_headers, "no token")]:
        if hdrs is auth_headers and not token:
            continue
        if hdrs is base_headers and not token:
            break
        try:
            rc = sess.get(full_url, headers=hdrs, allow_redirects=True, timeout=25)
            if rc.status_code == 200:
                data = _parse(rc)
                if data is not None:
                    print(f"  ✅ cloudscraper OK [{path}] ({label})")
                    return data
            print(f"  ❌ [{path}] cloudscraper {label} HTTP {rc.status_code}")
        except Exception as e:
            print(f"  ❌ [{path}] cloudscraper {label} error: {e}")

    return None


# ── CTFd data helpers ─────────────────────────────────────────────────────────

def _extract_members(team_data: dict) -> list:
    """
    Extract member IDs from a /teams/{id} response.
    Handles two formats:
      A) members: [101, 102, 103]            ← IDs directly
      B) members: [{"id":101,...}, ...]      ← objects with id key
    Returns a flat list of integer IDs.
    """
    raw = team_data.get("members", [])
    if not raw:
        return []
    if isinstance(raw[0], dict):
        ids = []
        for m in raw:
            uid = m.get("id") or m.get("user_id") or m.get("account_id")
            if uid:
                ids.append(int(uid))
        return ids
    return [int(x) for x in raw]


def _extract_solver_name(solve: dict) -> str:
    """
    Extract the solver's display name from a solve object.
    Different CTFd versions put this in different places.
    """
    user = solve.get("user")
    if isinstance(user, dict):
        name = user.get("name") or user.get("nick") or user.get("user_name")
        if name:
            return name

    for key in ("user_name", "name", "solver"):
        val = solve.get(key)
        if val and isinstance(val, str):
            return val

    team = solve.get("team")
    if isinstance(team, dict):
        name = team.get("name")
        if name:
            return name

    return "Unknown"


def _parse_solve(s: dict) -> dict:
    chall    = s.get("challenge") or {}
    raw_date = s.get("date", "")
    try:
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        time_str = (dt + timedelta(hours=6)).strftime("%I:%M %p (BDT)")
    except Exception:
        time_str = raw_date

    if isinstance(chall, dict):
        chall_name = chall.get("name") or chall.get("title") or "?"
        category   = chall.get("category") or chall.get("type") or "?"
        points     = chall.get("value") or chall.get("score") or 0
    else:
        chall_name = s.get("challenge_name") or s.get("name") or "?"
        category   = s.get("category") or "?"
        points     = s.get("score") or s.get("value") or 0

    return {
        "id":             s["id"],
        "challenge_name": chall_name,
        "category":       category,
        "points":         int(points) if points else 0,
        "solver":         _extract_solver_name(s),
        "solved_at":      time_str,
        "raw_date":       raw_date,
    }


def fetch_solves(cfg: dict) -> list[dict]:
    """
    Fetch all team/solo solves, sorted oldest-first.

    Strategy:
      1. Try /teams/{id}/solves  (works on some CTFd versions)
      2. Fall back to /users/{uid}/solves for each member individually
      3. Solo mode: /users/me/solves
    """
    url     = cfg["ctf_url"]
    token   = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))
    raw: list[dict] = []

    if team_id:
        # Strategy 1: team-level solves endpoint
        team_solves = _api_get(url, token, f"/teams/{team_id}/solves")
        if team_solves and isinstance(team_solves, list) and len(team_solves) > 0:
            print(f"  ✅ /teams/{team_id}/solves returned {len(team_solves)} solve(s)")
            raw = team_solves
        else:
            # Strategy 2: per-member solves
            team_data = _api_get(url, token, f"/teams/{team_id}")
            if not team_data:
                print(f"  ❌ Could not fetch team data for team {team_id}")
                return []

            member_ids = _extract_members(team_data)
            print(f"  👥 Team {team_id} members: {member_ids}")

            if not member_ids:
                print(f"  ⚠️  No member IDs found. Raw members field: {team_data.get('members')}")
                return []

            for uid in member_ids:
                solves = _api_get(url, token, f"/users/{uid}/solves")
                if solves and isinstance(solves, list):
                    print(f"  👤 User {uid}: {len(solves)} solve(s)")
                    raw.extend(solves)
                else:
                    print(f"  ⚠️  User {uid}: no solves returned (got: {type(solves).__name__})")
    else:
        raw = _api_get(url, token, "/users/me/solves") or []

    if not raw:
        return []

    parsed = [_parse_solve(s) for s in raw]
    return sorted(parsed, key=lambda x: x["raw_date"])


def fetch_team_name(cfg: dict) -> str:
    url     = cfg["ctf_url"]
    token   = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))
    if team_id:
        data = _api_get(url, token, f"/teams/{team_id}")
        if data and isinstance(data, dict):
            return data.get("name") or data.get("team_name") or "Unknown Team"
    else:
        data = _api_get(url, token, "/users/me")
        if data and isinstance(data, dict):
            return data.get("name") or "Unknown"
    return "Unknown Team"


def fetch_rank(cfg: dict) -> tuple[int, int]:
    url     = cfg["ctf_url"]
    token   = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))

    scoreboard = _api_get(url, token, "/scoreboard")
    if not scoreboard or not isinstance(scoreboard, list):
        return 0, 0

    total = len(scoreboard)

    if team_id:
        for entry in scoreboard:
            eid = entry.get("id") or entry.get("team_id") or entry.get("account_id")
            if eid and int(eid) == team_id:
                pos = entry.get("pos") or entry.get("place") or 0
                return int(pos), total
        td = _api_get(url, token, f"/teams/{team_id}")
        if td and isinstance(td, dict):
            pos = td.get("place") or td.get("pos") or 0
            if pos:
                return int(pos), total
    else:
        me = _api_get(url, token, "/users/me")
        if me and isinstance(me, dict):
            uid = me.get("id")
            for entry in scoreboard:
                eid = entry.get("id") or entry.get("account_id")
                if eid and eid == uid:
                    pos = entry.get("pos") or entry.get("place") or 0
                    return int(pos), total

    return 0, total


def fetch_ctf_end_time(cfg: dict):
    url   = cfg["ctf_url"]
    token = cfg.get("ctfd_token") or None

    data = _api_get(url, token, "/configs")
    if data:
        end_str = None
        if isinstance(data, list):
            for item in data:
                if item.get("key") == "end":
                    end_str = item.get("value")
                    break
        elif isinstance(data, dict):
            end_str = data.get("end")
        if end_str:
            try:
                dt = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            except Exception:
                pass

    end_str = cfg.get("end_time")
    if end_str:
        try:
            dt = datetime.fromisoformat(end_str)
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            print(f"  ℹ️  Using env-var end time for {cfg.get('ctf_name','?')}: {dt}")
            return dt
        except Exception as e:
            print(f"  ⚠️  Bad CTF_END_TIME value '{end_str}': {e}")

    return None


def member_stats(solves: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for s in solves:
        n = s["solver"]
        if n not in stats:
            stats[n] = {"count": 0, "points": 0}
        stats[n]["count"]  += 1
        stats[n]["points"] += s["points"]
    return stats


# ── Embed builders ─────────────────────────────────────────────────────────────

def build_embed(solve: dict, ctf_name: str, team_name: str = "",
                rank: int = 0, total: int = 0, test: bool = False) -> discord.Embed:

    rank_line = f"\n🏆 New Rank: **{rank}** / {total}" if rank and total else ""
    team_line = f"👥 Team: **{team_name}**\n" if team_name else ""

    desc = (
        f"🚩 **{ctf_name} — Challenge Solved**\n\n"
        f"{team_line}"
        f"\n"
        f"🧩 **{solve['challenge_name']}**\n"
        f"📂 {solve['category']}  •  💰 {solve['points']} pts  •  "
        f"🕐 {solve['solved_at']}  •  👤 Solver: **{solve['solver']}**"
        f"{rank_line}"
    )
    if test:
        desc = "🧪 **[TEST]** " + desc

    embed = discord.Embed(
    description=desc,
    color=discord.Color.from_rgb(100, 149, 237)
    )
    embed.set_footer(text=f"{FOOTER_TAG}{solve['id']}")
    return embed

def build_final_stats_embed(ctf_name: str, team_name: str,
                             rank: int, total: int,
                             total_points: int, stats: dict[str, dict]) -> discord.Embed:
    members = sorted(stats.items(), key=lambda x: x[1]["points"], reverse=True)

    lines = ""
    for i, (name, d) in enumerate(members):
        medal  = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i+1}."
        lines += f"{medal} **{name}** — {d['count']} solve(s) — {d['points']} pts\n"

    congrats = ""
    if len(members) >= 1:
        congrats = f"\n🎉 Congrats to **{members[0][0]}** for the amazing effort!"
    
    desc = (
        f"🏁 **{ctf_name} — Final Stats**\n"
        f"Team: **{team_name}**\n\n"
        f"🏆 Final Rank: **{rank} / {total}**\n"
        f"💰 Total Points: **{total_points}**\n\n"
        f"**Member Breakdown:**\n{lines}"
        f"{congrats}"
    )

    embed = discord.Embed(description=desc, color=discord.Color.from_rgb(255, 215, 0))
    embed.set_footer(text=f"{FOOTER_FINAL_TAG}{ctf_name} • CTF complete")
    return embed


# ── CTFtime upcoming events ────────────────────────────────────────────────────

def fetch_upcoming_ctfs(limit: int = 10) -> list[dict]:
    """Fetch the next {limit} upcoming CTFs from CTFtime public API."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    far_ts = now_ts + 60 * 60 * 24 * 365
    url = (
        f"https://ctftime.org/api/v1/events/"
        f"?limit={limit}&start={now_ts}&finish={far_ts}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CTF-SolveTrackerBot/1.0)",
        "Accept":     "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        print(f"  ⚠️  CTFtime API returned HTTP {r.status_code}")
    except Exception as e:
        print(f"  ❌ CTFtime fetch error: {e}")
    return []


def _fmt_ctftime_dt(iso_str: str) -> str:
    """Parse CTFtime ISO datetime and return formatted string: 21 March, 2026 [ 06:30 PM ]"""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        bdt = dt + timedelta(hours=6)
        return bdt.strftime("%d %B, %Y [ %I:%M %p ]")
    except Exception:
        return iso_str
        

def _duration_str(start_iso: str, finish_iso: str) -> str:
    """Return human-readable duration between two ISO timestamps."""
    try:
        s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        f = datetime.fromisoformat(finish_iso.replace("Z", "+00:00"))
        delta = f - s
        days  = delta.days
        hours = delta.seconds // 3600
        if days and hours:
            return f"{days}d {hours}h"
        if days:
            return f"{days} day(s)"
        return f"{hours} hour(s)"
    except Exception:
        return "?"


_FORMAT_ICON = {
    "jeopardy":           "🚩 Jeopardy",
    "attack-defence":     "⚔️ Attack-Defence",
    "ad":                 "⚔️ Attack-Defence",
    "king of the hill":   "👑 King of the Hill",
    "hardware":           "🔧 Hardware",
}

_RESTRICT_ICON = {
    "open":         "🌍 Open",
    "prequalified": "🎫 Pre-qualified",
    "academic":     "🎓 Academic",
    "onsite":       "📍 On-site",
}


def build_upcoming_embed(event: dict, index: int) -> discord.Embed:
    """Build a rich Discord embed for a single CTFtime event."""
    title       = event.get("title", "Unknown CTF")
    ctftime_url = event.get("ctftime_url") or f"https://ctftime.org/event/{event.get('id','')}"
    site_url    = event.get("url") or ""
    discord_url = event.get("discord") or ""
    fmt_raw     = (event.get("format") or "?").strip()
    fmt_label   = _FORMAT_ICON.get(fmt_raw.lower(), f"🎯 {fmt_raw}")
    restr_raw   = (event.get("restrictions") or "Open").strip()
    restr_label = _RESTRICT_ICON.get(restr_raw.lower(), f"🔒 {restr_raw}")
    weight      = float(event.get("weight") or 0)
    start_iso   = event.get("start", "")
    finish_iso  = event.get("finish", "")
    organizers  = event.get("organizers") or []
    org_names   = ", ".join(o.get("name", "?") for o in organizers) or "?"
    logo_url    = event.get("logo") or ""
    participants = event.get("participants") or 0

    if weight >= 30:
        color = discord.Color.from_rgb(255, 215, 0)
    elif weight >= 15:
        color = discord.Color.from_rgb(100, 149, 237)
    elif weight > 0:
        color = discord.Color.from_rgb(130, 100, 200)
    else:
        color = discord.Color.from_rgb(110, 110, 110)

    weight_str = f"⭐ {weight:.2f}" if weight > 0 else "⭐ Unrated"

    embed = discord.Embed(
        title=f"{index}. {title}",
        url=ctftime_url,
        color=color,
    )

    if logo_url:
        embed.set_thumbnail(url=logo_url)

    embed.add_field(name="🎯 Format", value=fmt_label,   inline=True)
    embed.add_field(name="👥 Type",   value=restr_label, inline=True)
    embed.add_field(name="⭐ Weight", value=weight_str,  inline=True)

    ts_start = to_unix(start_iso)
    embed.add_field(
        name="📅 Start Time",
        value=f"<t:{ts_start}:F>",
        inline=True
    )
    embed.add_field(
        name="⏳ Starts in",
        value=f"<t:{ts_start}:R>",
        inline=True
    )
    ts_end = to_unix(finish_iso)
    embed.add_field(
        name="🏁 Ends",
        value=f"<t:{ts_end}:F>  *(duration: {_duration_str(start_iso, finish_iso)})*",
        inline=False
    )

    if site_url:
        embed.add_field(name="🌐 Website", value=site_url, inline=True)
    if discord_url:
        embed.add_field(name="💬 Discord", value=discord_url, inline=True)
    if participants:
        embed.add_field(name="🧑‍💻 Interested participants", value=str(participants), inline=True)

    embed.set_footer(text=f"By: {org_names}  •  CTFtime  •  {FOOTER_UPCOMING_TAG}{event.get('id','')}")
    return embed


# ── Upcoming auto-post: recovery + poll loop ───────────────────────────────────

async def recover_upcoming_channel(channel) -> set[int]:
    """Scan channel history and return set of CTFtime event IDs already posted."""
    recovered: set[int] = set()
    try:
        async for msg in channel.history(limit=300):
            if msg.author.id != bot.user.id:
                continue
            for embed in msg.embeds:
                text = (embed.footer.text if embed.footer else "") or ""
                m = re.search(re.escape(FOOTER_UPCOMING_TAG) + r'(\d+)', text)
                if m:
                    try:
                        recovered.add(int(m.group(1)))
                    except ValueError:
                        pass
    except Exception as e:
        print(f"  ⚠️  Upcoming history scan failed #{channel.name}: {e}")
    print(f"  📜 #{channel.name} (upcoming): {len(recovered)} event(s) already posted")
    return recovered


async def upcoming_poll_loop():
    """Auto-post new upcoming CTFs to UPCOMING_CHANNEL_ID every UPCOMING_POLL_MINUTES."""
    await bot.wait_until_ready()

    if not UPCOMING_CHANNEL_ID:
        print("ℹ️  UPCOMING_CHANNEL_ID not set — auto upcoming disabled")
        return

    channel = bot.get_channel(int(UPCOMING_CHANNEL_ID))
    if not channel:
        print(f"⚠️  Upcoming channel {UPCOMING_CHANNEL_ID} not found")
        return

    # Recover already-posted IDs from history
    recovered = await recover_upcoming_channel(channel)
    upcoming_posted_ids.update(recovered)

    print(f"🔄 Upcoming auto-post: #{channel.name} every {UPCOMING_POLL_MINUTES} min")

    while not bot.is_closed():
        try:
            events = fetch_upcoming_ctfs(limit=UPCOMING_FETCH_COUNT)
            new_events = [e for e in events if int(e.get("id", 0)) not in upcoming_posted_ids]

            if new_events:
                # check existing bot messages in channel
                existing_msgs = [m async for m in channel.history(limit=50) if m.author.id == bot.user.id]
                if len(existing_msgs) + len(new_events) > 30:
                    # delete oldest messages to keep max 30
                    to_delete = len(existing_msgs) + len(new_events) - 30
                    for m in existing_msgs[-to_delete:]:  # oldest messages last
                        await m.delete()
                        
                await channel.send(
                    f"## 📅  New Upcoming CTFs  —  {len(new_events)} added\n"
                    f"-# Times in BDT (UTC+6)  •  Click a title to open CTFtime page"
                )
                new_events = list(reversed(new_events))  # newest first
                start_index = 1  # newest CTF gets number 1
                for i, event in enumerate(new_events, start=start_index):
                    existing_msg = None
                    async for msg in channel.history(limit=50):
                        if msg.author.id != bot.user.id:
                            continue
                        for embed in msg.embeds:
                            if embed.footer and str(event.get("id", 0)) in embed.footer.text:
                                existing_msg = msg
                                break
                    if existing_msg:
                        await existing_msg.edit(embed=build_upcoming_embed(event, i))
                    else:
                        await channel.send(embed=build_upcoming_embed(event, i))
                    upcoming_posted_ids.add(int(event.get("id", 0)))
                    print(f"📅 [upcoming] Posted: {event.get('title','?')} (id {event.get('id')})")
        except Exception as e:
            print(f"❌ Upcoming poll error: {e}")
            traceback.print_exc()

        await asyncio.sleep(UPCOMING_POLL_MINUTES * 60)


# ── Restart recovery ───────────────────────────────────────────────────────────

async def recover_channel(channel) -> tuple[set[int], bool]:
    recovered:    set[int] = set()
    final_posted: bool     = False
    try:
        async for msg in channel.history(limit=500):
            if msg.author.id != bot.user.id:
                continue
            for embed in msg.embeds:
                text = (embed.footer.text if embed.footer else "") or ""
                m = re.search(re.escape(FOOTER_TAG) + r'(\d+)', text)
                if m:
                    try:
                        recovered.add(int(m.group(1)))
                    except ValueError:
                        pass
                if re.search(re.escape(FOOTER_FINAL_TAG), text):
                    final_posted = True
    except Exception as e:
        print(f"  ⚠️  History scan failed #{channel.name}: {e}")
    print(
        f"  📜 #{channel.name}: {len(recovered)} solve(s) recovered | "
        f"final {'✅ posted' if final_posted else '🔲 pending'}"
    )
    return recovered, final_posted


# ── Poll loop ──────────────────────────────────────────────────────────────────

async def poll_loop():
    await bot.wait_until_ready()
    print(f"🔄 Polling {len(CTF_CONFIGS)} channel(s) every {POLL_SECS}s")

    while not bot.is_closed():
        for ch_id_str, cfg in CTF_CONFIGS.items():
            ch_id    = int(ch_id_str)
            channel  = bot.get_channel(ch_id)
            ctf_name = cfg.get("ctf_name", ch_id_str)

            if not channel:
                print(f"⚠️  Channel {ch_id_str} not found")
                continue

            posted = channel_posted_ids.setdefault(ch_id, set())

            try:
                solves     = fetch_solves(cfg)
                new_solves = [s for s in solves if s["id"] not in posted]

                if new_solves:
                    team_name   = fetch_team_name(cfg)

                    for solve in new_solves:
                        rank, total = fetch_rank(cfg)
                        await channel.send(embed=build_embed(
                            solve, ctf_name, team_name, rank, total
                        ))
                        posted.add(solve["id"])
                        print(f"✅ [{ctf_name}] {solve['challenge_name']} "
                              f"({solve['category']}, {solve['points']}pts) "
                              f"by {solve['solver']} | rank {rank}/{total}")

                # Auto-final-stats
                if ch_id not in channel_final_posted and solves:
                    end_time = fetch_ctf_end_time(cfg)
                    if end_time and datetime.now(timezone.utc) > end_time:
                        stats     = member_stats(solves)
                        team_name = fetch_team_name(cfg)
                        rank, total = fetch_rank(cfg)
                        await channel.send(
                            content=f"🏁 **{ctf_name} has ended!** Final results:",
                            embed=build_final_stats_embed(
                                ctf_name, team_name, rank, total,
                                sum(d["points"] for d in stats.values()), stats
                            )
                        )
                        channel_final_posted.add(ch_id)
                        print(f"🏁 [{ctf_name}] Auto final stats posted")

            except Exception as e:
                print(f"❌ Poll error [{ctf_name}]: {e}")
                traceback.print_exc()

        await asyncio.sleep(POLL_SECS)


# ── Bot events ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    print(f"   Channels: {list(CTF_CONFIGS.keys())}")
    for ch_id_str in CTF_CONFIGS:
        channel = bot.get_channel(int(ch_id_str))
        if channel:
            ids, final_done = await recover_channel(channel)
            channel_posted_ids[int(ch_id_str)] = ids
            if final_done:
                channel_final_posted.add(int(ch_id_str))
        else:
            print(f"⚠️  Channel {ch_id_str} not found at startup")
    asyncio.ensure_future(poll_loop())
    asyncio.ensure_future(upcoming_poll_loop())


def _cfg(ctx) -> dict | None:
    return CTF_CONFIGS.get(str(ctx.channel.id))


# ── Commands ───────────────────────────────────────────────────────────────────

@bot.command(name="solves")
async def cmd_solves(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` not configured. Use `;ctfs`.")
        return
    solves = fetch_solves(cfg)
    if not solves:
        await ctx.send("No solves found yet.")
        return
    team_name   = fetch_team_name(cfg)
    rank, total = fetch_rank(cfg)
    posted      = channel_posted_ids.setdefault(ctx.channel.id, set())
    await ctx.send(f"📋 **{len(solves)} solve(s)** — **{cfg['ctf_name']}** | Team: **{team_name}**")
    for solve in solves:
        await ctx.channel.send(embed=build_embed(solve, cfg["ctf_name"], team_name, rank, total))
        posted.add(solve["id"])


@bot.command(name="testsolves")
async def cmd_testsolves(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` not configured.")
        return
    solves = fetch_solves(cfg)
    if not solves:
        await ctx.send("No solves found.")
        return
    team_name   = fetch_team_name(cfg)
    rank, total = fetch_rank(cfg)
    await ctx.send(f"🧪 **Test mode** — {len(solves)} solve(s) (not tracked):")
    for solve in solves:
        await ctx.channel.send(
            embed=build_embed(solve, cfg["ctf_name"], team_name, rank, total, test=True)
        )


@bot.command(name="stats")
async def cmd_stats(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` not configured.")
        return
    solves = fetch_solves(cfg)
    if not solves:
        await ctx.send("No solves yet.")
        return
    stats     = member_stats(solves)
    members   = sorted(stats.items(), key=lambda x: x[1]["points"], reverse=True)
    team_name = fetch_team_name(cfg)
    rank, total = fetch_rank(cfg)
    lines = [f"📊 **{cfg['ctf_name']} — Member Stats** | Team: **{team_name}**\n"]
    for i, (name, d) in enumerate(members):
        medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} **{name}** — {d['count']} solve(s) — {d['points']} pts")
    lines.append(f"\n💰 **Total: {sum(d['points'] for d in stats.values())} pts**")
    if rank and total:
        lines.append(f"🏆 **Rank: {rank} / {total}**")
    await ctx.send("\n".join(lines))


@bot.command(name="finalstats")
async def cmd_finalstats(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` not configured.")
        return
    solves      = fetch_solves(cfg)
    stats       = member_stats(solves)
    team_name   = fetch_team_name(cfg)
    rank, total = fetch_rank(cfg)
    total_pts   = sum(d["points"] for d in stats.values())
    await ctx.send(embed=build_final_stats_embed(
        cfg["ctf_name"], team_name, rank, total, total_pts, stats
    ))
    channel_final_posted.add(ctx.channel.id)


@bot.command(name="rank")
async def cmd_rank(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` not configured.")
        return
    team_name   = fetch_team_name(cfg)
    rank, total = fetch_rank(cfg)
    if rank and total:
        await ctx.send(f"🏆 **{cfg['ctf_name']}** | **{team_name}** — Rank **{rank} / {total}**")
    else:
        await ctx.send("Could not fetch rank (scoreboard hidden or team not ranked yet).")


@bot.command(name="members")
async def cmd_members(ctx):
    """;members — list all team members with their usernames."""
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` not configured. Use `;ctfs`.")
        return

    url     = cfg["ctf_url"]
    token   = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))

    if not team_id:
        await ctx.send("ℹ️  This channel is in solo mode — no team members to list.")
        return

    team_data = _api_get(url, token, f"/teams/{team_id}")
    if not team_data:
        await ctx.send("❌ Could not fetch team data. Check the token or try `;debug`.")
        return

    team_name  = team_data.get("name") or team_data.get("team_name") or "Unknown Team"
    member_ids = _extract_members(team_data)

    if not member_ids:
        await ctx.send(f"⚠️  Team **{team_name}** was found but has no members listed in the API.")
        return

    # Fetch each member's display name from /users/{id}
    member_names: list[str] = []
    for uid in member_ids:
        user_data = _api_get(url, token, f"/users/{uid}")
        if user_data and isinstance(user_data, dict):
            name = (
                user_data.get("name")
                or user_data.get("nick")
                or user_data.get("user_name")
                or f"User#{uid}"
            )
        else:
            name = f"User#{uid}"
        member_names.append(name)

    # Build a clean embed
    lines = ""
    for i, name in enumerate(member_names):
        icon = f"`{i+1}.`"
        lines += f"{icon}  **{name}**\n"
        
    embed = discord.Embed(
        title=f"👥  {team_name}  —  Team Roster",
        description=lines.strip(),
        color=discord.Color.from_rgb(88, 101, 242),
    )
    embed.set_footer(text=f"{cfg['ctf_name']}  •  {len(member_names)} member(s)")

    await ctx.send(embed=embed)


@bot.command(name="upcoming")
async def cmd_upcoming(ctx):
    """;upcoming — show the next 10 upcoming CTFs from CTFtime."""
    msg = await ctx.send("⏳ Fetching upcoming CTFs from CTFtime…")
    events = fetch_upcoming_ctfs(limit=10)

    if not events:
        await msg.edit(content="❌ Could not fetch CTFtime data. Try again in a moment.")
        return

    await msg.edit(content=(
        f"## 📅  Upcoming CTFs  —  Next {len(events)}\n"
        f"-# Times shown in BDT (UTC+6)  •  Click a title to open CTFtime page"
    ))

    for i, event in enumerate(events, start=1):
        await ctx.send(embed=build_upcoming_embed(event, i))


@bot.command(name="status")
async def cmd_status(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` not configured. Use `;ctfs`.")
        return
    url     = cfg["ctf_url"]
    token   = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))
    posted  = channel_posted_ids.get(ctx.channel.id, set())
    if team_id:
        td   = _api_get(url, token, f"/teams/{team_id}") or {}
        mode = f"Team {team_id} ({td.get('name','?')}) | members: {_extract_members(td)}"
    else:
        me   = _api_get(url, token, "/users/me") or {}
        mode = f"Solo — {me.get('name','?')} (id {me.get('id','?')})"
    rank, total = fetch_rank(cfg)
    end_time    = fetch_ctf_end_time(cfg)
    await ctx.send(
        f"🤖 **{cfg['ctf_name']} — Status**\n```\n"
        f"URL       : {url}\n"
        f"Auth      : {'token set' if token else 'no token'}\n"
        f"Mode      : {mode}\n"
        f"Rank      : {f'{rank}/{total}' if rank else 'N/A'}\n"
        f"Ends at   : {end_time.strftime('%Y-%m-%d %H:%M UTC') if end_time else 'unknown'}\n"
        f"Tracked   : {len(posted)} solve(s)\n"
        f"Poll      : every {POLL_SECS}s\n"
        f"```"
    )


@bot.command(name="ctfs")
async def cmd_ctfs(ctx):
    if not CTF_CONFIGS:
        await ctx.send("⚠️  No CTFs configured.")
        return
    lines = ["**🗂️ Configured CTFs:**"]
    for ch_id_str, cfg in CTF_CONFIGS.items():
        posted = channel_posted_ids.get(int(ch_id_str), set())
        lines.append(
            f"  • <#{ch_id_str}> → **{cfg.get('ctf_name','?')}** "
            f"(`{cfg.get('ctf_url','')}`) | {len(posted)} tracked"
        )
    await ctx.send("\n".join(lines))


@bot.command(name="debug")
async def cmd_debug(ctx):
    """;debug — prints raw API responses to Render logs for diagnosis."""
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` not configured.")
        return
    url     = cfg["ctf_url"]
    token   = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))

    await ctx.send(f"🔍 Running debug for **{cfg['ctf_name']}** — check Render logs.")

    print(f"\n{'='*60}")
    print(f"DEBUG: {cfg['ctf_name']}  url={url}  team={team_id}")

    td = _api_get(url, token, f"/teams/{team_id}")
    print(f"  /teams/{team_id} → {td}")

    if td:
        members = _extract_members(td)
        print(f"  extracted member IDs: {members}")
        for uid in members[:3]:
            sv = _api_get(url, token, f"/users/{uid}/solves")
            print(f"  /users/{uid}/solves → {str(sv)[:300]}")

    ts = _api_get(url, token, f"/teams/{team_id}/solves")
    print(f"  /teams/{team_id}/solves → {str(ts)[:300]}")

    sc = _api_get(url, token, "/scoreboard")
    print(f"  /scoreboard → {str(sc)[:300] if sc else None}")

    print(f"{'='*60}\n")
    await ctx.send("✅ Debug done — check Render logs for raw API output.")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("❌ TOKEN env var not set!")
    if not CTF_CONFIGS:
        print("⚠️  No CTF channels configured.")
    bot.run(BOT_TOKEN)
