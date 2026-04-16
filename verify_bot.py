import discord
from discord.ext import commands, tasks
from discord import ui
import os
import re
import random
import string
import requests
from pymongo import MongoClient
import asyncio
import time
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=10000)

def keep_alive():
    t = threading.Thread(target=run)
    t.start()

TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
VERIFY_ROLE = int(os.getenv("VERIFY_ROLE"))

PREFIX = ";"

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

client = MongoClient(MONGO_URI)
db = client["ctfbot"]
verified = db["verified"]
pending = db["pending"]
spam_block = db["spam_block"]  # anti spam

# ---------------------
# Utility functions
# ---------------------

def gen_code():
    return "CTF-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

def fetch_profile(uid):
    url = f"https://ctftime.org/user/{uid}"
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        return None
    html = r.text
    name = re.search(r"<h2.*?>(.*?)</h2>", html)
    about = re.search(r"About</h3>.*?<p>(.*?)</p>", html, re.S)
    username = name.group(1).strip() if name else f"user{uid}"
    bio = about.group(1) if about else ""
    return {"username": username, "bio": bio, "url": url}

# ---------------------
# Anti-verify spam check
# ---------------------
def check_spam(user_id):
    record = spam_block.find_one({"discord": user_id})
    now = int(time.time())
    if record:
        last = record["last_time"]
        if now - last < 15:  # 15 sec cooldown
            return True
        spam_block.update_one({"discord": user_id}, {"$set": {"last_time": now}})
        return False
    else:
        spam_block.insert_one({"discord": user_id, "last_time": now})
        return False

# ---------------------
# On ready
# ---------------------
@bot.event
async def on_ready():
    print("Verify bot ready")

# ---------------------
# On member join
# ---------------------
@bot.event
async def on_member_join(member):
    guild = member.guild
    category = discord.utils.get(guild.categories, name="verification")
    if not category:
        category = await guild.create_category("verification")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    channel = await guild.create_text_channel(
        name=f"verify-{member.name}",
        category=category,
        overwrites=overwrites
    )
    # send button verify
    await channel.send(
        f"{member.mention} Welcome! Click below to verify your CTFtime account.",
        view=VerifyButtonView(member)
    )

# ---------------------
# Button Verify UI
# ---------------------
class VerifyButtonView(ui.View):
    def __init__(self, member):
        super().__init__(timeout=None)
        self.member = member

    @ui.button(label="Verify", style=discord.ButtonStyle.green)
    async def verify_button(self, interaction: discord.Interaction, button: ui.Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("Wait a few seconds before retrying.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Please use the command `;verify <CTFtime ID>` in this channel.", ephemeral=True
        )

# ---------------------
# Auto verify panel (admin can call)
# ---------------------
@bot.command()
@commands.has_permissions(administrator=True)
async def verify_panel(ctx):
    embed = discord.Embed(
        title="🔐 Verification Panel",
        description="Click the button below to verify your CTFtime account.",
        color=0x5865F2
    )
    await ctx.send(embed=embed, view=VerifyButtonView(ctx.author))

# ---------------------
# Existing verify / confirm / unverify commands
# ---------------------
@bot.command()
async def verify(ctx, ctftime_id: str = None):
    if check_spam(ctx.author.id):
        await ctx.send("You are doing this too fast, wait a few seconds.")
        return
    if not ctftime_id:
        embed = discord.Embed(
            title="🔐 CTFtime Verification",
            description="Link your CTFtime account to this server.",
            color=0x5865F2
        )
        embed.add_field(
            name="Example",
            value="`;verify 12345`\n\nFind your ID in:\nhttps://ctftime.org/user/**12345**"
        )
        await ctx.send(embed=embed)
        return

    uid = re.findall(r"\d+", ctftime_id)
    if not uid:
        await ctx.send("Invalid CTFtime ID.")
        return
    uid = int(uid[0])
    if verified.find_one({"discord": ctx.author.id}):
        await ctx.send("You are already verified.")
        return
    profile = fetch_profile(uid)
    if not profile:
        await ctx.send("Could not find that profile.")
        return
    code = gen_code()
    pending.update_one({"discord": ctx.author.id},{"$set": {"ctftime": uid, "code": code}}, upsert=True)
    embed = discord.Embed(
        title="Step 1 — Add verification code",
        description=f"Add this code to your CTFtime **About** section:\n\n```{code}```",
        color=0x5865F2
    )
    embed.add_field(name="Then run", value="`;confirm`")
    await ctx.send(embed=embed)

@bot.command()
async def confirm(ctx):
    data = pending.find_one({"discord": ctx.author.id})
    if not data:
        await ctx.send("No pending verification.")
        return
    profile = fetch_profile(data["ctftime"])
    if not profile:
        await ctx.send("Profile fetch failed.")
        return
    if data["code"] not in profile["bio"]:
        await ctx.send("Code not found in your profile.")
        return
    verified.insert_one({"discord": ctx.author.id,"ctftime": data["ctftime"],"username": profile["username"]})
    pending.delete_one({"discord": ctx.author.id})
    role = ctx.guild.get_role(VERIFY_ROLE)
    if role:
        await ctx.author.add_roles(role)
    try:
        await ctx.author.edit(nick=profile["username"])
    except:
        pass
    embed = discord.Embed(
        title="✅ Verification Complete",
        description="Welcome! Check:\n• #rules\n• #upcoming-ctf",
        color=0x57F287
    )
    await ctx.send(embed=embed)
    if ctx.channel.name.startswith("verify-"):
        await ctx.channel.delete()

@bot.command()
async def unverify(ctx):
    verified.delete_one({"discord": ctx.author.id})
    pending.delete_one({"discord": ctx.author.id})
    role = ctx.guild.get_role(VERIFY_ROLE)
    if role:
        await ctx.author.remove_roles(role)
    await ctx.send("Verification removed.")

keep_alive()
bot.run(TOKEN)
