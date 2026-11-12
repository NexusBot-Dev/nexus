import logging
import discord
from discord import app_commands
from discord.ext import commands, tasks
from database import db_settings, db_stream_alerts, db_limits
from systems.premium import is_premium
from cogs.utils import get_lang, module_required
from emojis import NexusEmojis
from systems.platform_registry import PLATFORM_HANDLERS
from config import NEXUS_FOOTER
from systems.ui import NexusView, NexusModal, error_msg, with_emoji
from systems.alert_dispatch import ALERT_CHANNEL_PERMS, finish_ended_streams
from systems.hierarchy import missing_perms, perm_label
from systems import stream_alert_service as alert_service
from systems import schedule_images, stream_schedule
from systems.premium_ui import send_upsell

log = logging.getLogger(__name__)

PLATFORM_DISPLAY = {
    "twitch":  {"emoji": NexusEmojis.Integrations.TWITCH, "label": "Twitch"},
    "youtube": {"emoji": NexusEmojis.Integrations.YOUTUBE, "label": "YouTube"},
    "kick":    {"emoji": NexusEmojis.Integrations.KICK, "label": "Kick"},
}

PLATFORM_PLACEHOLDER_FALLBACKS = {
    "twitch":  "z.B. Username oder https://twitch.tv/Username",
    "youtube": "z.B. Username oder https://youtube.com/@Username",
    "kick":    "z.B. Username oder https://kick.com/Username",
}


# Abbruchgründe aus systems/stream_alert_service → (Übersetzungsschlüssel, englischer Fallback)
ALERT_ERRORS = {
    "limit": ("alert_limit_reached", "Limit of {limit} monitored channels reached."),
    "user_not_found": ("alert_user_not_found", "User `{login}` was not found."),
    "subscription_failed": ("alert_subscription_failed", "Subscription could not be created. Please try again later."),
    "already_monitored": ("alert_already_added", "`{login}` is already being monitored on this server."),
    "not_monitored": ("alert_not_monitored", "`{login}` is not being monitored on this server."),
}


def alert_error_text(lang: dict, error: alert_service.AlertError, login: str) -> str:
    key, fallback = ALERT_ERRORS[error.reason]
    return error_msg(lang.get(key, fallback).format(login=login, **error.params))


def channel_warning(lang: dict, channel: discord.abc.GuildChannel | None) -> str:
    """Zusatzzeile für die Bestätigung, wenn Nexus im Alert-Kanal nicht posten kann."""
    if channel is None:
        return ""
    miss = missing_perms(channel, *ALERT_CHANNEL_PERMS)
    if not miss:
        return ""
    return "\n" + with_emoji(NexusEmojis.WARNING, lang.get(
        "alert_channel_missing_perms",
        "Heads up: I'm missing permissions in {channel} ({perms}). Until that's fixed, no alerts will show up there.",
    ).format(channel=channel.mention, perms=", ".join(f"`{perm_label(p)}`" for p in miss)))


# ─── /stream_schedule ────────────────────────────────────────────────────────

# Fehlerzustände aus dem letzten Abgleich → (Übersetzungsschlüssel, englischer Fallback)
SCHEDULE_ERRORS = {
    stream_schedule.ERR_PERMISSION: ("sched_err_missing_permission", "I'm missing the **Create Events** permission."),
    stream_schedule.ERR_NO_SCHEDULE: ("sched_err_no_schedule", "There is no stream schedule on Twitch."),
    stream_schedule.ERR_TWITCH: ("sched_err_twitch_error", "Twitch couldn't be reached — retrying within the hour."),
    stream_schedule.ERR_DISCORD: ("sched_err_discord_error", "Discord didn't accept every event — retrying within the hour."),
}


# Bildquelle → (Übersetzungsschlüssel, englischer Fallback)
SCHEDULE_IMAGES = {
    stream_schedule.IMAGE_BANNER: ("sched_image_banner", "Game banner"),
    stream_schedule.IMAGE_OFFLINE: ("sched_image_offline", "Offline image"),
    stream_schedule.IMAGE_NONE: ("sched_image_none", "No image"),
    stream_schedule.IMAGE_CUSTOM: ("sched_image_custom", "Own image"),
}


def _image_label(source: str, lang: dict) -> str:
    key, fallback = SCHEDULE_IMAGES[source]
    return lang.get(key, fallback)


