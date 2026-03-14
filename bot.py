import discord
from discord.ext import commands
import requests
import traceback
import re
from datetime import datetime, timedelta, timezone
import asyncio
import os

# ── Bot config ────────────────────────────────────────────────────────────────

BOT_TOKEN        = os.getenv("TOKEN")
POLL_SECS        = int(os.getenv("POLL_SECONDS", "35"))
FOOTER_TAG       = "solve_id:"
FOOTER_FINAL_TAG = "final_stats_id:"


def load_ctfs():
    ctfs = {}
    for i in range(1, 10):
        ch = os.getenv(f"CHANNEL{i}_ID")
        if not ch:
            continue
        ctfs[ch.strip()] = {
            "ctf_name":   os.getenv(f"CTF{i}_NAME", f"CTF{i}"),
            "ctf_url":    (os.getenv(f"CTF{i}_URL") or "").rstrip("/"),
            "ctfd_token": os.getenv(f"CTF{i}_TOKEN") or None,
            "team_id":    int(os.getenv(f"CTF{i}_TEAM", "0")),
            "end_time":   os.getenv(f"CTF{i}_END_TIME") or None,  # e.g. "2026-04-15T18:00:00" UTC
        }
    return ctfs


CTF_CONFIGS = load_ctfs()
print("Loaded configs:", CTF_CONFIGS)

# Per-channel memory
channel_posted_ids:   dict[int, set[int]] = {}
channel_final_posted: set[int]            = set()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ── CTFd API helpers ──────────────────────────────────────────────────────────

def _api_get(ctf_url: str, token: str | None, path: str):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Token {token}"
    try:
        r = requests.get(
            f"{ctf_url}/api/v1{path}",
            headers=headers,
            allow_redirects=False,
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("data")
        print(f"  API [{ctf_url}]{path} → HTTP {r.status_code}")
    except Exception as e:
        print(f"  API error [{ctf_url}]{path}: {e}")
    return None


def fetch_team_name(cfg: dict) -> str:
    url     = cfg["ctf_url"]
    token   = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))
    if team_id:
        data = _api_get(url, token, f"/teams/{team_id}")
        if data:
            return data.get("name", "Unknown Team")
    else:
        data = _api_get(url, token, "/users/me")
        if data:
            return data.get("name", "Unknown")
    return "Unknown Team"


def fetch_rank(cfg: dict) -> tuple[int, int]:
    url     = cfg["ctf_url"]
    token   = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))

    scoreboard = _api_get(url, token, "/scoreboard")
    if not scoreboard:
        return 0, 0

    total = len(scoreboard)

    if team_id:
        for entry in scoreboard:
            if entry.get("id") == team_id or entry.get("team_id") == team_id:
                return entry.get("pos", 0), total
        team_data = _api_get(url, token, f"/teams/{team_id}")
        if team_data:
            pos = team_data.get("place", 0)
            if pos:
                return pos, total
    else:
        me = _api_get(url, token, "/users/me")
        if me:
            uid = me.get("id")
            for entry in scoreboard:
                if entry.get("id") == uid or entry.get("account_id") == uid:
                    return entry.get("pos", 0), total

    return 0, total


