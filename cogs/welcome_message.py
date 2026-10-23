import logging
import discord
from discord import app_commands
from discord.ext import commands
from database import db_settings, db_welcome, db_limits
from systems.premium import is_premium
from cogs.utils import get_lang, module_required
from emojis import NexusEmojis
from systems.ui import NexusView, NexusModal

log = logging.getLogger(__name__)

# ─── Modal: Add & Edit teilen sich dieselbe Klasse ────────────────────────

class WelcomeMessageModal(NexusModal):
    def __init__(self, lang: dict, guild_id: int, message_id: int | None = None, current_text: str = ""):
        title = lang.get("welcome_msg_modal_edit_title", "Edit Message") if message_id else lang.get("welcome_msg_modal_title", "Create welcome message")
        super().__init__(lang, title=title)
        self.guild_id = guild_id
        self.message_id = message_id

        self.message_input = discord.ui.TextInput(
            label=lang.get("welcome_msg_modal_label", "Message"),
            style=discord.TextStyle.paragraph,
            placeholder=lang.get(
                "welcome_msg_modal_placeholder",
                "e.g. Welcome {user} to {server}! You are member #{membercount}.",
            ),
            default=current_text,
            max_length=1000,
            required=True,
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.message_id is None:
            await db_welcome.add_welcome_message(self.guild_id, self.message_input.value)
            text_key = "welcome_msg_added"
        else:
            await db_welcome.update_welcome_message(self.message_id, self.guild_id, self.message_input.value)
            text_key = "welcome_msg_updated"

        await interaction.response.send_message(
            self.lang.get(text_key, "{nexus_checkmark} Message saved.").format(nexus_checkmark=NexusEmojis.CHECKMARK),
            ephemeral=True,
        )

# ─── Auswahl-Views für Edit/Delete bei mehreren Nachrichten ───────────────

class MessageSelectView(NexusView):
    """Zwischenschritt bei mehreren Nachrichten — User wählt, welche gemeint ist."""
    def __init__(self, messages: list[dict], lang: dict, mode: str, guild_id: int, owner_id: int):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=60)
        self.messages = messages
        self.mode = mode  # "edit" oder "delete"
        self.guild_id = guild_id
        self.select_msg.placeholder = lang.get("welcome_msg_select_placeholder", "Select a message...")
        self.select_msg.options = [
            discord.SelectOption(label=f"#{m['id']} — {m['message'][:80]}", value=str(m["id"]))
            for m in messages
        ][:25]

    @discord.ui.select(placeholder="Select a message...")
    async def select_msg(self, interaction: discord.Interaction, select: discord.ui.Select):
        if self.owner_id is None:
            return
        chosen_id = int(select.values[0])
        chosen = next(m for m in self.messages if m["id"] == chosen_id)

        if self.mode == "edit":
            await interaction.response.send_modal(
                WelcomeMessageModal(self.lang, self.guild_id, message_id=chosen_id, current_text=chosen["message"])
            )
        else:  # delete
            await self.show(
                interaction,
                DeleteConfirmView(chosen_id, self.guild_id, self.lang, self.owner_id),
                content=self.lang.get("welcome_msg_delete_confirm", "⚠️ Are you sure you want to delete message #{id}?").format(id=chosen_id),
            )

class DeleteConfirmView(NexusView):
    def __init__(self, message_id: int, guild_id: int, lang: dict, owner_id: int):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=60)
        self.message_id = message_id
        self.guild_id = guild_id
        self.confirm_btn.label = lang.get("welcome_msg_delete_yes", "Yes, delete")
        self.cancel_btn.label = lang.get("welcome_msg_delete_no", "Cancel")

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_welcome.delete_welcome_message(self.message_id, self.guild_id)
        await interaction.response.edit_message(
            content=self.lang.get("welcome_msg_deleted", "{nexus_checkmark} Message #{id} deleted.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK, id=self.message_id
            ),
            view=None,
        )

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=self.lang.get("welcome_msg_delete_cancelled", "Canceled."), view=None
        )