def _schedule_status(entry: stream_schedule.ScheduleEntry, lang: dict, premium: bool) -> str:
    if not entry.enabled:
        return f"{NexusEmojis.OFF} {lang.get('sched_off', 'Off')}"
    if not entry.active:
        return lang.get("alert_inactive_premium", "⏸️ Inactive (Premium limit)")
    text = f"{NexusEmojis.ON} {lang.get('sched_on', 'On')} · " + lang.get(
        "sched_events", "Upcoming events: {count}").format(count=entry.events)
    text += " · " + lang.get("sched_image_line", "Image: {source}").format(
        source=_image_label(stream_schedule.effective_image_source(entry, premium), lang))
    if premium and entry.custom_text:
        text += " · " + lang.get("sched_text_custom", "Own text")
    if entry.error in SCHEDULE_ERRORS:
        key, fallback = SCHEDULE_ERRORS[entry.error]
        text += "\n> " + with_emoji(NexusEmojis.WARNING, lang.get(key, fallback))
    return text


async def schedule_panel(bot, guild: discord.Guild, lang: dict) -> tuple[discord.Embed, "StreamScheduleView | None"]:
    """Übersicht von /stream_schedule — ohne Twitch-Alert nur ein Hinweis, ohne Auswahl."""
    overview = await stream_schedule.get_overview(guild)
    description = lang.get(
        "sched_desc",
        "Dates from the Twitch schedule show up as server events — for the next **{days} days**. "
        "I check every hour and update or remove events when the schedule changes.",
    ).format(days=overview.days)
    if not overview.premium and overview.premium_limit > overview.limit:
        description += "\n" + lang.get(
            "sched_premium_hint", "With Premium: up to {max} streamers.",
        ).format(max=overview.premium_limit)
    if not overview.can_create_events:
        description += "\n\n" + with_emoji(NexusEmojis.WARNING, lang.get(
            "sched_missing_permission",
            "I'm missing the **Create Events** permission — until it's granted, I can't create any events.",
        ))
    embed = discord.Embed(
        title=lang.get("sched_title", "📅 Stream schedule as events"),
        description=description,
        color=await db_settings.get_color(guild.id),
    )
    embed.set_footer(text=NEXUS_FOOTER)
    if not overview.entries:
        embed.add_field(name="\u200b", value=lang.get(
            "sched_no_twitch", "No Twitch channel is being monitored yet. Add one first with `/alert_add`.",
        ), inline=False)
        return embed, None

    used = sum(e.enabled for e in overview.entries)
    display = PLATFORM_DISPLAY["twitch"]
    embed.add_field(
        name=f"{display['emoji']} {display['label']} · {min(used, overview.limit)}/{overview.limit}",
        value="\n".join(f"> **{e.streamer_name}** — {_schedule_status(e, lang, overview.premium)}" for e in overview.entries),
        inline=False,
    )
    return embed, StreamScheduleView(bot, lang, overview.entries, premium=overview.premium)


