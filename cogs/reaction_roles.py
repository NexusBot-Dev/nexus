import asyncio
import logging
import re
import discord
from discord import app_commands
from discord.ext import commands
from database import db_settings, db_reaction_roles
from systems.premium import is_premium
from cogs.utils import get_lang, module_required
from config import NEXUS_COLOR, NEXUS_FOOTER
from emojis import NexusEmojis

log = logging.getLogger(__name__)

# ─── Helper zum Parsen von Emojis ─────────────────────────────────────────────

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

# ─── Setup Flow Views ────────────────────────────────────────────────────────

class ReactionRoleCreateModal(discord.ui.Modal):
    def __init__(self, bot, channel: discord.TextChannel, lang: dict, color: int, premium: bool):
        super().__init__(title=lang.get("rr_modal_create_title", "Create Reaction Role"))
        self.bot = bot
        self.channel = channel
        self.lang = lang
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
        msg = await self.channel.send(embed=embed)

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

        await interaction.response.send_message(
            embed=setup_embed,
            view=ReactionRoleSetupView(self.bot, interaction.user.id, self.channel, msg, self.lang),
            ephemeral=True,
        )

class ReactionRoleSetupView(discord.ui.View):
    def __init__(self, bot, owner_id: int, channel: discord.TextChannel, message: discord.Message, lang: dict):
        super().__init__(timeout=300)
        self.bot      = bot
        self.owner_id = owner_id
        self.channel  = channel
        self.message  = message
        self.lang     = lang
        self.add_role_btn.label = lang.get("rr_btn_add_role", "➕ Add Role")
        self.finish_btn.label   = lang.get("rr_btn_finish",   "Finish")
        self.finish_btn.emoji = discord.PartialEmoji.from_str(NexusEmojis.CHECKMARK)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                self.lang.get("no_permission", "You don't have permission to use this command."), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="➕ Add Role", style=discord.ButtonStyle.success)
    async def add_role_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_step_role_embed(self.lang),
            view=RoleSelectView(self.bot, self.owner_id, self.channel, self.message, self.lang),
        )

    @discord.ui.button(label="✅ Fertig", style=discord.ButtonStyle.secondary)
    async def finish_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        color = await db_settings.get_color(interaction.guild_id)
        embed = discord.Embed(
            title=self.lang.get("rr_done", "{nexus_checkmark} Reaction roles configured!").format(nexus_checkmark=NexusEmojis.CHECKMARK),
            description=self.lang.get("rr_done_description", "The message in {channel} is ready.").format(channel=self.channel.mention),
            color=color,
        )
        await interaction.response.edit_message(embed=embed, view=None)

class RoleSelectView(discord.ui.View):
    def __init__(self, bot, owner_id: int, channel: discord.TextChannel, message: discord.Message, lang: dict):
        super().__init__(timeout=300)
        self.bot      = bot
        self.owner_id = owner_id
        self.channel  = channel
        self.message  = message
        self.lang     = lang
        self.select_role.placeholder = self.lang.get("rr_select_role_placeholder", "🎭 Select a role...")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                self.lang.get("no_permission", "You don't have permission to use this command."), ephemeral=True
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="🎭 Select a role...",
    )
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]

        if (
            interaction.user.id != interaction.guild.owner_id
            and role >= interaction.user.top_role
        ):
            await interaction.response.send_message(
                self.lang.get(
                    "rr_role_hierarchy_user",
                    "⚠️ **Error:** This role is ranked equal to or higher than your own highest role. "
                    "You cannot use it for reaction roles. Please choose a different role.",
                ),
                ephemeral=True
            )
            return

        if role.managed:
            await interaction.response.send_message(
                self.lang.get(
                    "rr_role_managed",
                    "❌ **Error:** This role is managed by an integration (e.g., another bot or Server Booster). "
                    "This role cannot be used for reaction roles. Please choose a different role.",
            ),
                ephemeral=True
            )
            return

        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                self.lang.get(
                    "rr_role_hierarchy",
                    "⚠️ **Error:** This role is **above** me in the server hierarchy. "
                    "I do not have permission to assign this role to users. Please move my role higher up in your server settings."
                ),
                ephemeral=True
            )
            return

        await interaction.response.edit_message(
            embed=_step_emoji_embed(role, self.lang),
            view=EmojiInputView(self.bot, self.owner_id, self.channel, self.message, role, self.lang),
        )

