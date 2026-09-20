import logging
import discord
from discord import app_commands
from discord.ext import commands
from database import db_settings
from config import NEXUS_COLOR, DASHBOARD_URL
from emojis import NexusEmojis
from cogs.utils import get_lang
from systems.hierarchy import check_bot_hierarchy, get_bot_position_info

log = logging.getLogger(__name__)

# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _apply_brand_role(interaction: discord.Interaction):
    """Erstellt oder weist die Nexus Brand Rolle zu."""
    existing_role = discord.utils.get(interaction.guild.roles, name="Nexus")
    try:
        bot_member = interaction.guild.get_member(interaction.client.user.id)
        if not bot_member:
            bot_member = await interaction.guild.fetch_member(interaction.client.user.id)
        if existing_role:
            if existing_role not in bot_member.roles:
                await bot_member.add_roles(existing_role)
        else:
            role = await interaction.guild.create_role(
                name="Nexus",
                color=discord.Color(NEXUS_COLOR),
                hoist=True,
                reason="Nexus Setup — set brand color",
            )
            await bot_member.add_roles(role)
            set_position = max(1, bot_member.top_role.position - 1)
            await role.edit(position=set_position)
    except (discord.Forbidden, discord.HTTPException) as e:
        log.info("Rollenfarbe konnte nicht verwaltet werden: %s", e)

def _dashboard_button(guild_id: int, lang: dict[str, str]) -> discord.ui.Button:
    return discord.ui.Button(
        label=lang.get("setup_dashboard_button", "🌐 Dashboard öffnen"),
        style=discord.ButtonStyle.link,
        url=f"{DASHBOARD_URL}/server/{guild_id}",
        row=4,
    )

def _get_missing_channel_permissions(channel, bot_member: discord.Member, for_logging: bool = False) -> list[str]:
    """Prüft, welche konkreten Berechtigungen dem Bot im Kanal fehlen."""
    if not hasattr(channel, "permissions_for"):
        return ["Kanal anzeigen (`View Channel`)"]

    perms = channel.permissions_for(bot_member)
    missing = []

    if not perms.view_channel:
        missing.append("Kanal anzeigen (`View Channel`)")
    if not perms.send_messages:
        missing.append("Nachrichten senden (`Send Messages`)")
    if not perms.embed_links:
        missing.append("Links einbetten (`Embed Links`)")
    if not perms.read_message_history:
        missing.append("Nachrichtenverlauf sehen (`Read Message History`)")
    if for_logging and not perms.attach_files:
        missing.append("Dateien anhängen (`Attach Files`)")

    return missing

async def _resolve_channel_and_check_perms(
    raw_channel,
    guild,
    bot_user_id: int,
    for_logging: bool = False
) -> tuple[discord.abc.GuildChannel | None, list[str]]:
    """
    Holt das Kanal-Objekt frisch von der API und ermittelt fehlende Rechte.
    Fängt 403 Forbidden (50001: Missing Access) bei privaten Kanälen sauber ab.
    """
    bot_member = guild.me or await guild.fetch_member(bot_user_id)
    
    try:
        channel = await guild.fetch_channel(raw_channel.id)
    except discord.Forbidden:
        # Bot hat keinen Zugriff auf privaten Kanal (Missing Access)
        return raw_channel, ["Kanal anzeigen (`View Channel`)"]
    except discord.HTTPException:
        return raw_channel, ["Kanal anzeigen (`View Channel`)"]

    missing = _get_missing_channel_permissions(channel, bot_member, for_logging=for_logging)
    return channel, missing

# ─── Embeds ──────────────────────────────────────────────────────────────────

