import json
import os
import asyncio
from threading import Thread
import time
import discord
from discord.ext import commands, tasks
from flask import Flask

# --- CONFIGURATION ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TOTAL_CHANNELS = 40
DEFAULT_MINUTES = 98
HELPER_ROLE_NAME = "B2 Helper"
DATA_FILE = "b2_channels.json"
AUTO_DELETE_SECONDS = 5  # Time before public notification messages self-destruct
# ---------------------

# --- KEEP-ALIVE WEB SERVER ---
app = Flask("")

@app.route("/")
def home():
    return "B2 Subway Tracker Bot is online!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.start()
# -----------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

channel_data = {}
tracker_message_info = {"channel_id": None, "message_id": None}
current_page = 1
data_lock = asyncio.Lock()


def is_helper(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.name == HELPER_ROLE_NAME for role in member.roles)


def save_data_sync():
    """Synchronous save – never call this directly from the event loop."""
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(
                {
                    "channels": channel_data,
                    "tracker": tracker_message_info,
                    "current_page": current_page,
                },
                f,
            )
    except Exception as e:
        print(f"[save_data] Failed to save: {e}")


async def save_data():
    """Non-blocking save."""
    await asyncio.to_thread(save_data_sync)


def load_data():
    global channel_data, tracker_message_info, current_page
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                channel_data = {int(k): v for k, v in data.get("channels", {}).items()}
                tracker_message_info = data.get(
                    "tracker", {"channel_id": None, "message_id": None}
                )
                current_page = data.get("current_page", 1)
        except Exception as e:
            print(f"[load_data] Failed to load: {e}")


def format_duration(minutes: int) -> str:
    hours = minutes // 60
    rem = minutes % 60
    if hours and rem:
        return f"{hours}h {rem}m"
    if hours:
        return f"{hours}h"
    return f"{rem}m"


def build_embed():
    now = int(time.time())
    embed = discord.Embed(
        title="🚇 B2 Subway Line — Jr. Wraith Channel Tracker",
        description=(
            "Use the **Dropdown** to switch channel pages (**1–15**, **16–30**,"
            " **31–40**).\nClick a **Channel Button** to claim it for"
            f" **{format_duration(DEFAULT_MINUTES)}**, or use **Claim Dibs** to queue next!"
        ),
        color=discord.Color.purple(),
    )

    lines_left = []
    lines_right = []

    for ch in range(1, TOTAL_CHANNELS + 1):
        info = channel_data.get(ch)
        if info and info["end_time"] > now:
            holder = f"<@{info['user_id']}>"
            queue_list = info.get("queue", [])
            queue_str = ""
            if queue_list:
                next_user = queue_list[0]
                queue_str = f" ➡️ *Dibs: <@{next_user}>*"

            entry = f"**Ch {ch}:** {holder} — expires <t:{info['end_time']}:R>{queue_str}"
        else:
            if ch in channel_data:
                del channel_data[ch]
            entry = f"**Ch {ch}:** 🟢 *Open*"

        if ch <= 20:
            lines_left.append(entry)
        else:
            lines_right.append(entry)

    embed.add_field(
        name="Channels 1 – 20", value="\n".join(lines_left) or "—", inline=True
    )
    embed.add_field(
        name="Channels 21 – 40", value="\n".join(lines_right) or "—", inline=True
    )
    embed.set_footer(text="Timers automatically clear when they hit 0.")
    return embed


async def update_tracker_message():
    await save_data()

    ch_id = tracker_message_info.get("channel_id")
    msg_id = tracker_message_info.get("message_id")
    if not ch_id or not msg_id:
        return

    channel = bot.get_channel(ch_id)
    if not channel:
        return

    try:
        msg = await channel.fetch_message(msg_id)
        await msg.edit(embed=build_embed(), view=TrackerView(page=current_page))
    except discord.NotFound:
        tracker_message_info["channel_id"] = None
        tracker_message_info["message_id"] = None
        await save_data()
    except discord.HTTPException as e:
        print(f"[update_tracker] HTTPException: {e}")


