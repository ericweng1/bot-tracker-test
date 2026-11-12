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
DATA_FILE = "b2_unified_data.json"
AUTO_DELETE_SECONDS = 5
# ---------------------

# --- KEEP-ALIVE WEB SERVER ---
app = Flask("")

@app.route("/")
def home():
    return "B2 Subway Unified Tracker Bot is online!"

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
            "Use the **Dropdown** to switch channel pages (**1–10**, **11–20**,"
            " **21–30**, **31–40**).\n"
            "🟢 = Member Claimed | 🔴 = Non-Member Timer | 🟠 = Unknown\n"
            "Click a **Channel Button** to claim it as a member, or use **Log Non-Member**!"
        ),
        color=discord.Color.purple(),
    )

    lines_left = []
    lines_right = []

    for ch in range(1, TOTAL_CHANNELS + 1):
        info = channel_data.get(ch)
        if info and info["end_time"] > now:
            if info["type"] == "member":
                holder = f"<@{info['user_id']}>"
                queue_list = info.get("queue", [])
                queue_str = f" ➡️ *Dibs: <@{queue_list[0]}>*" if queue_list else ""
                entry = f"**Ch {ch}:** 🟢 {holder} — expires <t:{info['end_time']}:R>{queue_str}"
            else:
                name_label = info.get("name", "Non-Member")
                reporter = f"<@{info['user_id']}>"
                entry = f"**Ch {ch}:** 🔴 {name_label} — expires <t:{info['end_time']}:R> *(reported by {reporter})*"
        else:
            if ch in channel_data:
                del channel_data[ch]
            entry = f"**Ch {ch}:** 🟠 *Unknown*"

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


async def auto_delete_msg(msg):
    try:
        await asyncio.sleep(AUTO_DELETE_SECONDS)
        await msg.delete()
    except Exception:
        pass


# --- VIEWS & MODALS ---

