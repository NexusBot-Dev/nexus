import logging
import discord
from discord import app_commands
from discord.ext import commands
from database import db_settings
from config import NEXUS_COLOR, DASHBOARD_URL
from emojis import NexusEmojis
from cogs.utils import get_lang, MODULE_DISPLAY, CORE_MODULES
from systems.hierarchy import check_bot_hierarchy, get_bot_position_info
from systems.ui import NexusView

log = logging.getLogger(__name__)

# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _apply_brand_role(interaction: discord.Interaction):
    """Erstellt die Nexus Brand-Rolle (oder findet sie per gespeicherter ID) und weist sie dem Bot zu."""
    guild    = interaction.guild
    settings = await db_settings.get_guild(guild.id) or {}
    role_id  = settings.get("brand_role_id")
    role     = guild.get_role(role_id) if role_id else None

    try:
        bot_member = guild.me or await guild.fetch_member(interaction.client.user.id)
        if role is None:
            role = await guild.create_role(
                name="Nexus",
                color=discord.Color(NEXUS_COLOR),
                hoist=True,
                permissions=discord.Permissions.none(),
                reason="Nexus Setup — set brand color",
            )
            await db_settings.update_guild(guild.id, brand_role_id=role.id)
            await role.edit(position=max(1, bot_member.top_role.position - 1))
        if role not in bot_member.roles:
            await bot_member.add_roles(role, reason="Nexus Setup — brand role")
    except (discord.Forbidden, discord.HTTPException) as e:
        log.info("Brand-Rolle auf %s (%s) konnte nicht verwaltet werden: %s", guild.name, guild.id, e)

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
    channel = guild.get_channel(raw_channel.id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(raw_channel.id)
        except discord.Forbidden:
            # Bot hat keinen Zugriff auf privaten Kanal (Missing Access)
            return raw_channel, ["Kanal anzeigen (`View Channel`)"]
        except discord.HTTPException:
            return raw_channel, ["Kanal anzeigen (`View Channel`)"]

    missing = _get_missing_channel_permissions(channel, bot_member, for_logging=for_logging)
    return channel, missing

async def _validate_restored_channels(interaction: discord.Interaction) -> dict[str, str]:
    """Prüft beim Restore die gespeicherten Channels: gelöschte werden genullt, fehlende Rechte markiert."""
    settings = await db_settings.get_guild(interaction.guild_id) or {}
    result: dict[str, str] = {}
    cleanup: dict[str, None] = {}

    for key, for_logging in (("log_channel_id", True), ("rules_channel_id", False), ("welcome_channel_id", False)):
        ch_id = settings.get(key)
        if not ch_id:
            result[key] = "—"
            continue
        ch = interaction.guild.get_channel(ch_id)
        if ch is None:
            cleanup[key] = None
            result[key] = "— *(gelöscht)*"
            continue
        _, missing = await _resolve_channel_and_check_perms(ch, interaction.guild, interaction.client.user.id, for_logging=for_logging)
        result[key] = f"{ch.mention} ⚠️" if missing else ch.mention

    if cleanup:
        await db_settings.update_guild(interaction.guild_id, **cleanup)
    return result

async def _finish_restore(interaction: discord.Interaction, lang: dict[str, str], setup_channel=None):
    """Gemeinsamer Abschluss für Restore (direkt oder nach der Hierarchie-Prüfung)."""
    await _apply_brand_role(interaction)
    channels = await _validate_restored_channels(interaction)

    embed = discord.Embed(
        title=lang.get("setup_restored_title", "{nexus_checkmark} Settings restored!").format(nexus_checkmark=NexusEmojis.CHECKMARK),
        description=lang.get("setup_restored_desc", "Your old settings have been successfully restored."),
        color=NEXUS_COLOR,
    )
    embed.add_field(name=lang.get("settings_log",     "📋 Log-Channel"),     value=channels["log_channel_id"],     inline=True)
    embed.add_field(name=lang.get("settings_rules",   "📜 Rules-Channel"),   value=channels["rules_channel_id"],   inline=True)
    embed.add_field(name=lang.get("settings_welcome", "👋 Welcome-Channel"), value=channels["welcome_channel_id"], inline=True)
    if any("⚠️" in v for v in channels.values()):
        embed.set_footer(text=lang.get("setup_restored_perm_hint", "⚠️ = Nexus is missing permissions in this channel. Check via /setup or the dashboard."))

    try:
        if setup_channel:
            async for msg in setup_channel.history(limit=10):
                if msg.author == interaction.guild.me and msg.components:
                    await msg.delete(delay=1)
                    break
    except (discord.NotFound, discord.Forbidden):
        pass

    await interaction.response.edit_message(embed=embed, view=None)
    log.info("Guild %s (%s) hat Einstellungen wiederhergestellt.", interaction.guild.name, interaction.guild_id)

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

class SetupStepView(NexusView):
    """
    Basis für alle Setup-Schritte.

    Zugriff: 'Manage Server' — bewusst KEIN Owner-Zwang. Die Setup-Nachricht wird beim
    Server-Join öffentlich gepostet, und wer Nexus einlädt, ist nicht immer der Owner.
    `owner_id` wird nur durchgereicht (Signatur bleibt kompatibel zu bot.py).
    """
    DENY_KEY = "setup_only_owner"

    def __init__(self, owner_id: int, lang: dict[str, str], setup_channel=None):
        super().__init__(lang, owner_id=None, require="manage_guild", timeout=300)
        self.setup_owner_id = owner_id
        self.setup_channel  = setup_channel


class ChannelPermissionWarningView(SetupStepView):
    def __init__(self, owner_id: int, channel, step_type: str, lang: dict[str, str], setup_channel=None):
        super().__init__(owner_id, lang, setup_channel)
        self.channel       = channel
        self.step_type     = step_type
        self.validate_btn.label = self.lang.get("setup_validate", "🔍 Validate")
        self.skip_btn.label     = self.lang.get("setup_skip", "⏭️ Skip")

    @discord.ui.button(label="🔍 Validate", style=discord.ButtonStyle.primary)
    async def validate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_logging = (self.step_type == "log")
        channel, missing = await _resolve_channel_and_check_perms(self.channel, interaction.guild, interaction.client.user.id, for_logging=is_logging)

        if missing:
            embed = await _channel_permission_warning_embed(channel, interaction.guild, interaction.client.user.id, is_logging, self.lang)
            await interaction.response.edit_message(embed=embed, view=self)
            return

        self.channel = channel
        await db_settings.update_guild(interaction.guild_id, **{self._column: channel.id})
        await self._proceed_next_step(interaction, channel.mention)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_settings.update_guild(interaction.guild_id, **{self._column: None})
        await self._proceed_next_step(interaction, "—")

    @property
    def _column(self) -> str:
        return f"{self.step_type}_channel_id"

    async def _proceed_next_step(self, interaction: discord.Interaction, mention: str):
        if self.step_type == "log":
            await self.show(
                interaction, RulesChannelView(self.setup_owner_id, self.lang, self.setup_channel),
                embed=await _rules_channel_embed(interaction.guild_id, mention, self.lang),
            )
        elif self.step_type == "rules":
            await self.show(
                interaction, WelcomeChannelView(self.setup_owner_id, self.lang, self.setup_channel),
                embed=await _welcome_channel_embed(interaction.guild_id, mention, self.lang),
            )
        elif self.step_type == "welcome":
            await self.show(
                interaction, ModulesView(self.setup_owner_id, self.lang, self.setup_channel),
                embed=await _modules_embed(interaction.guild_id, self.lang),
            )

# ─── Schritt 0: Rollenprüfung ─────────────────────────────────────────────────

class HierarchyView(SetupStepView):
    def __init__(self, owner_id: int, lang: dict[str, str], setup_channel=None, is_restore: bool = False):
        super().__init__(owner_id, lang, setup_channel)
        self.is_restore    = is_restore
        self.check_hierarchy.label = self.lang.get("setup_validate", "🔍 Validate")
        self.skip_hierarchy.label  = self.lang.get("setup_skip", "⏭️ Skip")

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
        if self.is_restore:
            self.stop()
            await _finish_restore(interaction, self.lang, self.setup_channel)
            return
        await _apply_brand_role(interaction)
        await self.show(
            interaction, LogChannelView(self.setup_owner_id, self.lang, self.setup_channel),
            embed=await _log_channel_embed(interaction.guild_id, self.lang),
        )

# ─── Setup Start ─────────────────────────────────────────────────────────────

class SetupView(SetupStepView):
    def __init__(self, owner_id: int, lang: dict[str, str], guild_id: int, setup_channel=None):
        super().__init__(owner_id, lang, setup_channel)
        self.start_setup.label = self.lang.get("setup_start_title", "Start")
        self.add_item(_dashboard_button(guild_id, lang)) 

    @discord.ui.button(label="Setup starten", style=discord.ButtonStyle.success)
    async def start_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not check_bot_hierarchy(interaction.guild):
            bot_pos, total = get_bot_position_info(interaction.guild)
            await self.show(
                interaction, HierarchyView(self.setup_owner_id, self.lang, self.setup_channel),
                embed=await _hierarchy_warning_embed(interaction.guild_id, bot_pos, total, self.lang),
            )
            return

        await _apply_brand_role(interaction)

        await self.show(
            interaction, LogChannelView(self.setup_owner_id, self.lang, self.setup_channel),
            embed=await _log_channel_embed(interaction.guild_id, self.lang),
        )

# ─── Schritt 1: Log-Channel ───────────────────────────────────────────────────

class LogChannelView(SetupStepView):
    def __init__(self, owner_id: int, lang: dict[str, str], setup_channel=None):
        super().__init__(owner_id, lang, setup_channel)
        self.select_log_channel.placeholder = self.lang.get("setup_placeholder", "📋 Choose an existing Channel...")
        self.create_log_channel.label       = self.lang.get("setup_create", "✨ Create one for me")
        self.skip_step.label                = self.lang.get("setup_skip", "⏭️ Skip")

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        row=0
    )
    async def select_log_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        raw_channel = select.values[0]
        channel, missing = await _resolve_channel_and_check_perms(raw_channel, interaction.guild, interaction.client.user.id, for_logging=True)

        if missing:
            embed = await _channel_permission_warning_embed(channel, interaction.guild, interaction.client.user.id, True, self.lang)
            view = ChannelPermissionWarningView(self.setup_owner_id, channel, "log", self.lang, self.setup_channel)
            await self.show(interaction, view, embed=embed)
            return

        await db_settings.update_guild(interaction.guild_id, log_channel_id=channel.id)
        await self.show(
            interaction, RulesChannelView(self.setup_owner_id, self.lang, setup_channel=self.setup_channel),
            embed=await _rules_channel_embed(interaction.guild_id, channel.mention, self.lang),
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
        await self.show(
            interaction, RulesChannelView(self.setup_owner_id, self.lang, self.setup_channel),
            embed=await _rules_channel_embed(interaction.guild_id, channel.mention, self.lang),
        )

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, row=1)
    async def skip_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_settings.update_guild(interaction.guild_id, log_channel_id=None)
        await self.show(
            interaction, RulesChannelView(self.setup_owner_id, self.lang, self.setup_channel),
            embed=await _rules_channel_embed(interaction.guild_id, "—", self.lang),
        )

