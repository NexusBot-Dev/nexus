import asyncio
import logging
import discord
import time
from discord import app_commands
from discord.ext import commands, tasks
from database import db_settings, db_honeypot
from cogs.utils import get_lang, module_required
from emojis import NexusEmojis
from systems import honeypot as hp_system
from systems.hierarchy import format_issue
from systems.ui import ConfirmView, NexusModal, NexusView

log = logging.getLogger(__name__)

# ─── Honeypot ───────────────────────────────────────────────────────────────
HONEYPOT_COOLDOWN_SECONDS = 60
COUNTER_UPDATE_DELAY = 5
_honeypot_seen: dict[tuple[int, int], float] = {}
_counter_pending: set[int] = set()

# ─── Honeypot Helpers ────────────────────────────────────────────────────────

_LOG_STATUS = {
    # Aktion → (Key bei Erfolg, Fallback), (Key bei Fehlschlag, Fallback)
    "timeout": (("honeypot_log_timeout_success", "{days} day timeout applied"),
                ("honeypot_log_timeout_failed", "Timeout failed (Hierarchy/Permissions?)")),
    "kick":    (("honeypot_log_kick_success", "Kicked"),
                ("honeypot_log_kick_failed", "Kick failed (Hierarchy/Permissions?)")),
    "ban":     (("honeypot_log_ban_success", "Banned (messages from the last 24 h deleted)"),
                ("honeypot_log_ban_failed", "Ban failed (Hierarchy/Permissions?)")),
}

async def _send_honeypot_log(
    bot,
    guild: discord.Guild,
    lang: dict,
    member: discord.Member,
    message_content: str,
    action: str,
    applied: bool,
    days: int,
    total: int,
):
    settings = await db_settings.get_guild(guild.id)
    if settings is None or not settings.get("log_channel_id"):
        return
    channel = bot.get_channel(settings["log_channel_id"])
    if channel is None:
        return

    embed = discord.Embed(
        title=lang.get("honeypot_log_title", "🍯 Honeypot Triggered"),
        description=lang.get(
            "honeypot_log_desc",
            "{user} posted in the honeypot channel.",
        ).format(user=member.mention, days=days),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name=lang.get("honeypot_log_message", "💬 Message"),
        value=(message_content[:1000] or "*(leer/Anhang)*"),
        inline=False,
    )
    key, fallback = _LOG_STATUS[action][0 if applied else 1]
    embed.add_field(name=lang.get("honeypot_log_status", "Status"), value=lang.get(key, fallback).format(days=days), inline=False)
    embed.set_footer(text=lang.get("honeypot_log_footer", "Nexus • Honeypot • Catch #{count}").format(count=f"{total:,}"))

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning("[HONEYPOT] Keine Berechtigung, Log-Channel %s zu schreiben.", channel)
    except discord.HTTPException:
        log.exception("[HONEYPOT] Fehler beim Schreiben in Log-Channel %s.", channel)

async def _update_counter(bot, guild: discord.Guild, channel_id: int, lang: dict) -> None:
    """Schreibt den Fang-Zähler in den Footer des Warn-Embeds im Honeypot-Channel.
    Höchstens eine Aktualisierung alle COUNTER_UPDATE_DELAY Sekunden pro Server (Edit-Limits bei Spam-Wellen)."""
    if guild.id in _counter_pending:
        return
    _counter_pending.add(guild.id)
    try:
        await asyncio.sleep(COUNTER_UPDATE_DELAY)
        channel = guild.get_channel(channel_id)
        if channel is None:
            return

        hp = await db_honeypot.get_honeypot_settings(guild.id)
        message = await hp_system.find_message(guild, channel, hp)
        if message is None:
            return

        embed = message.embeds[0].copy()
        embed.set_footer(text=hp_system.counter_footer(lang, hp["catches"]))
        await message.edit(embed=embed)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        log.warning("[HONEYPOT] Zähler auf %s konnte nicht aktualisiert werden.", guild.name)
    except Exception:
        log.exception("[HONEYPOT] Unerwarteter Fehler beim Aktualisieren des Zählers auf %s.", guild.name)
    finally:
        _counter_pending.discard(guild.id)