async def _hierarchy_warning_embed(guild_id: int, bot_pos: int, total: int, lang: dict[str, str]) -> discord.Embed:
    setup_desc = lang.get(
        "setup_step_one_desc",
        "Nexus is currently in position **{bot_pos}** out of **{total}**.\n\n"
        "In order for me to function properly, my role must be placed "
        "above your members' roles.\n\n"
        "**This is easily done:**\n",
    ).format(bot_pos=bot_pos, total=total)
    
    embed = discord.Embed(
        title=lang.get("setup_step_one_title", "✨ Step 1: Get your Nexus ready"),
        description=(
            f"{setup_desc}"
            f"{NexusEmojis.Numbers.ONE} {lang.get('setup_explain_step_one', 'Go to **Server Settings** → **Roles**')}\n"
            f"{NexusEmojis.Numbers.TWO} {lang.get('setup_explain_step_two', 'Drag the **Nexus** role above the moderator and member roles')}\n"
            f"{NexusEmojis.Numbers.THREE} {lang.get('setup_explain_step_three', 'Admin and owner roles can safely stay above me')}\n"
            f"{NexusEmojis.Numbers.FOUR} {lang.get('setup_explain_step_four', 'Click on **Validate**, to proceed')}\n"
            f"*{lang.get('setup_skip_hint', 'Already know what you are doing? Hit **Skip** to proceed anyway.')}*"
        ),
        color=NEXUS_COLOR,
    )
    embed.set_footer(text=f"Nexus Setup • {lang.get('setup_step', 'Step')} 1/5")
    return embed

async def _log_channel_embed(guild_id: int, lang: dict[str, str]) -> discord.Embed:
    embed = discord.Embed(
        title=lang.get("setup_step_two_title", "📝 Step 2: Set up moderation logs"),
        description=lang.get(
            "setup_step_two_desc",
            "{nexus_checkmark} **Role check successful!**\n\n"
            "Where should all future mod actions be logged?\n\n"
            "*Use the dropdown menu to choose a channel, "
            "create one automatically, or skip this step.*",
        ).format(nexus_checkmark=NexusEmojis.CHECKMARK),
        color=NEXUS_COLOR,
    )
    embed.set_footer(text=f"Nexus Setup • {lang.get('setup_step', 'Step')} 2/5")
    return embed

async def _rules_channel_embed(guild_id: int, log_mention: str, lang: dict[str, str]) -> discord.Embed:
    embed = discord.Embed(
        title=lang.get("setup_step_three_title", "📜 Step 3: Set rules channel"),
        description=lang.get(
            "setup_step_three_desc",
            "{nexus_checkmark} **Log channel:** {log_mention}\n\n"
            "In which channel should the server rules be posted?",
        ).format(
            nexus_checkmark=NexusEmojis.CHECKMARK,
            log_mention=log_mention
        ),
        color=NEXUS_COLOR,
    )
    embed.set_footer(text=f"Nexus Setup • {lang.get('setup_step', 'Step')} 3/5")
    return embed

async def _welcome_channel_embed(guild_id: int, rules_mention: str, lang: dict[str, str]) -> discord.Embed:
    embed = discord.Embed(
        title=lang.get("setup_step_four_title", "👋 Step 4: Enable welcome messages"),
        description=lang.get(
            "setup_step_four_desc",
            "{nexus_checkmark} **Rules channel:** {rules_mention}\n\n"
            "Where should new members be welcomed?",
        ).format(
            nexus_checkmark=NexusEmojis.CHECKMARK,
            rules_mention=rules_mention
        ),
        color=NEXUS_COLOR,
    )
    embed.set_footer(text=f"Nexus Setup • {lang.get('setup_step', 'Step')} 4/5")
    return embed

async def _modules_embed(guild_id: int, lang: dict[str, str]) -> discord.Embed:
    embed = discord.Embed(
        title=lang.get("setup_step_five_title", "🚀 Step 5: Enable & start modules"),
        description=lang.get(
            "setup_step_five_desc",
            "**Almost there!**\n"
            "Which modules should be enabled for this server right away?\n\n"
            "*(You can change these modules at any time using `/module`)*"
        ),
        color=NEXUS_COLOR,
    )
    embed.set_footer(text=f"Nexus Setup • {lang.get('setup_step', 'Step')} 5/5")
    return embed