class EmojiInputView(discord.ui.View):
    def __init__(self, bot, owner_id: int, channel: discord.TextChannel, message: discord.Message, role: discord.Role, lang: dict):
        super().__init__(timeout=300)
        self.bot      = bot
        self.owner_id = owner_id
        self.channel  = channel
        self.message  = message
        self.role     = role
        self.lang     = lang
        self.emoji_btn.label = lang.get("rr_btn_emoji", "😀 Select Emoji")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                self.lang.get("no_permission", "You don't have permission to use this command."), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="😀 Select Emoji", style=discord.ButtonStyle.primary)
    async def emoji_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
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
            return user.id == self.owner_id and reaction.message.id == self.message.id

        while True:
            try:
                reaction, user = await self.bot.wait_for("reaction_add", timeout=60.0, check=check)
                emoji_str = str(reaction.emoji)

                try:
                    await self.message.remove_reaction(reaction.emoji, user)
                except:
                    pass

                # --- FEHLER-CHECK 1: Wird DIESES EMOJI schon auf der Nachricht genutzt? ---
                existing_rr = await db_reaction_roles.get_reaction_role(
                    interaction.guild_id, self.message.id, emoji_str
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
                        view=RoleSelectView(self.bot, self.owner_id, self.channel, self.message, self.lang),
                    )
                    break # Loop beenden, da View gewechselt wurde

                # --- FEHLER-CHECK 2: Wird DIESE ROLLE schon auf dieser Nachricht verwendet? ---
                all_current_roles = await db_reaction_roles.get_reaction_roles(
                    interaction.guild_id, self.message.id
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
                        view=RoleSelectView(self.bot, self.owner_id, self.channel, self.message, self.lang),
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
                            view=RoleSelectView(self.bot, self.owner_id, self.channel, self.message, self.lang),
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
                    view=ModeSelectView(self.bot, self.owner_id, self.channel, self.message, self.role, emoji_str, self.lang, has_premium=premium_status),
                )
                break

            except asyncio.TimeoutError:
                await interaction.edit_original_response(
                    embed=_step_emoji_embed(self.role, self.lang),
                    view=EmojiInputView(self.bot, self.owner_id, self.channel, self.message, self.role, self.lang),
                )
                await interaction.followup.send(
                    self.lang.get("rr_timeout", "❌ Time expired. Click the button again.",),
                    ephemeral=True,
                )
                break

class ModeSelectView(discord.ui.View):
    def __init__(self, bot, owner_id: int, channel: discord.TextChannel, message: discord.Message, role: discord.Role, emoji: str, lang: dict, has_premium: bool):
        super().__init__(timeout=300)
        self.bot      = bot
        self.owner_id = owner_id
        self.channel  = channel
        self.message  = message
        self.role     = role
        self.emoji    = emoji
        self.lang     = lang
        self.toggle_btn.label = lang.get("rr_btn_toggle", "Toggle")
        self.toggle_btn.emoji = discord.PartialEmoji.from_str(NexusEmojis.CHECKMARK)

        if has_premium:
            self.unique_btn.label = lang.get("rr_btn_unique_premium", "Unique")
            self.unique_btn.emoji = discord.PartialEmoji.from_str("🎯")
            self.unique_btn.style = discord.ButtonStyle.primary
        else:
            self.unique_btn.label = lang.get("rr_btn_unique_free", "🔒 Unique (Premium)")
            self.unique_btn.style = discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                self.lang.get("no_permission", "You don't have permission to use this command."), ephemeral=True
            )
            return False
        return True

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
            await interaction.response.edit_message(
                embed=_step_group_embed(self.role, self.emoji, self.lang),
                view=GroupInputView(self.bot, self.owner_id, self.channel, self.message, self.role, self.emoji, self.lang),
            )
        else:
            await self._save(interaction, mode=mode, group_id=None)

    async def _save(self, interaction: discord.Interaction, mode: str, group_id: int | None):
        msg_ref = await db_reaction_roles.get_message_ref(interaction.guild_id, self.message.id)
        display_mode = msg_ref["display_mode"] if msg_ref else "reaction"

        if display_mode == "reaction":
            try:
                await self.message.add_reaction(self.emoji)
            except discord.HTTPException:
                await interaction.response.send_message(
                    self.lang.get("rr_emoji_failed", "❌ Emoji could not be added to the message."), ephemeral=True
                )
                return

        await db_reaction_roles.add_reaction_role(
            guild_id=interaction.guild_id,
            channel_id=self.channel.id,
            message_id=self.message.id,
            role_id=self.role.id,
            emoji=self.emoji,
            mode=mode,
            group_id=group_id,
        )

        if display_mode == "dropdown":
            await _update_dropdown_message(self.message, interaction.guild_id, self.message.id, self.lang)

        color = await db_settings.get_color(interaction.guild_id)
        embed = discord.Embed(
            title=self.lang.get("rr_role_added_title", "{nexus_checkmark} Role added!").format(nexus_checkmark=NexusEmojis.CHECKMARK),
            description=self.lang.get("rr_added").format(emoji=self.emoji, role=self.role.mention, mode=mode),
            color=color,
        )
        embed.set_footer(text=self.lang.get("rr_add_another_hint", "Would you like to add more roles?"))
        await interaction.response.edit_message(
            embed=embed,
            view=ReactionRoleSetupView(self.bot, self.owner_id, self.channel, self.message, self.lang),
        )