# ─── /honeypot: Hub ──────────────────────────────────────────────────────────

async def _hub_embed(guild: discord.Guild, lang: dict) -> tuple[discord.Embed, bool]:
    """Übersicht für /honeypot. Zweiter Wert: gibt es gerade einen Honeypot?"""
    channel = await hp_system.get_channel(guild)
    hp = await db_honeypot.get_honeypot_settings(guild.id)
    title, text = await hp_system.load_texts(guild, channel, hp, lang)
    days = hp["timeout_days"]

    embed = discord.Embed(
        title="🍯 Honeypot",
        description=lang.get(
            "honeypot_hub_desc",
            "A trap channel for raid and spam bots: anyone who posts there is punished automatically. "
            "Members with **Manage Server** are exempt.",
        ),
        color=await db_settings.get_color(guild.id),
    )
    if channel is None:
        embed.add_field(
            name=lang.get("honeypot_hub_status", "Status"),
            value=lang.get("honeypot_hub_inactive", "❌ No honeypot set up yet."),
            inline=False,
        )
    else:
        embed.add_field(
            name=lang.get("honeypot_hub_status", "Status"),
            value=f"{NexusEmojis.CHECKMARK} {channel.mention}",
            inline=False,
        )
    embed.add_field(
        name=lang.get("honeypot_hub_members", "👤 Members"),
        value=hp_system.punishment_label(lang, hp["punish_members"], days),
        inline=True,
    )
    embed.add_field(
        name=lang.get("honeypot_hub_bots", "🤖 Bots"),
        value=hp_system.punishment_label(lang, hp["punish_bots"], days),
        inline=True,
    )
    embed.add_field(
        name=lang.get("honeypot_hub_catches", "🍯 Caught"),
        value=f"{hp['catches']:,}",
        inline=True,
    )
    if channel is not None:
        preview = f"**{discord.utils.escape_markdown(title)}**\n{text[:300]}{'…' if len(text) > 300 else ''}"
        embed.add_field(name=lang.get("honeypot_hub_message", "Warning message"), value=preview, inline=False)
    embed.set_footer(text="Nexus • Honeypot")
    return embed, channel is not None

async def _show_hub(interaction: discord.Interaction, lang: dict, *, previous: NexusView | None = None) -> None:
    """Zeigt den Hub frisch aus der DB in der bestehenden Nachricht (neue View-Instanz, wie bei Reaction Roles)."""
    if previous is not None:
        previous.stop()
    # _hub_embed liest beim ersten Aufruf die alte Warn-Nachricht (API) — vorher bestätigen,
    # Discord gibt nur 3 s für die erste Antwort
    if not interaction.response.is_done():
        await interaction.response.defer()
    embed, active = await _hub_embed(interaction.guild, lang)
    view = HoneypotHubView(lang, interaction.user.id, active)
    await interaction.edit_original_response(content=None, embed=embed, view=view)
    view.bind(interaction)