# ─── Haupt-Panel ────────────────────────────────────────────────────────

class WelcomeManageView(NexusView):
    def __init__(self, owner_id: int, guild_id: int, lang: dict, messages: list[dict], limit: int):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=120)
        self.guild_id = guild_id
        self.messages = messages
        self.limit = limit
        self._update_buttons()

    def _update_buttons(self):
        self.add_btn.label = self.lang.get("welcome_msg_btn_add", "➕ Add")
        self.edit_btn.label = self.lang.get("welcome_msg_btn_edit", "✏️ Edit")
        self.delete_btn.label = self.lang.get("welcome_msg_btn_delete", "🗑️ Delete")

        self.add_btn.disabled = len(self.messages) >= self.limit
        self.edit_btn.disabled = len(self.messages) == 0
        self.delete_btn.disabled = len(self.messages) == 0

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WelcomeMessageModal(self.lang, self.guild_id))

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.owner_id is None:
            return
        if len(self.messages) == 1:
            m = self.messages[0]
            await interaction.response.send_modal(
                WelcomeMessageModal(self.lang, self.guild_id, message_id=m["id"], current_text=m["message"])
            )
        else:
            view = MessageSelectView(self.messages, self.lang, "edit", self.guild_id, self.owner_id)
            await interaction.response.send_message(
                self.lang.get("welcome_msg_pick_edit", "Which message would you like to edit?"),
                view=view,
                ephemeral=True,
            )
            view.bind(interaction)

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.owner_id is None:
            return
        if len(self.messages) == 1:
            m = self.messages[0]
            view = DeleteConfirmView(m["id"], self.guild_id, self.lang, self.owner_id)
            await interaction.response.send_message(
                self.lang.get("welcome_msg_delete_confirm", "⚠️ Are you sure you want to delete message #{id}?").format(id=m["id"]),
                view=view,
                ephemeral=True,
            )
            view.bind(interaction)
        else:
            view = MessageSelectView(self.messages, self.lang, "delete", self.guild_id, self.owner_id)
            await interaction.response.send_message(
                self.lang.get("welcome_msg_pick_delete", "Which message would you like to delete?"),
                view=view,
                ephemeral=True,
            )
            view.bind(interaction)

def _build_welcome_embed(messages: list[dict], lang: dict, color: int, limit: int) -> discord.Embed:
    embed = discord.Embed(title=lang.get("welcome_msg_panel_title", "👋 Welcome Messages"), color=color)
    if not messages:
        embed.description = lang.get("welcome_msg_panel_default_notice", "*No custom messages set — default message will be used.*")
    else:
        if len(messages) > limit:
            embed.description = lang.get(
                "msg_inactive_hint",
                "🔒 Messages beyond your limit of {limit} are kept but not used. "
                "They become active again with Premium or when you delete an active one.",
            ).format(limit=limit)
        for i, m in enumerate(messages):
            tag = f" {lang.get('msg_inactive_tag', '🔒 inactive')}" if i >= limit else ""
            embed.add_field(name=f"#{m['id']}{tag}", value=m["message"][:200], inline=False)
    embed.set_footer(text=lang.get(
        "welcome_msg_panel_placeholders",
        "Placeholders: {user} {username} {server} {membercount}",
    ))
    return embed

class WelcomeMessages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="welcome_message", description=app_commands.locale_str("cmd_welcome_message_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("welcome")
    async def welcome_message(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return
        lang = await get_lang(interaction.guild_id)
        color = await db_settings.get_color(interaction.guild_id)

        messages = await db_welcome.get_welcome_messages(interaction.guild_id)
        premium = await is_premium(interaction.guild_id)
        limit = await db_limits.get_limit(interaction.guild_id, "welcome_messages", premium)

        embed = _build_welcome_embed(messages, lang, color, limit)
        view = WelcomeManageView(interaction.user.id, interaction.guild_id, lang, messages, limit)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.bind(interaction)

async def setup(bot):
    await bot.add_cog(WelcomeMessages(bot))