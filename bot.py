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

solved_cache = set()

app = Flask('')

@app.route('/')
def home():
    return "CTF Bot is alive"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

async def check_solves():
    await client.wait_until_ready()

    channel = client.get_channel(CHANNEL_ID)

    headers = {
        "Authorization": f"Token {CTFD_TOKEN}"
    }

    while not client.is_closed():

        try:

            async with aiohttp.ClientSession() as session:

                url = f"{CTFD_URL}/api/v1/teams/{TEAM_ID}/solves"

                async with session.get(url, headers=headers) as resp:

                    data = await resp.json()

                solves = data["data"]

                for solve in solves:

                    solve_id = solve["id"]

                    if solve_id not in solved_cache:

                        solved_cache.add(solve_id)

                        challenge = solve["challenge"]["name"]
                        category = solve["challenge"]["category"]
                        points = solve["challenge"]["value"]
                        solver = solve["user"]["name"]

                        embed = discord.Embed(
                            title="🚩 Challenge Solved!",
                            color=discord.Color.green()
                        )

                        embed.add_field(
                            name="🧩 Challenge",
                            value=challenge,
                            inline=False
                        )

                        embed.add_field(
                            name="📂 Category",
                            value=category,
                            inline=True
                        )

                        embed.add_field(
                            name="💰 Points",
                            value=points,
                            inline=True
                        )

                        embed.add_field(
                            name="👨‍💻 Solver",
                            value=solver,
                            inline=False
                        )

                        await channel.send(embed=embed)

            await asyncio.sleep(30)

        except Exception as e:

            print("Error:", e)

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

        embed.add_field(
            name="🧩 Challenge",
            value="Buffer Overflow 1",
            inline=False
        )

        embed.add_field(
            name="📂 Category",
            value="Pwn",
            inline=True
        )

        embed.add_field(
            name="💰 Points",
            value="200",
            inline=True
        )

        embed.add_field(
            name="👨‍💻 Solver",
            value="Tushar",
            inline=False
        )

        await message.channel.send(embed=embed)

@client.event
async def on_ready():

    print(f"Logged in as {client.user}")

    client.loop.create_task(check_solves())

keep_alive()

client.run(TOKEN)
