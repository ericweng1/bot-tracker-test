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

        await interaction.followup.send(
            f"✅ Updated **Ch {self.ch_num}** timer to **{mins} minutes**.",
            ephemeral=True,
        )
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
                    f"ℹ️ You already have dibs placed on **Ch {ch}**. Use **Cancel My Dibs** to remove it.", ephemeral=True
                )
                return

            info["queue"].append(interaction.user.id)

        await interaction.followup.send(
            f"🙋 You claimed dibs on **Ch {ch}**! You are next in line.",
            ephemeral=True,
        )
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
        label="Type 1 for Channel Reset | Type 2 for Dibs Only",
        placeholder="1 = Full Channel Reset | 2 = Clear Dibs Only",
        default="1",
        min_length=1,
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            ch = int(self.ch_input.value)
            val = self.action_input.value.strip().lower()
            if ch < 1 or ch > TOTAL_CHANNELS:
                await interaction.followup.send(
                    f"❌ Enter a valid channel between **1** and **{TOTAL_CHANNELS}**.",
                    ephemeral=True,
                )
                return
        except ValueError:
            await interaction.followup.send(
                "❌ Please enter a valid channel number.", ephemeral=True
            )
            return

        async with data_lock:
            if ch not in channel_data:
                await interaction.followup.send(
                    f"ℹ️ **Ch {ch}** is already open.", ephemeral=True
                )
                return

            if val in ["2", "dibs", "dib"]:
                channel_data[ch]["queue"] = []
                msg = f"🛡️ Cleared all dibs for **Ch {ch}**. Active holder preserved."
            elif val in ["1", "channel", "chan", "all"]:
                del channel_data[ch]
                msg = f"🛡️ Completely reset **Ch {ch}** back to open."
            else:
                await interaction.followup.send(
                    "❌ Invalid choice! Enter **1** for Full Reset or **2** for Dibs Only.",
                    ephemeral=True,
                )
                return

        await interaction.followup.send(msg, ephemeral=True)
        await update_tracker_message()


class ChannelPageSelect(discord.ui.Select):
    def __init__(self, current_page_num: int):
        options = [
            discord.SelectOption(
                label="Channels 1 – 15",
                value="1",
                description="Show claim buttons for Ch 1 through Ch 15",
                default=(current_page_num == 1),
            ),
            discord.SelectOption(
                label="Channels 16 – 30",
                value="2",
                description="Show claim buttons for Ch 16 through Ch 30",
                default=(current_page_num == 2),
            ),
            discord.SelectOption(
                label="Channels 31 – 40",
                value="3",
                description="Show claim buttons for Ch 31 through Ch 40",
                default=(current_page_num == 3),
            ),
        ]
        super().__init__(
            placeholder="Select Channel Group...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="channel_page_select",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        global current_page
        current_page = int(self.values[0])
        await interaction.response.edit_message(
            embed=build_embed(), view=TrackerView(page=current_page)
        )
        await save_data()


class ChannelButton(discord.ui.Button):
    def __init__(self, ch_num: int, is_taken: bool, row_num: int):
        style = (
            discord.ButtonStyle.secondary
            if is_taken
            else discord.ButtonStyle.success
        )
        super().__init__(
            label=f"Ch {ch_num}",
            style=style,
            custom_id=f"claim_ch_{ch_num}",
            row=row_num,
        )
        self.ch_num = ch_num

    async def callback(self, interaction: discord.Interaction):
        now = int(time.time())

        async with data_lock:
            current = channel_data.get(self.ch_num)
            if current and current["end_time"] > now:
                if current["user_id"] != interaction.user.id:
                    await interaction.response.send_message(
                        f"⚠️ **Ch {self.ch_num}** is currently claimed by"
                        f" <@{current['user_id']}>. Use **Claim Dibs** to get next in line!",
                        ephemeral=True,
                    )
                    return
                else:
                    await interaction.response.send_message(
                        f"ℹ️ You already hold **Ch {self.ch_num}**. Use **Edit Timer** to"
                        " adjust your time.",
                        ephemeral=True,
                    )
                    return

            # Release any other channel this user currently holds
            for ch, info in list(channel_data.items()):
                if info["user_id"] == interaction.user.id:
                    del channel_data[ch]

            channel_data[self.ch_num] = {
                "user_id": interaction.user.id,
                "end_time": now + (DEFAULT_MINUTES * 60),
                "queue": [],
            }

        await interaction.response.send_message(
            f"🚇 You claimed **Ch {self.ch_num}** for **{format_duration(DEFAULT_MINUTES)}**!",
            ephemeral=True,
        )
        await update_tracker_message()


class ClaimDibsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🙋 Claim Dibs",
            style=discord.ButtonStyle.success,
            custom_id="claim_dibs_btn",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ClaimDibsModal())


class CancelDibsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="❌ Cancel My Dibs",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_my_dibs",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        removed_channels = []
        async with data_lock:
            for ch, info in channel_data.items():
                if "queue" in info and interaction.user.id in info["queue"]:
                    info["queue"].remove(interaction.user.id)
                    removed_channels.append(ch)

        if removed_channels:
            ch_list = ", ".join([f"**Ch {ch}**" for ch in removed_channels])
            await interaction.response.send_message(
                f"✅ Removed your dibs from {ch_list}.", ephemeral=True
            )
            await update_tracker_message()
        else:
            await interaction.response.send_message(
                "ℹ️ You don't have active dibs placed on any channel.", ephemeral=True
            )


class EditTimerButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="⏱️ Edit My Timer",
            style=discord.ButtonStyle.primary,
            custom_id="edit_my_timer",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        now = int(time.time())
        user_ch = next(
            (
                ch
                for ch, info in channel_data.items()
                if info["user_id"] == interaction.user.id and info["end_time"] > now
            ),
            None,
        )
        if not user_ch:
            await interaction.response.send_message(
                "❌ You don't currently have a channel claimed!", ephemeral=True
            )
            return
        await interaction.response.send_modal(EditTimerModal(user_ch))


class LeaveChannelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🚪 Leave Channel",
            style=discord.ButtonStyle.danger,
            custom_id="leave_my_channel",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        async with data_lock:
            user_ch = next(
                (
                    ch
                    for ch, info in channel_data.items()
                    if info["user_id"] == interaction.user.id
                ),
                None,
            )
            if not user_ch:
                await interaction.response.send_message(
                    "❌ You don't have a channel claimed.", ephemeral=True
                )
                return
            del channel_data[user_ch]

        await interaction.response.send_message(
            f"✅ You released **Ch {user_ch}**.", ephemeral=True
        )
        await update_tracker_message()


class HelperClearButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🛡️ Helper Clear",
            style=discord.ButtonStyle.secondary,
            custom_id="helper_clear_btn",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_helper(interaction.user):
            await interaction.response.send_message(
                f"❌ Only users with the **{HELPER_ROLE_NAME}** role can use this.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(HelperClearModal())


class TrackerView(discord.ui.View):
    def __init__(self, page: int = 1):
        super().__init__(timeout=None)
        now = int(time.time())
        self.add_item(ChannelPageSelect(page))

        start_ch = (page - 1) * 15 + 1
        end_ch = min(page * 15, TOTAL_CHANNELS)

        for idx, ch in enumerate(range(start_ch, end_ch + 1)):
            row_num = 1 + (idx // 5)
            info = channel_data.get(ch)
            is_taken = bool(info and info["end_time"] > now)
            self.add_item(ChannelButton(ch, is_taken, row_num))

        self.add_item(ClaimDibsButton())
        self.add_item(CancelDibsButton())
        self.add_item(EditTimerButton())
        self.add_item(LeaveChannelButton())
        self.add_item(HelperClearButton())


@tasks.loop(seconds=45)
async def cleanup_expired():
    now = int(time.time())
    expired = []
    async with data_lock:
        expired = [
            ch for ch, info in channel_data.items() if info["end_time"] <= now
        ]
        for ch in expired:
            del channel_data[ch]

    if expired:
        await update_tracker_message()


@bot.event
async def on_ready():
    load_data()
    bot.add_view(TrackerView(page=current_page))
    if not cleanup_expired.is_running():
        cleanup_expired.start()
    print(f"Logged in as {bot.user}!")
    await update_tracker_message()


# Command to post the channel claim embed in Discord
@bot.command(name="b2test")
@commands.has_permissions(manage_messages=True)
async def setup_b2(ctx):
    await ctx.message.delete()

    old_ch_id = tracker_message_info.get("channel_id")
    old_msg_id = tracker_message_info.get("message_id")
    if old_ch_id and old_msg_id:
        old_channel = bot.get_channel(old_ch_id)
        if old_channel:
            try:
                old_msg = await old_channel.fetch_message(old_msg_id)
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    msg = await ctx.send(embed=build_embed(), view=TrackerView(page=1))
    tracker_message_info["channel_id"] = msg.channel.id
    tracker_message_info["message_id"] = msg.id
    global current_page
    current_page = 1
    await save_data()


@bot.command()
async def clear_ch(ctx, ch: int):
    if not is_helper(ctx.author):
        return
    await ctx.message.delete()

    cleared = False
    async with data_lock:
        if ch in channel_data:
            del channel_data[ch]
            cleared = True

    if cleared:
        await update_tracker_message()
        try:
            await ctx.send(f"🛡️ Cleared **Ch {ch}**.", delete_after=5)
        except discord.HTTPException:
            pass
    else:
        try:
            await ctx.send(f"ℹ️ **Ch {ch}** is already open.", delete_after=5)
        except discord.HTTPException:
            pass


@bot.command()
async def clear_all(ctx):
    if not is_helper(ctx.author):
        return
    await ctx.message.delete()

    async with data_lock:
        channel_data.clear()

    await update_tracker_message()
    try:
        await ctx.send("🛡️ All channels cleared.", delete_after=5)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    keep_alive()
    bot.run(BOT_TOKEN)
