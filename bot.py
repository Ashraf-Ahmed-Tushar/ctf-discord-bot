"""
CTF Solve Tracker — Discord Bot
Supports multiple CTFs across multiple channels from a single bot instance.

Config: Set CTF_CONFIGS as a JSON env var. See README section at bottom.
"""

import discord
from discord.ext import commands
import requests
from datetime import datetime
import asyncio
import json
import os

# ── Bot config ────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TOKEN")
POLL_SECS = int(os.getenv("POLL_SECONDS", "35"))
FOOTER_TAG = "solve_id:"  # embedded in real embed footers for restart dedup

# CTF_CONFIGS maps Discord channel IDs (as strings) to CTF configs.
# Paste the JSON as a single env var. Example:
# {
#   "1111111111111111111": {
#     "ctf_name":   "CodeVinci CTF",
#     "ctf_url":    "https://challs.codevinci.it",
#     "ctfd_token": "ctfd_abc123",   <-- null if public API (no auth needed)
#     "team_id":    0                <-- 0 = solo mode (/users/me), else team ID
#   },
#   "2222222222222222222": {
#     "ctf_name":   "UTCTF 2026",
#     "ctf_url":    "https://utctf.live",
#     "ctfd_token": null,
#     "team_id":    117
#   }
# }
try:
    CTF_CONFIGS = json.loads(os.getenv("CTF_CONFIGS", "{}"))
except Exception as e:
    print("CTF_CONFIGS parse error:", e)
    CTF_CONFIGS = {}

# Per-channel memory: channel_id (int) → set of solve IDs already posted
channel_posted_ids: dict[int, set[int]] = {}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ── CTFd API helpers ─────────────────────────────────────────────────────────


def _api_get(ctf_url: str, token: str | None, path: str):
    """
    Single CTFd API GET.
    - allow_redirects=False is CRITICAL: CTFd silently redirects unauthed
      requests to /login, so without this you'd always get HTML back.
    - token may be None for platforms with public APIs (e.g. utctf.live).
    """
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


def _parse_solve(s: dict) -> dict:
    chall = s.get("challenge", {})
    try:
        dt = datetime.fromisoformat(s["date"]).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        dt = s.get("date", "?")
    return {
        "id": s["id"],
        "challenge_name": chall.get("name", "?"),
        "category": chall.get("category", "?"),
        "points": chall.get("value", 0),
        "solver": s.get("user", {}).get("name", "?"),
        "solved_at": dt,
        "raw_date": s.get("date", ""),
    }


def fetch_solves(cfg: dict) -> list[dict]:
    """
    Fetch all relevant solves for a channel's CTF config.

    TEAM mode  (team_id > 0):
        Reads team members from /teams/{team_id}, then gets each member's
        individual solves. Works even if the bot's token belongs to a
        different account — member solve endpoints are usually public.

    SOLO mode  (team_id == 0):
        Reads /users/me/solves using the provided token.

    NO TOKEN:
        Skips Authorization header. Works for platforms with public APIs.
    """
    url = cfg["ctf_url"]
    token = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))
    raw: list[dict] = []

    if team_id:
        team_data = _api_get(url, token, f"/teams/{team_id}")
        member_ids = team_data.get("members", []) if team_data else []
        for uid in member_ids:
            data = _api_get(url, token, f"/users/{uid}/solves") or []
            raw.extend(data)
    else:
        raw = _api_get(url, token, "/users/me/solves") or []

    return sorted([_parse_solve(s) for s in raw], key=lambda x: x["raw_date"])


# ── Discord embed builder ────────────────────────────────────────────────────


def build_embed(solve: dict,
                ctf_name: str = "",
                *,
                test: bool = False) -> discord.Embed:
    embed = discord.Embed(
        title="🚩 Challenge Solved!",
        color=discord.Color.green(),
    )
    if ctf_name:
        embed.set_author(name=f"📡 {ctf_name}")
    embed.add_field(name="🧩 Challenge",
                    value=solve["challenge_name"],
                    inline=False)
    embed.add_field(name="📂 Category", value=solve["category"], inline=True)
    embed.add_field(name="💰 Points", value=str(solve["points"]), inline=True)
    embed.add_field(name="👤 Solver", value=solve["solver"], inline=False)
    embed.add_field(name="🕐 Solved At", value=solve["solved_at"], inline=False)

    # Real embeds carry the solve ID in the footer so we can recover state after restart.
    # Test embeds use a different footer so they don't affect dedup.
    embed.set_footer(text="🧪 Test message — not tracked"
                     if test else f"{FOOTER_TAG}{solve['id']}")
    return embed


# ── Restart recovery ─────────────────────────────────────────────────────────


async def recover_channel(channel) -> set[int]:
    """
    Scan up to 500 recent messages in a channel.
    Extracts solve IDs from bot embed footers so we never re-post after restart.
    """
    recovered: set[int] = set()
    try:
        async for msg in channel.history(limit=500):
            if msg.author.id != bot.user.id:
                continue
            for embed in msg.embeds:
                footer = embed.footer.text or ""
                if footer.startswith(FOOTER_TAG):
                    try:
                        recovered.add(int(footer[len(FOOTER_TAG):]))
                    except ValueError:
                        pass
    except Exception as e:
        print(f"  Warning: history scan failed in #{channel.name}: {e}")
    print(
        f"  📜 #{channel.name}: recovered {len(recovered)} solve ID(s) from history"
    )
    return recovered