# ─── Schritt 2: Regel-Channel ─────────────────────────────────────────────────

class RulesChannelView(SetupStepView):
    def __init__(self, owner_id: int, lang: dict[str, str], setup_channel=None):
        super().__init__(owner_id, lang, setup_channel)
        self.select_rules_channel.placeholder = self.lang.get("setup_placeholder", "📋 Choose an existing Channel...")
        self.create_rules_channel.label       = self.lang.get("setup_create", "✨ Create one for me")
        self.skip_step.label                  = self.lang.get("setup_skip", "⏭️ Skip")

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        row=0
    )
    async def select_rules_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        raw_channel = select.values[0]
        channel, missing = await _resolve_channel_and_check_perms(raw_channel, interaction.guild, interaction.client.user.id, for_logging=False)

        if missing:
            embed = await _channel_permission_warning_embed(channel, interaction.guild, interaction.client.user.id, False, self.lang)
            view = ChannelPermissionWarningView(self.setup_owner_id, channel, "rules", self.lang, self.setup_channel)
            await self.show(interaction, view, embed=embed)
            return

        await db_settings.update_guild(interaction.guild_id, rules_channel_id=channel.id)
        await self.show(
            interaction, WelcomeChannelView(self.setup_owner_id, self.lang, self.setup_channel),
            embed=await _welcome_channel_embed(interaction.guild_id, channel.mention, self.lang),
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
        await self.show(
            interaction, WelcomeChannelView(self.setup_owner_id, self.lang, self.setup_channel),
            embed=await _welcome_channel_embed(interaction.guild_id, channel.mention, self.lang),
        )

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, row=1)
    async def skip_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_settings.update_guild(interaction.guild_id, rules_channel_id=None)
        await self.show(
            interaction, WelcomeChannelView(self.setup_owner_id, self.lang, self.setup_channel),
            embed=await _welcome_channel_embed(interaction.guild_id, "—", self.lang),
        )

