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

app = Flask('')

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

BLANK_LINE = "\u200e"  

known_solves = set()

@app.route('/')
def home():
    return "Bot alive"

async def fetch_solves():
    url = f"{CTFD_URL}/api/v1/users/me/solves"
    headers = {"Authorization": f"Token {CTFD_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
    
    solves = sorted(data['data'], key=lambda x: x['date'])
    return solves

async def announce_solve(channel, solve):
    challenge = solve["challenge"]["name"]
    category = solve["challenge"]["category"]
    points = solve["challenge"]["value"]
    solver = solve["user"]["name"]

    msg = f"""
{BLANK_LINE}
🚩 Challenge Solved
🧩  {challenge}
📂  {category}
💰  {points}
👨‍💻  {solver}
{BLANK_LINE}
"""
    await channel.send(msg)

async def solve_tracker():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)

    solves = await fetch_solves()

    last_messages = [msg async for msg in channel.history(limit=30)]
    last_challenges = set()
    for msg in last_messages:
        if "🚩 Challenge Solved" in msg.content:
           
            lines = msg.content.splitlines()
            for line in lines:
                if line.startswith("🧩 "):
                    last_challenges.add(line[2:].strip())

    for solve in solves:
        challenge_name = solve["challenge"]["name"]
        if challenge_name not in last_challenges:
            known_solves.add(solve["id"])
            await announce_solve(channel, solve)
            await asyncio.sleep(2) 

    while not client.is_closed():
        try:
            solves = await fetch_solves()
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
🚩  Challenge Solved
🧩  Buffer Overflow 1
📂  Pwn
💰  200
👨‍💻  _2shar_
{BLANK_LINE}
"""
        await message.channel.send(msg)

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    client.loop.create_task(solve_tracker())

keep_alive()
client.run(TOKEN)
