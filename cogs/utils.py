import logging
import discord
from database import db_settings
from translations import TRANSLATIONS
from discord import app_commands
from emojis import NexusEmojis
from config import NEXUS_COLOR, NEXUS_FOOTER

log = logging.getLogger(__name__)

MODULE_COMMANDS = {
    "moderation": [
        "kick", "ban", "unban", "warn", "warnings", "unwarn",
        "clearwarnings", "add_role", "remove_role", "set_slowmode",
        "purge_messages", "timeout",
    ],
    "levels": [
        "rank", "leaderboard", "set_level", "add_xp", "remove_xp",
        "wipe_xp", "level_roles",
    ],
    "welcome": [
        "welcome_message",
        "goodbye",
    ],
    "reaction_roles": [
        "reactionrole_create",
        "reactionrole_list",
        "reactionrole_delete",
    ],
    "boosts": [
        "boost_message",
    ],
    "embeds": [
        "embed",
        "post_rules",
    ],
    "automod": ["automod"],
    "help": ["help", "info"],
    "stream_alerts": [
        "alert_add",
        "alert_remove",
        "alert_list",
        "alert_role",
        "alert_edit",
    ],
    "tickets": [
        "add_category",
        "edit_category",
        "ticket_panel",
        "force_close",
    ],
    "giveaways": [
       "giveaways"
    ],
}

CORE_MODULES = {"moderation", "embeds", "giveaways"}

# Anzeige-Metadaten für die "Modul deaktiviert"-Karte
MODULE_DISPLAY = {
    "welcome":        ("👋", "Welcome"),
    "boosts":         (NexusEmojis.Modules.BOOST, "Boosts"),
    "reaction_roles": ("🎭", "Reaction Roles"),
    "embeds":         ("📋", "Embeds"),
    "automod":        ("🛡️", "AutoMod"),
    "stream_alerts":  (NexusEmojis.Modules.BROADCAST, "Stream Alerts"),
    "tickets":        ("🎫", "Tickets"),
    "levels":         ("📈", "Level-System"),
    "giveaways":      ("🎉", "Giveaways")
}

async def get_lang(guild_id: int) -> dict[str, str]:
    """Gibt das komplette Sprach-Dict für eine Guild zurück."""
    settings = await db_settings.get_guild(guild_id)
    locale   = settings.get("locale", "en") if settings else "en"
    return TRANSLATIONS.get(locale, TRANSLATIONS["en"])

def get_user_lang(interaction: discord.Interaction):
    raw_locale = str(interaction.locale)
    base_locale = raw_locale.split("-")[0]
    return TRANSLATIONS.get(base_locale, TRANSLATIONS.get("en", {}))

class ModuleEnableView(discord.ui.View):
    def __init__(self, module: str, guild_id: int, lang: dict):
        super().__init__(timeout=60)
        self.module = module
        self.guild_id = guild_id
        self.add_item(discord.ui.Button(
            label=lang.get("module_dashboard_btn", "🌐 Open Dashboard"),
            style=discord.ButtonStyle.link,
            url=f"https://dashboard.trynexus.de/server/{guild_id}",
        ))
        self.enable_btn.label = lang.get("module_enable_btn", "Enable module")
        self.enable_btn.emoji = NexusEmojis.CHECKMARK

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def enable_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        lang = await get_lang(self.guild_id)
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                lang.get("module_enable_perm_error", "You need 'Manage Server' permission to change this."),
                ephemeral=True
            )
            return
        ok, msg = await toggle_module(interaction.client, interaction.guild, self.module, True, lang)
        if ok:
            await interaction.response.edit_message(
                content=f"{NexusEmojis.CHECKMARK} {lang.get('module_enabled_msg', 'Module enabled. Please run the command again.')}",
                embed=None, view=None,
            )
        else:
            await interaction.response.send_message(msg, ephemeral=True)