async def _channel_permission_warning_embed(
        channel,
        guild,
        bot_user_id: int,
        for_logging: bool,
        lang: dict[str, str]
    ) -> discord.Embed:
    bot_member = guild.me or await guild.fetch_member(bot_user_id)
    missing_perms = _get_missing_channel_permissions(channel, bot_member, for_logging)
    missing_list_str = "\n".join([f"• **{p}**" for p in missing_perms])

    is_private = True
    if hasattr(channel, "permissions_for"):
        is_private = not channel.permissions_for(guild.default_role).view_channel

    if is_private:
        scenario_note = "🔒 **Dieser Kanal ist privat.**"
    else:
        scenario_note = "✍️ **Dieser Kanal ist schreibgeschützt oder eingeschränkt.**"

    embed = discord.Embed(
        title=lang.get("setup_perm_warning_title", "⚠️ Berechtigungen erforderlich"),
        description=(
            f"Ich habe noch nicht alle nötigen Rechte in {channel.mention}.\n\n"
            f"{scenario_note}\n\n"
            f"**Fehlende Berechtigungen (bitte auf ✅ setzen):**\n"
            f"{missing_list_str}\n\n"
            "**So richtest du es ein:**\n"
            f"{NexusEmojis.Numbers.ONE} Rechtsklick auf {channel.mention} → **Kanal bearbeiten** → **Berechtigungen**\n"
            f"{NexusEmojis.Numbers.TWO} Rolle **Nexus** (oder Bot) hinzufügen bzw. auswählen\n"
            f"{NexusEmojis.Numbers.THREE} Die oben gelisteten Rechte auf **Erlaubt (✅)** setzen\n"
            f"{NexusEmojis.Numbers.FOUR} Unten auf **🔍 Validate** klicken"
        ),
        color=NEXUS_COLOR,
    )
    return embed

# ─── Permission Warning View ───────────────────────────────────────────────────

class ChannelPermissionWarningView(discord.ui.View):
    def __init__(self, owner_id: int, channel, step_type: str, lang: dict[str, str], setup_channel=None):
        super().__init__(timeout=300)
        self.owner_id      = owner_id
        self.channel       = channel
        self.step_type     = step_type
        self.lang          = lang
        self.setup_channel = setup_channel
        self.validate_btn.label = self.lang.get("setup_validate", "🔍 Validate")
        self.skip_btn.label     = self.lang.get("setup_skip", "⏭️ Skip")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                self.lang.get("setup_only_owner", "You need the 'Manage Server' permission to run the setup."),
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🔍 Validate", style=discord.ButtonStyle.primary)
    async def validate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_logging = (self.step_type == "log")
        channel, missing = await _resolve_channel_and_check_perms(self.channel, interaction.guild, interaction.client.user.id, for_logging=is_logging)

        if missing:
            embed = await _channel_permission_warning_embed(channel, interaction.guild, interaction.client.user.id, is_logging, self.lang)
            await interaction.response.edit_message(embed=embed, view=self)
            return

        self.channel = channel
        await self._proceed_next_step(interaction)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._proceed_next_step(interaction)

    async def _proceed_next_step(self, interaction: discord.Interaction):
        if self.step_type == "log":
            await interaction.response.edit_message(
                embed=await _rules_channel_embed(interaction.guild_id, self.channel.mention, self.lang),
                view=RulesChannelView(self.owner_id, self.lang, self.setup_channel),
            )
        elif self.step_type == "rules":
            await interaction.response.edit_message(
                embed=await _welcome_channel_embed(interaction.guild_id, self.channel.mention, self.lang),
                view=WelcomeChannelView(self.owner_id, self.lang, self.setup_channel),
            )
        elif self.step_type == "welcome":
            await interaction.response.edit_message(
                embed=await _modules_embed(interaction.guild_id, self.lang),
                view=ModulesView(self.owner_id, self.lang, self.setup_channel),
            )

# ─── Schritt 0: Rollenprüfung ─────────────────────────────────────────────────

