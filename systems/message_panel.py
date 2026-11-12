"""Panel für eigene Bot-Nachrichten in Discord: anlegen, bearbeiten, löschen.

Gemeinsamer Ablauf für /welcome_message und /boost_message — die beiden unterscheiden sich nur in
Datenbank-Funktionen, Modul, Limit und Texten (MessageKind). Die Texte heißen <key>_msg_<name>,
z. B. welcome_msg_panel_title bzw. boost_msg_panel_title.
"""
import discord
from dataclasses import dataclass
from typing import Awaitable, Callable
from database import db_limits, db_settings
from emojis import NexusEmojis
from systems.premium import is_premium
from systems.premium_ui import send_upsell
from systems.ui import NexusModal, NexusView, with_emoji


@dataclass(frozen=True)
class MessageKind:
    key: str                                   # Textpräfix: "welcome" → welcome_msg_*
    limit_key: str                             # Eintrag in feature_limits, z. B. "welcome_messages"
    upsell: str                                # Premium-Hinweis (systems/premium_ui), z. B. "welcome_limit"
    get: Callable[[int], Awaitable[list[dict]]]                 # (guild_id) -> Nachrichten
    add: Callable[[int, str], Awaitable[int]]                   # (guild_id, text)
    update: Callable[[int, int, str], Awaitable[bool]]          # (message_id, guild_id, text)
    delete: Callable[[int, int], Awaitable[bool]]               # (message_id, guild_id)
    fallbacks: dict[str, str]                  # englische Ersatztexte, falls ein Text fehlt

    def text(self, lang: dict, name: str) -> str:
        return lang.get(f"{self.key}_msg_{name}", self.fallbacks.get(name, name))


# Gemeinsame englische Ersatztexte; MessageKind.fallbacks ergänzt/überschreibt sie je Art
COMMON_FALLBACKS = {
    "modal_edit_title": "Edit Message",
    "modal_label": "Message",
    "added": "{nexus_checkmark} Message saved.",
    "updated": "{nexus_checkmark} Message saved.",
    "select_placeholder": "Select a message...",
    "delete_confirm": "Are you sure you want to delete message #{id}?",
    "delete_yes": "Yes, delete",
    "delete_no": "Cancel",
    "deleted": "{nexus_checkmark} Message #{id} deleted.",
    "delete_cancelled": "Canceled.",
    "btn_add": "➕ Add",
    "btn_edit": "✏️ Edit",
    "btn_delete": "🗑️ Delete",
    "pick_edit": "Which message would you like to edit?",
    "pick_delete": "Which message would you like to delete?",
    "panel_default_notice": "*No custom messages set — default message will be used.*",
}


# ─── Modal: Anlegen & Bearbeiten teilen sich dieselbe Klasse ─────────────────

class MessageModal(NexusModal):
    def __init__(self, kind: MessageKind, lang: dict, guild_id: int, message_id: int | None = None, current_text: str = ""):
        super().__init__(lang, title=kind.text(lang, "modal_edit_title" if message_id else "modal_title"))
        self.kind = kind
        self.guild_id = guild_id
        self.message_id = message_id
        self.message_input = discord.ui.TextInput(
            label=kind.text(lang, "modal_label"),
            style=discord.TextStyle.paragraph,
            placeholder=kind.text(lang, "modal_placeholder"),
            default=current_text,
            max_length=1000,
            required=True,
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.message_id is None:
            await self.kind.add(self.guild_id, self.message_input.value)
            name = "added"
        else:
            await self.kind.update(self.message_id, self.guild_id, self.message_input.value)
            name = "updated"
        await interaction.followup.send(
            self.kind.text(self.lang, name).format(nexus_checkmark=NexusEmojis.CHECKMARK), ephemeral=True,
        )


# ─── Auswahl bei mehreren Nachrichten, Löschen bestätigen ────────────────────

class MessageSelectView(NexusView):
    """Zwischenschritt bei mehreren Nachrichten — welche ist gemeint?"""

    def __init__(self, kind: MessageKind, messages: list[dict], lang: dict, mode: str, guild_id: int, owner_id: int):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=60)
        self.kind = kind
        self.messages = messages
        self.mode = mode  # "edit" oder "delete"
        self.guild_id = guild_id
        self.select_msg.placeholder = kind.text(lang, "select_placeholder")
        self.select_msg.options = [
            discord.SelectOption(label=f"#{m['id']} — {m['message'][:80]}", value=str(m["id"]))
            for m in messages
        ][:25]

    @discord.ui.select(placeholder="Select a message...")
    async def select_msg(self, interaction: discord.Interaction, select: discord.ui.Select):
        chosen_id = int(select.values[0])
        chosen = next(m for m in self.messages if m["id"] == chosen_id)
        if self.mode == "edit":
            await interaction.response.send_modal(
                MessageModal(self.kind, self.lang, self.guild_id, message_id=chosen_id, current_text=chosen["message"])
            )
        else:
            await self.show(
                interaction,
                DeleteConfirmView(self.kind, chosen_id, self.guild_id, self.lang, self.owner_id),
                content=with_emoji(NexusEmojis.WARNING, self.kind.text(self.lang, "delete_confirm").format(id=chosen_id)),
            )