class GroupInputView(discord.ui.View):
    def __init__(self, bot, owner_id: int, channel: discord.TextChannel, message: discord.Message, role: discord.Role, emoji: str, lang: dict):
        super().__init__(timeout=300)
        self.bot      = bot
        self.owner_id = owner_id
        self.channel  = channel
        self.message  = message
        self.role     = role
        self.emoji    = emoji
        self.lang     = lang
        self.new_group_btn.label      = lang.get("rr_btn_new_group",      "➕ Create New Group")
        self.existing_group_btn.label = lang.get("rr_btn_existing_group", "📋 Existing Group")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                self.lang.get("no_permission", "You don't have permission to use this command."), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="➕ Neue Gruppe erstellen", style=discord.ButtonStyle.primary)
    async def new_group_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            GroupModal(self.bot, self.owner_id, self.channel, self.message, self.role, self.emoji, self.lang)
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
        await interaction.response.edit_message(
            embed=_step_group_select_embed(self.lang),
            view=GroupSelectView(self.bot, self.owner_id, self.channel, self.message, self.role, self.emoji, groups, self.lang),
        )

class GroupModal(discord.ui.Modal):
    group_name = discord.ui.TextInput(
        label="Group Name",
        placeholder="e.g., Colors, Teams, Regions...",
        min_length=1,
        max_length=100,
    )

    def __init__(self, bot, owner_id: int, channel, message, role, emoji, lang: dict):
        super().__init__(title=lang.get("rr_modal_group_title", "Create Group"))
        self.bot      = bot
        self.owner_id = owner_id
        self.channel  = channel
        self.message  = message
        self.role     = role
        self.emoji    = emoji
        self.lang     = lang

        self.group_name.label = self.lang.get("rr_modal_group_label", "Group Name")
        self.group_name.placeholder = self.lang.get("rr_modal_group_placeholder", "e.g., Colors, Teams, Regions...")

    async def on_submit(self, interaction: discord.Interaction):
        group_id  = await db_reaction_roles.create_group(
            interaction.guild_id, self.group_name.value, "unique"
        )
        premium_status = await is_premium(interaction.guild_id)
        mode_view = ModeSelectView(
            self.bot, self.owner_id, self.channel, self.message, self.role, self.emoji, self.lang, has_premium=premium_status
        )
        await mode_view._save(interaction, mode="unique", group_id=group_id)

class GroupSelectView(discord.ui.View):
    def __init__(self, bot, owner_id: int, channel, message, role, emoji, groups: list[dict], lang: dict):
        super().__init__(timeout=300)
        self.bot      = bot
        self.owner_id = owner_id
        self.channel  = channel
        self.message  = message
        self.role     = role
        self.emoji    = emoji
        self.lang     = lang
        
        options = [
            discord.SelectOption(label=g["name"], value=str(g["id"]))
            for g in groups
        ][:25]
        select = discord.ui.Select(placeholder=self.lang.get("rr_select_group", "Select a group..."), options=options)
        select.callback = self._select_callback
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                self.lang.get("no_permission", "You don't have permission to use this command."), ephemeral=True
            )
            return False
        return True

    async def _select_callback(self, interaction: discord.Interaction):
        group_id  = int(interaction.data["values"][0])
        premium_status = await is_premium(interaction.guild_id)
        mode_view = ModeSelectView(
            self.bot, self.owner_id, self.channel, self.message, self.role, self.emoji, self.lang, has_premium=premium_status
        )
        await mode_view._save(interaction, mode="unique", group_id=group_id)

