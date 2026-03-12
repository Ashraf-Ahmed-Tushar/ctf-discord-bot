import os
import discord
import requests
from datetime import datetime

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CODEVINCI_TOKEN = os.environ.get("CODEVINCI_TOKEN")
BASE = "https://challs.codevinci.it"

if not DISCORD_TOKEN or not CODEVINCI_TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN and CODEVINCI_TOKEN in environment variables")

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Bot(intents=intents)

def api_get(path: str):
    headers = {
        "Authorization": f"Token {CODEVINCI_TOKEN}",
        "Content-Type": "application/json"
    }
    response = requests.get(f"{BASE}/api/v1{path}", headers=headers)
    response.raise_for_status()
    return response.json()["data"]

def format_solve(solve: dict) -> str:
    chall = solve["challenge"]
    solver = solve.get("user", {}).get("name", "_unknown_")
    pts = chall["value"]
    category = chall["category"]
    name = chall["name"]
    special_space = "\u200e"
    return (
        f"{special_space}\n"
        f"🚩 Challenge Solved\n\n"
        f"🧩 {name}\n"
        f"📂 {category}\n"
        f"💰 {pts}\n"
        f"👨‍💻 {solver}\n"
        f"{special_space}"
    )

@bot.slash_command(description="Post your CodeVinci solves")
async def testsolve(ctx: discord.ApplicationContext):
    await ctx.defer()
    last_msgs = [m async for m in ctx.channel.history(limit=30)]
    last_content = {m.content for m in last_msgs}
    try:
        solves_data = api_get("/users/me/solves")
    except Exception as e:
        await ctx.respond(f"Error fetching solves: {e}")
        return
    sorted_solves = sorted(solves_data, key=lambda x: x.get("date", ""))
    posted_count = 0
    for solve in sorted_solves:
        msg_text = format_solve(solve)
        if msg_text in last_content:
            continue
        await ctx.channel.send(msg_text)
        posted_count += 1
    if posted_count == 0:
        await ctx.respond("No new solves to post.")
    else:
        await ctx.respond(f"Posted {posted_count} new solve(s).")

bot.run(DISCORD_TOKEN)
