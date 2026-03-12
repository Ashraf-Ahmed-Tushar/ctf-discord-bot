import discord
from discord.ext import commands
import requests
from datetime import datetime
import os

BLANK_SPACE = "\u200e" 

CTFD_TOKEN = os.getenv("CTFD_TOKEN")
CTFD_URL = os.getenv("CTFD_URL")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def api_get(path):
    headers = {
        "Authorization": f"Token {CTFD_TOKEN}",
        "Content-Type": "application/json"
    }
    resp = requests.get(f"{CTFD_URL}/api/v1{path}", headers=headers)
    resp.raise_for_status()
    return resp.json()["data"]

def fetch_solves():
    solves_data = api_get("/users/me/solves")
    solves = []
    for s in sorted(solves_data, key=lambda x: x.get("date", "")):
        chall = s["challenge"]
        try:
            dt = datetime.fromisoformat(s["date"]).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            dt = s.get("date", "?")
        solves.append({
            "challenge_name": chall["name"],
            "category": chall["category"],
            "points": chall["value"],
            "solver": os.getenv("SOLVER_NAME", "_2shar_"),  # Optional env var for solver name
            "solved_at": dt
        })
    return solves

async def has_recent_duplicate(channel, challenge_name):
    async for message in channel.history(limit=30):
        if message.embeds:
            embed = message.embeds[0]
            if embed.title == f"{BLANK_SPACE}🚩 Challenge Solved":
                for field in embed.fields:
                    if field.name == "🧩 Challenge" and field.value == challenge_name:
                        return True
    return False

async def send_solve_message(channel, solve):
    if await has_recent_duplicate(channel, solve["challenge_name"]):
        return
    embed = discord.Embed(
        title=f"{BLANK_SPACE}🚩 Challenge Solved",
        color=discord.Color.green()
    )
    embed.add_field(name="🧩 Challenge", value=solve["challenge_name"], inline=False)
    embed.add_field(name="📂 Category", value=solve["category"], inline=False)
    embed.add_field(name="💰 Points", value=str(solve["points"]), inline=False)
    embed.add_field(name="👨‍💻 Solver", value=solve["solver"], inline=False)
    embed.set_footer(text=BLANK_SPACE)
    await channel.send(embed=embed)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.command()
async def solves(ctx):
    if ctx.channel.id != CHANNEL_ID:
        await ctx.send("This command can only be used in the configured channel.")
        return
    solves = fetch_solves()
    if not solves:
        await ctx.send("No solves found.")
        return
    for solve in solves:
        await send_solve_message(ctx.channel, solve)

if __name__ == "__main__":
    bot.run(os.getenv("TOKEN"))