class HierarchyView(discord.ui.View):
    def __init__(self, owner_id: int, lang: dict[str, str], setup_channel=None, is_restore: bool = False):
        super().__init__(timeout=300)
        self.owner_id      = owner_id
        self.setup_channel = setup_channel
        self.lang          = lang
        self.is_restore    = is_restore
        self.check_hierarchy.label = self.lang.get("setup_validate", "🔍 Validate")
        self.skip_hierarchy.label  = self.lang.get("setup_skip", "⏭️ Skip")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                self.lang.get("setup_only_owner", "You need the 'Manage Server' permission to run the setup."),
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🔍 Validate", style=discord.ButtonStyle.primary)
    async def check_hierarchy(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not check_bot_hierarchy(interaction.guild):
            bot_pos, total = get_bot_position_info(interaction.guild)
            await interaction.response.edit_message(
                embed=await _hierarchy_warning_embed(interaction.guild_id, bot_pos, total, self.lang),
                view=self,
            )
            return
        await self._proceed(interaction)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary)
    async def skip_hierarchy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._proceed(interaction)

    async def _proceed(self, interaction: discord.Interaction):
        await _apply_brand_role(interaction)
        if self.is_restore:
            embed = discord.Embed(
                title=self.lang.get("setup_restored_title", "{nexus_checkmark} Settings restored!").format(
                    nexus_checkmark=NexusEmojis.CHECKMARK
                ),
                description=self.lang.get("setup_restored_desc", "Your old settings have been successfully restored."),
                color=NEXUS_COLOR,
            )
            settings = await db_settings.get_guild(interaction.guild_id)
            if settings:
                log_ch     = f"<#{settings['log_channel_id']}>"     if settings.get("log_channel_id")     else "—"
                rules_ch   = f"<#{settings['rules_channel_id']}>"   if settings.get("rules_channel_id")   else "—"
                welcome_ch = f"<#{settings['welcome_channel_id']}>" if settings.get("welcome_channel_id") else "—"
                embed.add_field(name=self.lang.get("settings_log",     "📋 Log-Channel"),     value=log_ch,     inline=True)
                embed.add_field(name=self.lang.get("settings_rules",   "📜 Rules-Channel"),   value=rules_ch,   inline=True)
                embed.add_field(name=self.lang.get("settings_welcome", "👋 Welcome-Channel"), value=welcome_ch, inline=True)

            try:
                if self.setup_channel:
                    async for msg in self.setup_channel.history(limit=10):
                        if msg.author == interaction.guild.me and msg.components:
                            await msg.delete(delay=1)
                            break
            except (discord.NotFound, discord.Forbidden):
                pass

            await interaction.response.edit_message(embed=embed, view=None)
            log.info("Guild %s hat Restore abgeschlossen.", interaction.guild.name)
        else:
            await interaction.response.edit_message(
                embed=await _log_channel_embed(interaction.guild_id, self.lang),
                view=LogChannelView(self.owner_id, self.lang, self.setup_channel),
            )

# ─── Setup Start ─────────────────────────────────────────────────────────────

class SetupView(discord.ui.View):
    def __init__(self, owner_id: int, lang: dict[str, str], guild_id: int, setup_channel=None):
        super().__init__(timeout=300)
        self.owner_id      = owner_id
        self.lang          = lang
        self.setup_channel = setup_channel
        self.start_setup.label = self.lang.get("setup_start_title", "Start")
        self.add_item(_dashboard_button(guild_id, lang)) 

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                self.lang.get("setup_only_owner", "You need the 'Manage Server' permission to run the setup."),
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Setup starten", style=discord.ButtonStyle.success)
    async def start_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not check_bot_hierarchy(interaction.guild):
            bot_pos, total = get_bot_position_info(interaction.guild)
            await interaction.response.edit_message(
                embed=await _hierarchy_warning_embed(interaction.guild_id, bot_pos, total, self.lang),
                view=HierarchyView(self.owner_id, self.lang, self.setup_channel),
            )
            return

        await _apply_brand_role(interaction)

        await interaction.response.edit_message(
            embed=await _log_channel_embed(interaction.guild_id, self.lang),
            view=LogChannelView(self.owner_id, self.lang, self.setup_channel),
        )

# ─── Schritt 1: Log-Channel ───────────────────────────────────────────────────