# ─── Schritt 3: Willkommens-Channel ──────────────────────────────────────────

class WelcomeChannelView(SetupStepView):
    def __init__(self, owner_id: int, lang: dict[str, str], setup_channel=None):
        super().__init__(owner_id, lang, setup_channel)
        self.select_welcome_channel.placeholder = self.lang.get("setup_placeholder", "📋 Choose an existing Channel...")
        self.create_welcome_channel.label       = self.lang.get("setup_create", "✨ Create one for me")
        self.skip_step.label                    = self.lang.get("setup_skip", "⏭️ Skip")

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        row=0
    )
    async def select_welcome_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        raw_channel = select.values[0]
        channel, missing = await _resolve_channel_and_check_perms(raw_channel, interaction.guild, interaction.client.user.id, for_logging=False)

        if missing:
            embed = await _channel_permission_warning_embed(channel, interaction.guild, interaction.client.user.id, False, self.lang)
            view = ChannelPermissionWarningView(self.setup_owner_id, channel, "welcome", self.lang, self.setup_channel)
            await self.show(interaction, view, embed=embed)
            return

        await db_settings.update_guild(interaction.guild_id, welcome_channel_id=channel.id)
        await self.show(
            interaction, ModulesView(self.setup_owner_id, self.lang, self.setup_channel),
            embed=await _modules_embed(interaction.guild_id, self.lang),
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
        await self.show(
            interaction, ModulesView(self.setup_owner_id, self.lang, self.setup_channel),
            embed=await _modules_embed(interaction.guild_id, self.lang),
        )

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, row=1)
    async def skip_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_settings.update_guild(interaction.guild_id, welcome_channel_id=None)
        await self.show(
            interaction, ModulesView(self.setup_owner_id, self.lang, self.setup_channel),
            embed=await _modules_embed(interaction.guild_id, self.lang),
        )

