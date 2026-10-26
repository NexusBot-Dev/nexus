import logging
import discord
from discord import app_commands
from discord.ext import commands
from database import db_settings, db_stream_alerts
from cogs.utils import get_lang
from config import NEXUS_COLOR, NEXUS_FOOTER
from systems.health_checks import get_guild_issues, GuildIssue
from emojis import NexusEmojis

log = logging.getLogger(__name__)

OK, WARN, ERR = NexusEmojis.CHECKMARK, "⚠️", "❌"
MAX_LINES = 8

_SEVERITY_ICON = {"info": "ℹ️", "warn": WARN, "error": ERR}

def _lines(items: list[str], lang: dict) -> str:
    """Begrenzt Listen, damit das Embed-Feld (1024 Zeichen) nicht platzt."""
    if len(items) <= MAX_LINES:
        return "\n".join(items)
    rest = len(items) - MAX_LINES
    more = lang.get("diag_more", "… and {count} more").format(count=rest)
    return "\n".join(items[:MAX_LINES] + [more])

def _section_lines(issues: list[GuildIssue], section: str) -> list[str]:
    return [f"{_SEVERITY_ICON[i.severity]} {i.text}" for i in issues if i.section == section]

class Diagnose(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="diagnose", description=app_commands.locale_str("cmd_diagnose_desc"))
    @app_commands.default_permissions(manage_guild=True)
    async def diagnose(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        lang = await get_lang(guild.id)
        settings = await db_settings.get_guild(guild.id) or {}
        modules = settings.get("modules") or {}

        issues = await get_guild_issues(guild, settings, lang)
        # "info"-Einträge (z.B. Rollen über Nexus) werden angezeigt, zählen aber nicht als Problem
        problems = sum(1 for i in issues if i.severity != "info")

        embed = discord.Embed(
            title=lang.get("diag_title", "🩺 Nexus Diagnosis"),
            color=NEXUS_COLOR,
        )
        embed.set_footer(text=NEXUS_FOOTER)

        # ─── 1. Grundrechte ────────────────────────────────────────────────
        if guild.me.guild_permissions.administrator:
            perms_text = f"{OK} " + lang.get(
                "diag_perms_admin",
                "Nexus has Administrator. Everything works, but it isn't required.",
            )
        else:
            perm_issues = _section_lines(issues, "perms")
            perms_text = _lines(perm_issues, lang) if perm_issues else f"{OK} " + lang.get("diag_perms_ok", "All recommended permissions are present.")
        embed.add_field(name=lang.get("diag_section_perms", "🔑 Permissions"), value=perms_text, inline=False)

        # ─── 2. Hierarchie ─────────────────────────────────────────────────
        hier_lines = _section_lines(issues, "hierarchy")
        if not hier_lines:
            hier_lines = [f"{OK} " + lang.get("diag_hierarchy_ok", "Role hierarchy looks good.")]
        embed.add_field(name=lang.get("diag_section_hierarchy", "📊 Role hierarchy"), value=_lines(hier_lines, lang), inline=False)

        # ─── 3. Konfigurierte Channels ─────────────────────────────────────
        ch_lines = _section_lines(issues, "channels")
        configured_channels = any(settings.get(k) for k in (
            "log_channel_id", "welcome_channel_id", "rules_channel_id", "ticket_log_channel_id",
            "goodbye_channel_id", "boost_channel_id",
        ))
        # Auch zeigen, wenn es Probleme gibt — sonst bliebe z.B. ein defekter Goodbye-Channel unsichtbar
        if configured_channels or ch_lines:
            if not ch_lines:
                ch_lines = [f"{OK} " + lang.get("diag_channels_ok", "All configured channels are accessible.")]
            embed.add_field(name=lang.get("diag_section_channels", "💬 Channels"), value=_lines(ch_lines, lang), inline=False)

        # ─── 3b. Stream-Alerts (nur wenn es welche gibt, sonst kein irreführendes "alles ok") ───
        alert_lines = _section_lines(issues, "alerts")
        has_alerts = bool(alert_lines) or (
            bool(modules.get("stream_alerts"))
            and bool(await db_stream_alerts.get_subscriptions_for_guild(guild.id))
        )
        if has_alerts:
            if not alert_lines:
                alert_lines = [f"{OK} " + lang.get("diag_alerts_ok", "All alert channels and ping roles are fine.")]
            embed.add_field(name=lang.get("diag_section_alerts", "📡 Stream alerts"), value=_lines(alert_lines, lang), inline=False)

        # ─── 4. Rollen aus Modulen (Levels, Reaction Roles) ────────────────
        if modules.get("levels") or modules.get("reaction_roles"):
            role_lines = _section_lines(issues, "roles")
            if not role_lines:
                role_lines = [f"{OK} " + lang.get("diag_roles_ok", "All module roles can be assigned.")]
            embed.add_field(name=lang.get("diag_section_roles", "🎭 Module roles"), value=_lines(role_lines, lang), inline=False)

        # ─── 5. Tickets ────────────────────────────────────────────────────
        if modules.get("tickets"):
            ticket_lines = _section_lines(issues, "tickets")
            ticket_text = _lines(ticket_lines, lang) if ticket_lines else f"{OK} " + lang.get("diag_tickets_ok", "Tickets can be created.")
            embed.add_field(name=lang.get("diag_section_tickets", "🎫 Tickets"), value=ticket_text, inline=False)

        # ─── Fazit ─────────────────────────────────────────────────────────
        if problems:
            embed.description = lang.get(
                "diag_summary_problems", "Found **{count}** issue(s). Details below."
            ).format(count=problems)
            embed.color = discord.Color.orange()
        else:
            embed.description = lang.get("diag_summary_ok", "Everything looks good! 🎉")
            embed.color = NEXUS_COLOR

        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("Diagnose auf %s ausgeführt: %d Problem(e).", guild.id, problems)

async def setup(bot):
    await bot.add_cog(Diagnose(bot))