class LogChannelView(discord.ui.View):
    def __init__(self, owner_id: int, lang: dict[str, str], setup_channel=None):
        super().__init__(timeout=300)
        self.owner_id      = owner_id
        self.lang          = lang
        self.setup_channel = setup_channel
        self.select_log_channel.placeholder = self.lang.get("setup_placeholder", "📋 Choose an existing Channel...")
        self.create_log_channel.label       = self.lang.get("setup_create", "✨ Create one for me")
        self.skip_step.label                = self.lang.get("setup_skip", "⏭️ Skip")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                self.lang.get("setup_only_owner", "You need the 'Manage Server' permission to run the setup."),
                ephemeral=True
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        row=0
    )
    async def select_log_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        raw_channel = select.values[0]
        await db_settings.update_guild(interaction.guild_id, log_channel_id=raw_channel.id)

        channel, missing = await _resolve_channel_and_check_perms(raw_channel, interaction.guild, interaction.client.user.id, for_logging=True)

        if missing:
            embed = await _channel_permission_warning_embed(channel, interaction.guild, interaction.client.user.id, True, self.lang)
            view = ChannelPermissionWarningView(self.owner_id, channel, "log", self.lang, self.setup_channel)
            await interaction.response.edit_message(embed=embed, view=view)
            return

        await interaction.response.edit_message(
            embed=await _rules_channel_embed(interaction.guild_id, channel.mention, self.lang),
            view=RulesChannelView(self.owner_id, self.lang, setup_channel=self.setup_channel),
        )

    @discord.ui.button(label="✨ Create one for me", style=discord.ButtonStyle.secondary, row=1)
    async def create_log_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_role = interaction.guild.me.top_role
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_role:                       discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, attach_files=True),
        }
        for role in interaction.guild.roles:
            if role.permissions.manage_guild:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True)

        try:
            channel = await interaction.guild.create_text_channel(
                name="📝┃nexus-logs",
                overwrites=overwrites,
                reason="Nexus Setup — Log-Channel",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ {self.lang.get('setup_right_error', 'I am missing the \'Manage Channels\' permission.')}", ephemeral=True
            )
            return

        await db_settings.update_guild(interaction.guild_id, log_channel_id=channel.id)
        await interaction.response.edit_message(
            embed=await _rules_channel_embed(interaction.guild_id, channel.mention, self.lang),
            view=RulesChannelView(self.owner_id, self.lang, self.setup_channel),
        )

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, row=1)
    async def skip_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_settings.update_guild(interaction.guild_id, log_channel_id=None)
        await interaction.response.edit_message(
            embed=await _rules_channel_embed(interaction.guild_id, "—", self.lang),
            view=RulesChannelView(self.owner_id, self.lang, self.setup_channel),
        )

# ─── Schritt 2: Regel-Channel ─────────────────────────────────────────────────

class RulesChannelView(discord.ui.View):
    def __init__(self, owner_id: int, lang: dict[str, str], setup_channel=None):
        super().__init__(timeout=300)
        self.owner_id      = owner_id
        self.setup_channel = setup_channel
        self.lang          = lang
        self.select_rules_channel.placeholder = self.lang.get("setup_placeholder", "📋 Choose an existing Channel...")
        self.create_rules_channel.label       = self.lang.get("setup_create", "✨ Create one for me")
        self.skip_step.label                  = self.lang.get("setup_skip", "⏭️ Skip")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                self.lang.get("setup_only_owner", "You need the 'Manage Server' permission to run the setup."),
                ephemeral=True
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        row=0
    )
    async def select_rules_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        raw_channel = select.values[0]
        await db_settings.update_guild(interaction.guild_id, rules_channel_id=raw_channel.id)

        channel, missing = await _resolve_channel_and_check_perms(raw_channel, interaction.guild, interaction.client.user.id, for_logging=False)

        if missing:
            embed = await _channel_permission_warning_embed(channel, interaction.guild, interaction.client.user.id, False, self.lang)
            view = ChannelPermissionWarningView(self.owner_id, channel, "rules", self.lang, self.setup_channel)
            await interaction.response.edit_message(embed=embed, view=view)
            return

        await interaction.response.edit_message(
            embed=await _welcome_channel_embed(interaction.guild_id, channel.mention, self.lang),
            view=WelcomeChannelView(self.owner_id, self.lang, self.setup_channel),
        )

    @discord.ui.button(label="✨ Create one for me", style=discord.ButtonStyle.secondary, row=1)
    async def create_rules_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_role = interaction.guild.me.top_role
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=False, add_reactions=False
            ),
            bot_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True, add_reactions=True
            ),
        }
        try:
            channel = await interaction.guild.create_text_channel(
                name="📜┃regeln",
                overwrites=overwrites,
                reason="Nexus Setup — Regel-Channel",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ {self.lang.get('setup_right_error', 'I am missing the \'Manage Channels\' permission.')}", ephemeral=True
            )
            return

        await db_settings.update_guild(interaction.guild_id, rules_channel_id=channel.id)
        await interaction.response.edit_message(
            embed=await _welcome_channel_embed(interaction.guild_id, channel.mention, self.lang),
            view=WelcomeChannelView(self.owner_id, self.lang, self.setup_channel),
        )

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, row=1)
    async def skip_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_settings.update_guild(interaction.guild_id, rules_channel_id=None)
        await interaction.response.edit_message(
            embed=await _welcome_channel_embed(interaction.guild_id, "—", self.lang),
            view=WelcomeChannelView(self.owner_id, self.lang, self.setup_channel),
        )