# ─── Schritt 4: Module ────────────────────────────────────────────────────────

class ModulesView(SetupStepView):
    """Schritt 5: Alle Module per Multi-Select anbieten statt nur Levels/Welcome."""

    # Vorausgewählt: was die meisten Server wollen
    PRESELECTED = {"levels", "welcome", "stream_alerts", "tickets"}

    def __init__(self, owner_id: int, lang: dict[str, str], setup_channel=None):
        super().__init__(owner_id, lang, setup_channel)

        from database.db_settings import DEFAULT_MODULES
        self.available = [
            key for key in MODULE_DISPLAY
            if key in DEFAULT_MODULES and key not in CORE_MODULES
        ]
        self.selected = {k for k in self.available if k in self.PRESELECTED}

        self.module_select.options = self._build_options()
        self.module_select.max_values = len(self.available)
        self.module_select.placeholder = lang.get("setup_modules_placeholder", "Choose modules…")
        self.finish_setup.label = lang.get("setup_finish", "Finish Setup")
        self.finish_setup.emoji = discord.PartialEmoji.from_str(NexusEmojis.CHECKMARK)

    def _build_options(self) -> list[discord.SelectOption]:
        options = []
        for key in self.available:
            emoji, fallback_name = MODULE_DISPLAY[key]
            name = self.lang.get(f"module_name_{key}", fallback_name)
            desc = self.lang.get(f"setup_module_desc_{key}", "")
            options.append(discord.SelectOption(
                label=name[:100],
                value=key,
                description=desc[:100] or None,
                emoji=discord.PartialEmoji.from_str(emoji),
                default=key in self.selected,
            ))
        return options

    @discord.ui.select(cls=discord.ui.Select, min_values=0, row=0)
    async def module_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected = set(select.values)
        # Auswahl in den Optionen merken, damit sie nach dem Edit sichtbar bleibt
        for opt in select.options:
            opt.default = opt.value in self.selected
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Finish Setup", style=discord.ButtonStyle.primary, row=1)
    async def finish_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        from database.db_settings import DEFAULT_MODULES
        modules = DEFAULT_MODULES.copy()
        for key in self.available:
            modules[key] = key in self.selected

        settings   = await db_settings.get_guild(interaction.guild_id)
        not_set    = "—"
        log_ch     = f"<#{settings['log_channel_id']}>"     if settings and settings.get("log_channel_id")     else not_set
        rules_ch   = f"<#{settings['rules_channel_id']}>"   if settings and settings.get("rules_channel_id")   else not_set
        welcome_ch = f"<#{settings['welcome_channel_id']}>" if settings and settings.get("welcome_channel_id") else not_set

        active = [
            f"{MODULE_DISPLAY[k][0]} {self.lang.get(f'module_name_{k}', MODULE_DISPLAY[k][1])}"
            for k in self.available if modules[k]
        ]

        embed = discord.Embed(
            title=f"{NexusEmojis.CHECKMARK} {self.lang.get('setup_done_title', 'Nexus is ready!')}",
            description=self.lang.get("setup_done_description", "Setup was completed successfully."),
            color=NEXUS_COLOR,
        )
        embed.add_field(name=self.lang.get("settings_log",     "📋 Log-Channel"),     value=log_ch,     inline=True)
        embed.add_field(name=self.lang.get("settings_rules",   "📜 Rules-Channel"),   value=rules_ch,   inline=True)
        embed.add_field(name=self.lang.get("settings_welcome", "👋 Welcome-Channel"), value=welcome_ch, inline=True)
        embed.add_field(
            name=self.lang.get("setup_active_modules", "🧩 Active modules"),
            value="\n".join(active) if active else not_set,
            inline=False,
        )
        embed.set_footer(text=f"Nexus • {self.lang.get('setup_completed', 'Setup completed')}")

        final_view = discord.ui.View(timeout=None)
        final_view.add_item(_dashboard_button(interaction.guild_id, self.lang))

        self.stop()
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
                except discord.HTTPException as e:
                    log.warning("Konnte Abschluss-Embed nicht in Log-Kanal senden (%s): %s", settings["log_channel_id"], e)

        log.info("Setup auf %s abgeschlossen. Module: %s", interaction.guild.name, sorted(self.selected))

        if not interaction.message.flags.ephemeral:
            try:
                if self.setup_channel:
                    async for msg in self.setup_channel.history(limit=10):
                        if msg.author == interaction.guild.me:
                            await msg.delete(delay=2)
                            break
                else:
                    await interaction.delete_original_response()
            except discord.HTTPException:
                pass