# ─── Premium Dropdown ────────────────────────────────────────────────────────

async def _update_dropdown_message(msg: discord.Message, guild_id: int, message_id: int, lang: dict):
    """Baut/aktualisiert den 'Select your roles'-Button unter einer Dropdown-Modus-Nachricht."""
    roles = await db_reaction_roles.get_reaction_roles(guild_id, message_id)
    if not roles:
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
                for gr in group_roles:
                    if gr["role_id"] != rr["role_id"]:
                        other_role = guild.get_role(gr["role_id"])
                        if other_role and other_role in member.roles:
                            await member.remove_roles(other_role)
                            channel = guild.get_channel(payload.channel_id)
                            if isinstance(channel, discord.TextChannel):
                                try:
                                    msg = await channel.fetch_message(payload.message_id)
                                    await msg.remove_reaction(gr["emoji"], member)
                                except (discord.NotFound, discord.Forbidden):
                                    pass
            await member.add_roles(role)
        else:
            await member.remove_roles(role)

    @app_commands.command(name="reactionrole_create", description=app_commands.locale_str("cmd_rr_create_desc"))
    @app_commands.default_permissions(manage_roles=True)
    @module_required("reaction_roles")
    async def reactionrole_create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        lang  = await get_lang(interaction.guild_id)
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
        messages = await db_reaction_roles.get_all_reaction_role_messages(interaction.guild_id)

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

    @reactionrole_delete.autocomplete("message_id")
    async def _rr_message_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        lang = await get_lang(interaction.guild_id)
        messages = await db_reaction_roles.get_all_reaction_role_messages(interaction.guild_id)

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
                    label=f"{label_prefix} {role_obj.name}",
                    value=str(rr["role_id"]),
                    emoji=partial_emoji,
                    description=desc_template.format(role_name=role_obj.name),
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
        roles_to_add = set()
        roles_to_remove = set()
        processed_unique_groups = set()

        for rr in roles:
            role_id = rr["role_id"]
            role_obj = interaction.guild.get_role(role_id)
            if not role_obj:
                continue

            if role_id in selected_role_ids:
                if role_id not in user_role_ids:
                    if rr["mode"] == "unique" and rr["group_id"]:
                        group_id = rr["group_id"]
                        if group_id not in processed_unique_groups:
                            processed_unique_groups.add(group_id)
                            group_roles = await db_reaction_roles.get_group_roles(interaction.guild_id, group_id)
                            for gr in group_roles:
                                if gr["role_id"] != role_id:
                                    other_role = interaction.guild.get_role(gr["role_id"])
                                    if other_role and (other_role in member.roles or other_role in roles_to_add):
                                        roles_to_remove.add(other_role)
                                        roles_to_add.discard(other_role)
                    roles_to_add.add(role_obj)
            else:
                if role_id in user_role_ids:
                    roles_to_remove.add(role_obj)

        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Nexus Dropdown Multi-Select")
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="Nexus Dropdown Multi-Select")
        except discord.Forbidden:
            await interaction.followup.send(
                lang.get("rr_no_permission", "❌ I don't have permission to manage roles."), ephemeral=True
            )
            return

        await interaction.followup.send(
            lang.get("rr_roles_updated", "{nexus_checkmark} Your roles have been updated!").format(
                nexus_checkmark=NexusEmojis.CHECKMARK
            ),
            ephemeral=True,
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        if isinstance(error, app_commands.CheckFailure) and not isinstance(error, app_commands.MissingPermissions):
            return
        lang = await get_lang(interaction.guild_id)
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                lang.get("no_permission", "No permission."), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                lang.get("error_occurred", "An error occurred: {error}").format(error=error),
                ephemeral=True,
            )

async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))