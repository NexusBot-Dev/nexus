import asyncio
import logging
import re
import discord
from dataclasses import dataclass, field
from discord import app_commands
from discord.ext import commands
from database import db_settings, db_reaction_roles
from systems.premium import is_premium
from cogs.utils import get_lang, module_required
from config import NEXUS_COLOR, NEXUS_FOOTER
from emojis import NexusEmojis
from systems.hierarchy import PermCheck, can_grant_role, can_manage_role, can_set_up_self_roles, can_use_channel, format_issue
from systems.ui import NexusView, NexusModal

log = logging.getLogger(__name__)

# ─── Helper zum Parsen von Emojis ─────────────────────────────────────────────

def _clip(text: str, limit: int = 100) -> str:
    """Discord-Limit für SelectOption label/description."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_emoji_for_select(emoji_str: str) -> tuple[str, discord.PartialEmoji]:
    """
    Sorgt dafür, dass Custom Emojis korrekt im Dropdown angezeigt werden.
    Gibt ein (Label, PartialEmoji)-Tupel zurück.
    """
    # Regulärer Ausdruck für Custom Emojis: <:name:id> oder <a:name:id>
    custom_emoji_match = re.match(r'<(a?):([^:]+):(\d+)>', emoji_str)

    if custom_emoji_match:
        animated = bool(custom_emoji_match.group(1))
        name = custom_emoji_match.group(2)
        emoji_id = int(custom_emoji_match.group(3))

        # Erstellt ein PartialEmoji, das discord.ui.Select versteht
        partial = discord.PartialEmoji(animated=animated, name=name, id=emoji_id)
        return name, partial  # Name als Text-Label, PartialEmoji fürs Icon
    else:
        # Standard Unicode Emoji
        partial = discord.PartialEmoji(name=emoji_str)
        return emoji_str, partial

def _role_issue(interaction: discord.Interaction, role: discord.Role, lang: dict) -> str | None:
    """
    Fehlermeldung, falls `role` nicht als Reaktionsrolle taugt — sonst None.
    Discords RoleSelect lässt sich nicht vorfiltern: @everyone, Bot-/Integrationsrollen und
    Rollen über Nexus sind dort immer auswählbar, deshalb wird direkt nach der Auswahl geprüft.
    Gemeinsam genutzt vom Setup (RoleSelectView) und vom Edit-Flow (PanelChangeRoleView).
    """
    if role.is_default():
        return format_issue(can_manage_role(interaction.guild, role), lang)

    if (
        interaction.user.id != interaction.guild.owner_id
        and role >= interaction.user.top_role
    ):
        return lang.get(
            "rr_role_hierarchy_user",
            "⚠️ **Error:** This role is ranked equal to or higher than your own highest role. "
            "You cannot use it for reaction roles. Please choose a different role.",
        )

    if role.managed:
        return lang.get(
            "rr_role_managed",
            "❌ **Error:** This role is managed by an integration (e.g., another bot or Server Booster). "
            "This role cannot be used for reaction roles. Please choose a different role.",
        )

    if role >= interaction.guild.me.top_role:
        return lang.get(
            "rr_role_hierarchy",
            "⚠️ **Error:** This role is **above** me in the server hierarchy. "
            "I do not have permission to assign this role to users. Please move my role higher up in your server settings."
        )

    if not (check := can_grant_role(interaction.guild, role, interaction.user)):
        return format_issue(check, lang)

    return None

# Discord-Grenzen pro Nachricht: höchstens 20 verschiedene Reaktionen bzw. 25 Optionen im
# Select-Menü. Ein eigenes Premium-Limit (feature_limits) gibt es für Reaction Roles nicht.
_MAX_ENTRIES = {"reaction": 20, "dropdown": 25}

def _entry_cap(display_mode: str) -> int:
    return _MAX_ENTRIES.get(display_mode, _MAX_ENTRIES["reaction"])

def _limit_text(lang: dict, cap: int) -> str:
    return lang.get(
        "rr_edit_limit_reached",
        "❌ This panel already has the maximum of {max} roles. Remove a role first or create a second panel.",
    ).format(max=cap)

# ─── Setup Flow Views ────────────────────────────────────────────────────────

class ReactionRoleCreateModal(NexusModal):
    def __init__(self, bot, channel: discord.TextChannel, lang: dict, color: int, premium: bool):
        super().__init__(lang, title=lang.get("rr_modal_create_title", "Create Reaction Role"))
        self.bot = bot
        self.channel = channel
        self.color = color
        self.premium = premium

        self.rr_title = discord.ui.TextInput(
            label=lang.get("rr_modal_field_title", "Message Title",),
            placeholder=lang.get("rr_modal_title_placeholder", "e.g., 🎭 Role Selection"),
            max_length=256,
            required=True
        )
        self.rr_description = discord.ui.TextInput(
            label=lang.get("rr_modal_field_desc", "Description / Instructions"),
            style=discord.TextStyle.paragraph,
            placeholder=lang.get("rr_modal_desc_placeholder", "React to this message to receive a role..."),
            max_length=4000,
            required=True
        )
        self.add_item(self.rr_title)
        self.add_item(self.rr_description)

        if self.premium:
            radio_group = discord.ui.RadioGroup(
                custom_id="rr_display_mode",
                required=True,
                options=[
                    discord.RadioGroupOption(
                        label=lang.get("rr_mode_reaction_label", "Emoji reactions"),
                        value="reaction",
                        description=lang.get("rr_mode_reaction_desc", "Classic — users click an emoji below the message"),
                        default=True,
                    ),
                    discord.RadioGroupOption(
                        label=lang.get("rr_mode_dropdown_label", "Dropdown menu"),
                        value="dropdown",
                        description=lang.get("rr_mode_dropdown_desc", "Tidy overview for many roles — one click opens a personal menu"),
                        default=False,
                    ),
                ],
            )
            self.display_mode_group = discord.ui.Label(
                text=lang.get("rr_mode_select_label", "How should users pick their roles?"),
                component=radio_group,
            )
            self.add_item(self.display_mode_group)
        else:
            self.display_mode_group = None

    async def on_submit(self, interaction: discord.Interaction):
        display_mode = "reaction"
        if self.display_mode_group is not None:
            display_mode = self.display_mode_group.component.value or "reaction"

        embed = discord.Embed(
            title=self.rr_title.value,
            description=self.rr_description.value,
            color=self.color
        )
        embed.set_footer(text=NEXUS_FOOTER)

        # Ohne Admin: Channel-Rechte vorab prüfen (Reaktions-Modus braucht zusätzlich Reaktionen)
        needed = ["send_messages", "embed_links", "read_message_history"]
        if display_mode == "reaction":
            needed.append("add_reactions")
        check = can_use_channel(self.channel, *needed)
        if not check:
            await interaction.response.send_message(format_issue(check, self.lang), ephemeral=True)
            return

        try:
            msg = await self.channel.send(embed=embed)
        except discord.HTTPException as e:
            log.warning("[RR] Konnte RR-Nachricht in %s nicht senden: %s", self.channel.id, e)
            await interaction.response.send_message(
                self.lang.get("error_forbidden", "❌ I'm missing permissions or my role is too low for this. Run `/diagnose` to see what's wrong."),
                ephemeral=True,
            )
            return

        await db_reaction_roles.create_message_ref(
            interaction.guild_id, self.channel.id, msg.id,
            self.rr_title.value, self.rr_description.value,
            display_mode=display_mode,
        )

        setup_embed = discord.Embed(
            title=self.lang.get("rr_setup_title", "⚙️ Nexus Reaction Roles Setup"),
            description=self.lang.get("rr_setup_description",
                "The target message was created in {channel}.\n"
                "[🔗 Jump to message]({url})\n\n"
                "Click **Add Role** to get started!",
            ).format(channel=self.channel.mention, url=msg.jump_url),
            color=self.color,
        )

        view = ReactionRoleSetupView(self.bot, interaction.user.id, self.channel, msg, self.lang)
        await interaction.response.send_message(embed=setup_embed, view=view, ephemeral=True)
        view.bind(interaction)

class ReactionRoleSetupView(NexusView):
    def __init__(self, bot, owner_id: int, channel: discord.TextChannel, message: discord.Message, lang: dict):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        self.bot      = bot
        self.channel  = channel
        self.panel_message = message
        self.add_role_btn.label = lang.get("rr_btn_add_role", "➕ Add Role")
        self.finish_btn.label   = lang.get("rr_btn_finish",   "Finish")
        self.finish_btn.emoji = discord.PartialEmoji.from_str(NexusEmojis.CHECKMARK)

    @discord.ui.button(label="➕ Add Role", style=discord.ButtonStyle.success)
    async def add_role_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Auch hier begrenzen: nach "Rolle hinzufügen" im Edit-Flow landet der Admin in dieser View
        msg_ref = await db_reaction_roles.get_message_ref(interaction.guild_id, self.panel_message.id)
        cap = _entry_cap(msg_ref["display_mode"] if msg_ref else "reaction")
        entries = await db_reaction_roles.get_reaction_roles(interaction.guild_id, self.panel_message.id)
        if len(entries) >= cap:
            await interaction.response.send_message(_limit_text(self.lang, cap), ephemeral=True)
            return

        await self.show(
            interaction,
            RoleSelectView(self.bot, self.owner_id, self.channel, self.panel_message, self.lang),
            embed=_step_role_embed(self.lang),
        )

    @discord.ui.button(label="✅ Fertig", style=discord.ButtonStyle.secondary)
    async def finish_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        color = await db_settings.get_color(interaction.guild_id)
        embed = discord.Embed(
            title=self.lang.get("rr_done", "{nexus_checkmark} Reaction roles configured!").format(nexus_checkmark=NexusEmojis.CHECKMARK),
            description=self.lang.get("rr_done_description", "The message in {channel} is ready.").format(channel=self.channel.mention),
            color=color,
        )
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)

class RoleSelectView(NexusView):
    def __init__(self, bot, owner_id: int, channel: discord.TextChannel, message: discord.Message, lang: dict, *, from_edit: bool = False):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        self.bot      = bot
        self.from_edit = from_edit
        self.channel  = channel
        self.panel_message = message
        self.select_role.placeholder = self.lang.get("rr_select_role_placeholder", "🎭 Select a role...")

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="🎭 Select a role...",
    )
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]

        if issue := _role_issue(interaction, role, self.lang):
            await interaction.response.send_message(issue, ephemeral=True)
            return

        await self.show(
            interaction,
            EmojiInputView(self.bot, self.owner_id, self.channel, self.panel_message, role, self.lang, from_edit=self.from_edit),
            embed=_step_emoji_embed(role, self.lang),
        )

class EmojiInputView(NexusView):
    def __init__(self, bot, owner_id: int, channel: discord.TextChannel, message: discord.Message, role: discord.Role, lang: dict, *, from_edit: bool = False):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        self.bot      = bot
        self.from_edit = from_edit
        self.channel  = channel
        self.panel_message = message
        self.role     = role
        self.emoji_btn.label = lang.get("rr_btn_emoji", "😀 Select Emoji")

    @discord.ui.button(label="😀 Select Emoji", style=discord.ButtonStyle.primary)
    async def emoji_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        # 1. Update der Setup-Nachricht: Warte auf Reaktion
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=self.lang.get("rr_waiting_emoji_title", "⏳ Waiting for Emoji..."),
                description=self.lang.get("rr_waiting_emoji_desc", 
                    "React to the created message in {channel} now!\n\n"
                    "You have **60 seconds**.",
                ).format(channel=self.channel.mention),
                color=NEXUS_COLOR,
            ),
            view=None,
        )

        def check(reaction, user):
            return user.id == self.owner_id and reaction.message.id == self.panel_message.id

        while True:
            try:
                reaction, user = await self.bot.wait_for("reaction_add", timeout=60.0, check=check)
                emoji_str = str(reaction.emoji)

                try:
                    await self.panel_message.remove_reaction(reaction.emoji, user)
                except discord.HTTPException:
                    pass

                if isinstance(reaction.emoji, discord.PartialEmoji) or (
                    isinstance(reaction.emoji, discord.Emoji) and not reaction.emoji.is_usable()
                ):
                    await interaction.followup.send(
                        self.lang.get(
                            "rr_emoji_foreign",
                            "❌ I can't use this emoji because it's from a server I'm not on. Please pick another one.",
                        ),
                        ephemeral=True,
                    )
                    continue

                # --- FEHLER-CHECK 1: Wird DIESES EMOJI schon auf der Nachricht genutzt? ---
                existing_rr = await db_reaction_roles.get_reaction_role(
                    interaction.guild_id, self.panel_message.id, emoji_str
                )
                if existing_rr is not None:
                    # Wir bringen den User zurück zur Rollenauswahl, damit er neu ansetzen kann
                    await interaction.followup.send(
                        self.lang.get(
                            "rr_emoji_already_set",
                            "⚠️ **Notice:** The emoji {emoji_str} is already assigned to this message.\n"
                            "Returning you to the selection so you can try a different role or emoji!",          
                        ).format(emoji_str=emoji_str),
                        ephemeral=True
                    )
                    await interaction.edit_original_response(
                        embed=_step_role_embed(self.lang),
                        view=RoleSelectView(self.bot, self.owner_id, self.channel, self.panel_message, self.lang, from_edit=self.from_edit),
                    )
                    break # Loop beenden, da View gewechselt wurde

                # --- FEHLER-CHECK 2: Wird DIESE ROLLE schon auf dieser Nachricht verwendet? ---
                all_current_roles = await db_reaction_roles.get_reaction_roles(
                    interaction.guild_id, self.panel_message.id
                )
                role_already_used = any(rr["role_id"] == self.role.id for rr in all_current_roles)

                if role_already_used:
                    # Freundlichere Formulierung & automatischer Reset zur Rollenauswahl
                    await interaction.followup.send(
                        self.lang.get(
                            "rr_role_already_set",
                            "ℹ️ **Already assigned:** The role {mentioned_role} is already linked to an emoji.\n"
                            "Simply select a different role to continue!",
                        ).format(mentioned_role=self.role.mention),
                        ephemeral=True
                    )
                    await interaction.edit_original_response(
                        embed=_step_role_embed(self.lang),
                        view=RoleSelectView(self.bot, self.owner_id, self.channel, self.panel_message, self.lang, from_edit=self.from_edit),
                    )
                    break # Loop beenden!

                # --- FEHLER-CHECK 3: Premium-Check für Custom Emojis ---
                if isinstance(reaction.emoji, discord.Emoji):
                    if not await is_premium(interaction.guild_id):
                        await interaction.followup.send(
                            self.lang.get(
                                "rr_premium_emoji",
                                "💎 **Nexus Premium Feature**\n"
                                "Using custom server emojis is a premium feature.\n"
                                "I have reset your selection – please use a standard emoji or choose a different role.",
                            ),
                            ephemeral=True
                        )
                        await interaction.edit_original_response(
                            embed=_step_role_embed(self.lang),
                            view=RoleSelectView(self.bot, self.owner_id, self.channel, self.panel_message, self.lang, from_edit=self.from_edit),
                        )
                        break

                # --- ERFOLG: Weiter zu Schritt 3 ---
                await self.channel.send(
                    self.lang.get(
                        "rr_emoji_feedback",
                        "{nexus_checkmark} Emoji {emoji} has been applied! Go back to <#{channel_id}> to continue."
                    ).format(
                        nexus_checkmark=NexusEmojis.CHECKMARK,
                        emoji=emoji_str,
                        channel_id=interaction.channel_id,
                    ),
                    delete_after=5,
                )

                premium_status = await is_premium(interaction.guild_id)
                await interaction.edit_original_response(
                    embed=_step_mode_embed(self.role, emoji_str, self.lang),
                    view=ModeSelectView(self.bot, self.owner_id, self.channel, self.panel_message, self.role, emoji_str, self.lang, has_premium=premium_status, from_edit=self.from_edit),
                )
                break

            except asyncio.TimeoutError:
                await interaction.edit_original_response(
                    embed=_step_emoji_embed(self.role, self.lang),
                    view=EmojiInputView(self.bot, self.owner_id, self.channel, self.panel_message, self.role, self.lang, from_edit=self.from_edit),
                )
                await interaction.followup.send(
                    self.lang.get("rr_timeout", "❌ Time expired. Click the button again.",),
                    ephemeral=True,
                )
                break

class ModeSelectView(NexusView):
    def __init__(self, bot, owner_id: int, channel: discord.TextChannel, message: discord.Message, role: discord.Role, emoji: str, lang: dict, has_premium: bool, from_edit: bool = False):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        self.bot      = bot
        self.from_edit = from_edit
        self.channel  = channel
        self.panel_message = message
        self.role     = role
        self.emoji    = emoji
        self.toggle_btn.label = lang.get("rr_btn_toggle", "Toggle")
        self.toggle_btn.emoji = discord.PartialEmoji.from_str(NexusEmojis.CHECKMARK)

        if has_premium:
            self.unique_btn.label = lang.get("rr_btn_unique_premium", "Unique")
            self.unique_btn.emoji = discord.PartialEmoji.from_str("🎯")
            self.unique_btn.style = discord.ButtonStyle.primary
        else:
            self.unique_btn.label = lang.get("rr_btn_unique_free", "🔒 Unique (Premium)")
            self.unique_btn.style = discord.ButtonStyle.secondary

    @discord.ui.button(label="Toggle", style=discord.ButtonStyle.success)
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._save_and_continue(interaction, mode="toggle")

    @discord.ui.button(label="🔒 Unique (Premium)", style=discord.ButtonStyle.secondary)
    async def unique_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_premium(interaction.guild_id):
            await interaction.response.send_message(
                self.lang.get("rr_premium_unique"), ephemeral=True
            )
            return
        await self._save_and_continue(interaction, mode="unique")

    async def _save_and_continue(self, interaction: discord.Interaction, mode: str):
        if mode == "unique":
            await self.show(
                interaction,
                GroupInputView(self.bot, self.owner_id, self.channel, self.panel_message, self.role, self.emoji, self.lang, from_edit=self.from_edit),
                embed=_step_group_embed(self.role, self.emoji, self.lang),
            )
        else:
            await self._save(interaction, mode=mode, group_id=None, previous_view=self)

    async def _save(
        self, interaction: discord.Interaction, mode: str, group_id: int | None,
        *, previous_view: NexusView | None = None,
    ):
        """previous_view: die View, die bei Erfolg gestoppt werden muss — meist self
        (direkter Button-Klick), bei Aufruf über GroupModal/GroupSelectView aber eine
        ANDERE, noch sichtbare View (siehe dort)."""
        msg_ref = await db_reaction_roles.get_message_ref(interaction.guild_id, self.panel_message.id)
        display_mode = msg_ref["display_mode"] if msg_ref else "reaction"

        if display_mode == "reaction":
            try:
                await self.panel_message.add_reaction(self.emoji)
            except discord.HTTPException:
                await interaction.response.send_message(
                    self.lang.get("rr_emoji_failed", "❌ Emoji could not be added to the message."), ephemeral=True
                )
                return

        await db_reaction_roles.add_reaction_role(
            guild_id=interaction.guild_id,
            channel_id=self.channel.id,
            message_id=self.panel_message.id,
            role_id=self.role.id,
            emoji=self.emoji,
            mode=mode,
            group_id=group_id,
        )

        if display_mode == "dropdown":
            await _update_dropdown_message(self.panel_message, interaction.guild_id, self.panel_message.id, self.lang)

        if self.from_edit:
            # Aus /reactionrole_edit gestartet: zurück in den Hub, der zeigt die neue Rolle direkt in der Liste
            await _show_hub(interaction, self.bot, self.owner_id, self.panel_message.id, self.lang, previous=previous_view)
            return

        color = await db_settings.get_color(interaction.guild_id)
        embed = discord.Embed(
            title=self.lang.get("rr_role_added_title", "{nexus_checkmark} Role added!").format(nexus_checkmark=NexusEmojis.CHECKMARK),
            description=self.lang.get(
                "rr_added", "{emoji} → {role} (Mode: {mode})"
            ).format(emoji=self.emoji, role=self.role.mention, mode=mode),
            color=color,
        )
        embed.set_footer(text=self.lang.get("rr_add_another_hint", "Would you like to add more roles?"))
        if previous_view is not None:
            previous_view.stop()
        await interaction.response.edit_message(
            embed=embed,
            view=ReactionRoleSetupView(self.bot, self.owner_id, self.channel, self.panel_message, self.lang),
        )

class GroupInputView(NexusView):
    def __init__(self, bot, owner_id: int, channel: discord.TextChannel, message: discord.Message, role: discord.Role, emoji: str, lang: dict, *, from_edit: bool = False):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        self.bot      = bot
        self.from_edit = from_edit
        self.channel  = channel
        self.panel_message = message
        self.role     = role
        self.emoji    = emoji
        self.new_group_btn.label      = lang.get("rr_btn_new_group",      "➕ Create New Group")
        self.existing_group_btn.label = lang.get("rr_btn_existing_group", "📋 Existing Group")

    @discord.ui.button(label="➕ Neue Gruppe erstellen", style=discord.ButtonStyle.primary)
    async def new_group_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Öffnet nur das Modal — diese View bleibt bis zum Submit sichtbar,
        # deshalb sich selbst als parent_view mitgeben, damit das Modal sie
        # bei Erfolg stoppen kann (sonst läuft ihr 300s-Timeout unabhängig weiter).
        await interaction.response.send_modal(
            GroupModal(self.bot, self.owner_id, self.channel, self.panel_message, self.role, self.emoji, self.lang, parent_view=self, from_edit=self.from_edit)
        )

    @discord.ui.button(label="📋 Bestehende Gruppe", style=discord.ButtonStyle.secondary)
    async def existing_group_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        groups = await db_reaction_roles.get_groups(interaction.guild_id)
        if not groups:
            await interaction.response.send_message(
                self.lang.get("rr_no_groups", "No existing groups found."),
                ephemeral=True,
            )
            return
        await self.show(
            interaction,
            GroupSelectView(self.bot, self.owner_id, self.channel, self.panel_message, self.role, self.emoji, groups, self.lang, from_edit=self.from_edit),
            embed=_step_group_select_embed(self.lang),
        )

class GroupModal(NexusModal):
    group_name = discord.ui.TextInput(
        label="Group Name",
        placeholder="e.g., Colors, Teams, Regions...",
        min_length=1,
        max_length=100,
    )

    def __init__(self, bot, owner_id: int, channel, message, role, emoji, lang: dict, parent_view: NexusView, *, from_edit: bool = False):
        super().__init__(lang, title=lang.get("rr_modal_group_title", "Create Group"))
        self.bot      = bot
        self.from_edit = from_edit
        self.owner_id = owner_id
        self.channel  = channel
        self.panel_message = message
        self.role     = role
        self.emoji    = emoji
        self.parent_view = parent_view

        self.group_name.label = self.lang.get("rr_modal_group_label", "Group Name")
        self.group_name.placeholder = self.lang.get("rr_modal_group_placeholder", "e.g., Colors, Teams, Regions...")

    async def on_submit(self, interaction: discord.Interaction):
        group_id  = await db_reaction_roles.create_group(
            interaction.guild_id, self.group_name.value, "unique"
        )
        premium_status = await is_premium(interaction.guild_id)
        mode_view = ModeSelectView(
            self.bot, self.owner_id, self.channel, self.panel_message, self.role, self.emoji, self.lang, has_premium=premium_status, from_edit=self.from_edit
        )
        await mode_view._save(interaction, mode="unique", group_id=group_id, previous_view=self.parent_view)

class GroupSelectView(NexusView):
    def __init__(self, bot, owner_id: int, channel, message, role, emoji, groups: list[dict], lang: dict, *, from_edit: bool = False):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        self.bot      = bot
        self.from_edit = from_edit
        self.channel  = channel
        self.panel_message = message
        self.role     = role
        self.emoji    = emoji

        options = [
            discord.SelectOption(label=g["name"], value=str(g["id"]))
            for g in groups
        ][:25]
        select = discord.ui.Select(placeholder=self.lang.get("rr_select_group", "Select a group..."), options=options)
        select.callback = self._select_callback
        self.add_item(select)

    async def _select_callback(self, interaction: discord.Interaction):
        group_id  = int(interaction.data["values"][0])
        premium_status = await is_premium(interaction.guild_id)
        mode_view = ModeSelectView(
            self.bot, self.owner_id, self.channel, self.panel_message, self.role, self.emoji, self.lang, has_premium=premium_status, from_edit=self.from_edit
        )
        await mode_view._save(interaction, mode="unique", group_id=group_id, previous_view=self)

# ─── Premium Dropdown ────────────────────────────────────────────────────────

async def _update_dropdown_message(msg: discord.Message, guild_id: int, message_id: int, lang: dict):
    """Baut/aktualisiert den 'Select your roles'-Button unter einer Dropdown-Modus-Nachricht."""
    roles = await db_reaction_roles.get_reaction_roles(guild_id, message_id)
    if not roles:
        # Keine Rollen mehr -> Button entfernen, sonst öffnet er ein leeres Menü
        await msg.edit(view=None)
        return

    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label=lang.get("rr_dropdown_open_button", "🎭 Select your roles"),
            style=discord.ButtonStyle.primary,
            custom_id=f"rr_open_{message_id}",
        )
    )
    await msg.edit(view=view)

# ─── Embed Helpers ────────────────────────────────────────────────────────────

def _step_role_embed(lang: dict) -> discord.Embed:
    return discord.Embed(
        title=lang.get("rr_step1_title", "🎭 Step 1: Select Role"),
        description=lang.get("rr_step1_desc", "Select the role that should be assigned."),
        color=NEXUS_COLOR,
    )

def _step_emoji_embed(role: discord.Role, lang: dict) -> discord.Embed:
    return discord.Embed(
        title=lang.get("rr_step2_title", "😀 Step 2: Choose Emoji"),
        description=lang.get(
            "rr_step2_desc",
            "Role: {role}\n\n1. Click the blue button below.\n" \
            "2. React **directly on the setup message** with your desired emoji.\n\n"
            "**Custom Server Emojis:** ⭐ Premium Only",
        ).format(role=role.mention),
        color=NEXUS_COLOR,
    )

def _step_mode_embed(role: discord.Role, emoji: str, lang: dict) -> discord.Embed:
    return discord.Embed(
        title=lang.get("rr_step3_title", "⚙️ Step 3: Choose Mode"),
        description=lang.get(
            "rr_step3_desc",
            "Role: {role} | Emoji: {emoji}\n\n"
            "**Toggle** — Clicking grants the role, clicking again removes it.\n"
            "**Unique** ⭐ — Users can only own one role from this group.",           
        ).format(role=role.mention, emoji=emoji),
        color=NEXUS_COLOR,
    )

def _step_group_embed(role: discord.Role, emoji: str, lang: dict) -> discord.Embed:
    return discord.Embed(
        title=lang.get("rr_step4_title", "📋 Step 4: Choose Group"),
        description=lang.get(
            "rr_step4_desc",
            "Role: {role} | Emoji: {emoji}\n\nGroups ensure that when selecting a role, all other roles in this group are automatically removed."
        ).format(role=role.mention, emoji=emoji),
        color=NEXUS_COLOR,
    )

def _step_group_select_embed(lang: dict) -> discord.Embed:
    return discord.Embed(
        title=lang.get("rr_group_select_title", "📋 Select Existing Group"),
        description=lang.get("rr_group_select_desc", "Select an existing group from the dropdown."),
        color=NEXUS_COLOR,
    )

# ─── /reactionrole_edit — Übersicht + Hub ────────────────────────────────────

def _panel_gone_text(lang: dict) -> str:
    return lang.get(
        "rr_edit_panel_gone",
        "❌ This reaction role panel no longer exists (deleted in the meantime?).",
    )

async def _fetch_panel_message(
    guild: discord.Guild, channel_id: int, message_id: int,
) -> tuple[discord.TextChannel | None, discord.Message | None, str]:
    """
    Holt Channel + Panel-Nachricht und meldet den Zustand:
      "ok"           — Nachricht erreichbar
      "channel_gone" — Channel gelöscht (oder kein Text-Channel mehr)
      "message_gone" — Nachricht manuell gelöscht
      "no_access"    — Nexus kann den Verlauf nicht lesen. Die Nachricht kann noch existieren,
                       deshalb wird in diesem Fall bewusst KEIN Neu-Posten angeboten (sonst doppelt).
    """
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return None, None, "channel_gone"
    if not can_use_channel(channel, "read_message_history"):
        return channel, None, "no_access"
    try:
        return channel, await channel.fetch_message(message_id), "ok"
    except discord.NotFound:
        return channel, None, "message_gone"
    except discord.Forbidden:
        return channel, None, "no_access"

# ─── Gemeinsame Edit-Logik (Bot-Hub + Dashboard) ─────────────────────────────

async def save_panel_text(guild: discord.Guild, message_id: int, title: str, description: str) -> str:
    """
    Speichert Titel/Beschreibung eines Panels und zieht die Discord-Nachricht per message.edit nach.
    Erst die DB — so bleibt der neue Text auch erhalten, wenn die Nachricht gerade nicht erreichbar ist.
    Ergebnis:
      "updated"    — DB + Nachricht aktualisiert
      "saved_only" — nur DB, Nachricht nicht erreichbar (der Text wird beim Neu-Posten verwendet)
      "not_own"    — nur DB, die Nachricht stammt nicht von Nexus (Alt-Panels an fremden Nachrichten)
      "failed"     — nur DB, Discord hat das Bearbeiten abgelehnt
      "gone"       — Panel existiert nicht (mehr), nichts gespeichert
    """
    msg_ref = await db_reaction_roles.get_message_ref(guild.id, message_id)
    if msg_ref is not None:
        channel_id = msg_ref["channel_id"]
        await db_reaction_roles.update_message_ref(guild.id, message_id, title, description)
    else:
        entries = await db_reaction_roles.get_reaction_roles(guild.id, message_id)
        if not entries:
            return "gone"
        # Alt-Panel ohne Metadaten: Eintrag nachträglich anlegen, damit der Text beim Neu-Posten bleibt
        channel_id = entries[0]["channel_id"]
        await db_reaction_roles.create_message_ref(guild.id, channel_id, message_id, title, description)

    _, message, _ = await _fetch_panel_message(guild, channel_id, message_id)
    if message is None:
        return "saved_only"
    if message.author.id != guild.me.id:
        return "not_own"

    # Bestehendes Embed übernehmen, damit Farbe/Footer/Bild erhalten bleiben
    if message.embeds:
        embed = message.embeds[0].copy()
    else:
        embed = discord.Embed(color=await db_settings.get_color(guild.id))
    embed.title       = title
    embed.description = description
    try:
        await message.edit(embed=embed)
    except discord.NotFound:
        return "saved_only"
    except discord.HTTPException as e:
        log.warning("[RR] Konnte Panel %s auf %s nicht bearbeiten: %s", message_id, guild.id, e)
        return "failed"
    return "updated"

@dataclass
class RepostResult:
    """
    status:
      "reposted"    — neu gepostet; failed listet Emojis/Button, die nicht wiederhergestellt wurden
      "gone"        — Panel existiert nicht (mehr)
      "still_there" — alte Nachricht ist erreichbar oder nicht prüfbar → bewusst nicht doppelt posten
      "no_perms"    — Nexus fehlen Rechte im Ziel-Channel (Details in check)
      "send_failed" — Discord hat das Senden abgelehnt
    """
    status: str
    message: discord.Message | None = None
    failed: list[str] = field(default_factory=list)
    check: PermCheck | None = None

async def repost_panel(guild: discord.Guild, message_id: int, channel: discord.TextChannel, lang: dict) -> RepostResult:
    """Postet ein Panel neu, dessen Nachricht oder Channel gelöscht wurde, und zieht alle Einträge mit um."""
    msg_ref = await db_reaction_roles.get_message_ref(guild.id, message_id)
    entries = await db_reaction_roles.get_reaction_roles(guild.id, message_id)
    if msg_ref is None and not entries:
        return RepostResult("gone")

    # Nur posten, wenn die alte Nachricht sicher weg ist — bei "no_access" könnte sie noch existieren
    old_channel_id = msg_ref["channel_id"] if msg_ref else entries[0]["channel_id"]
    _, _, status = await _fetch_panel_message(guild, old_channel_id, message_id)
    if status not in ("message_gone", "channel_gone"):
        return RepostResult("still_there")

    display_mode = msg_ref["display_mode"] if msg_ref else "reaction"
    needed = ["send_messages", "embed_links", "read_message_history"]
    if display_mode == "reaction":
        needed.append("add_reactions")
    if not (check := can_use_channel(channel, *needed)):
        return RepostResult("no_perms", check=check)

    title       = (msg_ref or {}).get("title") or lang.get("rr_edit_untitled", "(untitled)")
    description = (msg_ref or {}).get("description") or ""
    embed = discord.Embed(
        title=title,
        description=description or None,
        color=await db_settings.get_color(guild.id),
    )
    embed.set_footer(text=NEXUS_FOOTER)

    try:
        new_msg = await channel.send(embed=embed)
    except discord.HTTPException as e:
        log.warning("[RR] Konnte Panel %s auf %s nicht neu posten: %s", message_id, guild.id, e)
        return RepostResult("send_failed")

    # Metadaten + alle Zuordnungen auf die neue Nachricht umziehen
    await db_reaction_roles.move_message_ref(guild.id, message_id, channel.id, new_msg.id)
    if msg_ref is None:
        await db_reaction_roles.create_message_ref(guild.id, channel.id, new_msg.id, title, description)
    log.info("[RR] Panel %s auf %s neu gepostet als %s in %s.", message_id, guild.id, new_msg.id, channel.id)

    failed = []
    if display_mode == "dropdown":
        try:
            await _update_dropdown_message(new_msg, guild.id, new_msg.id, lang)
        except discord.HTTPException as e:
            log.warning("[RR] Dropdown-Button für neu gepostetes Panel %s fehlt: %s", new_msg.id, e)
            failed.append(lang.get("rr_dropdown_open_button", "🎭 Select your roles"))
    else:
        for rr in entries:
            try:
                await new_msg.add_reaction(rr["emoji"])
            except discord.HTTPException:
                failed.append(rr["emoji"])

    return RepostResult("reposted", message=new_msg, failed=failed)

async def _show_hub(
    interaction: discord.Interaction,
    bot,
    owner_id: int,
    message_id: int,
    lang: dict,
    *,
    previous: NexusView | None = None,
):
    """
    Zeigt den Hub frisch aus der DB in der bestehenden (ephemeralen) Nachricht an.
    Anders als CategoryEditView in tickets.py wird hier jedes Mal eine NEUE Instanz gebaut:
    So läuft der Timeout pro Anzeige neu, und ein zwischenzeitlich abgelaufener Hub kann
    keine toten Buttons hinterlassen, wenn der Admin aus einer Unter-View zurückkommt.
    previous: die gerade sichtbare View — wird gestoppt, damit ihr Timeout den Hub nicht überschreibt.
    """
    if previous is not None:
        previous.stop()

    view = PanelEditView(bot, owner_id, message_id, lang)
    embed = await view.load(interaction.guild)
    if embed is None:
        view.stop()
        kwargs = {"content": _panel_gone_text(lang), "embed": None, "view": None}
    else:
        kwargs = {"content": None, "embed": embed, "view": view}

    if interaction.response.is_done():
        await interaction.edit_original_response(**kwargs)
    else:
        await interaction.response.edit_message(**kwargs)

    if embed is not None:
        view.bind(interaction)

class PanelEditView(NexusView):
    """Hub für /reactionrole_edit: Text ändern, Rollen hinzufügen/entfernen/tauschen, Neu-Posten."""

    def __init__(self, bot, owner_id: int, message_id: int, lang: dict):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        self.bot        = bot
        self.message_id = message_id
        self.msg_ref: dict = {}
        self.has_ref    = True
        self.entries: list[dict] = []
        self.channel: discord.TextChannel | None = None
        self.panel_message: discord.Message | None = None
        self.status     = "ok"

        self.text_btn.label   = lang.get("rr_edit_btn_text",   "Edit text")
        self.add_btn.label    = lang.get("rr_edit_btn_add",    "Add role")
        self.remove_btn.label = lang.get("rr_edit_btn_remove", "Remove role")
        self.change_btn.label = lang.get("rr_edit_btn_change", "Change role")
        self.repost_btn.label = lang.get("rr_edit_btn_repost", "Repost")

    async def load(self, guild: discord.Guild) -> discord.Embed | None:
        """Lädt Panel + Einträge frisch und setzt die Buttons passend. None = Panel existiert nicht mehr."""
        msg_ref = await db_reaction_roles.get_message_ref(guild.id, self.message_id)
        entries = await db_reaction_roles.get_reaction_roles(guild.id, self.message_id)
        if msg_ref is None and not entries:
            return None

        if msg_ref is None:
            # Alt-Panel ohne Metadaten-Eintrag: Channel aus den Zuordnungen, klassischer Reaktions-Modus
            msg_ref = {
                "channel_id": entries[0]["channel_id"],
                "title": None,
                "description": None,
                "display_mode": "reaction",
            }
            self.has_ref = False

        self.msg_ref = msg_ref
        self.entries = entries
        self.channel, self.panel_message, self.status = await _fetch_panel_message(
            guild, msg_ref["channel_id"], self.message_id
        )

        # Hinzufügen braucht die Nachricht (Emoji-Reaktion darauf), Neu-Posten nur, wenn sie wirklich weg ist
        self.add_btn.disabled    = self.status != "ok"
        self.remove_btn.disabled = not entries
        self.change_btn.disabled = not entries
        self.repost_btn.disabled = self.status not in ("message_gone", "channel_gone")
        if not self.repost_btn.disabled:
            self.repost_btn.style = discord.ButtonStyle.success

        color = await db_settings.get_color(guild.id)
        return self._overview_embed(guild, color)

    def current_text(self) -> tuple[str, str]:
        """Titel/Beschreibung aus der DB — bei Alt-Panels ohne Eintrag aus dem Embed der Nachricht."""
        title       = self.msg_ref.get("title") or ""
        description = self.msg_ref.get("description") or ""
        if not title and self.panel_message and self.panel_message.embeds:
            source      = self.panel_message.embeds[0]
            title       = source.title or ""
            description = description or source.description or ""
        return title, description

    def _overview_embed(self, guild: discord.Guild, color: int) -> discord.Embed:
        lang = self.lang
        title = self.current_text()[0] or lang.get("rr_edit_untitled", "(untitled)")
        embed = discord.Embed(
            title=lang.get("rr_edit_title", "{setting} Edit message: {name}").format(
                setting=NexusEmojis.Modules.SETTINGS, name=_clip(title, 200)
            ),
            color=color,
        )

        lines = []
        if self.status == "ok":
            lines.append(lang.get("rr_edit_location", "📍 {channel} • [🔗 Jump to message]({url})").format(
                channel=self.channel.mention, url=self.panel_message.jump_url
            ))
        elif self.status == "message_gone":
            lines.append(lang.get(
                "rr_edit_message_gone",
                "⚠️ The panel message in {channel} was deleted. Click **Repost** to post it again — all roles are kept.",
            ).format(channel=self.channel.mention))
        elif self.status == "channel_gone":
            lines.append(lang.get(
                "rr_edit_channel_gone",
                "⚠️ The panel's channel no longer exists. Click **Repost** and pick a new channel — all roles are kept.",
            ))
        else:
            # Kein Lesezugriff: konkret sagen, was fehlt (Forbidden trotz passender Rechte → generischer Text)
            issue = format_issue(can_use_channel(self.channel, "read_message_history"), lang)
            lines.append(issue or lang.get(
                "error_forbidden",
                "❌ I'm missing permissions or my role is too low for this. Run `/diagnose` to see what's wrong.",
            ))

        mode = self.msg_ref["display_mode"]
        if mode == "dropdown":
            mode_label = lang.get("rr_mode_dropdown_label", "Dropdown menu")
        else:
            mode_label = lang.get("rr_mode_reaction_label", "Emoji reactions")
        lines.append(lang.get("rr_edit_display_mode", "🧩 Display: **{mode}**").format(mode=mode_label))
        embed.description = "\n".join(lines)

        deleted_label = lang.get("rr_edit_role_deleted", "⚠️ deleted role")
        role_lines = []
        for rr in self.entries:
            role_text = f"<@&{rr['role_id']}>" if guild.get_role(rr["role_id"]) else deleted_label
            role_lines.append(f"{rr['emoji']} → {role_text} ({rr['mode']})")
        embed.add_field(
            name=lang.get("rr_edit_roles_field", "🎭 Roles ({count}/{max})").format(
                count=len(self.entries), max=_entry_cap(mode)
            ),
            value=_clip("\n".join(role_lines), 1024) if role_lines else lang.get(
                "rr_edit_no_roles", "*No roles yet — click **Add role**.*"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Nexus • Reaction Roles • {self.message_id}")
        return embed

    @discord.ui.button(label="Edit text", emoji="✏️", style=discord.ButtonStyle.secondary, row=0)
    async def text_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PanelTextModal(self, self.lang))

    @discord.ui.button(label="Add role", emoji="➕", style=discord.ButtonStyle.success, row=0)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        display_mode = self.msg_ref["display_mode"]
        cap = _entry_cap(display_mode)
        entries = await db_reaction_roles.get_reaction_roles(interaction.guild_id, self.message_id)
        if len(entries) >= cap:
            await interaction.response.send_message(_limit_text(self.lang, cap), ephemeral=True)
            return

        # Reaktions-Modus: Nexus hängt das Emoji am Ende selbst an die Nachricht
        needed = ["read_message_history"]
        if display_mode == "reaction":
            needed.append("add_reactions")
        if not (check := can_use_channel(self.channel, *needed)):
            await interaction.response.send_message(format_issue(check, self.lang), ephemeral=True)
            return

        # Ab hier übernimmt der bestehende Setup-Flow (Rolle → Emoji → Modus) inkl. aller Prüfungen
        await self.show(
            interaction,
            RoleSelectView(self.bot, self.owner_id, self.channel, self.panel_message, self.lang, from_edit=True),
            embed=_step_role_embed(self.lang),
        )

    @discord.ui.button(label="Remove role", emoji="➖", style=discord.ButtonStyle.danger, row=0)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show(
            interaction,
            PanelEntrySelectView(self, interaction.guild, action="remove"),
            embed=discord.Embed(
                title=self.lang.get("rr_edit_remove_title", "➖ Remove roles"),
                description=self.lang.get(
                    "rr_edit_remove_desc",
                    "Select the roles to remove from this panel. Members keep roles they already have.",
                ),
                color=NEXUS_COLOR,
            ),
        )

    @discord.ui.button(label="Change role", emoji="🔄", style=discord.ButtonStyle.primary, row=0)
    async def change_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show(
            interaction,
            PanelEntrySelectView(self, interaction.guild, action="change"),
            embed=discord.Embed(
                title=self.lang.get("rr_edit_change_title", "🔄 Change role"),
                description=self.lang.get(
                    "rr_edit_change_pick_desc",
                    "Select the entry whose role should be replaced. Emoji and mode stay the same. "
                    "Members keep the old role — only new reactions grant the new one.",
                ),
                color=NEXUS_COLOR,
            ),
        )

    @discord.ui.button(label="Repost", emoji="📤", style=discord.ButtonStyle.secondary, row=1)
    async def repost_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show(
            interaction,
            PanelRepostView(self),
            embed=discord.Embed(
                title=self.lang.get("rr_edit_repost_title", "📤 Repost"),
                description=self.lang.get(
                    "rr_edit_repost_desc",
                    "Choose the channel where the panel should be posted again. All roles and settings are kept.",
                ),
                color=NEXUS_COLOR,
            ),
        )

class PanelTextModal(NexusModal):
    def __init__(self, hub: PanelEditView, lang: dict):
        super().__init__(lang, title=lang.get("rr_edit_text_modal_title", "Edit text"))
        self.hub = hub
        current_title, current_description = hub.current_text()

        self.rr_title = discord.ui.TextInput(
            label=lang.get("rr_modal_field_title", "Message Title"),
            default=current_title[:256] or None,
            max_length=256,
            required=True,
        )
        self.rr_description = discord.ui.TextInput(
            label=lang.get("rr_modal_field_desc", "Description / Instructions"),
            style=discord.TextStyle.paragraph,
            default=current_description[:4000] or None,
            max_length=4000,
            required=True,
        )
        self.add_item(self.rr_title)
        self.add_item(self.rr_description)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        hub = self.hub

        # "gone": inzwischen woanders gelöscht (Dashboard, /reactionrole_delete) — _show_hub meldet das selbst
        status = await save_panel_text(interaction.guild, hub.message_id, self.rr_title.value, self.rr_description.value)
        if status == "saved_only":
            warning = self.lang.get(
                "rr_edit_text_saved_only",
                "💾 Text saved. The panel message is currently not reachable — the new text will be used when you repost it.",
            )
        elif status == "not_own":
            warning = self.lang.get(
                "rr_edit_not_own_message",
                "⚠️ Text saved, but this panel message wasn't posted by me, so I can't edit it.",
            )
        elif status == "failed":
            warning = self.lang.get(
                "error_forbidden",
                "❌ I'm missing permissions or my role is too low for this. Run `/diagnose` to see what's wrong.",
            )
        else:
            warning = None

        await _show_hub(interaction, hub.bot, hub.owner_id, hub.message_id, self.lang, previous=hub)
        if warning:
            await interaction.followup.send(warning, ephemeral=True)

class PanelEntrySelectView(NexusView):
    """Auswahl von Einträgen des Panels — mehrere zum Entfernen, einer zum Rollentausch."""

    def __init__(self, hub: PanelEditView, guild: discord.Guild, *, action: str):
        super().__init__(hub.lang, owner_id=hub.owner_id, require=None, timeout=180)
        self.hub    = hub
        self.action = action
        self.back_btn.label = self.lang.get("rr_edit_btn_back", "Back")

        deleted_label = self.lang.get("rr_edit_role_deleted", "⚠️ deleted role")
        options = []
        for rr in hub.entries[:25]:
            role = guild.get_role(rr["role_id"])
            _, emoji = parse_emoji_for_select(rr["emoji"])
            options.append(discord.SelectOption(
                label=_clip(role.name if role else deleted_label),
                value=str(rr["id"]),
                emoji=emoji,
                description=_clip(rr["mode"]),
            ))

        if action == "remove":
            placeholder = self.lang.get("rr_edit_select_remove", "Select roles to remove...")
        else:
            placeholder = self.lang.get("rr_edit_select_change", "Select the entry to change...")
        select = discord.ui.Select(
            placeholder=placeholder,
            options=options,
            min_values=1,
            max_values=len(options) if action == "remove" else 1,
            row=0,
        )
        select.callback = self._select_callback
        self.add_item(select)

    @discord.ui.button(label="Back", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _show_hub(interaction, self.hub.bot, self.owner_id, self.hub.message_id, self.lang, previous=self)

    async def _select_callback(self, interaction: discord.Interaction):
        entry_ids = {int(v) for v in interaction.data["values"]}
        if self.action == "remove":
            await self._remove(interaction, entry_ids)
            return

        entry = next((rr for rr in self.hub.entries if rr["id"] in entry_ids), None)
        if entry is None:
            await _show_hub(interaction, self.hub.bot, self.owner_id, self.hub.message_id, self.lang, previous=self)
            return
        await self.show(
            interaction,
            PanelChangeRoleView(self.hub, entry),
            embed=discord.Embed(
                title=self.lang.get("rr_edit_change_title", "🔄 Change role"),
                description=self.lang.get(
                    "rr_edit_change_desc",
                    "Current: {emoji} → {role}\n\nSelect the new role for this emoji.",
                ).format(emoji=entry["emoji"], role=f"<@&{entry['role_id']}>"),
                color=NEXUS_COLOR,
            ),
        )

    async def _remove(self, interaction: discord.Interaction, entry_ids: set[int]):
        await interaction.response.defer()
        hub      = self.hub
        guild_id = interaction.guild_id

        removed = []
        for rr in hub.entries:
            if rr["id"] in entry_ids and await db_reaction_roles.delete_reaction_role(rr["id"], guild_id):
                removed.append(rr)

        reactions_left = False
        message = hub.panel_message
        if message is not None and removed:
            if hub.msg_ref["display_mode"] == "dropdown":
                try:
                    await _update_dropdown_message(message, guild_id, hub.message_id, self.lang)
                except discord.HTTPException as e:
                    log.warning("[RR] Dropdown von Panel %s auf %s nicht aktualisiert: %s", hub.message_id, guild_id, e)
            else:
                for rr in removed:
                    try:
                        await message.clear_reaction(rr["emoji"])
                    except discord.Forbidden:
                        # Ohne "Nachrichten verwalten" bleiben die Reaktionen der Mitglieder stehen —
                        # harmlos (keine Zuordnung mehr), aber wenigstens die eigene entfernen
                        reactions_left = True
                        try:
                            await message.remove_reaction(rr["emoji"], interaction.guild.me)
                        except discord.HTTPException:
                            pass
                    except discord.HTTPException:
                        pass

        await _show_hub(interaction, hub.bot, self.owner_id, hub.message_id, self.lang, previous=self)
        if reactions_left:
            await interaction.followup.send(
                self.lang.get(
                    "rr_edit_reactions_left",
                    "⚠️ Roles removed, but I couldn't clear the members' reactions (missing **Manage Messages**). "
                    "They no longer do anything and can be removed manually.",
                ),
                ephemeral=True,
            )

class PanelChangeRoleView(NexusView):
    """Tauscht die Rolle eines Eintrags aus — Emoji, Modus und Gruppe bleiben unverändert."""

    def __init__(self, hub: PanelEditView, entry: dict):
        super().__init__(hub.lang, owner_id=hub.owner_id, require=None, timeout=180)
        self.hub   = hub
        self.entry = entry
        self.select_role.placeholder = self.lang.get("rr_select_role_placeholder", "🎭 Select a role...")
        self.back_btn.label = self.lang.get("rr_edit_btn_back", "Back")

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="🎭 Select a role...", row=0)
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role     = select.values[0]
        guild_id = interaction.guild_id

        if issue := _role_issue(interaction, role, self.lang):
            await interaction.response.send_message(issue, ephemeral=True)
            return

        entries = await db_reaction_roles.get_reaction_roles(guild_id, self.hub.message_id)
        current = next((rr for rr in entries if rr["id"] == self.entry["id"]), None)
        if current is None:
            # Eintrag inzwischen woanders entfernt — zurück in den Hub, der zeigt den aktuellen Stand
            await _show_hub(interaction, self.hub.bot, self.owner_id, self.hub.message_id, self.lang, previous=self)
            await interaction.followup.send(
                self.lang.get("rr_edit_entry_gone", "❌ This entry no longer exists."), ephemeral=True
            )
            return

        # Gleiche Rolle gewählt: nichts zu tun (UPDATE würde 0 Zeilen melden und wie ein Fehler aussehen)
        if role.id == current["role_id"]:
            await _show_hub(interaction, self.hub.bot, self.owner_id, self.hub.message_id, self.lang, previous=self)
            return

        if any(rr["role_id"] == role.id for rr in entries):
            await interaction.response.send_message(
                self.lang.get(
                    "rr_role_already_set",
                    "ℹ️ **Already assigned:** The role {mentioned_role} is already linked to an emoji.\n"
                    "Simply select a different role to continue!",
                ).format(mentioned_role=role.mention),
                ephemeral=True,
            )
            return

        await db_reaction_roles.update_reaction_role_role(current["id"], guild_id, role.id)
        log.info("[RR] Panel %s auf %s: %s → Rolle %s getauscht.", self.hub.message_id, guild_id, current["emoji"], role.id)
        await _show_hub(interaction, self.hub.bot, self.owner_id, self.hub.message_id, self.lang, previous=self)

    @discord.ui.button(label="Back", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _show_hub(interaction, self.hub.bot, self.owner_id, self.hub.message_id, self.lang, previous=self)

class PanelRepostView(NexusView):
    """Postet ein Panel neu, dessen Nachricht oder Channel gelöscht wurde, und zieht alle Einträge mit um."""

    def __init__(self, hub: PanelEditView):
        super().__init__(hub.lang, owner_id=hub.owner_id, require=None, timeout=180)
        self.hub = hub
        self.select_channel.placeholder = self.lang.get("rr_edit_repost_placeholder", "📍 Select a channel...")
        self.back_btn.label = self.lang.get("rr_edit_btn_back", "Back")

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="📍 Select a channel...",
        row=0,
    )
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        hub     = self.hub
        guild   = interaction.guild
        channel = guild.get_channel(select.values[0].id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                self.lang.get("error_generic", "❌ Something went wrong. Please try again later."), ephemeral=True
            )
            return

        await interaction.response.defer()
        result = await repost_panel(guild, hub.message_id, channel, self.lang)

        # Bei fehlenden Rechten bleibt die Channel-Auswahl offen, damit ein anderer Channel gewählt werden kann
        if result.status == "no_perms":
            await interaction.followup.send(format_issue(result.check, self.lang), ephemeral=True)
            return
        if result.status == "send_failed":
            await interaction.followup.send(
                self.lang.get("error_forbidden", "❌ I'm missing permissions or my role is too low for this. Run `/diagnose` to see what's wrong."),
                ephemeral=True,
            )
            return

        # "gone" / "still_there": der Hub zeigt den aktuellen Stand (Panel weg bzw. Nachricht wieder erreichbar)
        new_id = result.message.id if result.message else hub.message_id
        await _show_hub(interaction, hub.bot, self.owner_id, new_id, self.lang, previous=self)
        if failed := result.failed:
            await interaction.followup.send(
                self.lang.get(
                    "rr_edit_repost_partial",
                    "⚠️ Panel reposted, but I couldn't restore: {items}. Remove and re-add these roles if needed.",
                ).format(items=" ".join(failed)),
                ephemeral=True,
            )

    @discord.ui.button(label="Back", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _show_hub(interaction, self.hub.bot, self.owner_id, self.hub.message_id, self.lang, previous=self)

# ─── Cog ─────────────────────────────────────────────────────────────────────

class ReactionRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        await self._handle_reaction(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        await self._handle_reaction(payload, add=False)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent, add: bool):
        emoji_str = str(payload.emoji)
        rr = await db_reaction_roles.get_reaction_role(
            payload.guild_id, payload.message_id, emoji_str
        )
        if rr is None:
            return

        msg_ref = await db_reaction_roles.get_message_ref(payload.guild_id, payload.message_id)
        display_mode = msg_ref["display_mode"] if msg_ref else "reaction"
        if display_mode != "reaction":
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except (discord.NotFound, discord.HTTPException):
                log.warning(
                    "[RR] Konnte Member %s auf %s nicht auflösen (Cache-Miss + fetch fehlgeschlagen).",
                    payload.user_id, guild.name
                )
                return

        role = guild.get_role(rr["role_id"])
        if not role or member.bot:
            return

        if add:
            if rr["mode"] == "unique" and rr["group_id"]:
                group_roles = await db_reaction_roles.get_group_roles(
                    payload.guild_id, rr["group_id"]
                )
                rr_msg = None
                channel = guild.get_channel(payload.channel_id)
                for gr in group_roles:
                    if gr["role_id"] != rr["role_id"]:
                        other_role = guild.get_role(gr["role_id"])
                        if other_role and other_role in member.roles:
                            if not can_manage_role(guild, other_role):
                                continue
                            try:
                                await member.remove_roles(other_role, reason="Nexus Reaction Roles (unique)")
                            except discord.HTTPException as e:
                                log.warning("[RR] Konnte Rolle %s auf %s nicht entfernen: %s", other_role.id, guild.id, e)
                                continue
                            if isinstance(channel, discord.TextChannel):
                                try:
                                    if rr_msg is None:
                                        rr_msg = await channel.fetch_message(payload.message_id)
                                    await rr_msg.remove_reaction(gr["emoji"], member)
                                except discord.HTTPException:
                                    pass
        check = can_manage_role(guild, role)
        if not check:
            log.warning(
                "[RR] Rolle %s auf %s nicht verwaltbar: %s %s",
                role.id, guild.id, check.issue.value, check.missing,
            )
            return

        try:
            if add:
                await member.add_roles(role, reason="Nexus Reaction Roles")
            else:
                await member.remove_roles(role, reason="Nexus Reaction Roles")
        except discord.HTTPException as e:
            log.warning("[RR] Rollenänderung %s für %s auf %s fehlgeschlagen: %s", role.id, member.id, guild.id, e)

    @app_commands.command(name="reactionrole_create", description=app_commands.locale_str("cmd_rr_create_desc"))
    @app_commands.default_permissions(manage_roles=True)
    @module_required("reaction_roles")
    async def reactionrole_create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        lang  = await get_lang(interaction.guild_id)
        # default_permissions lässt sich pro Server überschreiben — vor dem Erstellen selbst prüfen
        if not (check := can_set_up_self_roles(interaction.guild, interaction.user)):
            await interaction.response.send_message(format_issue(check, lang), ephemeral=True)
            return

        color = await db_settings.get_color(interaction.guild_id)
        premium = await is_premium(interaction.guild_id)

        await interaction.response.send_modal(
            ReactionRoleCreateModal(self.bot, channel, lang, color, premium)
        )

    @app_commands.command(name="reactionrole_list", description=app_commands.locale_str("cmd_rr_list_desc"))
    @app_commands.default_permissions(manage_roles=True)
    @module_required("reaction_roles")
    async def reactionrole_list(self, interaction: discord.Interaction):
        lang     = await get_lang(interaction.guild_id)
        messages = await db_reaction_roles.get_all_panels(interaction.guild_id)

        if not messages:
            await interaction.response.send_message(
                lang.get("rr_list_empty"), ephemeral=True
            )
            return

        color = await db_settings.get_color(interaction.guild_id)
        embed = discord.Embed(title=lang.get("rr_list_title"), color=color)
        for msg_data in messages:
            roles = await db_reaction_roles.get_reaction_roles(
                interaction.guild_id, msg_data["message_id"]
            )
            role_list = "\n".join(
                f"{rr['emoji']} → <@&{rr['role_id']}> ({rr['mode']})"
                for rr in roles
            )
            embed.add_field(
                name=f"Nachricht `{msg_data['message_id']}`" + 
                    (f" — {msg_data['title']}" if msg_data.get('title') else ""),
                value=role_list or "—",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="reactionrole_delete", description=app_commands.locale_str("cmd_rr_delete_desc"))
    @app_commands.default_permissions(manage_roles=True)
    @module_required("reaction_roles")
    async def reactionrole_delete(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        # default_permissions lässt sich pro Server überschreiben — vor dem Löschen selbst prüfen (wie create/edit)
        if not (check := can_set_up_self_roles(interaction.guild, interaction.user)):
            await interaction.followup.send(format_issue(check, lang), ephemeral=True)
            return

        try:
            msg_id = int(message_id)
        except ValueError:
            await interaction.followup.send(
                lang.get("invalid_id", "❌ Invalid ID."), ephemeral=True
            )
            return

        msg_ref = await db_reaction_roles.get_message_ref(interaction.guild_id, msg_id)
        roles = await db_reaction_roles.get_reaction_roles(interaction.guild_id, msg_id)
        if not msg_ref and not roles:
            await interaction.followup.send(
                lang.get("rr_not_found", "❌ This reaction role no longer exists."), ephemeral=True
            )
            return

        channel_id = msg_ref["channel_id"] if msg_ref else roles[0].get("channel_id")
        channel = interaction.guild.get_channel(channel_id)
        display_mode = msg_ref["display_mode"] if msg_ref else "reaction"

        if isinstance(channel, discord.TextChannel):
            try:
                msg = await channel.fetch_message(msg_id)
                if display_mode == "dropdown":
                    await msg.edit(view=None)
                else:
                    await msg.clear_reactions()
            except (discord.NotFound, discord.Forbidden):
                pass

        for rr in roles:
            await db_reaction_roles.delete_reaction_role(rr["id"], interaction.guild_id)
        await db_reaction_roles.delete_message_ref(interaction.guild_id, msg_id)

        await interaction.followup.send(
            lang.get("rr_delete_success", "{nexus_checkmark} All reaction roles for message `{id}` have been removed.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK,
                id=msg_id
            ), ephemeral=True
        )

    @app_commands.command(name="reactionrole_edit", description=app_commands.locale_str("cmd_rr_edit_desc"))
    @app_commands.default_permissions(manage_roles=True)
    @module_required("reaction_roles")
    @app_commands.describe(message_id=app_commands.locale_str("cmd_rr_edit_param_desc"))
    async def reactionrole_edit(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ):
        lang = await get_lang(interaction.guild_id)
        # default_permissions lässt sich pro Server überschreiben — vor dem Bearbeiten selbst prüfen
        if not (check := can_set_up_self_roles(interaction.guild, interaction.user)):
            await interaction.response.send_message(format_issue(check, lang), ephemeral=True)
            return

        try:
            msg_id = int(message_id)
        except ValueError:
            await interaction.response.send_message(lang.get("invalid_id", "❌ Invalid ID."), ephemeral=True)
            return

        msg_ref = await db_reaction_roles.get_message_ref(interaction.guild_id, msg_id)
        roles = await db_reaction_roles.get_reaction_roles(interaction.guild_id, msg_id)
        if not msg_ref and not roles:
            await interaction.response.send_message(
                lang.get("rr_not_found", "❌ This reaction role no longer exists."), ephemeral=True
            )
            return

        # Der Hub holt die Discord-Nachricht per API — das kann knapp an die 3-Sekunden-Grenze gehen
        await interaction.response.defer(ephemeral=True)
        await _show_hub(interaction, self.bot, interaction.user.id, msg_id, lang)

    @reactionrole_edit.autocomplete("message_id")
    async def _rr_edit_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        # Auch Panels ohne Rollen anbieten (frisch angelegt oder alle entfernt) — genau die will man bearbeiten
        messages = await db_reaction_roles.get_all_panels(interaction.guild_id)
        return await self._panel_choices(interaction, current, messages)

    @reactionrole_delete.autocomplete("message_id")
    async def _rr_message_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        # Panels ohne Rollen gehören dazu — sonst ließen sie sich nur per Hand-ID löschen
        messages = await db_reaction_roles.get_all_panels(interaction.guild_id)
        return await self._panel_choices(interaction, current, messages)

    async def _panel_choices(
        self,
        interaction: discord.Interaction,
        current: str,
        messages: list[dict],
    ) -> list[app_commands.Choice[str]]:
        lang = await get_lang(interaction.guild_id)
        choices = []

        msg_template = lang.get("rr_autocomplete_label", "Message in {channel_name} ({msg_id})")
        id_fallback = lang.get("rr_autocomplete_id_fallback", "ID:")

        for msg_data in messages:
            msg_id = str(msg_data["message_id"])
            channel_id = msg_data.get("channel_id")

            channel = interaction.guild.get_channel(channel_id)
            channel_name = f"#{channel.name}" if channel else f"{id_fallback} {channel_id}"

            label = msg_template.format(channel_name=channel_name, msg_id=msg_id)

            if len(label) > 100:
                template_base_len = len(msg_template.format(channel_name="", msg_id=msg_id))
                allowed_channel_len = 100 - template_base_len - 3 # -3 für die "..."
                
                short_channel = channel_name[:allowed_channel_len] + "..."
                label = msg_template.format(channel_name=short_channel, msg_id=msg_id)
            
            if current.lower() in label.lower() or current in msg_id:
                choices.append(app_commands.Choice(name=label, value=msg_id))
        
        return choices[:25]

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id") if interaction.data else None
        if not custom_id or not custom_id.startswith("rr_"):
            return
        if interaction.type != discord.InteractionType.component:
            return
        if interaction.response.is_done():
            return

        if custom_id.startswith("rr_open_"):
            await self._open_personal_select(interaction, custom_id)
            return

        if custom_id.startswith("rr_submit_"):
            await self._handle_dropdown_submit(interaction, custom_id)
            return

    async def _open_personal_select(self, interaction: discord.Interaction, custom_id: str):
        lang = await get_lang(interaction.guild_id)
        msg_id = int(custom_id.split("rr_open_")[1])
        roles = await db_reaction_roles.get_reaction_roles(interaction.guild_id, msg_id)

        if not roles:
            await interaction.response.send_message(
                lang.get("rr_not_found", "This reaction role no longer exists."), ephemeral=True
            )
            return

        member = interaction.user
        user_role_ids = {r.id for r in member.roles}

        label_prefix = lang.get("rr_prem_label_prefix", "Role:")
        desc_template = lang.get("rr_prem_desc_template", "Select this for the {role_name} role")

        options = []
        for rr in roles:
            role_obj = interaction.guild.get_role(rr["role_id"])
            if not role_obj:
                continue
            label_text, partial_emoji = parse_emoji_for_select(rr["emoji"])
            options.append(
                discord.SelectOption(
                    label=_clip(f"{label_prefix} {role_obj.name}"),
                    value=str(rr["role_id"]),
                    emoji=partial_emoji,
                    description=_clip(desc_template.format(role_name=role_obj.name)),
                    default=(rr["role_id"] in user_role_ids),
                )
            )

        if not options:
            await interaction.response.send_message(
                lang.get("rr_not_found", "This reaction role no longer exists."), ephemeral=True
            )
            return

        options = options[:25]

        select = discord.ui.Select(
            placeholder=lang.get("rr_prem_placeholder", "Select your roles..."),
            options=options,
            custom_id=f"rr_submit_{msg_id}",
            min_values=0,
            max_values=len(options),
        )
        view = discord.ui.View(timeout=300)
        view.add_item(select)

        await interaction.response.send_message(view=view, ephemeral=True)

    async def _handle_dropdown_submit(self, interaction: discord.Interaction, custom_id: str):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)

        selected_role_ids = {int(val) for val in interaction.data.get("values", [])}
        member = interaction.user
        msg_id = int(custom_id.split("rr_submit_")[1])
        roles = await db_reaction_roles.get_reaction_roles(interaction.guild_id, msg_id)

        if not roles:
            await interaction.followup.send(
                lang.get("rr_not_found", "This reaction role no longer exists."), ephemeral=True
            )
            return

        user_role_ids = {r.id for r in member.roles}
        guild = interaction.guild

        # 1. Pro Unique-Gruppe genau EINE Rolle festlegen.
        #    Neu gewählte Rollen haben Vorrang vor bereits vorhandenen.
        group_choice: dict[int, int] = {}
        for rr in roles:
            if rr["mode"] == "unique" and rr["group_id"] and rr["role_id"] in selected_role_ids:
                gid = rr["group_id"]
                prev = group_choice.get(gid)
                if prev is None or (prev in user_role_ids and rr["role_id"] not in user_role_ids):
                    group_choice[gid] = rr["role_id"]

        roles_to_add: set[discord.Role] = set()
        roles_to_remove: set[discord.Role] = set()

        for rr in roles:
            role_obj = guild.get_role(rr["role_id"])
            if not role_obj:
                continue

            if rr["mode"] == "unique" and rr["group_id"]:
                wanted = group_choice.get(rr["group_id"]) == rr["role_id"]
            else:
                wanted = rr["role_id"] in selected_role_ids

            if wanted and rr["role_id"] not in user_role_ids:
                roles_to_add.add(role_obj)
            elif not wanted and rr["role_id"] in user_role_ids:
                roles_to_remove.add(role_obj)

        # 2. Gruppen können über mehrere Nachrichten gehen → andere Gruppenrollen ebenfalls entfernen
        for gid, chosen_id in group_choice.items():
            if chosen_id in user_role_ids:
                continue
            for gr in await db_reaction_roles.get_group_roles(interaction.guild_id, gid):
                other = guild.get_role(gr["role_id"])
                if other and other.id != chosen_id and other.id in user_role_ids:
                    roles_to_remove.add(other)

        # 3. Nur Rollen anfassen, die Nexus auch verwalten darf — sonst scheitert der ganze Batch
        skipped = [r for r in roles_to_add | roles_to_remove if not can_manage_role(guild, r)]
        if skipped:
            log.warning(
                "[RR] Dropdown auf %s: %d Rolle(n) nicht verwaltbar: %s",
                guild.id, len(skipped), [r.id for r in skipped],
            )
            roles_to_add -= set(skipped)
            roles_to_remove -= set(skipped)

        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Nexus Dropdown Multi-Select")
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="Nexus Dropdown Multi-Select")
        except discord.HTTPException as e:
            log.warning("[RR] Dropdown-Rollenänderung auf %s fehlgeschlagen: %s", interaction.guild_id, e)
            await interaction.followup.send(
                lang.get("rr_no_permission", "❌ I don't have permission to manage roles."), ephemeral=True
            )
            return

        if skipped:
            await interaction.followup.send(
                lang.get(
                    "rr_roles_partially_updated",
                    "⚠️ Your roles were updated, but I couldn't change: {roles}. Please let a server admin know.",
                ).format(roles=", ".join(r.mention for r in skipped)),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            lang.get("rr_roles_updated", "{nexus_checkmark} Your roles have been updated!").format(
                nexus_checkmark=NexusEmojis.CHECKMARK
            ),
            ephemeral=True,
        )

async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))