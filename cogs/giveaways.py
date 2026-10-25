import logging
import discord
import asyncio
from datetime import datetime, timedelta, timezone
from discord import app_commands
from discord.ext import commands, tasks
from database import db_settings, db_giveaways
from cogs.utils import get_lang, NEXUS_FOOTER
from emojis import NexusEmojis
from systems.ui import NexusView, NexusModal
from typing import Literal
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

MAX_WINNERS = 20

DURATIONS = [
    (3600,    "giveaway_dur_1h",  "1 hour"),
    (28800,   "giveaway_dur_8h",  "8 hours"),
    (86400,   "giveaway_dur_24h", "24 hours"),
    (259200,  "giveaway_dur_3d",  "3 days"),
    (604800,  "giveaway_dur_1w",  "1 week"),
    (1209600, "giveaway_dur_2w",  "2 weeks"),
]
DEFAULT_DURATION = 86400

FinishGiveaway = Callable[..., Awaitable[bool]]

# ─── Views & Modals ──────────────────────────────────────────────────────────

class GiveawaysView(NexusView):
    def __init__(self, guild_id: int, lang: dict, finish: FinishGiveaway):
        super().__init__(lang, timeout=180, require="moderate_members")
        self.guild_id = guild_id
        self.finish = finish
        self.add_btn.label = lang.get("giveaways_btn_add", "Add")
        self.add_btn.emoji = "➕"
        self.cancel_btn.label = lang.get("giveaways_btn_cancel", "Cancel")
        self.cancel_btn.emoji = "🗑️"

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddGiveawayModal(self.guild_id, self.lang))

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        giveaways = await db_giveaways.get_active_giveaways(self.guild_id)
        if not giveaways:
            await interaction.response.send_message(
                self.lang.get("no_giveaways_found", "No Giveaways are ongoing currently on this server."),
                ephemeral=True,
            )
            return
        view = CancelGiveawaySelectView(self.guild_id, self.lang, giveaways, self.finish)
        await interaction.response.send_message(
            content=self.lang.get("giveaways_cancel_prompt", "Select the giveaway to cancel:"),
            view=view,
            ephemeral=True,
        )
        view.bind(interaction)

