import discord
from discord.ext import commands, tasks
import requests
from datetime import datetime
import os

BLANK = "\u200e"

CTFD_TOKEN = os.getenv("CTFD_TOKEN")
CTFD_URL   = os.getenv("CTFD_URL", "https://challs.codevinci.it")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
BOT_TOKEN  = os.getenv("TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory set of solve IDs that have already been posted.
# Loaded on startup so the bot never re-posts old solves after a restart.
posted_ids: set[int] = set()


# ── CTFd API ──────────────────────────────────────────────────────────────────

def _api_get(path: str):
    """GET from CTFd. allow_redirects=False is critical — CTFd sends a 302
    redirect to /login when the auth header is stripped by the redirect."""
    headers = {
        "Authorization": f"Token {CTFD_TOKEN}",
        "Content-Type":  "application/json",
    }
    resp = requests.get(
        f"{CTFD_URL}/api/v1{path}",
        headers=headers,
        allow_redirects=False,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["data"]


def fetch_solves() -> list[dict]:
    """Return all solves sorted oldest→newest."""
    raw = _api_get("/users/me/solves")
    result = []
    for s in sorted(raw, key=lambda x: x.get("date", "")):
        chall = s["challenge"]
        try:
            dt = datetime.fromisoformat(s["date"]).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            dt = s.get("date", "?")
        result.append({
            "id":             s["id"],          # unique solve ID — used for dedup
            "challenge_name": chall["name"],
            "category":       chall["category"],
            "points":         chall["value"],
            "solver":         s["user"]["name"], # real solver name from API
            "solved_at":      dt,
        })
    return result


# ── Discord embed ─────────────────────────────────────────────────────────────

def build_embed(solve: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"{BLANK}🚩 Challenge Solved!",
        color=discord.Color.green(),
    )
    embed.add_field(name="🧩 Challenge", value=solve["challenge_name"], inline=False)
    embed.add_field(name="📂 Category",  value=solve["category"],       inline=True)
    embed.add_field(name="💰 Points",    value=str(solve["points"]),    inline=True)
    embed.add_field(name="👤 Solver",    value=solve["solver"],         inline=False)
    embed.add_field(name="🕐 Solved At", value=solve["solved_at"],      inline=False)
    embed.set_footer(text=BLANK)
    return embed


# ── Background polling task ───────────────────────────────────────────────────

@tasks.loop(minutes=5)
async def poll_new_solves():
    """Every 5 minutes: fetch solves and post any we haven't seen before."""
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("⚠️  Channel not found — check CHANNEL_ID")
        return
    try:
        solves = fetch_solves()
        for solve in solves:
            if solve["id"] not in posted_ids:
                await channel.send(embed=build_embed(solve))
                posted_ids.add(solve["id"])
                print(f"Posted solve: {solve['challenge_name']} by {solve['solver']}")
    except Exception as e:
        print(f"Error polling solves: {e}")


@poll_new_solves.before_loop
async def before_poll():
    await bot.wait_until_ready()


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

    # Silently load all current solve IDs so we never re-post them after restart.
    try:
        solves = fetch_solves()
        for s in solves:
            posted_ids.add(s["id"])
        print(f"Loaded {len(posted_ids)} existing solve(s) — will only post new ones.")
    except Exception as e:
        print(f"Warning: could not load initial solves: {e}")

    poll_new_solves.start()


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.command(name="solves")
async def cmd_solves(ctx):
    """!solves — show all current solves in this channel."""
    if ctx.channel.id != CHANNEL_ID:
        await ctx.send("❌ This command can only be used in the configured channel.")
        return
    solves = fetch_solves()
    if not solves:
        await ctx.send("No solves found yet.")
        return
    await ctx.send(f"📋 **{len(solves)} solve(s) so far:**")
    for solve in solves:
        await ctx.channel.send(embed=build_embed(solve))


@bot.command(name="testsolves")
async def cmd_testsolves(ctx):
    """!testsolves — post all CodeVinci solves in order (testing only).
    Does NOT update the dedup set, so solves remain 'new' for the poller."""
    solves = fetch_solves()
    if not solves:
        await ctx.send("No solves found.")
        return
    await ctx.send(f"🧪 Test mode — sending **{len(solves)}** solve(s) in chronological order:")
    for solve in solves:
        await ctx.channel.send(embed=build_embed(solve))


@bot.command(name="status")
async def cmd_status(ctx):
    """!status — show bot status and tracking info."""
    await ctx.send(
        f"🤖 Bot is running.\n"
        f"📌 Tracking CTF: `{CTFD_URL}`\n"
        f"🗂️  Solve IDs in memory: `{len(posted_ids)}`\n"
        f"🔄 Polling every **5 minutes** for new solves."
    )


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
