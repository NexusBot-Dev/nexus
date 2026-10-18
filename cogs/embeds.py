import logging
import discord
import asyncio
from math import ceil
from zoneinfo import ZoneInfo
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime
from database import db_settings, db_embeds, db_limits
from systems.premium import is_premium
from cogs.utils import get_lang
from config import NEXUS_FOOTER
from emojis import NexusEmojis
from systems.ui import NexusModal, NexusView, ConfirmView, send_ephemeral

log = logging.getLogger(__name__)


EMBEDS_PER_PAGE = 10
PICK_PAGE_SIZE = 25
SAVE_NAME_MAX = 80
VIEW_PERMISSION = "manage_messages"

# ─── Helfer ──────────────────────────────────────────────────────────────────

async def _embed_limit(guild_id: int) -> int:
    """Aktuelles Limit gespeicherter Embeds (Free/Premium)."""
    premium = await is_premium(guild_id)
    return await db_limits.get_limit(guild_id, "saved_embeds", premium)

def _build_embed(title: str | None, description: str, color: int | None) -> discord.Embed:
    embed = discord.Embed(title=title or None, description=description, color=color)
    embed.set_footer(text=NEXUS_FOOTER)
    return embed

def _find_channel(guild: discord.Guild, channel_id: int | None):
    """Channel/Thread der Guild oder None (auch wenn er keine Nachrichten empfangen kann)."""
    if not channel_id:
        return None
    channel = guild.get_channel_or_thread(channel_id)
    return channel if hasattr(channel, "send") else None

async def _send_embed(interaction: discord.Interaction, lang: dict, channel, embed: discord.Embed) -> bool:
    """Postet den Embed. Bei einem Fehler wird die Interaction beantwortet und False zurückgegeben."""
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        await send_ephemeral(interaction, lang.get(
            "embed_no_permission",
            "❌ I don't have permission to send messages in {channel}.",
        ).format(channel=channel.mention))
        return False
    except discord.HTTPException as e:
        log.warning("Konnte Embed nicht in %s posten: %s", channel, e)
        await send_ephemeral(interaction, lang.get("error_generic", "❌ Something went wrong. Please try again later."))
        return False
    return True

def _label(text: str, component: discord.ui.Item, description: str | None = None) -> discord.ui.Label:
    """Modal-Zeile mit Beschriftung (Discord-Limits: 45 / 100 Zeichen)."""
    return discord.ui.Label(
        text=text[:45],
        description=description[:100] if description else None,
        component=component,
    )

def _title_input(lang: dict) -> discord.ui.TextInput:
    return discord.ui.TextInput(
        style=discord.TextStyle.short,
        placeholder=lang.get("embed_placeholder_title", "e.g., 📋 Server Rules"),
        required=False,
        max_length=256,
    )

def _content_input(lang: dict) -> discord.ui.TextInput:
    return discord.ui.TextInput(
        style=discord.TextStyle.paragraph,
        placeholder=lang.get("embed_placeholder_cont", "Write your text here...\nMulti-line supported!"),
        required=True,
        max_length=4000,
    )

async def _hub_embed(guild_id: int, lang: dict) -> discord.Embed:
    """Startseite von /embed: kurzer Text + Slot-Auslastung."""
    async with asyncio.TaskGroup() as tg:
        count_task = tg.create_task(db_embeds.get_embed_count(guild_id))
        limit_task = tg.create_task(_embed_limit(guild_id))
        color_task = tg.create_task(db_settings.get_color(guild_id))

    usage = lang.get("embed_list_count", "{count}/{limit} used").format(
        count=count_task.result(), limit=limit_task.result()
    )
    text = lang.get(
        "embed_hub_text",
        "Create embeds, post them, schedule them (Premium) or delete saved ones.",
    )
    return discord.Embed(
        title=lang.get("embed_hub_title", "📋 Embeds"),
        description=f"{text}\n\n{usage}",
        color=color_task.result(),
    )