class StreamScheduleView(NexusView):
    """Auswahl 1: einen Twitch-Kanal ein- oder ausschalten. Auswahl 2: Bildquelle eines eingeschalteten Kanals.
    Button „Text & Bild“: eigene Beschreibung und eigenes Bild (Premium). Danach zeigt sich die Übersicht neu."""

    def __init__(self, bot, lang: dict, entries: list[stream_schedule.ScheduleEntry], *, premium: bool):
        super().__init__(lang, timeout=300)
        self.bot = bot
        self.premium = premium
        self.entries = {e.monitored_channel_id: e for e in entries}
        self.select_streamer.placeholder = lang.get("sched_select_placeholder", "Turn a streamer on or off…")
        self.select_streamer.options = [
            discord.SelectOption(
                label=e.streamer_name[:100], value=str(e.monitored_channel_id),
                description=lang.get("sched_opt_disable", "Turn off") if e.enabled else lang.get("sched_opt_enable", "Turn on"),
                emoji=NexusEmojis.ON if e.enabled else NexusEmojis.OFF,
            )
            for e in entries
        ][:25]
        # Pro eingeschaltetem Kanal die jeweils anderen Quellen, z. B. „Rusirda → Offline-Bild“
        self.select_image.placeholder = lang.get("sched_image_placeholder", "Change image…")
        self.select_image.options = [
            discord.SelectOption(
                label=f"{e.streamer_name} → {_image_label(source, lang)}"[:100], value=f"{e.monitored_channel_id}:{source}",
            )
            for e in entries if e.enabled
            for source in stream_schedule.IMAGE_SOURCES if source != stream_schedule.effective_image_source(e, premium)
            and (source != stream_schedule.IMAGE_CUSTOM or (premium and e.custom_image_hash))   # nur mit Premium und Bild
        ][:25]
        if not self.select_image.options:
            self.remove_item(self.select_image)
        self.enabled_entries = [e for e in entries if e.enabled]
        self.custom_button.label = lang.get("sched_custom_button", "Text & image")
        if not self.enabled_entries:
            self.remove_item(self.custom_button)

    @discord.ui.select(row=0)
    async def select_streamer(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        entry = self.entries[int(select.values[0])]
        try:
            await stream_schedule.set_enabled(self.bot, interaction.guild_id, entry.monitored_channel_id, not entry.enabled)
        except stream_schedule.ScheduleError as e:
            if e.reason == "limit" and e.params["max"] > e.params["limit"]:   # mit Premium mehr möglich
                await send_upsell(interaction, self.lang, "stream_schedule_limit", limit=e.params["limit"], max=e.params["max"])
            elif e.reason == "limit":
                await interaction.followup.send(error_msg(self.lang.get(
                    "sched_limit_reached", "Limit of {limit} streamers reached. Turn another one off first.",
                ).format(limit=e.params["limit"])), ephemeral=True)
            # not_monitored: Alert inzwischen entfernt — die neue Übersicht zeigt es
        embed, view = await schedule_panel(self.bot, interaction.guild, self.lang)
        await self.show(interaction, view or NexusView(self.lang), embed=embed)

    @discord.ui.select(row=1)
    async def select_image(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        monitored_channel_id, source = select.values[0].split(":", 1)
        try:
            await stream_schedule.set_image_source(self.bot, interaction.guild_id, int(monitored_channel_id), source)
        except stream_schedule.ScheduleError:
            pass   # inzwischen ausgeschaltet — die neue Übersicht zeigt es
        embed, view = await schedule_panel(self.bot, interaction.guild, self.lang)
        await self.show(interaction, view or NexusView(self.lang), embed=embed)

    @discord.ui.button(row=2, style=discord.ButtonStyle.secondary, emoji="✏️")
    async def custom_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_premium(interaction.guild_id):
            await send_upsell(interaction, self.lang, "stream_schedule_custom")
            return
        if len(self.enabled_entries) == 1:
            await interaction.response.send_modal(ScheduleCustomModal(self, self.enabled_entries[0]))
            return
        await self.show(interaction, ScheduleCustomPickView(self))   # Übersicht bleibt stehen, nur die Auswahl wechselt

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Übersicht in derselben Nachricht neu aufbauen (nach Formular oder Auswahl)."""
        embed, view = await schedule_panel(self.bot, interaction.guild, self.lang)
        await self.show(interaction, view or NexusView(self.lang), embed=embed)


class ScheduleCustomPickView(NexusView):
    """Mehrere eingeschaltete Streamer: erst auswählen, für wen Text und Bild gelten."""

    def __init__(self, panel: StreamScheduleView):
        super().__init__(panel.lang, timeout=300)
        self.bot = panel.bot
        self.panel = panel
        self.select_entry.placeholder = panel.lang.get("sched_custom_pick", "Text & image for which streamer?")
        self.select_entry.options = [
            discord.SelectOption(label=e.streamer_name[:100], value=str(e.monitored_channel_id))
            for e in panel.enabled_entries
        ][:25]
        self.back.label = panel.lang.get("sched_custom_back", "Back")

    @discord.ui.select(row=0)
    async def select_entry(self, interaction: discord.Interaction, select: discord.ui.Select):
        entry = next(e for e in self.panel.enabled_entries if str(e.monitored_channel_id) == select.values[0])
        await interaction.response.send_modal(ScheduleCustomModal(self, entry))

    @discord.ui.button(row=1, style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.refresh(interaction)

    async def refresh(self, interaction: discord.Interaction) -> None:
        embed, view = await schedule_panel(self.bot, interaction.guild, self.lang)
        await self.show(interaction, view or NexusView(self.lang), embed=embed)


# Gründe aus schedule_images.ImageRejected → (Übersetzungsschlüssel, englischer Fallback)
SCHEDULE_IMAGE_PROBLEMS = {
    "too_large": ("sched_custom_err_too_large", "The image is too large — at most {mb} MB."),
    "too_many_pixels": ("sched_custom_err_too_many_pixels", "The image has too many pixels."),
    "not_an_image": ("sched_custom_err_not_an_image", "That's not a supported image — please use PNG, JPG or WebP."),
}


class ScheduleCustomModal(NexusModal):
    """Eigene Beschreibung und eigenes Bild für einen Streamer (Premium). Danach einmal abgleichen und die
    Übersicht neu zeigen; Fehler kommen als eigene, nur sichtbare Nachricht."""

    def __init__(self, origin: "StreamScheduleView | ScheduleCustomPickView", entry: stream_schedule.ScheduleEntry):
        lang = origin.lang
        super().__init__(lang, title=lang.get("sched_custom_title", "Text & image: {streamer}").format(
            streamer=entry.streamer_name))
        self.origin, self.entry = origin, entry
        self.bot = origin.bot
        placeholders = " ".join(f"{{{p}}}" for p in stream_schedule.TEXT_PLACEHOLDERS)

        self.text_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, required=False, max_length=stream_schedule.MAX_TEXT_LENGTH,
            default=entry.custom_text or "",
            placeholder=lang.get("sched_custom_text_placeholder", "Live now: {title}")[:100],
        )
        self.add_item(discord.ui.Label(
            text=lang.get("sched_custom_text_label", "Description"),
            description=lang.get("sched_custom_text_hint", "Placeholders: {placeholders} · empty = default text")
            .format(placeholders=placeholders)[:100],
            component=self.text_input,
        ))

        self.image_upload = discord.ui.FileUpload(required=False, max_values=1)
        self.add_item(discord.ui.Label(
            text=lang.get("sched_custom_image_label", "Image (cropped to 800×320)"),
            description=lang.get("sched_custom_image_hint", "PNG, JPG or WebP, max. {mb} MB · empty = keep current")
            .format(mb=schedule_images.MAX_UPLOAD_BYTES // (1024 * 1024))[:100],
            component=self.image_upload,
        ))

        self.remove_image = None
        if entry.custom_image_hash:
            self.remove_image = discord.ui.Checkbox()
            self.add_item(discord.ui.Label(
                text=lang.get("sched_custom_remove_label", "Remove own image"),
                description=lang.get("sched_custom_remove_hint", "The game banner is used again.")[:100],
                component=self.remove_image,
            ))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id, mc_id = interaction.guild_id, self.entry.monitored_channel_id
        problems: list[str] = []
        premium_lost = changed = False
        try:
            text = self.text_input.value.strip() or None
            if text != self.entry.custom_text:
                await stream_schedule.set_custom_text(self.bot, guild_id, mc_id, text, sync=False)
                changed = True
            upload = self.image_upload.values[0] if self.image_upload.values else None
            if upload is not None:
                problem = await self._store_image(guild_id, mc_id, upload)
                if problem:
                    problems.append(problem)
                else:
                    changed = True
            elif self.remove_image is not None and self.remove_image.value:
                await stream_schedule.set_custom_image(self.bot, guild_id, mc_id, None, sync=False)
                changed = True
        except stream_schedule.ScheduleError as e:
            if e.reason == "premium":   # Premium inzwischen abgelaufen
                premium_lost = True
            elif e.reason == "text_too_long":
                problems.append(self.lang.get("sched_custom_err_text_too_long", "The text is too long — at most {max} characters.")
                                .format(max=e.params["max"]))
            # not_enabled: inzwischen ausgeschaltet — die neue Übersicht zeigt es
        if changed:
            await stream_schedule.sync_guild(self.bot, guild_id)
        await self.origin.refresh(interaction)
        if premium_lost:
            await send_upsell(interaction, self.lang, "stream_schedule_custom")
        for problem in problems:
            await interaction.followup.send(error_msg(problem), ephemeral=True)

    async def _store_image(self, guild_id: int, mc_id: int, upload: discord.Attachment) -> str | None:
        """Bild laden und speichern. Gibt den Fehlertext zurück, wenn es abgelehnt wurde."""
        mb = schedule_images.MAX_UPLOAD_BYTES // (1024 * 1024)
        if upload.size > schedule_images.MAX_UPLOAD_BYTES:   # gar nicht erst herunterladen
            key, fallback = SCHEDULE_IMAGE_PROBLEMS["too_large"]
            return self.lang.get(key, fallback).format(mb=mb)
        try:
            data = await upload.read()
        except discord.HTTPException:
            return self.lang.get("sched_custom_err_download", "I couldn't load the image — please try again.")
        try:
            await stream_schedule.set_custom_image(self.bot, guild_id, mc_id, data, sync=False)
        except stream_schedule.ScheduleError as e:
            if e.reason != "image_rejected":
                raise
            key, fallback = SCHEDULE_IMAGE_PROBLEMS.get(e.params["problem"], SCHEDULE_IMAGE_PROBLEMS["not_an_image"])
            return self.lang.get(key, fallback).format(mb=mb)
        return None


class AlertMessageModal(NexusModal):
    def __init__(self, sub: dict, lang: dict, cog):
        super().__init__(lang, title=lang.get("alert_message_modal_title", "Edit Custom Message"))
        self.sub = sub
        self.cog = cog

        self.message_input = discord.ui.TextInput(
            label=lang.get("alert_message_modal_label", "Message (leave empty to remove)"),
            style=discord.TextStyle.paragraph,
            placeholder=lang.get("alert_add_modal_message_placeholder", "e.g., Let's go, team! 🎉"),
            default=self.sub.get("message") or "",
            max_length=200,
            required=False,
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        new_message = self.message_input.value.strip() or None

        await db_stream_alerts.update_subscription(
                    guild_id=interaction.guild_id,
                    monitored_channel_id=self.sub["monitored_channel_id"],
                    message=new_message,
                )

        self.sub["message"] = new_message
        color = await db_settings.get_color(interaction.guild_id)
        embed = self.cog._build_edit_embed(self.sub, self.lang, color)

        # Kein eigenes View-Objekt hier zum self.show()en — dieses Modal ist selbst
        # keine View, daher direktes edit_message (kein alter View-Timeout zu stoppen).
        await interaction.edit_original_response(
            embed=embed,
            view=AlertEditView(self.sub, self.lang, self.cog)
        )

class ChannelPickView(NexusView):
    def __init__(self, sub: dict, lang: dict, cog):
        super().__init__(lang, owner_id=None, require=None, timeout=60)
        self.sub = sub
        self.cog = cog
        self.select_channel.placeholder = lang.get("alert_edit_channel_placeholder", "Select new channel...")

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Select new channel...")
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await interaction.response.defer()
        new_channel = select.values[0]
        await db_stream_alerts.update_subscription(
            guild_id=interaction.guild_id,
            monitored_channel_id=self.sub["monitored_channel_id"],
            channel_id=new_channel.id,
        )
        self.sub["channel_id"] = new_channel.id
        color = await db_settings.get_color(interaction.guild_id)
        embed = self.cog._build_edit_embed(self.sub, self.lang, color)

        feedback_text = self.lang.get(
            "alert_edit_channel_updated",
            "{nexus_checkmark} Alert-Channel was changed to {channel}."
        ).format(
            nexus_checkmark=NexusEmojis.CHECKMARK, 
            channel=new_channel.mention
        )
        feedback_text += channel_warning(self.lang, interaction.guild.get_channel(new_channel.id))

        self.stop()
        await interaction.edit_original_response(
            content=feedback_text, 
            embed=embed, 
            view=AlertEditView(self.sub, self.lang, self.cog)
        )


class AlertEditView(NexusView):
    def __init__(self, sub: dict, lang: dict, cog):
        super().__init__(lang, owner_id=None, require=None, timeout=120)
        self.sub = sub
        self.cog = cog
        self.edit_message_btn.label = lang.get("alert_edit_btn_message", "✏️ Change Message")
        self.edit_channel_btn.label = lang.get("alert_edit_btn_channel", "📺 Change Channel")

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def edit_message_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AlertMessageModal(self.sub, self.lang, self.cog))

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def edit_channel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show(
            interaction,
            ChannelPickView(self.sub, self.lang, self.cog),
            content=self.lang.get("alert_edit_pick_channel", "Select the new channel:"),
            embed=None,
        )


class StreamerSelectView(NexusView):
    def __init__(self, subs: list[dict], lang: dict, cog):
        super().__init__(lang, owner_id=None, require=None, timeout=60)
        self.subs = subs
        self.cog = cog
        self.select_streamer.placeholder = lang.get("alert_edit_select_placeholder", "Select a channel...")
        self.select_streamer.options = [
            discord.SelectOption(label=s["streamer_name"], value=str(s["monitored_channel_id"]))
            for s in subs
        ][:25]

    @discord.ui.select(placeholder="Select a channel...")
    async def select_streamer(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        chosen_id = int(select.values[0])
        sub = next(s for s in self.subs if s["monitored_channel_id"] == chosen_id)
        lang = await get_lang(interaction.guild_id)
        color = await db_settings.get_color(interaction.guild_id)
        embed = self.cog._build_edit_embed(sub, lang, color)
        await self.show(interaction, AlertEditView(sub, lang, self.cog), content=None, embed=embed)

class RoleQuickSetView(NexusView):
    def __init__(self, lang: dict, guild_id: int, platform_value: str):
        super().__init__(lang, owner_id=None, require=None, timeout=60)
        self.guild_id = guild_id
        self.platform_value = platform_value
        self._interaction: discord.Interaction | None = None
        self._message_id: int | None = None
        self.select_role.placeholder = lang.get("alert_role_prompt_placeholder", "Select a role (optional)...")

    def bind_followup(self, interaction: discord.Interaction, message: discord.Message) -> "RoleQuickSetView":
        """Diese View lebt auf einer eigenen ephemeren Follow-up-Nachricht, nicht der
        @original-Antwort — deshalb ein eigener Timeout-Pfad statt bind()."""
        self._interaction = interaction
        self._message_id = message.id
        return self

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self._interaction and self._message_id:
            try:
                await self._interaction.followup.edit_message(self._message_id, view=self)
            except discord.HTTPException:
                pass

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Select a role (optional)...", min_values=0, max_values=1)
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not select.values:
            await interaction.response.edit_message(
                content=self.lang.get("alert_role_prompt_skipped", "Alright, no ping configured."),
                view=None,
            )
            return

        role = select.values[0]
        try:
            confirmation_text = self.lang.get("alert_role_set", "{nexus_checkmark} {role} will now be pinged for {platform}.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK,
                role=role.mention,
                platform=self.lang.get("alert_role_all_platforms", "all platforms"),
            )
        except KeyError:
            log.exception("Formatierungsfehler bei alert_role_set — Platzhalter fehlt.")
            confirmation_text = f"{NexusEmojis.CHECKMARK} {role.mention}"  # simpler Fallback, kann nicht crashen
        await db_stream_alerts.set_ping_role(self.guild_id, "all", role.id)
        await interaction.response.edit_message(content=confirmation_text, view=None)

class AddChannelModal(NexusModal):
    def __init__(self, lang: dict, platform_value: str, platform_label: str, handler, channel: discord.TextChannel):
        super().__init__(lang, title=lang.get("alert_add_modal_title", "Add {platform} Channel").format(platform=platform_label))
        self.platform_value = platform_value
        self.handler = handler
        self.channel = channel

        fallback = PLATFORM_PLACEHOLDER_FALLBACKS.get(platform_value, "z.B. Username")
        placeholder_key = f"alert_add_modal_streamer_{platform_value}_placeholder"

        self.streamer_input = discord.ui.TextInput(
            label=lang.get("alert_add_modal_streamer_label", "Username or Link"),
            placeholder=lang.get(placeholder_key, fallback),
            required=True,
            max_length=200,
        )
        self.message_input = discord.ui.TextInput(
            label=lang.get("alert_add_modal_message_label", "Custom Message (optional)"),
            style=discord.TextStyle.paragraph,
            placeholder=lang.get("alert_add_modal_message_placeholder", "e.g., Let's go, team! 🎉"),
            required=False,
            max_length=200,
        )
        self.add_item(self.streamer_input)
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = self.lang

        login = self.handler.normalize_input(self.streamer_input.value)
        if login is None:
            await interaction.followup.send(
                error_msg(lang.get("alert_invalid_username", "That doesn't look like a valid Username or Link.")),
                ephemeral=True,
            )
            return

        message = self.message_input.value.strip() or None
        try:
            await alert_service.add_alert(interaction.guild_id, self.platform_value, login, self.channel.id, message)
        except alert_service.AlertError as e:
            if e.reason == "limit" and not await is_premium(interaction.guild_id):
                premium_max = await db_limits.get_limit(interaction.guild_id, f"stream_alerts_{self.platform_value}", True)
                if premium_max > e.params["limit"]:
                    await send_upsell(interaction, lang, "alert_limit", limit=e.params["limit"], max=premium_max,
                                      platform=PLATFORM_DISPLAY[self.platform_value]["label"])
                    return
            await interaction.followup.send(alert_error_text(lang, e, login), ephemeral=True)
            return

        await interaction.followup.send(
            lang.get("alert_add_success", "{nexus_checkmark} `{login}` is now being monitored — alerts will be posted in {channel}.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK, login=login, channel=self.channel.mention,
            ) + channel_warning(lang, self.channel),
            ephemeral=True,
        )

        existing_role = await db_stream_alerts.get_ping_role(interaction.guild_id, self.platform_value)
        if existing_role is None:
            role_view = RoleQuickSetView(lang, interaction.guild_id, self.platform_value)
            role_msg = await interaction.followup.send(
                lang.get("alert_role_prompt", "🔔 Would you like to set a role to be pinged during live events?"),
                view=role_view,
                ephemeral=True,
                wait=True,
            )
            role_view.bind_followup(interaction, role_msg)

class StreamAlerts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.finish_streams_loop.start()

    async def cog_unload(self):
        self.finish_streams_loop.cancel()

    @tasks.loop(seconds=30)
    async def finish_streams_loop(self):
        """Beendet Streams nach Ablauf der Schonfrist (Live-Nachricht -> "Stream beendet")."""
        try:
            await finish_ended_streams(self.bot)
        except Exception:
            log.exception("Schonfrist-Prüfung der Stream-Alerts fehlgeschlagen.")

    @finish_streams_loop.before_loop
    async def _before_finish_streams(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="alert_add", description=app_commands.locale_str("cmd_alert_add_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("stream_alerts")
    @app_commands.describe(
        platform=app_commands.locale_str("cmd_alert_platform_desc"),
        channel=app_commands.locale_str("cmd_alert_channel_desc"),
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="Twitch", value="twitch"),
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Kick", value="kick"),
    ])
    async def alert_add(
        self,
        interaction: discord.Interaction,
        platform: app_commands.Choice[str],
        channel: discord.TextChannel,
    ):
        lang = await get_lang(interaction.guild_id)
        handler = PLATFORM_HANDLERS.get(platform.value)
        if handler is None:
            await interaction.response.send_message(
                error_msg(lang.get("alert_platform_unavailable", "This platform isn't available yet.")),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            AddChannelModal(lang, platform.value, platform.name, handler, channel)
        )

    @app_commands.command(name="alert_remove", description=app_commands.locale_str("cmd_alert_remove_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("stream_alerts")
    @app_commands.describe(platform=app_commands.locale_str("cmd_alert_platform_desc"))
    @app_commands.choices(platform=[
        app_commands.Choice(name="Twitch", value="twitch"),
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Kick", value="kick"),
    ])
    async def alert_remove(
        self,
        interaction: discord.Interaction,
        platform: app_commands.Choice[str],
        streamer: str,
    ):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)

        handler = PLATFORM_HANDLERS.get(platform.value)
        if handler is None:
            await interaction.followup.send(
                error_msg(lang.get("alert_platform_unavailable", "This platform isn't available yet.")),
                ephemeral=True,
            )
            return

        login = handler.normalize_input(streamer)
        if login is None:
            await interaction.followup.send(
                error_msg(lang.get("alert_invalid_username", "That doesn't look like a valid Twitch username.")),
                ephemeral=True,
            )
            return

        user = await handler.resolve_user(login)
        if user is None:
            await interaction.followup.send(
                error_msg(lang.get("alert_user_not_found", "User `{login}` was not found.").format(login=login)),
                ephemeral=True,
            )
            return

        external_id = user["id"]
        monitored = await db_stream_alerts.get_monitored_channel(platform.value, external_id)
        if monitored is None:
            await interaction.followup.send(
                error_msg(lang.get("alert_not_monitored", "`{login}` is not being monitored on this server.").format(login=login)),
                ephemeral=True,
            )
            return

        try:
            await alert_service.remove_alert(interaction.guild_id, monitored)
        except alert_service.AlertError as e:
            await interaction.followup.send(alert_error_text(lang, e, login), ephemeral=True)
            return

        await interaction.followup.send(
            lang.get("alert_remove_success", "{nexus_checkmark} `{login}` is no longer being monitored.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK, login=login,
            ),
            ephemeral=True,
        )

    @alert_remove.autocomplete("streamer")
    async def alert_remove_autocomplete(self, interaction: discord.Interaction, current: str):
        selected_platform = interaction.namespace.platform or "twitch"
        subs = await db_stream_alerts.get_subscriptions_for_guild(interaction.guild_id)
        return [
            app_commands.Choice(name=f"{s['streamer_name']} ({s['platform']})", value=s['streamer_name'])
            for s in subs
            if s["platform"] == selected_platform and current.lower() in s["streamer_name"].lower()
        ][:25]

    @app_commands.command(name="alert_list", description=app_commands.locale_str("cmd_alert_list_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("stream_alerts")
    async def alert_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        subs = await db_stream_alerts.get_subscriptions_for_guild(interaction.guild_id)

        if not subs:
            await interaction.followup.send(
                lang.get("alert_list_empty", "No monitored channels on this server."),
                ephemeral=True,
            )
            return

        grouped: dict[str, list[dict]] = {}
        for s in subs:
            grouped.setdefault(s["platform"], []).append(s)

        color = await db_settings.get_color(interaction.guild_id)
        embed = discord.Embed(
            title=lang.get("alert_list_title", "{broadcast} Überwachte Kanäle").format(broadcast=NexusEmojis.Modules.BROADCAST),
            color=color,
        )

        for platform in ("twitch", "youtube", "kick"):
            entries = grouped.get(platform)
            if not entries:
                continue

            premium = await is_premium(interaction.guild_id)
            limit = await db_limits.get_limit(interaction.guild_id, f"stream_alerts_{platform}", premium)
            sorted_entries = sorted(entries, key=lambda s: s["created_at"])

            display = PLATFORM_DISPLAY.get(platform, {"emoji": "❔", "label": platform.capitalize()})
            
            # Einträge mit Blockquotes (>) als saubere Kacheln formatieren
            entry_blocks = []
            for i, s in enumerate(sorted_entries):
                if i >= limit:
                    status_text = lang.get("alert_inactive_premium", "⏸️ Inactive (Premium limit)")
                else:
                    status_text = lang.get("alert_live", "🔴 Live") if s["live"] else lang.get("alert_offline", "⚫ Offline")
                
                # Mit "> " formatieren für den Card-Look
                entry_blocks.append(f"> **{s['streamer_name']}** — {status_text} — <#{s['channel_id']}>")

            embed.add_field(
                name=f"{display['emoji']} {display['label']}",
                value="\n".join(entry_blocks),
                inline=False,
            )

        embed.set_footer(text=NEXUS_FOOTER)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="alert_role", description=app_commands.locale_str("cmd_alert_role_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("stream_alerts")
    @app_commands.describe(role=app_commands.locale_str("cmd_alert_role_role_desc"))
    @app_commands.describe(platform=app_commands.locale_str("cmd_alert_role_platform_desc"))
    @app_commands.choices(platform=[
        app_commands.Choice(name="Twitch", value="twitch"),
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Kick", value="kick"),
    ])
    async def alert_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role = None,
        platform: app_commands.Choice[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        premium = await is_premium(interaction.guild_id)

        fallback_notice = ""
        if platform is not None and not premium:
            platform = None  # Fällt zurück auf 'all'
            fallback_notice = lang.get(
                "alert_role_platform_premium",
                "\n*Note: Platform-specific roles require Premium. Setting role for all platforms instead.*"
            )

        target_platform = platform.value if platform else "all"
        label = platform.name if platform else lang.get("alert_role_all_platforms", "all platforms")
        if role is None:
            await db_stream_alerts.remove_ping_role(interaction.guild_id, target_platform)
            await interaction.followup.send(
                lang.get("alert_role_removed", "{nexus_checkmark} Ping role for {platform} has been removed.").format(
                    nexus_checkmark=NexusEmojis.CHECKMARK, platform=label
                ) + fallback_notice,
                ephemeral=True,
            )
            return
        bot_member = interaction.guild.me
        can_ping = role.mentionable or bot_member.guild_permissions.mention_everyone

        await db_stream_alerts.set_ping_role(interaction.guild_id, target_platform, role.id)

        if not can_ping:
            await interaction.followup.send(
                with_emoji(NexusEmojis.WARNING, lang.get(
                    "alert_role_not_pingable",
                    "{role} was saved, but is currently not marked as mentionable by everyone.\n"
                    "For the ping to actually notify (not just display), enable "
                    "**'Allow anyone to @mention this role'** in Server Settings → Roles → {role}, "
                    "or grant me the 'Mention @everyone, @here, and All Roles' permission.",
                ).format(role=role.mention)) + fallback_notice,
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            lang.get("alert_role_set", "{nexus_checkmark} {role} will now be pinged for {platform}.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK, 
                role=role.mention, 
                platform=label
            ) + fallback_notice,
            ephemeral=True,
        )

    def _build_edit_embed(self, sub: dict, lang: dict, color: int) -> discord.Embed:
        display = PLATFORM_DISPLAY.get(sub["platform"], {"emoji": "❔", "label": sub["platform"].capitalize()})
        embed = discord.Embed(
            title=f"{display['emoji']} {sub['streamer_name']} ({display['label']})",
            color=color,
        )
        embed.add_field(name=lang.get("alert_edit_channel_field", "📺 Channel"), value=f"<#{sub['channel_id']}>", inline=True)
        embed.add_field(
            name=lang.get("alert_edit_message_field", "💬 Message"),
            value=sub.get("message") or lang.get("alert_edit_no_message", "*None set*"),
            inline=False,
        )
        embed.set_footer(text=lang.get("alert_edit_role_hint", "Change ping role: /alert_role"))
        return embed

    @app_commands.command(name="alert_edit", description=app_commands.locale_str("cmd_alert_edit_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("stream_alerts")
    @app_commands.describe(platform=app_commands.locale_str("cmd_alert_platform_desc"))
    @app_commands.choices(platform=[
        app_commands.Choice(name="Twitch", value="twitch"),
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Kick", value="kick"),
    ])
    async def alert_edit(self, interaction: discord.Interaction, platform: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        all_subs = await db_stream_alerts.get_subscriptions_for_guild(interaction.guild_id)
        subs = [s for s in all_subs if s["platform"] == platform.value]

        if not subs:
            await interaction.followup.send(
                error_msg(lang.get("alert_edit_none_for_platform", "Nothing is currently being monitored on this platform.")),
                ephemeral=True,
            )
            return

        color = await db_settings.get_color(interaction.guild_id)

        if len(subs) == 1:
            embed = self._build_edit_embed(subs[0], lang, color)
            view = AlertEditView(subs[0], lang, self)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            view.bind(interaction)
            return

        view = StreamerSelectView(subs, lang, self)
        await interaction.followup.send(
            lang.get("alert_edit_pick_streamer", "Which channel would you like to edit?"),
            view=view,
            ephemeral=True,
        )
        view.bind(interaction)

    @app_commands.command(name="stream_schedule", description=app_commands.locale_str("cmd_stream_schedule_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("stream_alerts")
    async def stream_schedule_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        embed, view = await schedule_panel(self.bot, interaction.guild, lang)
        if view is None:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.bind(interaction)

async def setup(bot):
    await bot.add_cog(StreamAlerts(bot))