async def auto_delete_msg(msg):
    """Utility to safely delete a message after AUTO_DELETE_SECONDS delay."""
    try:
        await asyncio.sleep(AUTO_DELETE_SECONDS)
        await msg.delete()
    except (discord.NotFound, discord.HTTPException, AttributeError):
        pass


class EditTimerModal(discord.ui.Modal, title="Edit Remaining Channel Time"):
    minutes_input = discord.ui.TextInput(
        label=f"Minutes Remaining (1 to {DEFAULT_MINUTES})",
        placeholder="e.g., 45",
        min_length=1,
        max_length=2,
        required=True,
    )

    def __init__(self, ch_num: int):
        super().__init__()
        self.ch_num = ch_num

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            mins = int(self.minutes_input.value)
            if mins < 1 or mins > DEFAULT_MINUTES:
                await interaction.followup.send(
                    f"❌ Please enter a number between **1** and **{DEFAULT_MINUTES}**.",
                    ephemeral=True,
                )
                return
        except ValueError:
            await interaction.followup.send(
                "❌ Please enter a valid number.", ephemeral=True
            )
            return

        async with data_lock:
            if self.ch_num not in channel_data:
                await interaction.followup.send(
                    f"❌ **Ch {self.ch_num}** is no longer claimed.", ephemeral=True
                )
                return
            channel_data[self.ch_num]["end_time"] = int(time.time()) + (mins * 60)

        # Confirm to user publicly & auto-delete
        msg = await interaction.followup.send(
            f"⏱️ <@{interaction.user.id}> updated **Ch {self.ch_num}** timer to **{mins} mins**.",
            wait=True,
        )
        asyncio.create_task(auto_delete_msg(msg))
        await update_tracker_message()


class ClaimDibsModal(discord.ui.Modal, title="Claim Dibs on a Channel"):
    ch_input = discord.ui.TextInput(
        label=f"Channel Number to Queue For (1 to {TOTAL_CHANNELS})",
        placeholder="e.g., 14",
        min_length=1,
        max_length=2,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            ch = int(self.ch_input.value)
            if ch < 1 or ch > TOTAL_CHANNELS:
                await interaction.followup.send(
                    f"❌ Enter a valid channel between **1** and **{TOTAL_CHANNELS}**.",
                    ephemeral=True,
                )
                return
        except ValueError:
            await interaction.followup.send(
                "❌ Please enter a valid number.", ephemeral=True
            )
            return

        now = int(time.time())
        async with data_lock:
            info = channel_data.get(ch)
            if not info or info["end_time"] <= now:
                await interaction.followup.send(
                    f"ℹ️ **Ch {ch}** is currently open! You can claim it directly using the channel button.",
                    ephemeral=True,
                )
                return

            if info["user_id"] == interaction.user.id:
                await interaction.followup.send(
                    f"❌ You already own **Ch {ch}**!", ephemeral=True
                )
                return

            if "queue" not in info:
                info["queue"] = []

            if interaction.user.id in info["queue"]:
                await interaction.followup.send(
                    f"ℹ️ You already have dibs placed on **Ch {ch}**. Use **Cancel My Dibs** to remove it.",
                    ephemeral=True,
                )
                return

            info["queue"].append(interaction.user.id)

        # Send public notice & auto-delete after 5s
        msg = await interaction.followup.send(
            f"🙋 <@{interaction.user.id}> claimed dibs on **Ch {ch}**! They are next in line.",
            wait=True,
        )
        asyncio.create_task(auto_delete_msg(msg))
        await update_tracker_message()


class HelperClearModal(discord.ui.Modal, title="Helper Clear Option"):
    ch_input = discord.ui.TextInput(
        label=f"Channel Number (1 to {TOTAL_CHANNELS})",
        placeholder="e.g., 14",
        min_length=1,
        max_length=2,
        required=True,
    )
    action_input = discord.ui.TextInput(
        label="Type 1 (Full Reset) or 2 (Dibs Only)",
        placeholder="1 = Channel | 2 = Dibs",
        default="1",
        min_length=1,
        max_length=5,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.