class HoneypotHubView(NexusView):
    """Ohne Honeypot nur "Erstellen", sonst "Text bearbeiten", "Bestrafung" und "Entfernen"."""

    def __init__(self, lang: dict, owner_id: int, active: bool):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        self.create_btn.label = lang.get("honeypot_btn_create", "Create")
        self.edit_btn.label = lang.get("honeypot_btn_edit", "Edit text")
        self.punish_btn.label = lang.get("honeypot_btn_punish", "Punishment")
        self.delete_btn.label = lang.get("honeypot_btn_delete", "Remove")
        for item in ((self.edit_btn, self.punish_btn, self.delete_btn) if not active else (self.create_btn,)):
            self.remove_item(item)

    @discord.ui.button(emoji="➕", style=discord.ButtonStyle.success)
    async def create_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(HoneypotTextModal(self.lang, self, create=True))

    @discord.ui.button(emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # check:interaction-ok Modal muss erste Antwort sein; API nur einmalig bei der Migration
        hp = await db_honeypot.get_honeypot_settings(interaction.guild_id)
        channel = await hp_system.get_channel(interaction.guild)
        title, text = await hp_system.load_texts(interaction.guild, channel, hp, self.lang)
        await interaction.response.send_modal(HoneypotTextModal(self.lang, self, create=False, title=title, text=text))

    @discord.ui.button(emoji="⚙️", style=discord.ButtonStyle.secondary)
    async def punish_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        hp = await db_honeypot.get_honeypot_settings(interaction.guild_id)
        view = HoneypotPunishView(self.lang, self.owner_id, hp)
        await self.show(interaction, view, embed=view.embed(hp))

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await hp_system.get_channel(interaction.guild)
        if channel is None:
            await _show_hub(interaction, self.lang, previous=self)
            return
        confirm = ConfirmView(self.lang, owner_id=self.owner_id)
        await interaction.response.send_message(
            self.lang.get("honeypot_delete_confirm", "Really remove the honeypot {channel}? The channel will be deleted.").format(
                channel=channel.mention,
            ),
            view=confirm,
            ephemeral=True,
        )
        confirm.bind(interaction)
        if not await confirm.wait_result():
            return
        await hp_system.cleanup(interaction.guild, channel.id)
        # Hub-Nachricht über die Interaction aktualisieren, die sie zuletzt bearbeitet hat
        target = self._edit_target()
        if target is not None:
            embed, active = await _hub_embed(interaction.guild, self.lang)
            view = HoneypotHubView(self.lang, self.owner_id, active)
            self.stop()
            try:
                await target.edit_original_response(embed=embed, view=view)
                view.bind(target)
            except discord.HTTPException:
                pass
        await interaction.followup.send(self.lang.get("honeypot_deleted", "{nexus_checkmark} Honeypot removed.").format(
            nexus_checkmark=NexusEmojis.CHECKMARK,
        ), ephemeral=True)

class HoneypotTextModal(NexusModal):
    """Erstellen (mit Channel-Name) oder Titel/Text der Warn-Nachricht bearbeiten."""

    def __init__(self, lang: dict, hub: HoneypotHubView, *, create: bool, title: str = "", text: str = ""):
        super().__init__(lang, title=lang.get("honeypot_modal_title", "Create Honeypot") if create
                         else lang.get("honeypot_modal_edit_title", "Edit warning message"))
        self.hub = hub
        self.create = create

        self.name_input = None
        if create:
            self.name_input = discord.ui.TextInput(
                label=lang.get("honeypot_modal_name_label", "Channel Name"),
                placeholder=lang.get("honeypot_modal_name_placeholder", "e.g. dont-write-here"),
                max_length=90,
                required=True,
            )
            self.add_item(self.name_input)
        self.title_input = discord.ui.TextInput(
            label=lang.get("honeypot_modal_title_label", "Title"),
            default=title or hp_system.default_title(lang),
            max_length=db_honeypot.WARN_TITLE_MAX,
            required=False,
        )
        self.text_input = discord.ui.TextInput(
            label=lang.get("honeypot_modal_warntext_label", "Warning Text in Channel"),
            style=discord.TextStyle.paragraph,
            placeholder=lang.get(
                "honeypot_modal_warntext_placeholder",
                "⚠️ Raid bot trap: Anyone posting here will be automatically timed out.",
            ),
            default=text or None,
            max_length=db_honeypot.WARN_TEXT_MAX,
            required=True,
        )
        self.add_item(self.title_input)
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        if self.create:
            try:
                channel = await hp_system.create(guild, self.name_input.value, self.title_input.value, self.text_input.value, self.lang)
            except hp_system.HoneypotError as e:
                if e.code == "perms":
                    message = format_issue(e.check, self.lang)
                elif e.code == "exists":
                    message = self.lang.get("honeypot_exists", "❌ A honeypot already exists.")
                else:
                    message = self.lang.get("setup_right_error", "I am missing the 'Manage Channels' permission.")
                await interaction.followup.send(message, ephemeral=True)
                return
            result = self.lang.get("honeypot_created", "{nexus_checkmark} Honeypot set up in {channel}.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK, channel=channel.mention,
            )
        else:
            if await hp_system.get_channel(guild) is None:
                await _show_hub(interaction, self.lang, previous=self.hub)
                await interaction.followup.send(self.lang.get("honeypot_missing", "❌ There is no honeypot (anymore)."), ephemeral=True)
                return
            status = await hp_system.update_texts(guild, self.title_input.value, self.text_input.value, self.lang)
            result = {
                "updated":    self.lang.get("honeypot_text_updated", "{nexus_checkmark} Warning message updated."),
                "reposted":   self.lang.get("honeypot_text_reposted", "{nexus_checkmark} The warning message was missing and has been posted again."),
                "saved_only": self.lang.get("honeypot_text_saved_only", "⚠️ Saved, but the message in the channel couldn't be updated (missing permissions?)."),
            }[status].format(nexus_checkmark=NexusEmojis.CHECKMARK)

        await _show_hub(interaction, self.lang, previous=self.hub)
        await interaction.followup.send(result, ephemeral=True)

class HoneypotPunishView(NexusView):
    """Bestrafung für Mitglieder und Bots getrennt, dazu die Timeout-Dauer. Jede Auswahl speichert sofort."""

    def __init__(self, lang: dict, owner_id: int, hp: dict):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        days = hp["timeout_days"]
        self.members_select.placeholder = lang.get("honeypot_select_members", "Punishment for members")
        self.bots_select.placeholder = lang.get("honeypot_select_bots", "Punishment for bots")
        # Jede Option nennt, wofür die Auswahl ist ("Mitglieder: Timeout") — Discord zeigt im geschlossenen
        # Menü nur die gewählte Option, der Platzhalter wäre dann nicht mehr zu sehen
        self.members_select.options = self._punish_options(
            hp["punish_members"], days, "👤", lang.get("honeypot_opt_members", "Members: {punishment}"))
        self.bots_select.options = self._punish_options(
            hp["punish_bots"], days, "🤖", lang.get("honeypot_opt_bots", "Bots: {punishment}"))
        self.days_select.placeholder = lang.get("honeypot_select_days", "Timeout duration")
        days_template = lang.get("honeypot_opt_days", "Timeout duration: {days}")
        options = sorted(set(db_honeypot.TIMEOUT_DAY_OPTIONS) | {days})   # ältere Werte (z. B. 5) bleiben wählbar
        self.days_select.options = [
            discord.SelectOption(label=days_template.format(days=self._days_label(d)), value=str(d), emoji="⏱️", default=d == days)
            for d in options
        ]
        # Die Dauer zählt nur für Timeouts — ohne Timeout ist die Auswahl gesperrt
        self.days_select.disabled = "timeout" not in (hp["punish_members"], hp["punish_bots"])
        self.back_btn.label = lang.get("honeypot_btn_back", "Back")

    def _days_label(self, days: int) -> str:
        if days == 1:
            return self.lang.get("honeypot_days_one", "1 day")
        return self.lang.get("honeypot_days", "{days} days").format(days=days)

    def _punish_options(self, current: str, days: int, emoji: str, template: str) -> list[discord.SelectOption]:
        descriptions = {
            "timeout": self.lang.get("honeypot_opt_timeout_desc", "Muted for the selected duration"),
            "kick":    self.lang.get("honeypot_opt_kick_desc", "Removed from the server, can rejoin"),
            "ban":     self.lang.get("honeypot_opt_ban_desc", "Banned, messages from the last 24 h are deleted"),
        }
        return [
            discord.SelectOption(
                label=template.format(punishment=hp_system.punishment_label(self.lang, action, days)), value=action,
                emoji=emoji, description=descriptions[action], default=action == current,
            )
            for action in db_honeypot.PUNISHMENTS
        ]

    def embed(self, hp: dict) -> discord.Embed:
        days = hp["timeout_days"]
        embed = discord.Embed(
            title=self.lang.get("honeypot_punish_title", "⚙️ Honeypot punishment"),
            description=self.lang.get(
                "honeypot_punish_desc",
                "Choose what happens to members and bots who post in the honeypot. "
                "The duration applies to timeouts. Changes are saved immediately.",
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(name=self.lang.get("honeypot_hub_members", "👤 Members"), value=hp_system.punishment_label(self.lang, hp["punish_members"], days))
        embed.add_field(name=self.lang.get("honeypot_hub_bots", "🤖 Bots"), value=hp_system.punishment_label(self.lang, hp["punish_bots"], days))
        return embed

    async def _save(self, interaction: discord.Interaction, **fields):
        await interaction.response.defer()
        hp = await db_honeypot.update_honeypot_settings(interaction.guild_id, **fields)
        view = HoneypotPunishView(self.lang, self.owner_id, hp)
        await self.show(interaction, view, embed=view.embed(hp))

    @discord.ui.select(row=0)
    async def members_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self._save(interaction, punish_members=select.values[0])

    @discord.ui.select(row=1)
    async def bots_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self._save(interaction, punish_bots=select.values[0])

    @discord.ui.select(row=2)
    async def days_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self._save(interaction, timeout_days=int(select.values[0]))

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary, row=3)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _show_hub(interaction, self.lang, previous=self)

# ─── Cog ─────────────────────────────────────────────────────────────────────

class Honeypot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._tasks: set[asyncio.Task] = set()
        self._cleanup_seen.start()

    def cog_unload(self):
        self._cleanup_seen.cancel()

    @tasks.loop(minutes=10)
    async def _cleanup_seen(self):
        try:
            mono = time.monotonic()
            for k in [k for k, t in _honeypot_seen.items() if mono - t >= HONEYPOT_COOLDOWN_SECONDS]:
                del _honeypot_seen[k]
        except Exception:
            log.exception("[HONEYPOT-CLEANUP] Fehler beim Bereinigen des Cooldown-Caches.")

    @_cleanup_seen.before_loop
    async def _before_cleanup_seen(self):
        await self.bot.wait_until_ready()

    # ─── /honeypot ─────────────────────────────────────────────────────────────

    @app_commands.command(name="honeypot", description=app_commands.locale_str("cmd_honeypot_desc"))
    @app_commands.default_permissions(administrator=True)
    @module_required("automod")
    async def honeypot(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        embed, active = await _hub_embed(interaction.guild, lang)
        view = HoneypotHubView(lang, interaction.user.id, active)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.bind(interaction)

    # ─── Honeypot Listener ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.id == self.bot.user.id:
            return

        settings = await db_settings.get_guild(message.guild.id)
        if settings is None or not settings.get("honeypot_channel_id"):
            return
        if message.channel.id != settings["honeypot_channel_id"]:
            return

        # Jede Nachricht im Honeypot wird weggeräumt, auch die vom Team
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

        member = message.author
        if not isinstance(member, discord.Member):
            # Webhook-/System-Nachrichten: kein Member, den man bestrafen könnte
            return
        if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
            # Team ist immun: keine Strafe, kein Zähler, kein Log
            return

        key = (message.guild.id, member.id)
        now = time.monotonic()
        first_hit = now - _honeypot_seen.get(key, -HONEYPOT_COOLDOWN_SECONDS) >= HONEYPOT_COOLDOWN_SECONDS
        if not first_hit:
            return
        _honeypot_seen[key] = now

        total = await db_honeypot.increment_catches(message.guild.id)
        hp = await db_honeypot.get_honeypot_settings(message.guild.id)
        days = hp["timeout_days"]
        action = hp["punish_bots"] if member.bot else hp["punish_members"]

        result = await hp_system.punish(message.guild, member, action, days)
        if result.applied:
            log.info("[HONEYPOT] %s auf %s bestraft (%s, %s Tage).", member, message.guild.name, action, days)
        elif result.check is not None:
            log.warning("[HONEYPOT] %s für %s auf %s nicht möglich (%s).", action, member, message.guild.name, result.check.issue.value)

        lang = await get_lang(message.guild.id)
        await _send_honeypot_log(self.bot, message.guild, lang, member, message.content, action, result.applied, days, total)

        task = asyncio.create_task(_update_counter(self.bot, message.guild, message.channel.id, lang))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

async def setup(bot):
    await bot.add_cog(Honeypot(bot))