def module_required(module: str):
    """Decorator für Slash-Commands: bricht mit Hinweis-Embed ab, falls das
    zugehörige Modul deaktiviert ist. Ersetzt das alte Tree-Add/Remove-Verhalten."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if module in CORE_MODULES:
            return True

        settings = await db_settings.get_guild(interaction.guild_id)
        if settings and settings.get("modules", {}).get(module, False):
            return True

        lang = await get_lang(interaction.guild_id)
        emoji, name = MODULE_DISPLAY.get(module, (NexusEmojis.Modules.SETTINGS, module.title()))

        embed = discord.Embed(
            title=lang.get("module_disabled_title", "{emoji} {name} is currently disabled").format(emoji=emoji, name=name),
            description=lang.get(
                "module_disabled_desc",
                "To use this feature on your server, enable it or adjust the settings in the web dashboard."
            ),
            color=NEXUS_COLOR,
        )
        await interaction.response.send_message(
            embed=embed,
            view=ModuleEnableView(module, interaction.guild_id, lang),
            ephemeral=True,
        )
        return False
    return app_commands.check(predicate)

async def toggle_module(
    bot,
    guild: discord.Guild,
    module: str,
    enable: bool,
    lang:   dict,
) -> tuple[bool, str]:
    """Schaltet ein Modul ein oder aus. (True, "") bei Erfolg, sonst (False, Fehlermeldung)."""
    if module in CORE_MODULES:
        return False, lang.get("module_core_error", "The module `{module}` is a core module and cannot be disabled.").format(module=module)

    await db_settings.set_module(guild.id, module, enable)
    log.info("Modul '%s' für %s %s.", module, guild.name, "aktiviert" if enable else "deaktiviert")

    if module == "automod" and not enable:
        from cogs.honeypot import cleanup_honeypot
        settings = await db_settings.get_guild(guild.id)
        if settings and settings.get("honeypot_channel_id"):
            await cleanup_honeypot(guild, settings["honeypot_channel_id"])

    return True, ""

async def toggle_modules_bulk(
    bot,
    guild: discord.Guild,
    changes: dict[str, bool],
    lang: dict,
) -> tuple[bool, str]:
    """Schaltet mehrere Module in einem Rutsch um (Dashboard-Bulk-Save)."""
    row = await db_settings.get_or_create_guild(guild.id)
    modules = row["modules"]
    for module, enabled in changes.items():
        if module in CORE_MODULES:
            continue
        modules[module] = enabled

    await db_settings.update_guild(guild.id, modules=modules)
    log.info("Bulk-Modul-Update für %s: %s", guild.name, changes)

    if changes.get("automod") is False:
        from cogs.honeypot import cleanup_honeypot
        settings = await db_settings.get_guild(guild.id)
        if settings and settings.get("honeypot_channel_id"):
            await cleanup_honeypot(guild, settings["honeypot_channel_id"])

    return True, ""

async def post_logging(
    bot,
    guild_id: int,
    title: str,
    moderator: discord.Member,
    target: discord.Member | discord.User,
    lang: dict,
    reason: str = None,
    color: discord.Color = discord.Color.red(),
):
    settings = await db_settings.get_guild(guild_id)
    if settings is None or not settings["log_channel_id"]:
        return
    channel = bot.get_channel(settings["log_channel_id"])
    if channel is None:
        return
    if reason is None:
        reason = lang.get("no_reason", "No reason given")
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="User",      value=target.mention,    inline=True)
    embed.add_field(name="Moderator", value=moderator.mention, inline=True)
    embed.add_field(name=lang.get("dm_reason", "Reason"), value=reason, inline=True)

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning("Keine Berechtigung in Log-Channel %s zu schreiben.", channel)
    except discord.HTTPException as e:
        log.exception("Fehler beim Schreiben in Log-Channel %s: %s", channel, e)


async def dm_user(
    member: discord.Member | discord.User,
    title: str,
    reason: str,
    color: discord.Color,
    lang: dict,
    footer: str = "",
    extra: str = "",
):
    if member.bot:
        return
    try:
        embed = discord.Embed(title=title, color=color)
        embed.add_field(name=lang.get("dm_reason", "Reason"), value=reason, inline=False)
        if extra:
            embed.add_field(name=lang.get("dm_info", "Info"), value=extra, inline=False)
        if footer:
            embed.set_footer(text=NEXUS_FOOTER)
        await member.send(embed=embed)
    except discord.Forbidden:
        log.info("Konnte %s keine DM schicken (DMs deaktiviert).", member)

async def get_member_safe(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:   # NotFound: User hat den Server verlassen
        return None