class CancelGiveawaySelectView(NexusView):
    def __init__(self, guild_id: int, lang: dict[str, str], giveaways: list[dict], finish: FinishGiveaway):
        super().__init__(lang, timeout=300, require="moderate_members")
        self.guild_id = guild_id
        self.finish = finish
        self.giveaways = {g["id"]: g for g in giveaways}

        self.pick.placeholder = lang.get("giveaways_cancel_placeholder", "Select a giveaway...")
        self.pick.options = [
            discord.SelectOption(
                label=g["prize"][:100],
                value=str(g["id"]),
                description=f"ID {g['id']}",
            )
            for g in giveaways[:25]
        ]

    @discord.ui.select(placeholder="...", min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer(ephemeral=True)

        giveaway = self.giveaways[int(select.values[0])]
        ok = await self.finish(giveaway, status="cancelled")
        text = (
            self.lang.get("giveaway_cancelled_admin", "{checkmark} Giveaway cancelled.").format(checkmark=NexusEmojis.CHECKMARK)
            if ok else
            self.lang.get("giveaway_cancel_failed", "❌ I can't access that giveaway's channel. Check my permissions and try again.")
        )
        self.stop()
        await interaction.edit_original_response(content=text, view=None)

class GiveawayJoinView(NexusView):
    def __init__(self, lang: dict[str, str], enabled: bool):
        super().__init__(lang, timeout=None, require=None)
        self.join_giveaway.label = self.lang.get("join_giveaway", "Join Giveaway")
        self.join_giveaway.emoji = "🎉"
        self.join_giveaway.disabled = not enabled

    @discord.ui.button(
        label="Join Giveaway", 
        style=discord.ButtonStyle.primary, 
        custom_id="join_giveaway")
    async def join_giveaway(self, interaction: discord.Interaction, button: discord.ui.Button):
        message_id = interaction.message.id
        user_id = interaction.user.id
        giveaway = await db_giveaways.get_giveaway(interaction.guild_id, message_id)
        lang = await get_lang(interaction.guild_id)

        if giveaway is None:
            await interaction.response.send_message(
                lang.get("giveaway_not_found", "❌ This giveaway no longer exists."),
                ephemeral=True,
            )
            return

        if giveaway["status"] != "running" or giveaway["end_time"] <= datetime.now(timezone.utc).replace(tzinfo=None):
            await interaction.response.send_message(
                lang.get("giveaway_ended", "The Giveaway has already ended"),
                ephemeral=True,
            )
            return

        joined = await db_giveaways.add_participant(giveaway["id"], user_id)
        if not joined:
            await interaction.response.send_message(
                lang.get("giveaway_already_joined", "You've already joined this giveaway."),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            lang.get("giveaway_joined", "You have joined the giveaway!"),
            ephemeral=True
        )

async def post_giveaway(
    *,
    guild_id: int,
    host_id: int,
    channel: discord.TextChannel,
    prize: str,
    description: str | None,
    winners_count: int,
    end_utc: datetime,
    lang: dict[str, str],
) -> int:
    """Postet das Giveaway-Embed und legt den DB-Eintrag an.

    Gemeinsam genutzt von Slash-Command (Modal) und Dashboard.
    end_utc ist naive UTC. Gibt die Giveaway-ID zurück.
    """
    embed = discord.Embed(
        title=f"🎉 {prize}",
        description=description or None,
        color=await db_settings.get_color(guild_id),
    )
    end_ts = int(end_utc.replace(tzinfo=timezone.utc).timestamp())
    embed.add_field(name=lang.get("giveaways_winners", "Winners"), value=str(winners_count))
    embed.add_field(name=lang.get("giveaways_end_date", "End Date"), value=f"<t:{end_ts}:R>")
    embed.set_footer(text=NEXUS_FOOTER)

    message = await channel.send(embed=embed, view=GiveawayJoinView(lang, enabled=True))
    try:
        return await db_giveaways.create_giveaway(
            guild_id=guild_id,
            message_id=message.id,
            prize=prize,
            end_time=end_utc,
            host_id=host_id,
            channel_id=channel.id,
            winners_count=winners_count,
        )
    except Exception:
        await message.delete()
        raise

class AddGiveawayModal(NexusModal):
    def __init__(self, guild_id: int, lang: dict):
        super().__init__(lang, title=lang.get("giveaways_add_modal_title", "Create a Giveaway"))
        self.guild_id = guild_id
        self.prize_input = discord.ui.TextInput(
            label=lang.get("giveaways_prize_label", "Prize"),
            placeholder=lang.get("giveaways_prize_placeholder", "e.g. Some awesome Prize"),
            required=True,
            max_length=200,
        )
        self.add_item(self.prize_input)

        self.duration_select = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label=lang.get(key, fallback),
                    value=str(seconds),
                    default=(seconds == DEFAULT_DURATION),
                )
                for seconds, key, fallback in DURATIONS
            ],
            required=True,
        )
        self.add_item(discord.ui.Label(
            text=lang.get("giveaways_duration_label", "Duration"),
            component=self.duration_select,
        ))

        self.winners_count_input = discord.ui.TextInput(
            label=lang.get("giveaways_winners_count_label", "How many Winners?"),
            placeholder=lang.get("giveaways_winners_count_placeholder", "e.g. 3"),
            required=True,
            max_length=2
        )
        self.add_item(self.winners_count_input)

        self.description_input = discord.ui.TextInput(
            label=lang.get("giveaways_description_label", "Description"),
            placeholder=lang.get("giveaways_description_placeholder", "Optional details about the giveaway..."),
            style=discord.TextStyle.paragraph,
            required=False,
        )
        self.add_item(self.description_input)

        self.channel_picker = discord.ui.ChannelSelect(
            placeholder=lang.get("giveaways_channel_placeholder", "Select the Channel..."),
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            required=True,
        )
        self.add_item(discord.ui.Label(
            text=lang.get("giveaways_channel_label", "Channel"),
            component=self.channel_picker,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # 1. Dauer prüfen (Schutz gegen manipulierte Werte) und Endzeit berechnen
        seconds = int(self.duration_select.values[0])
        if seconds not in {d[0] for d in DURATIONS}:
            await interaction.followup.send(
                self.lang.get("giveaway_winners_count_invalid", "❌ Invalid input."),
                ephemeral=True,
            )
            return
        scheduled_utc = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=seconds)

        # 2. Gewinneranzahl prüfen
        try:
            winners_count = int(self.winners_count_input.value)
        except ValueError:
            winners_count = 0
        if not 1 <= winners_count <= MAX_WINNERS:
            await interaction.followup.send(
                self.lang.get("giveaway_winners_count_invalid", "❌ Invalid winners count."),
                ephemeral=True,
            )
            return

        # 3. Channel auflösen und Rechte prüfen
        picked = self.channel_picker.values[0]
        try:
            channel = picked.resolve() or await picked.fetch()
        except discord.HTTPException:
            await interaction.followup.send(
                self.lang.get("giveaway_channel_unreachable", "❌ I can't access that channel."),
                ephemeral=True,
            )
            return

        bot_perms = channel.permissions_for(interaction.guild.me)
        if not (bot_perms.view_channel and bot_perms.send_messages and bot_perms.embed_links):
            await interaction.followup.send(
                self.lang.get(
                    "giveaway_bot_missing_perms",
                    "❌ I need View Channel, Send Messages and Embed Links in {channel}.",
                ).format(channel=channel.mention),
                ephemeral=True,
            )
            return

        if not channel.permissions_for(interaction.user).send_messages:
            await interaction.followup.send(
                self.lang.get(
                    "giveaway_user_missing_perms",
                    "❌ You can't send messages in {channel}.",
                ).format(channel=channel.mention),
                ephemeral=True,
            )
            return

        # 4. Posten + DB-Eintrag (Fehler hier landen bewusst in NexusModal.on_error)
        await post_giveaway(
            guild_id=interaction.guild_id,
            host_id=interaction.user.id,
            channel=channel,
            prize=self.prize_input.value,
            description=self.description_input.value,
            winners_count=winners_count,
            end_utc=scheduled_utc,
            lang=self.lang,
        )

        await interaction.followup.send(
            self.lang.get("giveaway_created", "{checkmark} Giveaway created!").format(checkmark=NexusEmojis.CHECKMARK),
            ephemeral=True,
        )

class Giveaways(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_giveaways.start()

    async def cog_unload(self):
        self.check_giveaways.cancel()

    @tasks.loop(minutes=1)
    async def check_giveaways(self):
        try:
            ended = await db_giveaways.get_due_giveaways()
        except Exception:
            log.exception("Konnte fällige Giveaways nicht laden.")
            return

        for giveaway in ended:
            try:
                done = await self._finish_giveaway(giveaway, status="ended")
                if not done and giveaway["end_time"] < datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24):
                    await db_giveaways.end_giveaway(giveaway["guild_id"], giveaway["message_id"], "cancelled")
            except Exception:
                log.exception("Fehler beim Beenden von Giveaway %s.", giveaway["id"])

    @check_giveaways.before_loop
    async def before_check_giveaways(self):
        await self.bot.wait_until_ready()

    async def _finish_giveaway(self, giveaway: dict, status: Literal["ended", "cancelled"]) -> bool:
        """True, wenn abgeschlossen; False, wenn es später erneut versucht wird (z. B. fehlende Rechte)."""
        guild_id = giveaway["guild_id"]
        channel_id = giveaway["channel_id"]
        message_id = giveaway["message_id"]

        lang = await get_lang(guild_id)

        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            # Channel oder Nachricht wurde gelöscht
            await db_giveaways.end_giveaway(guild_id, message_id, "cancelled")
            return True
        except discord.Forbidden:
            log.warning("Kein Zugriff auf Giveaway %s (Channel %s), versuche es später erneut.", giveaway["id"], channel_id)
            return False

        # Sperre: Nur wer den Status von 'running' umstellt, darf auswerten.
        if not await db_giveaways.end_giveaway(guild_id, message_id, status):
            return True

        ended_view = GiveawayJoinView(lang, enabled=False)
        embed = message.embeds[0]

        if status == "cancelled":
            embed.color = discord.Color.red()
            embed.title = f"❌ {giveaway['prize']}"
            # Index 1 = End Date (Reihenfolge siehe post_giveaway)
            embed.set_field_at(
                1,
                name=lang.get("giveaways_status", "Status"),
                value=lang.get("giveaway_cancelled", "❌ Cancelled"),
                inline=True,
            )
            await message.edit(embed=embed, view=ended_view)
            return True

        entries = await db_giveaways.draw_winners(giveaway["id"], giveaway["winners_count"])
        mentions = ", ".join(f"<@{entry['user_id']}>" for entry in entries)

        embed.color = await db_settings.get_color(guild_id)
        embed.timestamp = datetime.now(timezone.utc)
        embed.add_field(
            name=lang.get("giveaway_winners_field", "🏆 Winners"),
            value=mentions or lang.get("no_participants", "Nobody entered."),
            inline=False,
        )
        if entries:
            announcement = lang.get(
                "giveaway_winner_announcement", "🎉 Congratulations {winners}! You won **{prize}**!"
            ).format(winners=mentions, prize=giveaway["prize"])
        else:
            announcement = lang.get(
                "giveaway_no_winner", "😢 There were no participants for **{prize}**."
            ).format(prize=giveaway["prize"])

        await message.edit(embed=embed, view=ended_view)
        await channel.send(
            announcement,
            allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(id=entry["user_id"]) for entry in entries],
                roles=False,
                everyone=False,
            ),
        )
        return True

    @app_commands.command(name="giveaways", description=app_commands.locale_str("cmd_giveaways_desc"))
    @app_commands.default_permissions(moderate_members=True)
    async def giveaways(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id is None:
            return
        async with asyncio.TaskGroup() as tg:
            color_task = tg.create_task(db_settings.get_color(interaction.guild_id))
            giveaways_task = tg.create_task(db_giveaways.get_active_giveaways(interaction.guild_id))
            lang_task = tg.create_task(get_lang(interaction.guild_id))

        lang = lang_task.result()
        color = color_task.result()
        giveaways = giveaways_task.result()

        embed = await self._build_giveaways_embed(giveaways, lang, color)

        view = GiveawaysView(interaction.guild_id, lang, self._finish_giveaway)

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.bind(interaction)

    async def _build_giveaways_embed(self, giveaways: list[dict], lang: dict, color: int) -> discord.Embed:
        embed = discord.Embed(
            title=lang.get("giveaways_title", "📋 Configured Giveaways"),
            color=color
        )
        if not giveaways:
            embed.description = lang.get("no_giveaways_found", "No Giveaways are ongoing currently on this server.")
        else:
            text = ""
            for entry in giveaways:
                count = await db_giveaways.get_participant_count(entry["id"])
                text += (
                    f"**{entry['prize']}**\n"
                    f"{lang.get('giveaways_participants', 'Participants')}: {count} / "
                    f"{lang.get('giveaways_winners', 'Winners')}: {entry['winners_count']}\n"
                    f"{lang.get('giveaways_end_date', 'End Date')}: <t:{int(entry['end_time'].replace(tzinfo=timezone.utc).timestamp())}:R>\n\n"
                )
            embed.description = text
        return embed

async def setup(bot):
    await bot.add_cog(Giveaways(bot))
    bot.add_view(GiveawayJoinView(lang={}, enabled=True))