def fetch_ctf_end_time(cfg: dict):
    """
    Returns a timezone-aware datetime (UTC) for when the CTF ends, or None.
    Priority:
      1. CTFd /configs API endpoint
      2. CTF{i}_END_TIME env var fallback
    """
    url   = cfg["ctf_url"]
    token = cfg.get("ctfd_token") or None

    # Priority 1: API
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
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                pass

    # Priority 2: Env var fallback
    end_str = cfg.get("end_time")
    if end_str:
        try:
            dt = datetime.fromisoformat(end_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            print(f"  ℹ️  Using env var end time for {cfg.get('ctf_name', '?')}: {dt}")
            return dt
        except Exception as e:
            print(f"  ⚠️  Invalid CTF end time in env var: '{end_str}' — {e}")
            print(f"      Correct format: YYYY-MM-DDTHH:MM:SS  e.g. 2026-04-15T18:00:00")

    return None


def _parse_solve(s: dict) -> dict:
    chall    = s.get("challenge", {})
    raw_date = s.get("date", "")
    try:
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        bdt      = dt + timedelta(hours=6)
        time_str = bdt.strftime("%I:%M %p (BDT)")
    except Exception:
        time_str = raw_date
    return {
        "id":             s["id"],
        "challenge_name": chall.get("name", "?"),
        "category":       chall.get("category", "?"),
        "points":         chall.get("value", 0),
        "solver":         s.get("user", {}).get("name", "?"),
        "solved_at":      time_str,
        "raw_date":       raw_date,
    }


def fetch_solves(cfg: dict) -> list[dict]:
    url     = cfg["ctf_url"]
    token   = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))
    raw: list[dict] = []

    if team_id:
        team_data  = _api_get(url, token, f"/teams/{team_id}")
        member_ids = team_data.get("members", []) if team_data else []
        for uid in member_ids:
            data = _api_get(url, token, f"/users/{uid}/solves") or []
            raw.extend(data)
    else:
        raw = _api_get(url, token, "/users/me/solves") or []

    parsed = [_parse_solve(s) for s in raw]
    return sorted(parsed, key=lambda x: x["raw_date"])


def member_stats(solves: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for s in solves:
        name = s["solver"]
        if name not in stats:
            stats[name] = {"count": 0, "points": 0}
        stats[name]["count"]  += 1
        stats[name]["points"] += s["points"]
    return stats


# ── Discord embed builders ────────────────────────────────────────────────────

def build_embed(solve: dict, ctf_name: str, team_name: str = "",
                rank: int = 0, total: int = 0, test: bool = False) -> discord.Embed:
    color = discord.Color.from_rgb(100, 149, 237)

    rank_line = ""
    if rank and total:
        rank_line = f"\n🏆 New Rank: **{rank}** / {total}"

    team_line = f"Team: **{team_name}**\n" if team_name else ""

    description = (
        f"🚩 **{ctf_name} — Challenge Solved**\n"
        f"{team_line}"
        f"\n"
        f"🧩 **{solve['challenge_name']}**\n"
        f"📂 {solve['category']}  •  💰 {solve['points']} pts  •  "
        f"🕐 {solve['solved_at']}  •  👤 Solver: **{solve['solver']}**"
        f"{rank_line}"
    )

    if test:
        description = "🧪 **[TEST]** " + description

    # ID টা spoiler এ লুকানো — message এ দেখা যাবে না, click করলে reveal হবে
    # Bot internally এটা scan করে dedup করে
    description += f"\n||{FOOTER_TAG}{solve['id']}||"

    embed = discord.Embed(description=description, color=color)
    return embed


def build_final_stats_embed(
    ctf_name:     str,
    team_name:    str,
    rank:         int,
    total:        int,
    total_points: int,
    stats:        dict[str, dict],
) -> discord.Embed:
    color = discord.Color.from_rgb(255, 215, 0)  # Gold

    sorted_members = sorted(stats.items(), key=lambda x: x[1]["points"], reverse=True)

    member_lines = ""
    for i, (name, data) in enumerate(sorted_members):
        medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i+1}."
        member_lines += (
            f"{medal} **{name}** — {data['count']} solve(s) — {data['points']} pts\n"
        )

    congrats = ""
    if len(sorted_members) >= 2:
        congrats = (
            f"\n🎉 Huge congrats to **{sorted_members[0][0]}** and "
            f"**{sorted_members[1][0]}** for their outstanding performance!"
        )
    elif len(sorted_members) == 1:
        congrats = f"\n🎉 Congrats to **{sorted_members[0][0]}** for their amazing effort!"

    description = (
        f"🏁 **{ctf_name} — Final Stats**\n"
        f"Team: **{team_name}**\n\n"
        f"🏆 Final Rank: **{rank} / {total}**\n"
        f"💰 Total Points: **{total_points}**\n\n"
        f"**Member Breakdown:**\n"
        f"{member_lines}"
        f"{congrats}"
    )

    # Final stats unique ID — spoiler এ লুকানো, restart এর পর duplicate থেকে বাঁচায়
    description += f"\n||{FOOTER_FINAL_TAG}{ctf_name}||"

    embed = discord.Embed(description=description, color=color)
    embed.set_footer(text="CTF complete • GG everyone!")
    return embed