# ─── Schritt 3: Willkommens-Channel ──────────────────────────────────────────

class WelcomeChannelView(discord.ui.View):
    def __init__(self, owner_id: int, lang: dict[str, str], setup_channel=None):
        super().__init__(timeout=300)
        self.owner_id      = owner_id
        self.setup_channel = setup_channel
        self.lang          = lang
        self.select_welcome_channel.placeholder = self.lang.get("setup_placeholder", "📋 Choose an existing Channel...")
        self.create_welcome_channel.label       = self.lang.get("setup_create", "✨ Create one for me")
        self.skip_step.label                    = self.lang.get("setup_skip", "⏭️ Skip")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                self.lang.get("setup_only_owner", "You need the 'Manage Server' permission to run the setup."),
                ephemeral=True
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        row=0
    )
    async def select_welcome_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        raw_channel = select.values[0]
        await db_settings.update_guild(interaction.guild_id, welcome_channel_id=raw_channel.id)

        channel, missing = await _resolve_channel_and_check_perms(raw_channel, interaction.guild, interaction.client.user.id, for_logging=False)

        if missing:
            embed = await _channel_permission_warning_embed(channel, interaction.guild, interaction.client.user.id, False, self.lang)
            view = ChannelPermissionWarningView(self.owner_id, channel, "welcome", self.lang, self.setup_channel)
            await interaction.response.edit_message(embed=embed, view=view)
            return

        await interaction.response.edit_message(
            embed=await _modules_embed(interaction.guild_id, self.lang),
            view=ModulesView(self.owner_id, self.lang, self.setup_channel),
        )

    @discord.ui.button(label="✨ Create one for me", style=discord.ButtonStyle.secondary, row=1)
    async def create_welcome_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_role = interaction.guild.me.top_role
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=False
            ),
            bot_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True, attach_files=True
            ),
        }
        try:
            channel = await interaction.guild.create_text_channel(
                name="👋┃willkommen",
                overwrites=overwrites,
                reason="Nexus Setup — Willkommens-Channel",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ {self.lang.get('setup_right_error', 'I am missing the \'Manage Channels\' permission.')}", ephemeral=True
            )
            return

        await db_settings.update_guild(interaction.guild_id, welcome_channel_id=channel.id)
        await interaction.response.edit_message(
            embed=await _modules_embed(interaction.guild_id, self.lang),
            view=ModulesView(self.owner_id, self.lang, self.setup_channel),
        )

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, row=1)
    async def skip_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_settings.update_guild(interaction.guild_id, welcome_channel_id=None)
        await interaction.response.edit_message(
            embed=await _modules_embed(interaction.guild_id, self.lang),
            view=ModulesView(self.owner_id, self.lang, self.setup_channel),
        )

# ─── Schritt 4: Module ────────────────────────────────────────────────────────