class DeleteConfirmView(NexusView):
    def __init__(self, kind: MessageKind, message_id: int, guild_id: int, lang: dict, owner_id: int):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=60)
        self.kind = kind
        self.message_id = message_id
        self.guild_id = guild_id
        self.confirm_btn.label = kind.text(lang, "delete_yes")
        self.cancel_btn.label = kind.text(lang, "delete_no")

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.kind.delete(self.message_id, self.guild_id)
        await interaction.edit_original_response(
            content=self.kind.text(self.lang, "deleted").format(nexus_checkmark=NexusEmojis.CHECKMARK, id=self.message_id),
            view=None,
        )

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=self.kind.text(self.lang, "delete_cancelled"), view=None)


# ─── Haupt-Panel ─────────────────────────────────────────────────────────────

class MessageManageView(NexusView):
    def __init__(self, kind: MessageKind, owner_id: int, guild_id: int, lang: dict, messages: list[dict], limit: int,
                 upgrade_max: int | None = None):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=120)
        self.kind = kind
        self.guild_id = guild_id
        self.messages = messages
        self.limit = limit
        self.upgrade_max = upgrade_max   # Limit mit Premium, falls ein Upgrade mehr erlaubt (sonst None)
        self.add_btn.label = kind.text(lang, "btn_add")
        self.edit_btn.label = kind.text(lang, "btn_edit")
        self.delete_btn.label = kind.text(lang, "btn_delete")
        # Ohne Premium bleibt „Hinzufügen“ am Limit klickbar und zeigt den Premium-Hinweis
        self.add_btn.disabled = len(messages) >= limit and upgrade_max is None
        self.edit_btn.disabled = self.delete_btn.disabled = not messages

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.messages) >= self.limit:
            await send_upsell(interaction, self.lang, self.kind.upsell, limit=self.limit, max=self.upgrade_max)
            return
        await interaction.response.send_modal(MessageModal(self.kind, self.lang, self.guild_id))

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.messages) == 1:
            m = self.messages[0]
            await interaction.response.send_modal(
                MessageModal(self.kind, self.lang, self.guild_id, message_id=m["id"], current_text=m["message"])
            )
            return
        await self._pick(interaction, "edit")

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.messages) == 1:
            m = self.messages[0]
            view = DeleteConfirmView(self.kind, m["id"], self.guild_id, self.lang, self.owner_id)
            await interaction.response.send_message(
                with_emoji(NexusEmojis.WARNING, self.kind.text(self.lang, "delete_confirm").format(id=m["id"])), view=view, ephemeral=True,
            )
            view.bind(interaction)
            return
        await self._pick(interaction, "delete")

    async def _pick(self, interaction: discord.Interaction, mode: str) -> None:
        view = MessageSelectView(self.kind, self.messages, self.lang, mode, self.guild_id, self.owner_id)
        await interaction.response.send_message(self.kind.text(self.lang, f"pick_{mode}"), view=view, ephemeral=True)
        view.bind(interaction)


def build_panel_embed(kind: MessageKind, messages: list[dict], lang: dict, color: int, limit: int) -> discord.Embed:
    embed = discord.Embed(title=kind.text(lang, "panel_title"), color=color)
    if not messages:
        embed.description = kind.text(lang, "panel_default_notice")
    else:
        if len(messages) > limit:
            embed.description = with_emoji(NexusEmojis.LOCKED, lang.get(
                "msg_inactive_hint",
                "Messages beyond your limit of {limit} are kept but not used. "
                "They become active again with Premium or when you delete an active one.",
            ).format(limit=limit))
        for i, m in enumerate(messages):
            tag = f" {with_emoji(NexusEmojis.LOCKED, lang.get('msg_inactive_tag', 'inactive'))}" if i >= limit else ""
            embed.add_field(name=f"#{m['id']}{tag}", value=m["message"][:200], inline=False)
    embed.set_footer(text=kind.text(lang, "panel_placeholders"))
    return embed


async def open_panel(interaction: discord.Interaction, kind: MessageKind, lang: dict) -> None:
    """Panel als ephemere Antwort (Interaction muss schon deferred sein)."""
    guild_id = interaction.guild_id
    messages = await kind.get(guild_id)
    premium = await is_premium(guild_id)
    limit = await db_limits.get_limit(guild_id, kind.limit_key, premium)
    premium_limit = await db_limits.get_limit(guild_id, kind.limit_key, True)
    upgrade_max = premium_limit if not premium and premium_limit > limit else None

    embed = build_panel_embed(kind, messages, lang, await db_settings.get_color(guild_id), limit)
    view = MessageManageView(kind, interaction.user.id, guild_id, lang, messages, limit, upgrade_max)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    view.bind(interaction)