class DibsApprovalView(discord.ui.View):
    def __init__(self, ch_num: int, owner_id: int, requester_id: int):
        super().__init__(timeout=60)
        self.ch_num = ch_num
        self.owner_id = owner_id
        self.requester_id = requester_id
        self.message = None

    async def on_timeout(self):
        async with data_lock:
            info = channel_data.get(self.ch_num)
            if info:
                info.pop("pending_dib", None)
        
        if self.message:
            try:
                await self.message.delete()
            except Exception:
                pass

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def accept_dibs(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Only the current channel owner can approve dibs.", ephemeral=True)
            return

        now = int(time.time())
        async with data_lock:
            info = channel_data.get(self.ch_num)
            if not info or info.get("end_time", 0) <= now or info.get("user_id") != self.owner_id:
                try:
                    await interaction.message.delete()
                except Exception:
                    pass
                await interaction.response.send_message("❌ This channel is no longer active or yours.", ephemeral=True)
                return

            info.pop("pending_dib", None)

            if "queue" not in info:
                info["queue"] = []

            if self.requester_id in info["queue"]:
                try:
                    await interaction.message.delete()
                except Exception:
                    pass
                await interaction.response.send_message(f"ℹ️ <@{self.requester_id}> is already in the dibs queue for **Ch {self.ch_num}**.", ephemeral=True)
                return

            info["queue"].append(self.requester_id)

        try:
            await interaction.message.delete()
        except Exception:
            pass

        await interaction.response.send_message(f"✅ <@{self.owner_id}> accepted dibs for <@{self.requester_id}> on **Ch {self.ch_num}**!", ephemeral=True)
        asyncio.create_task(auto_delete_msg(await interaction.original_response()))
        await update_tracker_message()

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline_dibs(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Only the current channel owner can decline dibs.", ephemeral=True)
            return

        async with data_lock:
            info = channel_data.get(self.ch_num)
            if info:
                info.pop("pending_dib", None)

        try:
            await interaction.message.delete()
        except Exception:
            pass

        await interaction.response.send_message(f"❌ <@{self.owner_id}> declined dibs request from <@{self.requester_id}> for **Ch {self.ch_num}**.", ephemeral=True)
        asyncio.create_task(auto_delete_msg(await interaction.original_response()))


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
                await interaction.followup.send(f"❌ Enter between 1 and {DEFAULT_MINUTES}.", ephemeral=True)
                return
        except ValueError:
            await interaction.followup.send("❌ Enter a valid number.", ephemeral=True)
            return

        async with data_lock:
            if self.ch_num not in channel_data:
                await interaction.followup.send("❌ Channel no longer active.", ephemeral=True)
                return
            channel_data[self.ch_num]["end_time"] = int(time.time()) + (mins * 60)

        msg = await interaction.followup.send(f"⏱️ <@{interaction.user.id}> updated **Ch {self.ch_num}** to **{mins}m**.", wait=True)
        asyncio.create_task(auto_delete_msg(msg))
        await update_tracker_message()


class CombinedDibsModal(discord.ui.Modal, title="Dibs Management"):
    input_field = discord.ui.TextInput(
        label="Channel # (to claim) OR type 'cancel'",
        placeholder="e.g., 14 or cancel",
        min_length=1,
        max_length=6,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        val = self.input_field.value.strip().lower()

        if val in ["cancel", "c", "remove"]:
            await interaction.response.defer(ephemeral=True)
            removed = []
            async with data_lock:
                for ch, info in channel_data.items():
                    if info.get("type") == "member":
                        if "queue" in info and interaction.user.id in info["queue"]:
                            info["queue"].remove(interaction.user.id)
                            removed.append(ch)
                        if info.get("pending_dib") == interaction.user.id:
                            info.pop("pending_dib", None)
                            removed.append(ch)

            if removed:
                ch_list = ", ".join([f"**Ch {ch}**" for ch in set(removed)])
                msg = await interaction.followup.send(f"✅ Removed your dibs/pending requests from {ch_list}.", wait=True)
                asyncio.create_task(auto_delete_msg(msg))
                await update_tracker_message()
            else:
                msg = await interaction.followup.send("ℹ️ You have no active dibs or pending requests to cancel.", wait=True)
                asyncio.create_task(auto_delete_msg(msg))
            return

        try:
            ch = int(val)
            if ch < 1 or ch > TOTAL_CHANNELS:
                await interaction.response.send_message(f"❌ Enter between 1 and {TOTAL_CHANNELS}.", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid channel number or 'cancel'.", ephemeral=True)
            return

        now = int(time.time())
        owner_id = None
        
        async with data_lock:
            for c, info in channel_data.items():
                if info.get("type") == "member":
                    if interaction.user.id in info.get("queue", []):
                        await interaction.response.send_message("❌ You already have active dibs on another channel! You can only hold one dib at a time.", ephemeral=True)
                        return
                    if info.get("pending_dib") == interaction.user.id:
                        await interaction.response.send_message("❌ You already have a pending dibs request waiting for approval on another channel!", ephemeral=True)
                        return

            info = channel_data.get(ch)
            if not info or info["end_time"] <= now or info["type"] == "non_member":
                await interaction.response.send_message("ℹ️ Channel is unknown or non-member! Claim it directly.", ephemeral=True)
                return

            if info["user_id"] == interaction.user.id:
                await interaction.response.send_message("❌ You already own this channel!", ephemeral=True)
                return

            if info.get("pending_dib"):
                await interaction.response.send_message("⚠️ There is already a pending dibs request for this channel. Please wait until it is processed.", ephemeral=True)
                return

            info["pending_dib"] = interaction.user.id
            owner_id = info["user_id"]

        await interaction.response.send_message(f"⏳ Sent a dibs request to <@{owner_id}> for **Ch {ch}**.", ephemeral=True)
        asyncio.create_task(auto_delete_msg(await interaction.original_response()))

        view = DibsApprovalView(ch, owner_id, interaction.user.id)
        msg = await interaction.channel.send(
            f"🔔 <@{owner_id}>, <@{interaction.user.id}> is requesting dibs for **Ch {ch}**. Do you accept or decline?",
            view=view
        )
        view.message = msg


class NonMemberTimerModal(discord.ui.Modal, title="Log / Clear Non-Member Timer"):
    ch_input = discord.ui.TextInput(
        label=f"Channel Number (1 to {TOTAL_CHANNELS})",
        placeholder="e.g., 14",
        min_length=1,
        max_length=2,
        required=True,
    )
    mins_input = discord.ui.TextInput(
        label="Minutes OR type 'clear'",
        placeholder="e.g., 30 or clear",
        min_length=1,
        max_length=5,
        required=True,
    )
    name_input = discord.ui.TextInput(
        label="Non-Member Name (Optional)",
        placeholder="Defaults to Non-Member if blank",
        min_length=0,
        max_length=25,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            ch = int(self.ch_input.value)
            if ch < 1 or ch > TOTAL_CHANNELS:
                await interaction.followup.send(f"❌ Enter between 1 and {TOTAL_CHANNELS}.", ephemeral=True)
                return
        except ValueError:
            await interaction.followup.send("❌ Enter a valid channel number.", ephemeral=True)
            return

        mins_val = self.mins_input.value.strip().lower()
        if mins_val in ["clear", "c", "remove", "delete"]:
            async with data_lock:
                current = channel_data.get(ch)
                if current and current.get("type") == "non_member":
                    del channel_data[ch]
                    cleared = True
                else:
                    cleared = False

            if cleared:
                msg = await interaction.followup.send(f"🧹 Cleared non-member timer for **Ch {ch}**.", wait=True)
                asyncio.create_task(auto_delete_msg(msg))
                await update_tracker_message()
            else:
                msg = await interaction.followup.send(f"ℹ️ **Ch {ch}** does not have an active non-member timer.", wait=True)
                asyncio.create_task(auto_delete_msg(msg))
            return

        try:
            mins = int(mins_val)
            if mins < 1:
                await interaction.followup.send("❌ Minutes must be at least 1.", ephemeral=True)
                return
        except ValueError:
            await interaction.followup.send("❌ Enter valid minutes or 'clear'.", ephemeral=True)
            return

        nm_name = self.name_input.value.strip()
        if not nm_name:
            nm_name = "Non-Member"

        now = int(time.time())
        async with data_lock:
            current = channel_data.get(ch)
            if current and current["end_time"] > now and current.get("type") == "member":
                await interaction.followup.send(f"❌ Cannot log non-member timer: **Ch {ch}** currently has an active member claim!", ephemeral=True)
                return

            channel_data[ch] = {
                "type": "non_member",
                "name": nm_name,
                "user_id": interaction.user.id,
                "end_time": now + (mins * 60)
            }

        msg = await interaction.followup.send(f"🔴 <@{interaction.user.id}> logged **{nm_name}** timer for **Ch {ch}** (**{mins}m**).", wait=True)
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
        await interaction.response.defer(ephemeral=True)
        try:
            ch = int(self.ch_input.value)
            val = self.action_input.value.strip().lower()
            if ch < 1 or ch > TOTAL_CHANNELS:
                return
        except ValueError:
            return

        async with data_lock:
            if ch not in channel_data:
                await interaction.followup.send("ℹ️ Channel already unknown.", ephemeral=True)
                return
            if val in ["2", "dibs", "dib"] and channel_data[ch].get("type") == "member":
                channel_data[ch]["queue"] = []
                channel_data[ch].pop("pending_dib", None)
                msg_text = f"🛡️ Cleared dibs for **Ch {ch}**."
            else:
                del channel_data[ch]
                msg_text = f"🛡️ Reset **Ch {ch}** to unknown."

        msg = await interaction.followup.send(msg_text, wait=True)
        asyncio.create_task(auto_delete_msg(msg))
        await update_tracker_message()


# --- VIEWS & BUTTONS ---

class ChannelPageSelect(discord.ui.Select):
    def __init__(self, page_num: int):
        options = [
            discord.SelectOption(label="Channels 1 – 10", value="1", default=(page_num == 1)),
            discord.SelectOption(label="Channels 11 – 20", value="2", default=(page_num == 2)),
            discord.SelectOption(label="Channels 21 – 30", value="3", default=(page_num == 3)),
            discord.SelectOption(label="Channels 31 – 40", value="4", default=(page_num == 4)),
        ]
        super().__init__(placeholder="Select Channel Group...", min_values=1, max_values=1, options=options, custom_id="channel_page_select", row=0)

    async def callback(self, interaction: discord.Interaction):
        global current_page
        current_page = int(self.values[0])
        await interaction.response.edit_message(embed=build_embed(), view=TrackerView(page=current_page))
        await save_data()


class ChannelButton(discord.ui.Button):
    def __init__(self, ch_num: int, is_taken: bool, row_num: int):
        super().__init__(
            label=f"Ch {ch_num}",
            style=discord.ButtonStyle.secondary if is_taken else discord.ButtonStyle.success,
            custom_id=f"claim_ch_{ch_num}",
            row=row_num
        )
        self.ch_num = ch_num

    async def callback(self, interaction: discord.Interaction):
        now = int(time.time())
        async with data_lock:
            current = channel_data.get(self.ch_num)
            if current and current["end_time"] > now:
                if current["type"] == "member" and current["user_id"] != interaction.user.id:
                    await interaction.response.send_message(f"⚠️ Ch {self.ch_num} is claimed. Use Dibs!", ephemeral=True)
                    return
                elif current["type"] == "non_member":
                    pass
                elif current["type"] == "member" and current["user_id"] == interaction.user.id:
                    await interaction.response.send_message(f"ℹ️ You already hold Ch {self.ch_num}.", ephemeral=True)
                    return

            for ch, info in list(channel_data.items()):
                if info.get("type") == "member" and info["user_id"] == interaction.user.id:
                    del channel_data[ch]

            channel_data[self.ch_num] = {
                "type": "member",
                "user_id": interaction.user.id,
                "end_time": now + (DEFAULT_MINUTES * 60),
                "queue": [],
            }

        await interaction.response.send_message(f"🚇 <@{interaction.user.id}> claimed **Ch {self.ch_num}** for **{format_duration(DEFAULT_MINUTES)}**!")
        try:
            msg = await interaction.original_response()
            asyncio.create_task(auto_delete_msg(msg))
        except Exception:
            pass
        await update_tracker_message()


class CombinedDibsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🙋 Dibs (Claim/Cancel)", style=discord.ButtonStyle.success, custom_id="combined_dibs_btn", row=3)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CombinedDibsModal())


class EditTimerButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="⏱️ Edit Timer", style=discord.ButtonStyle.primary, custom_id="edit_my_timer", row=3)

    async def callback(self, interaction: discord.Interaction):
        now = int(time.time())
        user_ch = next(
            (ch for ch, info in channel_data.items() if info.get("type") == "member" and info["user_id"] == interaction.user.id and info["end_time"] > now),
            None
        )
        if not user_ch:
            await interaction.response.send_message("❌ You don't have an active member channel claimed!", ephemeral=True)
            return
        await interaction.response.send_modal(EditTimerModal(user_ch))


class LogNonMemberButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔴 Log Non-Member", style=discord.ButtonStyle.secondary, custom_id="log_non_member_btn", row=3)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NonMemberTimerModal())


class LeaveChannelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🚪 Leave Channel", style=discord.ButtonStyle.danger, custom_id="leave_my_channel", row=4)

    async def callback(self, interaction: discord.Interaction):
        now = int(time.time())
        released_ch = None
        next_user_id = None

        async with data_lock:
            user_ch = next(
                (ch for ch, info in channel_data.items() if info.get("type") == "member" and info["user_id"] == interaction.user.id),
                None
            )
            if not user_ch:
                await interaction.response.send_message("❌ You don't have a member channel claimed.", ephemeral=True)
                return

            released_ch = user_ch
            info = channel_data[released_ch]
            queue_list = info.get("queue", [])

            if queue_list:
                next_user_id = queue_list.pop(0)
                channel_data[released_ch] = {
                    "type": "member",
                    "user_id": next_user_id,
                    "end_time": now + (DEFAULT_MINUTES * 60),
                    "queue": queue_list,
                }
            else:
                del channel_data[released_ch]

        if next_user_id:
            await interaction.response.send_message(f"🚪 <@{interaction.user.id}> released **Ch {released_ch}** ➡️ Automatically claimed by <@{next_user_id}> for **{format_duration(DEFAULT_MINUTES)}**!")
        else:
            await interaction.response.send_message(f"🚪 <@{interaction.user.id}> released **Ch {released_ch}**.")

        try:
            msg = await interaction.original_response()
            asyncio.create_task(auto_delete_msg(msg))
        except Exception:
            pass
        await update_tracker_message()


class HelperClearButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🛡️ Helper Clear", style=discord.ButtonStyle.secondary, custom_id="helper_clear_btn", row=4)

    async def callback(self, interaction: discord.Interaction):
        if not is_helper(interaction.user):
            await interaction.response.send_message(f"❌ Only **{HELPER_ROLE_NAME}** can use this.", ephemeral=True)
            return
        await interaction.response.send_modal(HelperClearModal())


class TrackerView(discord.ui.View):
    def __init__(self, page: int = 1):
        super().__init__(timeout=None)
        now = int(time.time())
        self.add_item(ChannelPageSelect(page))

        start_ch = (page - 1) * 10 + 1
        end_ch = min(page * 10, TOTAL_CHANNELS)
        for idx, ch in enumerate(range(start_ch, end_ch + 1)):
            row_num = 1 + (idx // 5)
            is_taken = bool(channel_data.get(ch, {}).get("end_time", 0) > now)
            self.add_item(ChannelButton(ch, is_taken, row_num))

        self.add_item(CombinedDibsButton())
        self.add_item(EditTimerButton())
        self.add_item(LogNonMemberButton())
        self.add_item(LeaveChannelButton())
        self.add_item(HelperClearButton())


@tasks.loop(seconds=45)
async def cleanup_expired():
    now = int(time.time())
    updated = False
    async with data_lock:
        for ch, info in list(channel_data.items()):
            if info["end_time"] <= now:
                queue_list = info.get("queue", [])
                if queue_list and info.get("type") == "member":
                    next_user_id = queue_list.pop(0)
                    channel_data[ch] = {
                        "type": "member",
                        "user_id": next_user_id,
                        "end_time": now + (DEFAULT_MINUTES * 60),
                        "queue": queue_list,
                    }
                    updated = True
                else:
                    del channel_data[ch]
                    updated = True

    if updated:
        await update_tracker_message()


@bot.event
async def on_ready():
    load_data()
    bot.add_view(TrackerView(page=current_page))
    if not cleanup_expired.is_running():
        cleanup_expired.start()
    print(f"Unified Bot logged in as {bot.user}!")
    await update_tracker_message()


@bot.command(name="b2test")
async def setup_b2(ctx):
    if not is_helper(ctx.author):
        try:
            await ctx.message.delete()
            await ctx.send(f"❌ Only **{HELPER_ROLE_NAME}** or Administrators can post the tracker.", delete_after=5)
        except discord.HTTPException:
            pass
        return

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
    async with data_lock:
        existed = ch in channel_data
        if existed:
            del channel_data[ch]
    if existed:
        await update_tracker_message()
        await ctx.send(f"🛡️ Cleared **Ch {ch}**.", delete_after=5)
    else:
        await ctx.send(f"ℹ️ **Ch {ch}** is already unknown.", delete_after=5)


@bot.command()
async def clear_all(ctx):
    if not is_helper(ctx.author):
        return
    await ctx.message.delete()
    async with data_lock:
        channel_data.clear()
    await update_tracker_message()
    await ctx.send("🛡️ All channels reset to unknown.", delete_after=5)


@bot.command(name="add_member")
async def add_member(ctx, ch: int, minutes: int, member: discord.Member):
    if not is_helper(ctx.author):
        try:
            await ctx.message.delete()
            await ctx.send(f"❌ Only **{HELPER_ROLE_NAME}** can use this command.", delete_after=5)
        except discord.HTTPException:
            pass
        return

    await ctx.message.delete()

    if ch < 1 or ch > TOTAL_CHANNELS or minutes < 1:
        try:
            await ctx.send("❌ Invalid channel number or minutes.", delete_after=5)
        except discord.HTTPException:
            pass
        return

    now = int(time.time())
    async with data_lock:
        for c, info in list(channel_data.items()):
            if info.get("type") == "member" and info["user_id"] == member.id:
                del channel_data[c]

        channel_data[ch] = {
            "type": "member",
            "user_id": member.id,
            "end_time": now + (minutes * 60),
            "queue": [],
        }

    await update_tracker_message()
    try:
        await ctx.send(f"🛡️ Manually assigned **Ch {ch}** to {member.mention} for **{minutes}m**.", delete_after=5)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    keep_alive()
    bot.run(BOT_TOKEN)
