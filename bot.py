import discord
import aiohttp
import asyncio
import os
from bs4 import BeautifulSoup
from flask import Flask
from threading import Thread

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
CTFD_URL = os.getenv("CTFD_URL")
TEAM_ID = os.getenv("TEAM_ID")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

seen_score = 0

app = Flask('')

@app.route('/')
def home():
    return "alive"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

async def check_score():
    global seen_score
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)

    while not client.is_closed():
        try:
            url = f"{CTFD_URL}/users/{TEAM_ID}"

            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            td = soup.find("td")

            if td:
                score = int(td.text)

                if score > seen_score:

                    embed = discord.Embed(
                        title="🚩 New Solve Detected!",
                        color=discord.Color.green()
                    )

                    embed.add_field(name="👥 Team", value="_R0Ot_Hunt3rs", inline=False)
                    embed.add_field(name="📊 Score", value=str(score), inline=True)
                    embed.add_field(name="🌐 CTF", value="Codevinci Test", inline=True)

                    await channel.send(embed=embed)

                    seen_score = score

        except Exception as e:
            print(e)

        await asyncio.sleep(60)

@client.event
async def on_message(message):

    if message.author == client.user:
        return

    if message.content == "!testsolve":

        embed = discord.Embed(
            title="🚩 Challenge Solved!",
            color=discord.Color.green()
        )

        embed.add_field(name="🧩 Challenge", value="Buffer Overflow 1", inline=False)
        embed.add_field(name="📂 Category", value="Pwn", inline=True)
        embed.add_field(name="💰 Points", value="200", inline=True)
        embed.add_field(name="👨‍💻 Solver", value="Tushar", inline=False)

        await message.channel.send(embed=embed)

@client.event
async def on_ready():
    print("Bot Online")
    client.loop.create_task(check_score())

keep_alive()
client.run(TOKEN)