# ── Background poll loop ─────────────────────────────────────────────────────


async def poll_loop():
    await bot.wait_until_ready()
    print(f"🔄 Polling {len(CTF_CONFIGS)} channel(s) every {POLL_SECS}s")

    while not bot.is_closed():
        for ch_id_str, cfg in CTF_CONFIGS.items():
            ch_id = int(ch_id_str)
            channel = bot.get_channel(ch_id)
            if not channel:
                print(f"⚠️  Channel {ch_id_str} not found — check CTF_CONFIGS")
                continue

            posted = channel_posted_ids.setdefault(ch_id, set())
            name = cfg.get("ctf_name", ch_id_str)

            try:
                for solve in fetch_solves(cfg):
                    if solve["id"] not in posted:
                        await channel.send(
                            embed=build_embed(solve, cfg.get("ctf_name", "")))
                        posted.add(solve["id"])
                        print(
                            f"✅ [{name}] [{solve['category']}] {solve['challenge_name']}"
                            f" — {solve['points']} pts — by {solve['solver']}")
            except Exception as e:
                print(f"❌ Poll error [{name}]: {e}")

        await asyncio.sleep(POLL_SECS)


# ── Bot events ───────────────────────────────────────────────────────────────


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    print(f"   Channels in config: {list(CTF_CONFIGS.keys())}")

    for ch_id_str, cfg in CTF_CONFIGS.items():
        channel = bot.get_channel(int(ch_id_str))
        if channel:
            ids = await recover_channel(channel)
            channel_posted_ids[int(ch_id_str)] = ids
        else:
            print(f"⚠️  Channel {ch_id_str} not found at startup")

    asyncio.ensure_future(poll_loop())


# ── Helper: get CTF config for the current channel ──────────────────────────


def _cfg(ctx) -> dict | None:
    return CTF_CONFIGS.get(str(ctx.channel.id))


# ── Commands ─────────────────────────────────────────────────────────────────


@bot.command(name="solves")
async def cmd_solves(ctx):
    """!solves — show all current team solves and mark them as posted."""
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(
            f"❌ Channel `{ctx.channel.id}` is not configured. Use `!ctfs` to see active channels."
        )
        return
    solves = fetch_solves(cfg)
    if not solves:
        await ctx.send("No solves found yet.")
        return
    posted = channel_posted_ids.setdefault(ctx.channel.id, set())
    await ctx.send(f"📋 **{len(solves)} solve(s)** — {cfg.get('ctf_name', '')}:"
                   )
    for solve in solves:
        await ctx.channel.send(
            embed=build_embed(solve, cfg.get("ctf_name", "")))
        posted.add(solve["id"])


@bot.command(name="testsolves")
async def cmd_testsolves(ctx):
    """!testsolves — post all solves chronologically as TEST embeds (not tracked, safe to repeat)."""
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(f"❌ Channel `{ctx.channel.id}` is not configured.")
        return
    solves = fetch_solves(cfg)
    if not solves:
        await ctx.send("No solves found.")
        return
    await ctx.send(
        f"🧪 **Test mode** — {len(solves)} solve(s) in order (not tracked by bot):"
    )
    for solve in solves:
        await ctx.channel.send(
            embed=build_embed(solve, cfg.get("ctf_name", ""), test=True))


@bot.command(name="status")
async def cmd_status(ctx):
    """!status — show this channel's CTF config and bot state."""
    cfg = _cfg(ctx)
    if not cfg:
        await ctx.send(
            f"❌ Channel `{ctx.channel.id}` is not configured. Use `!ctfs` to see active channels."
        )
        return

    url = cfg["ctf_url"]
    token = cfg.get("ctfd_token") or None
    team_id = int(cfg.get("team_id", 0))
    posted = channel_posted_ids.get(ctx.channel.id, set())

    if team_id:
        team = _api_get(url, token, f"/teams/{team_id}") or {}
        mode = f"Team {team_id} ({team.get('name', '?')}) | members: {team.get('members', [])}"
    else:
        me = _api_get(url, token, "/users/me") or {}
        mode = f"Solo — {me.get('name', '?')} (user id {me.get('id', '?')})"

    await ctx.send(
        f"🤖 **Status for this channel**\n"
        f"```\n"
        f"CTF       : {cfg.get('ctf_name', '?')}\n"
        f"URL       : {url}\n"
        f"Auth      : {'token set' if token else 'no token (public API)'}\n"
        f"Mode      : {mode}\n"
        f"Tracked   : {len(posted)} solve ID(s) in memory\n"
        f"Poll rate : every {POLL_SECS}s\n"
        f"```")


@bot.command(name="ctfs")
async def cmd_ctfs(ctx):
    """!ctfs — list all CTFs and which channels they post to."""
    if not CTF_CONFIGS:
        await ctx.send(
            "⚠️  No CTFs configured. Set the `CTF_CONFIGS` environment variable."
        )
        return
    lines = ["**🗂️ Configured CTFs:**"]
    for ch_id_str, cfg in CTF_CONFIGS.items():
        posted = channel_posted_ids.get(int(ch_id_str), set())
        lines.append(
            f"  • <#{ch_id_str}> → **{cfg.get('ctf_name', '?')}** "
            f"(`{cfg.get('ctf_url', '')}`) | {len(posted)} tracked solve(s)")
    await ctx.send("\n".join(lines))


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