def _build_list_pages(embeds: list[dict], active_ids: set[int], limit: int, lang: dict, color: int, local_tz) -> list[discord.Embed]:
    """Baut die Seiten für die Embed-Liste. `embeds` ist FIFO sortiert, aktive stehen also oben."""
    description = lang.get("embed_list_count", "{count}/{limit} used").format(count=len(embeds), limit=limit)
    if len(embeds) > limit:
        description += "\n" + lang.get(
            "embed_over_limit", "⚠️ Only the {limit} oldest embeds are active."
        ).format(limit=limit)

    chunks = [embeds[i:i + EMBEDS_PER_PAGE] for i in range(0, len(embeds), EMBEDS_PER_PAGE)]
    pages = []
    for number, chunk in enumerate(chunks, start=1):
        page = discord.Embed(
            title=lang.get("embed_list_title", "📋 Saved Embeds"),
            description=description,
            color=color,
        )
        for e in chunk:
            field_value = f"**{e['title'][:50]}**\n{e['description'][:80]}..."
            inactive = e["id"] not in active_ids
            if inactive:
                field_value += "\n\n" + lang.get(
                    "embed_inactive_hint", "🔒 Inactive — over the limit. Delete embeds or get Premium."
                )

            if e.get("scheduled_at"):
                utc_dt = e["scheduled_at"].replace(tzinfo=ZoneInfo("UTC"))
                local_dt = utc_dt.astimezone(local_tz)
                time_str = local_dt.strftime("%d.%m.%Y um %H:%M")
                field_value += f"\n\n⏰ **Geplant für:** {time_str}"

            page.add_field(
                name=f"{'🔒 ' if inactive else ''}#{e['id']} — {e['name']}",
                value=field_value,
                inline=False,
            )
        if len(chunks) > 1:
            page.set_footer(text=lang.get("embed_list_page", "Page {page}/{pages}").format(page=number, pages=len(chunks)))
        pages.append(page)
    return pages

async def _load_list_pages(guild_id: int, lang: dict) -> list[discord.Embed]:
    """Lädt die Embeds der Guild und baut die Listenseiten. Leere Liste = keine gespeicherten Embeds."""
    embeds = await db_embeds.get_embed_summaries(guild_id)
    if not embeds:
        return []

    async with asyncio.TaskGroup() as tg:
        tz_task = tg.create_task(db_settings.get_timezone(guild_id))
        limit_task = tg.create_task(_embed_limit(guild_id))
        color_task = tg.create_task(db_settings.get_color(guild_id))

    local_tz   = ZoneInfo(tz_task.result() or "Europe/Berlin")
    limit      = limit_task.result()
    active_ids = db_embeds.active_embed_ids(embeds, limit)
    return _build_list_pages(embeds, active_ids, limit, lang, color_task.result(), local_tz)

async def _back_to_hub(view: NexusView, interaction: discord.Interaction) -> None:
    embed = await _hub_embed(interaction.guild_id, view.lang)
    await view.show(interaction, EmbedHubView(view.lang, view.owner_id), embed=embed)

# ─── Views ───────────────────────────────────────────────────────────────────