# ─── Restore Option ───────────────────────────────────────────────────────────

class RestoreView(SetupStepView):
    def __init__(self, owner_id: int, lang: dict, setup_channel=None):
        super().__init__(owner_id, lang, setup_channel)
        self.restore_btn.label = lang.get("setup_restore", "Restore")
        self.restore_btn.emoji = discord.PartialEmoji.from_str(NexusEmojis.CHECKMARK)
        self.restart_btn.label = lang.get("setup_restart", "🔄 Start fresh")

    @discord.ui.button(label="Restore", style=discord.ButtonStyle.success)
    async def restore_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not check_bot_hierarchy(interaction.guild):
            bot_pos, total = get_bot_position_info(interaction.guild)
            
            await self.show(
                interaction, HierarchyView(self.setup_owner_id, self.lang, self.setup_channel, is_restore=True),
                embed=await _hierarchy_warning_embed(interaction.guild_id, bot_pos, total, self.lang),
            )
            return

        self.stop()
        await _finish_restore(interaction, self.lang, self.setup_channel)

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.danger)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_settings.reset_guild(interaction.guild_id)
        embed = discord.Embed(
            title=self.lang.get("setup_hello_title", "👋 Hello, I'm Nexus!"),
            description=self.lang.get(
                "setup_hello_description",
                "I'll help you manage your server.\nClick **Start Setup** to get started."
            ),
            color=NEXUS_COLOR,
        )
        await self.show(
            interaction,
            SetupView(self.setup_owner_id, self.lang, interaction.guild_id, self.setup_channel),
            embed=embed,
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
        view = SetupView(interaction.user.id, lang, interaction.guild_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.bind(interaction)

async def setup(bot):
    await bot.add_cog(SetupCog(bot))