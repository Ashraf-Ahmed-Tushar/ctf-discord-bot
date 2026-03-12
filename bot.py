import discord
import aiohttp
import asyncio
import os
from flask import Flask
from threading import Thread

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
CTFD_URL = os.getenv("CTFD_URL")
CTFD_TOKEN = os.getenv("CTFD_TOKEN")
TEAM_ID = os.getenv("TEAM_ID")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

known_solves = set()

app = Flask('')

@app.route('/')
def home():
    return "alive"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

BLANK_LINE = "\u200e"  # special invisible character for consistent spacing

async def get_solves():
    url = f"{CTFD_URL}/api/v1/teams/{TEAM_ID}/solves"
    headers = {"Authorization": f"Token {CTFD_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
    return data["data"]

async def announce_solve(channel, solve):
    challenge = solve["challenge"]["name"]
    category = solve["challenge"]["category"]
    points = solve["challenge"]["value"]
    solver = solve["user"]["name"]

    msg = f"""
{BLANK_LINE}
🚩 Challenge Solved

🧩 {challenge}
📂 {category}
💰 {points}
👨‍💻 {solver}
{BLANK_LINE}
"""
    await channel.send(msg)

async def solve_tracker():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)

    solves = await get_solves()
    solves.sort(key=lambda x: x["date"])

    # announce past solves
    for solve in solves:
        solve_id = solve["id"]
        known_solves.add(solve_id)
        await announce_solve(channel, solve)
        await asyncio.sleep(2)

    # track new solves
    while not client.is_closed():
        try:
            solves = await get_solves()
            for solve in solves:
                solve_id = solve["id"]
                if solve_id not in known_solves:
                    known_solves.add(solve_id)
                    await announce_solve(channel, solve)
        except Exception as e:
            print(e)
        await asyncio.sleep(60)

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.content == "!testsolve":
        msg = f"""
{BLANK_LINE}
🚩 Challenge Solved

🧩 Buffer Overflow 1
📂 Pwn
💰 200
👨‍💻 _2shar_
{BLANK_LINE}
"""
        await message.channel.send(msg)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    client.loop.create_task(solve_tracker())

keep_alive()
client.run(TOKEN)