class EmbedHubView(NexusView):
    """Startseite von /embed (ephemeral): Buttons für alle Aktionen."""

    def __init__(self, lang: dict, owner_id: int):
        super().__init__(lang, owner_id=owner_id, require=VIEW_PERMISSION)
        self.btn_create.label   = lang.get("embed_btn_create", "Create")
        self.btn_create.emoji   = NexusEmojis.ADD
        self.btn_list.label     = lang.get("embed_btn_list", "List")
        self.btn_list.emoji     = "📋"
        self.btn_post.label     = lang.get("embed_btn_post", "Post")
        self.btn_post.emoji     = "📤"
        self.btn_schedule.label = lang.get("embed_btn_schedule", "Schedule ⭐")
        self.btn_schedule.emoji = "⏰"
        self.btn_delete.label   = lang.get("embed_btn_delete", "Delete")
        self.btn_delete.emoji   = NexusEmojis.DELETE

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def btn_create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EmbedCreateModal(self.lang))

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def btn_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        pages = await _load_list_pages(interaction.guild_id, self.lang)
        if not pages:
            await send_ephemeral(interaction, self.lang.get("embed_list_empty", "No saved embeds found."))
            return
        await self.show(interaction, EmbedListView(self.lang, pages, self.owner_id), embed=pages[0])

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def btn_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_picker(interaction, "post")

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def btn_schedule(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_premium(interaction.guild_id):
            await send_ephemeral(interaction, self.lang.get(
                "embed_schedule_premium", "⭐ Scheduled posting is a Premium feature!"
            ))
            return
        await self._open_picker(interaction, "schedule")

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_picker(interaction, "delete")

    async def _open_picker(self, interaction: discord.Interaction, mode: str) -> None:
        embeds = await db_embeds.get_embed_summaries(interaction.guild_id)
        if not embeds:
            await send_ephemeral(interaction, self.lang.get("embed_list_empty", "No saved embeds found."))
            return

        limit = await _embed_limit(interaction.guild_id)
        color = await db_settings.get_color(interaction.guild_id)
        picker = EmbedPickerView(
            self.lang, self.owner_id, mode, embeds,
            db_embeds.active_embed_ids(embeds, limit), color,
        )
        await self.show(interaction, picker, embed=picker.build_embed())

class EmbedListView(NexusView):
    """Blätterbare Embed-Liste (ephemeral)."""

    def __init__(self, lang: dict, pages: list[discord.Embed], owner_id: int):
        super().__init__(lang, owner_id=owner_id, require=VIEW_PERMISSION)
        self.pages = pages
        self.index = 0
        self.back.label = lang.get("embed_btn_back", "Back")
        if len(pages) == 1:
            self.remove_item(self.prev_page)
            self.remove_item(self.next_page)
        else:
            self._sync_buttons()

    def _sync_buttons(self):
        self.prev_page.disabled = self.index <= 0
        self.next_page.disabled = self.index >= len(self.pages) - 1

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(self.index - 1, 0)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(self.index + 1, len(self.pages) - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _back_to_hub(self, interaction)

_PICK_DEFAULTS = {
    "post": (
        "📤 Post embed",
        "Select an embed. Optionally pick a channel — otherwise the saved one is used.",
    ),
    "schedule": (
        "⏰ Schedule embed",
        "Select an embed and a target channel, then choose the time.",
    ),
    "delete": (
        "🗑️ Delete embed",
        "Select the embed you want to delete.",
    ),
}
_PICK_BUTTONS = {
    "post":     ("embed_btn_post",     "Post",       "📤", discord.ButtonStyle.success),
    "schedule": ("embed_btn_schedule", "Schedule ⭐", "⏰", discord.ButtonStyle.success),
    "delete":   ("embed_btn_delete",   "Delete",     "🗑️", discord.ButtonStyle.danger),
}

class EmbedPickerView(NexusView):
    """Embed auswählen und Aktion ausführen (Post / Schedule / Delete). Select ist auf 25 Einträge pro Seite begrenzt."""

    def __init__(
        self,
        lang: dict,
        owner_id: int,
        mode: str,
        embeds: list[dict],
        active_ids: set[int],
        color: int,
    ):
        super().__init__(lang, owner_id=owner_id, require=VIEW_PERMISSION)
        self.mode        = mode
        self.embeds      = embeds        # FIFO sortiert (älteste zuerst), wie in der Liste
        self.active_ids  = active_ids
        self.color       = color
        self.page        = 0
        self.selected_id: int | None = None
        self.channel_id:  int | None = None
        self._paged      = len(embeds) > PICK_PAGE_SIZE

        key, default, emoji, style = _PICK_BUTTONS[mode]
        self.action.label = lang.get(key, default)
        self.action.emoji = emoji
        self.action.style = style
        self.back.label   = lang.get("embed_btn_back", "Back")
        self.embed_select.placeholder   = lang.get("embed_pick_placeholder", "Select an embed...")
        self.channel_select.placeholder = lang.get("embed_pick_channel_placeholder", "Select a channel...")

        if mode == "delete":
            self.remove_item(self.channel_select)
        if not self._paged:
            self.remove_item(self.prev_page)
            self.remove_item(self.next_page)
        self._render()

    # ─── Darstellung ────────────────────────────────────────────────────────

    @property
    def _last_page(self) -> int:
        return max(ceil(len(self.embeds) / PICK_PAGE_SIZE) - 1, 0)

    def _ready(self) -> bool:
        if self.selected_id is None:
            return False
        if self.mode == "schedule" and self.channel_id is None:
            return False
        return True

    def _render(self) -> None:
        chunk = self.embeds[self.page * PICK_PAGE_SIZE:(self.page + 1) * PICK_PAGE_SIZE]
        self.embed_select.options = [
            discord.SelectOption(
                label=f"{'🔒 ' if e['id'] not in self.active_ids else ''}#{e['id']} — {e['name']}"[:100],
                value=str(e["id"]),
                description=((e["title"] or e["description"] or "")[:100]) or None,
                default=(e["id"] == self.selected_id),
            )
            for e in chunk
        ]
        if self._paged:
            self.prev_page.disabled = self.page <= 0
            self.next_page.disabled = self.page >= self._last_page
        self.action.disabled = not self._ready()

    def build_embed(self) -> discord.Embed:
        title_default, hint_default = _PICK_DEFAULTS[self.mode]
        embed = discord.Embed(
            title=self.lang.get(f"embed_pick_title_{self.mode}", title_default),
            description=self.lang.get(f"embed_pick_hint_{self.mode}", hint_default),
            color=self.color,
        )
        if self._paged:
            embed.set_footer(text=self.lang.get("embed_list_page", "Page {page}/{pages}").format(
                page=self.page + 1, pages=self._last_page + 1
            ))
        return embed

    async def _update(self, interaction: discord.Interaction) -> None:
        self._render()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # ─── Auswahl ────────────────────────────────────────────────────────────

    @discord.ui.select(placeholder="…", row=0)
    async def embed_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_id = int(select.values[0])
        await self._update(interaction)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="…",
        row=1,
    )
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.channel_id = select.values[0].id
        select.default_values = select.values   # Auswahl im Menü sichtbar lassen
        await self._update(interaction)

    # ─── Buttons ────────────────────────────────────────────────────────────

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=2)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(self.page - 1, 0)
        self.selected_id = None
        await self._update(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=2)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.page + 1, self._last_page)
        self.selected_id = None
        await self._update(interaction)

    @discord.ui.button(row=2, disabled=True)
    async def action(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.mode == "post":
            await self._do_post(interaction)
        elif self.mode == "schedule":
            await self._do_schedule(interaction)
        else:
            await self._do_delete(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="↩️", row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _back_to_hub(self, interaction)

    # ─── Aktionen ───────────────────────────────────────────────────────────

    async def _load_usable(self, interaction: discord.Interaction) -> dict | None:
        """Embed laden und prüfen, ob er noch existiert und aktiv ist. Antwortet selbst bei Fehlern."""
        embed_data = await db_embeds.get_embed(self.selected_id, interaction.guild_id)
        if embed_data is None:
            await send_ephemeral(interaction, self.lang.get("embed_not_found", "❌ Embed not found."))
            return None

        limit = await _embed_limit(interaction.guild_id)
        if not await db_embeds.is_embed_active(self.selected_id, interaction.guild_id, limit):
            await send_ephemeral(interaction, self.lang.get(
                "embed_inactive",
                "🔒 This embed is inactive: your server is over its saved-embed limit. Delete older embeds or get Premium.",
            ))
            return None
        return embed_data

    async def _do_post(self, interaction: discord.Interaction) -> None:
        embed_data = await self._load_usable(interaction)
        if embed_data is None:
            return

        channel = _find_channel(interaction.guild, self.channel_id or embed_data["channel_id"])
        if channel is None:
            await send_ephemeral(interaction, self.lang.get("embed_no_channel", "❌ No channel specified."))
            return

        color = await db_settings.get_color(interaction.guild_id)
        embed = _build_embed(embed_data["title"], embed_data["description"], color)
        if not await _send_embed(interaction, self.lang, channel, embed):
            return

        await send_ephemeral(interaction, self.lang.get(
            "embed_posted", "{nexus_checkmark} Embed has been posted in {channel}."
        ).format(nexus_checkmark=NexusEmojis.CHECKMARK, channel=channel.mention))

    async def _do_schedule(self, interaction: discord.Interaction) -> None:
        embed_data = await self._load_usable(interaction)
        if embed_data is None:
            return

        channel = _find_channel(interaction.guild, self.channel_id)
        if channel is None:
            await send_ephemeral(interaction, self.lang.get("embed_no_channel", "❌ No channel specified."))
            return

        await interaction.response.send_modal(ScheduleModal(self.lang, embed_data, channel))

    async def _do_delete(self, interaction: discord.Interaction) -> None:
        embed_id = self.selected_id
        confirm = ConfirmView(self.lang, owner_id=interaction.user.id)
        await interaction.response.send_message(
            self.lang.get("embed_delete_confirm", "Delete embed #{id}? This can't be undone.").format(id=embed_id),
            view=confirm,
            ephemeral=True,
        )
        confirm.bind(interaction)

        if not await confirm.wait_result():
            await interaction.edit_original_response(
                content=self.lang.get("embed_delete_cancelled", "Cancelled."), view=None
            )
            return

        deleted = await db_embeds.delete_embed(embed_id, interaction.guild_id)
        if deleted:
            message = self.lang.get("embed_deleted", "{checkmark} Embed #{id} has been deleted.").format(
                id=embed_id, checkmark=NexusEmojis.CHECKMARK
            )
        else:
            message = self.lang.get("embed_not_found", "❌ Embed not found.")
        await interaction.edit_original_response(content=message, view=None)

        await self._refresh_after_delete(interaction.guild_id)

    async def _refresh_after_delete(self, guild_id: int) -> None:
        """Auswahl neu laden (FIFO: nachgerückte Embeds), danach die Picker-Nachricht aktualisieren."""
        anchor = self._anchor
        if anchor is None:
            return
        self.embeds = await db_embeds.get_embed_summaries(guild_id)
        try:
            if not self.embeds:
                hub = EmbedHubView(self.lang, self.owner_id)
                self.stop()
                await anchor.edit_original_response(embed=await _hub_embed(guild_id, self.lang), view=hub)
                hub.bind(anchor)
                return

            self.active_ids  = db_embeds.active_embed_ids(self.embeds, await _embed_limit(guild_id))
            self.selected_id = None
            self.page        = min(self.page, self._last_page)
            self._render()
            await anchor.edit_original_response(embed=self.build_embed(), view=self)
        except discord.HTTPException:
            pass  # Token abgelaufen o.ä. — beim nächsten Öffnen stimmt die Liste wieder


# ─── Modals ──────────────────────────────────────────────────────────────────

class EmbedCreateModal(NexusModal):
    """Erstellen: Titel, Inhalt, Channel, Name und Aktion (Post / Save / Save & Post) — 5 von 5 Komponenten."""

    def __init__(self, lang: dict):
        super().__init__(lang, title=lang.get("embed_modal_title", "Create Embed"))

        self.embed_title       = _title_input(lang)
        self.embed_description = _content_input(lang)
        self.channel_select    = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder=lang.get("embed_placeholder_channel", "Select a channel..."),
            required=False,
        )
        self.save_name = discord.ui.TextInput(
            style=discord.TextStyle.short,
            placeholder=lang.get("embed_placeholder_name", "e.g. Rules v2"),
            required=False,
            max_length=SAVE_NAME_MAX,
        )
        self.mode = discord.ui.RadioGroup(options=[
            discord.RadioGroupOption(label=lang.get("embed_mode_post", "Post"), value="post", default=True),
            discord.RadioGroupOption(label=lang.get("embed_mode_save", "Save"), value="save"),
            discord.RadioGroupOption(label=lang.get("embed_mode_both", "Save & Post"), value="both"),
        ])

        self.add_item(_label(lang.get("embed_field_title", "Title"), self.embed_title))
        self.add_item(_label(lang.get("embed_field_content", "Content"), self.embed_description))
        self.add_item(_label(
            lang.get("embed_field_channel", "Channel"),
            self.channel_select,
            lang.get("embed_field_channel_desc", "Where to post the embed (needed for Post)"),
        ))
        self.add_item(_label(
            lang.get("embed_field_name", "Save as"),
            self.save_name,
            lang.get("embed_field_name_desc", "Optional name for the saved embed"),
        ))
        self.add_item(_label(lang.get("embed_field_action", "Action"), self.mode))

    async def on_submit(self, interaction: discord.Interaction):
        guild_id    = interaction.guild_id
        mode        = self.mode.value or "post"
        do_post     = mode in ("post", "both")
        do_save     = mode in ("save", "both")
        title       = self.embed_title.value or ""
        description = self.embed_description.value

        channel = None
        if self.channel_select.values:
            channel = _find_channel(interaction.guild, self.channel_select.values[0].id)

        if do_post and channel is None:
            await send_ephemeral(interaction, self.lang.get("embed_no_channel", "❌ No channel specified."))
            return

        if do_save and not await db_embeds.can_save_embed(guild_id):
            limit = await _embed_limit(guild_id)
            await send_ephemeral(interaction, self.lang.get(
                "embed_limit_reached", "❌ Limit of {limit} saved embeds reached."
            ).format(limit=limit))
            return

        color = await db_settings.get_color(guild_id)

        # Erst Discord, dann DB: scheitert das Posten, wird nichts gespeichert.
        if do_post:
            if not await _send_embed(interaction, self.lang, channel, _build_embed(title, description, color)):
                return

        embed_id = None
        if do_save:
            name = (self.save_name.value or "").strip() or title.strip()[:SAVE_NAME_MAX] or "Embed"
            embed_id = await db_embeds.save_embed(
                guild_id=guild_id,
                name=name,
                title=title,
                description=description,
                color=color,
                channel_id=channel.id if channel else None,
            )

        if do_post and do_save:
            message = self.lang.get(
                "embed_saved_posted", "{checkmark} Embed saved as #{id} and posted in {channel}."
            ).format(checkmark=NexusEmojis.CHECKMARK, id=embed_id, channel=channel.mention)
        elif do_save:
            message = self.lang.get("embed_saved", "{checkmark} Embed saved as #{id}.").format(
                checkmark=NexusEmojis.CHECKMARK, id=embed_id
            )
        else:
            message = self.lang.get("embed_posted", "{nexus_checkmark} Embed has been posted in {channel}.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK, channel=channel.mention
            )
        await send_ephemeral(interaction, message)

class RulesEmbedModal(NexusModal):
    """Regeln-Embed für /post_rules: Titel + Inhalt, Ziel ist der konfigurierte Regelchannel."""

    def __init__(self, lang: dict, channel: discord.abc.Messageable):
        super().__init__(lang, title=lang.get("embed_modal_title", "Create Embed"))
        self.channel = channel

        self.embed_title       = _title_input(lang)
        self.embed_description = _content_input(lang)
        self.add_item(_label(lang.get("embed_field_title", "Title"), self.embed_title))
        self.add_item(_label(lang.get("embed_field_content", "Content"), self.embed_description))

    async def on_submit(self, interaction: discord.Interaction):
        color = await db_settings.get_color(interaction.guild_id)
        embed = _build_embed(self.embed_title.value, self.embed_description.value, color)
        if not await _send_embed(interaction, self.lang, self.channel, embed):
            return

        await send_ephemeral(interaction, self.lang.get(
            "embed_posted", "{nexus_checkmark} Embed has been posted in {channel}."
        ).format(nexus_checkmark=NexusEmojis.CHECKMARK, channel=self.channel.mention))

# ─── Schedule Modal (Premium) ────────────────────────────────────────────────

class ScheduleModal(NexusModal):
    schedule_input = discord.ui.TextInput(
        label="Zeitpunkt (DD.MM.YYYY HH:MM)",
        placeholder="z.B. 25.12.2025 18:00",
        max_length=16,
    )

    def __init__(self, lang: dict, embed_data: dict, channel: discord.TextChannel):
        super().__init__(lang, title=lang.get("embed_schedule_title", "Scheduled Posting"))
        self.embed_data = embed_data
        self.channel    = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            tz_name = await db_settings.get_timezone(interaction.guild_id)
            local_tz = ZoneInfo(tz_name or "Europe/Berlin")

            naive_dt = datetime.strptime(self.schedule_input.value, "%d.%m.%Y %H:%M")

            scheduled_local = naive_dt.replace(tzinfo=local_tz)
            scheduled_utc = scheduled_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

            now_local = datetime.now(local_tz)
            if scheduled_local < now_local:
                await interaction.response.send_message(
                    self.lang.get("embed_schedule_past", "❌ The specified time is in the past."),
                    ephemeral=True,
                )
                return

            # Die geplante Kopie belegt einen Embed-Slot -> Limit gilt auch hier
            if not await db_embeds.can_save_embed(interaction.guild_id):
                limit = await _embed_limit(interaction.guild_id)
                await interaction.response.send_message(
                    self.lang.get("embed_limit_reached", "❌ Limit of {limit} saved embeds reached.").format(limit=limit),
                    ephemeral=True,
                )
                return

            await db_embeds.save_embed(
                guild_id=interaction.guild_id,
                name=f"Scheduled: {self.embed_data['name']}",
                title=self.embed_data["title"],
                description=self.embed_data["description"],
                color=self.embed_data["color"],
                channel_id=self.channel.id,
                scheduled_at=scheduled_utc,
            )

            await interaction.response.send_message(
                self.lang.get("embed_scheduled", "{checkmark} Embed will be posted on {date}.").format(
                    date=scheduled_local.strftime("%d.%m.%Y %H:%M"),
                    checkmark=NexusEmojis.CHECKMARK
                ),
                ephemeral=True,
            )

        except ValueError:
            await interaction.response.send_message(
                self.lang.get("embed_schedule_invalid", "❌ Invalid format. Please use DD.MM.YYYY HH:MM"),
                ephemeral=True,
            )

# ─── Cog ─────────────────────────────────────────────────────────────────────

class Embeds(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_scheduled.start()

    def cog_unload(self):
        self.check_scheduled.cancel()

    @tasks.loop(minutes=1)
    async def check_scheduled(self):
        """Prüft jede Minute ob geplante Embeds gepostet werden sollen."""
        try:
            due = await db_embeds.get_scheduled_embeds()
            for embed_data in due:
                guild = self.bot.get_guild(embed_data["guild_id"])
                if guild is None:
                    log.warning("Geplantes Embed %s ignoriert: Bot ist nicht auf Server %s.", embed_data["id"], embed_data["guild_id"])
                    await db_embeds.clear_scheduled(embed_data["id"])
                    continue

                limit = await _embed_limit(embed_data["guild_id"])
                if not await db_embeds.is_embed_active(embed_data["id"], embed_data["guild_id"], limit):
                    log.info("Geplantes Embed %s verworfen: Guild %s ist über dem Embed-Limit.", embed_data["id"], embed_data["guild_id"])
                    await db_embeds.clear_scheduled(embed_data["id"])
                    continue

                await db_embeds.clear_scheduled(embed_data["id"])

                channel = guild.get_channel(embed_data["channel_id"])
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(embed_data["channel_id"])
                    except (discord.NotFound, discord.Forbidden):
                        channel = None

                color = await db_settings.get_color(embed_data["guild_id"])
                embed = discord.Embed(
                    title=embed_data["title"],
                    description=embed_data["description"],
                    color=color,
                )

                if channel is None:
                    log.warning(
                        "Geplantes Embed %s übersprungen: Channel %s nicht erreichbar.",
                        embed_data["id"], embed_data["channel_id"],
                    )
                    continue

                try:
                    await channel.send(embed=embed)
                    log.info("Geplanter Embed %s in %s gepostet.", embed_data["id"], channel)
                except discord.HTTPException as e:
                    log.error("Konnte geplantes Embed %s nicht senden: %s", embed_data["id"], e)

        except Exception:
            log.exception("Fehler beim Verarbeiten geplanter Embeds.")

    @check_scheduled.before_loop
    async def before_check_scheduled(self):
        await self.bot.wait_until_ready()

    # ─── Commands ────────────────────────────────────────────────────────────

    @app_commands.command(name="embed", description=app_commands.locale_str("cmd_embed"))
    @app_commands.default_permissions(manage_messages=True)
    async def embed_command(self, interaction: discord.Interaction):
        lang = await get_lang(interaction.guild_id)
        view = EmbedHubView(lang, owner_id=interaction.user.id)
        await interaction.response.send_message(
            embed=await _hub_embed(interaction.guild_id, lang),
            view=view,
            ephemeral=True,
        )
        view.bind(interaction)

    @app_commands.command(name="post_rules", description=app_commands.locale_str("cmd_post_rules"))
    @app_commands.default_permissions(manage_guild=True)
    async def post_rules(self, interaction: discord.Interaction):
        lang     = await get_lang(interaction.guild_id)
        settings = await db_settings.get_guild(interaction.guild_id)

        if not settings or not settings["rules_channel_id"]:
            await interaction.response.send_message(
                lang.get("embed_no_rules_channel", "❌ No rules channel configured. Use `/settings` to set one."),
                ephemeral=True,
            )
            return

        channel = self.bot.get_channel(settings["rules_channel_id"])
        if channel is None:
            await interaction.response.send_message(
                lang.get("embed_channel_not_found", "❌ Rules channel not found."),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            RulesEmbedModal(lang, channel)
        )

async def setup(bot):
    await bot.add_cog(Embeds(bot))