# ── Restart recovery ──────────────────────────────────────────────────────────

async def recover_channel(channel) -> tuple[set[int], bool]:
    """
    Scan up to 500 recent messages.
    - Extracts solve IDs from spoiler tags:   ||solve_id:12345||
    - Checks if final stats was already posted: ||final_stats_id:CTFName||
    Returns (recovered_solve_ids, final_already_posted)
    """
    recovered:    set[int] = set()
    final_posted: bool     = False

    try:
        async for msg in channel.history(limit=500):
            if msg.author.id != bot.user.id:
                continue
            for embed in msg.embeds:
                text = embed.description or ""

                # Solve ID খোঁজা
                match = re.search(
                    r'\|\|' + re.escape(FOOTER_TAG) + r'(\d+)\|\|',
                    text
                )
                if match:
                    try:
                        recovered.add(int(match.group(1)))
                    except ValueError:
                        pass

                # Final stats ID খোঁজা
                if FOOTER_FINAL_TAG in text:
                    final_posted = True

    except Exception as e:
        print(f"  Warning: history scan failed in #{channel.name}: {e}")

    print(
        f"  📜 #{channel.name}: recovered {len(recovered)} solve ID(s) | "
        f"final stats {'✅ already posted' if final_posted else '🔲 not yet posted'}"
    )
    return recovered, final_posted


# ── Background poll loop ──────────────────────────────────────────────────────

async def poll_loop():
    await bot.wait_until_ready()
    print(f"🔄 Polling {len(CTF_CONFIGS)} channel(s) every {POLL_SECS}s")

    while not bot.is_closed():
        for ch_id_str, cfg in CTF_CONFIGS.items():
            ch_id   = int(ch_id_str)
            channel = bot.get_channel(ch_id)
            if not channel:
                print(f"⚠️  Channel {ch_id_str} not found — check your CHANNEL env vars")
                continue

            posted   = channel_posted_ids.setdefault(ch_id, set())
            ctf_name = cfg.get("ctf_name", ch_id_str)

            try:
                solves     = fetch_solves(cfg)
                new_solves = [s for s in solves if s["id"] not in posted]

                if new_solves:
                    team_name   = fetch_team_name(cfg)
                    rank, total = fetch_rank(cfg)

                    for solve in new_solves:
                        embed = build_embed(
                            solve,
                            ctf_name=ctf_name,
                            team_name=team_name,
                            rank=rank,
                            total=total,
                        )
                        await channel.send(embed=embed)
                        posted.add(solve["id"])
                        print(
                            f"✅ [{ctf_name}] [{solve['category']}] {solve['challenge_name']}"
                            f" — {solve['points']} pts — by {solve['solver']}"
                            f" | rank {rank}/{total}"
                        )

                # ── Auto-final-stats ──────────────────────────────────────
                if ch_id not in channel_final_posted and solves:
                    end_time = fetch_ctf_end_time(cfg)
                    if end_time and datetime.now(timezone.utc) > end_time:
                        stats     = member_stats(solves)
                        team_name = fetch_team_name(cfg)
                        rank, total = fetch_rank(cfg)
                        total_pts = sum(d["points"] for d in stats.values())
                        embed = build_final_stats_embed(
                            ctf_name=ctf_name,
                            team_name=team_name,
                            rank=rank,
                            total=total,
                            total_points=total_pts,
                            stats=stats,
                        )
                        await channel.send(
                            content=f"🏁 **{ctf_name} has ended!** Here are the final results:",
                            embed=embed,
                        )
                        channel_final_posted.add(ch_id)
                        print(f"🏁 [{ctf_name}] Final stats posted automatically.")

            except Exception as e:
                print(f"❌ Poll error [{ctf_name}]: {e}")
                traceback.print_exc()

        await asyncio.sleep(POLL_SECS)


# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    print(f"   Channels in config: {list(CTF_CONFIGS.keys())}")

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


def _cfg(ctx) -> dict | None:
    return CTF_CONFIGS.get(str(ctx.channel.id))


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.command(name="solves")
async def cmd_solves(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` is not configured. Use `!ctfs` to list active channels.")
        return
    solves = fetch_solves(cfg)
    if not solves:
        await ctx.send("No solves found yet.")
        return
    team_name   = fetch_team_name(cfg)
    rank, total = fetch_rank(cfg)
    posted      = channel_posted_ids.setdefault(ctx.channel.id, set())
    await ctx.send(f"📋 **{len(solves)} solve(s)** for **{cfg.get('ctf_name', '')}** | Team: **{team_name}**")
    for solve in solves:
        embed = build_embed(solve, cfg.get("ctf_name", ""), team_name=team_name, rank=rank, total=total)
        await ctx.channel.send(embed=embed)
        posted.add(solve["id"])


@bot.command(name="testsolves")
async def cmd_testsolves(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` is not configured.")
        return
    solves = fetch_solves(cfg)
    if not solves:
        await ctx.send("No solves found.")
        return
    team_name   = fetch_team_name(cfg)
    rank, total = fetch_rank(cfg)
    await ctx.send(f"🧪 **Test mode** — {len(solves)} solve(s) in chronological order (not tracked):")
    for solve in solves:
        embed = build_embed(solve, cfg.get("ctf_name", ""), team_name=team_name,
                            rank=rank, total=total, test=True)
        await ctx.channel.send(embed=embed)


@bot.command(name="stats")
async def cmd_stats(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` is not configured.")
        return
    solves = fetch_solves(cfg)
    if not solves:
        await ctx.send("No solves yet.")
        return
    stats          = member_stats(solves)
    sorted_members = sorted(stats.items(), key=lambda x: x[1]["points"], reverse=True)
    ctf_name       = cfg.get("ctf_name", "CTF")
    team_name      = fetch_team_name(cfg)
    lines = [f"📊 **{ctf_name} — Member Stats** | Team: **{team_name}**\n"]
    for i, (name, data) in enumerate(sorted_members):
        medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} **{name}** — {data['count']} solve(s) — {data['points']} pts")
    total_pts = sum(d["points"] for d in stats.values())
    lines.append(f"\n💰 **Total team points: {total_pts}**")
    rank, total = fetch_rank(cfg)
    if rank and total:
        lines.append(f"🏆 **Current Rank: {rank} / {total}**")
    await ctx.send("\n".join(lines))


@bot.command(name="finalstats")
async def cmd_finalstats(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` is not configured.")
        return
    solves      = fetch_solves(cfg)
    stats       = member_stats(solves)
    team_name   = fetch_team_name(cfg)
    rank, total = fetch_rank(cfg)
    total_pts   = sum(d["points"] for d in stats.values())
    embed = build_final_stats_embed(
        ctf_name=cfg.get("ctf_name", "CTF"),
        team_name=team_name,
        rank=rank,
        total=total,
        total_points=total_pts,
        stats=stats,
    )
    await ctx.send(embed=embed)
    channel_final_posted.add(ctx.channel.id)  # manual call ও mark হবে


@bot.command(name="rank")
async def cmd_rank(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` is not configured.")
        return
    team_name   = fetch_team_name(cfg)
    rank, total = fetch_rank(cfg)
    if rank and total:
        await ctx.send(
            f"🏆 **{cfg.get('ctf_name', 'CTF')}** | "
            f"Team **{team_name}** — Rank: **{rank} / {total}**"
        )
    else:
        await ctx.send("Could not fetch rank. Team may not be on scoreboard yet or scoreboard is hidden.")


@bot.command(name="status")
async def cmd_status(ctx):
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` is not configured. Use `!ctfs` to see active channels.")
        return
    url     = cfg["ctf_url"]
    token   = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))
    posted  = channel_posted_ids.get(ctx.channel.id, set())
    if team_id:
        team = _api_get(url, token, f"/teams/{team_id}") or {}
        mode = f"Team {team_id} ({team.get('name', '?')}) | members: {team.get('members', [])}"
    else:
        me   = _api_get(url, token, "/users/me") or {}
        mode = f"Solo — {me.get('name', '?')} (user id {me.get('id', '?')})"
    rank, total = fetch_rank(cfg)
    rank_str    = f"{rank} / {total}" if rank else "N/A"
    end_time    = fetch_ctf_end_time(cfg)
    end_str     = end_time.strftime("%Y-%m-%d %H:%M UTC") if end_time else "Unknown (set CTF1_END_TIME)"
    await ctx.send(
        f"🤖 **Status for this channel**\n"
        f"```\n"
        f"CTF       : {cfg.get('ctf_name', '?')}\n"
        f"URL       : {url}\n"
        f"Auth      : {'token set' if token else 'no token (public API)'}\n"
        f"Mode      : {mode}\n"
        f"Rank      : {rank_str}\n"
        f"Ends at   : {end_str}\n"
        f"Tracked   : {len(posted)} solve ID(s) in memory\n"
        f"Poll rate : every {POLL_SECS}s\n"
        f"```"
    )


@bot.command(name="ctfs")
async def cmd_ctfs(ctx):
    if not CTF_CONFIGS:
        await ctx.send("⚠️  No CTFs configured. Set CHANNEL1_ID, CTF1_NAME, CTF1_URL, etc.")
        return
    lines = ["**🗂️ Configured CTFs:**"]
    for ch_id_str, cfg in CTF_CONFIGS.items():
        posted = channel_posted_ids.get(int(ch_id_str), set())
        lines.append(
            f"  • <#{ch_id_str}> → **{cfg.get('ctf_name', '?')}** "
            f"(`{cfg.get('ctf_url', '')}`) | {len(posted)} tracked solve(s)"
        )
    await ctx.send("\n".join(lines))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("❌ TOKEN environment variable is not set!")
    if not CTF_CONFIGS:
        print("⚠️  Warning: No CTF channels configured. Set CHANNEL1_ID, CTF1_NAME, CTF1_URL, CTF1_TOKEN, CTF1_TEAM")
    bot.run(BOT_TOKEN)

"""
CTF Solve Tracker — Discord Bot
Supports multiple CTFs across multiple channels from a single bot instance.

Features:
  - Solve notifications with team name + rank out of total teams
  - Solve IDs hidden as Discord spoilers (||click to reveal||) — not cluttering the message
  - Final stats deduplication via spoiler tag scan on restart
  - Auto-final-stats when CTF ends (API detection + env var fallback)
  - Per-member solve count and points tracking
  - !finalstats, !stats, !rank commands

Env vars:
  TOKEN              — Discord bot token
  POLL_SECONDS       — poll interval in seconds (default 35)
  CHANNEL1_ID        — Discord channel ID for CTF 1
  CTF1_NAME          — Display name      (e.g. "UAPCTF 2026")
  CTF1_URL           — Base URL          (e.g. "https://uapctf.qzz.io")
  CTF1_TOKEN         — CTFd API token    (omit or leave blank for public APIs)
  CTF1_TEAM          — Team ID           (0 = solo/me mode)
  CTF1_END_TIME      — CTF end time UTC  (e.g. "2026-04-15T18:00:00") — optional fallback
  Repeat with CHANNEL2_ID / CTF2_* for more CTFs.
"""