class ModulesView(discord.ui.View):
    def __init__(self, owner_id: int, lang: dict[str, str], setup_channel=None):
        super().__init__(timeout=300)
        self.owner_id      = owner_id
        self.setup_channel = setup_channel
        self.levels        = True
        self.welcome       = True
        self.lang          = lang
        self._update_buttons()
        self.finish_setup.label = f"{self.lang.get('setup_finish', 'Finish Setup')}"
        self.finish_setup.emoji = discord.PartialEmoji.from_str(NexusEmojis.CHECKMARK)

    def _update_buttons(self):
        self.toggle_levels.style  = discord.ButtonStyle.success if self.levels else discord.ButtonStyle.secondary
        self.toggle_levels.label  = f"{self.lang.get('settings_level', '📈 Level-System')}: {self.lang.get('setup_on', 'ON') if self.levels else self.lang.get('setup_off', 'OFF')}"
        self.toggle_welcome.style = discord.ButtonStyle.success if self.welcome else discord.ButtonStyle.secondary
        self.toggle_welcome.label = f"{self.lang.get('welcome_title', '👋 Welcome!')}: {self.lang.get('setup_on', 'ON') if self.welcome else self.lang.get('setup_off', 'OFF')}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                self.lang.get("setup_only_owner", "You need the 'Manage Server' permission to run the setup."),
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="📈 Level-System: ON", style=discord.ButtonStyle.success)
    async def toggle_levels(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.levels = not self.levels
        self._update_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="👋 Welcome: ON", style=discord.ButtonStyle.success)
    async def toggle_welcome(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.welcome = not self.welcome
        self._update_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Finish Setup", style=discord.ButtonStyle.primary, row=1)
    async def finish_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        from database.db_settings import DEFAULT_MODULES
        modules = DEFAULT_MODULES.copy()

        modules["levels"]  = self.levels
        modules["welcome"] = self.welcome

        settings   = await db_settings.get_guild(interaction.guild_id)
        not_set    = "—"
        log_ch     = f"<#{settings['log_channel_id']}>"     if settings and settings.get("log_channel_id")     else not_set
        rules_ch   = f"<#{settings['rules_channel_id']}>"   if settings and settings.get("rules_channel_id")   else not_set
        welcome_ch = f"<#{settings['welcome_channel_id']}>" if settings and settings.get("welcome_channel_id") else not_set

        embed = discord.Embed(
            title=f"{NexusEmojis.CHECKMARK} {self.lang.get('setup_done_title', 'Nexus is ready!')}",
            description=self.lang.get("setup_done_description", "Setup was completed successfully."),
            color=NEXUS_COLOR,
        )
        embed.add_field(name=self.lang.get("settings_log",     "📋 Log-Channel"),     value=log_ch,     inline=True)
        embed.add_field(name=self.lang.get("settings_rules",   "📜 Rules-Channel"),   value=rules_ch,   inline=True)
        embed.add_field(name=self.lang.get("settings_welcome", "👋 Welcome-Channel"), value=welcome_ch, inline=True)
        embed.add_field(name=self.lang.get("settings_level",   "📈 Level-System"),    value=f"{NexusEmojis.CHECKMARK} {self.lang.get('setup_on', 'ON')}" if self.levels  else f"❌ {self.lang.get('setup_off', 'OFF')}", inline=True)
        embed.add_field(name=self.lang.get("welcome_title",    "👋 Welcome"),         value=f"{NexusEmojis.CHECKMARK} {self.lang.get('setup_on', 'ON')}" if self.welcome else f"❌ {self.lang.get('setup_off', 'OFF')}", inline=True)
        embed.set_footer(text=f"Nexus • {self.lang.get('setup_completed', 'Setup completed')}")

        final_view = discord.ui.View(timeout=None)
        final_view.add_item(_dashboard_button(interaction.guild_id, self.lang))

        await interaction.response.edit_message(embed=embed, view=final_view)

        await db_settings.update_guild(
            interaction.guild_id,
            modules=modules,
            setup_complete=True,
        )

        if settings and settings.get("log_channel_id"):
            log_channel = interaction.guild.get_channel(settings["log_channel_id"])
            if log_channel:
                try:
                    await log_channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("Konnte Abschluss-Embed nicht in Log-Kanal senden (%s): %s", settings["log_channel_id"], e)

        log.info("Setup auf %s abgeschlossen.", interaction.guild.name)

        if not interaction.message.flags.ephemeral:
            try:
                if self.setup_channel:
                    async for msg in self.setup_channel.history(limit=10):
                        if msg.author == interaction.guild.me:
                            await msg.delete(delay=2)
                            break
                else:
                    await interaction.delete_original_response()
            except (discord.NotFound, discord.Forbidden):
                pass

# ─── Restore Option ───────────────────────────────────────────────────────────

class RestoreView(discord.ui.View):
    def __init__(self, owner_id: int, lang: dict, setup_channel=None):
        super().__init__(timeout=300)
        self.owner_id      = owner_id
        self.lang          = lang
        self.setup_channel = setup_channel
        self.restore_btn.label = lang.get("setup_restore", "Restore")
        self.restore_btn.emoji = discord.PartialEmoji.from_str(NexusEmojis.CHECKMARK)
        self.restart_btn.label = lang.get("setup_restart", "🔄 Start fresh")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                self.lang.get("setup_only_owner", "You need the 'Manage Server' permission to run the setup."),
                ephemeral=True
            )
            return False
        return True
    
    @discord.ui.button(label="Restore", style=discord.ButtonStyle.success)
    async def restore_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not check_bot_hierarchy(interaction.guild):
            bot_pos, total = get_bot_position_info(interaction.guild)
            
            await interaction.response.edit_message(
                embed=await _hierarchy_warning_embed(interaction.guild_id, bot_pos, total, self.lang),
                view=HierarchyView(self.owner_id, self.lang, self.setup_channel, is_restore=True),
            )
            return

        await _apply_brand_role(interaction)

        embed = discord.Embed(
            title=self.lang.get("setup_restored_title", "{nexus_checkmark} Settings restored!").format(nexus_checkmark=NexusEmojis.CHECKMARK),
            description=self.lang.get("setup_restored_desc", "Your old settings have been successfully restored."),
            color=NEXUS_COLOR,
        )
        settings = await db_settings.get_guild(interaction.guild_id)
        if settings:
            log_ch     = f"<#{settings['log_channel_id']}>"     if settings.get("log_channel_id")     else "—"
            rules_ch   = f"<#{settings['rules_channel_id']}>"   if settings.get("rules_channel_id")   else "—"
            welcome_ch = f"<#{settings['welcome_channel_id']}>" if settings.get("welcome_channel_id") else "—"
            embed.add_field(name=self.lang.get("settings_log",     "📋 Log-Channel"),         value=log_ch,     inline=True)
            embed.add_field(name=self.lang.get("settings_rules",   "📜 Rules Channel"),       value=rules_ch,   inline=True)
            embed.add_field(name=self.lang.get("settings_welcome", "👋 Welcome Channel"),     value=welcome_ch, inline=True)

        try:
            if self.setup_channel:
                async for msg in self.setup_channel.history(limit=10):
                    if msg.author == interaction.guild.me and msg.components:
                        await msg.delete(delay=1)
                        break
        except (discord.NotFound, discord.Forbidden):
            pass

        await interaction.response.edit_message(embed=embed, view=None)
        log.info("Guild %s hat Einstellungen wiederhergestellt.", interaction.guild.name)

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.danger)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_settings.reset_guild(interaction.guild_id)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=self.lang.get("setup_hello_title", "👋 Hello, I'm Nexus!"),
                description=self.lang.get(
                    "setup_hello_description",
                    "I'll help you manage your server.\nClick **Start Setup** to get started."
                ),
                color=NEXUS_COLOR,
            ),
            view=SetupView(self.owner_id, self.lang, interaction.guild_id, self.setup_channel),
        )

# ─── Setup Command ────────────────────────────────────────────────────────────

class SetupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="setup", description=app_commands.locale_str("cmd_setup_desc"))
    @app_commands.default_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction):
        await db_settings.get_or_create_guild(interaction.guild_id)
        lang = await get_lang(interaction.guild_id)

        embed = discord.Embed(
            title=lang.get("setup_hello_title", "👋 Hello, I'm Nexus!"),
            description=lang.get(
                "setup_hello_description",
                "I'll help you manage your server.\nClick **Start Setup** to get started."
            ),
            color=NEXUS_COLOR,
        )
        embed.set_footer(text="Nexus • Setup")
        await interaction.response.send_message(
            embed=embed,
            view=SetupView(interaction.user.id, lang, interaction.guild_id),
            ephemeral=True,
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        lang = await get_lang(interaction.guild_id)
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                lang.get("no_permission", "You don't have permission to use this command."), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                lang.get("error_occurred", "An error occurred: {error}").format(error=error), ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(SetupCog(bot))