import logging
import re
import discord
from discord import app_commands
from discord.ext import commands
from database import db_settings
from emojis import NexusEmojis
from config import NEXUS_FOOTER
from translations import LOCALE_MAP
from cogs.utils import get_user_lang
from systems.ui import NexusView

log = logging.getLogger(__name__)

SITE_LOCALES = {"de", "en"}

_LEADING_EMOJI = re.compile(r"^\s*(?:<a?:\w+:\d+>|[^\w\s])+\s*")

# ─── Modul-Beschreibungen ────────────────────────────────────────────────────
MODULE_INFO = {
    "Moderation": {
        "emoji": NexusEmojis.SHIELD,
        "name_key": "help_mod_moderation",
        "commands": {
            "kick":           "help_cmd_kick",
            "ban":            "help_cmd_ban",
            "unban":          "help_cmd_unban",
            "timeout":        "help_cmd_timeout",
            "warn":           "help_cmd_warn",
            "warnings":       "help_cmd_warnings",
            "unwarn":         "help_cmd_unwarn",
            "clearwarnings":  "help_cmd_clearwarnings",
            "add_role":       "help_cmd_add_role",
            "remove_role":    "help_cmd_remove_role",
            "set_slowmode":   "help_cmd_set_slowmode",
            "purge_messages": "help_cmd_purge_messages",
            "timezone":       "help_cmd_timezone",
        }
    },
    "Levels": {
        "emoji": "📈",
        "name_key": "help_mod_levels",
        "commands": {
            "rank":              "help_cmd_rank",
            "leaderboard":       "help_cmd_leaderboard",
            "set_level":         "help_cmd_set_level",
            "add_xp":            "help_cmd_add_xp",
            "remove_xp":         "help_cmd_remove_xp",
            "wipe_xp":           "help_cmd_wipe_xp",
            "level_roles":       "help_cmd_level_roles",
            "levelup_channel":   "help_cmd_levelup_channel",
            "xp_channels":       "help_cmd_xp_channels",
            "import_mee6":       "help_cmd_import_mee6",
        }
    },
    "Reaction Roles": {
        "emoji": "🎭",
        "name_key": "help_mod_reaction_roles",
        "commands": {
            "reactionrole_create": "help_cmd_reactionrole_create",
            "reactionrole_list":   "help_cmd_reactionrole_list",
            "reactionrole_edit":   "help_cmd_reactionrole_edit",
            "reactionrole_delete": "help_cmd_reactionrole_delete",
        }
    },
    "Welcome": {
        "emoji": "👋",
        "name_key": "help_mod_welcome",
        "commands": {
            "welcome_message": "help_cmd_welcome_message",
            "goodbye":         "help_cmd_goodbye",
        }
    },
    "Embeds": {
        "emoji": "📝",
        "name_key": "help_mod_embeds",
        "commands": {
            "embed":        "help_cmd_embed",
            "post_rules":   "help_cmd_post_rules",
        }
    },
    "Boosts": {
        "emoji": NexusEmojis.Modules.BOOST,
        "name_key": "help_mod_boosts",
        "commands": {
            "boost_message": "help_cmd_boost_message",
        }
    },
    "Automod": {
        "emoji": "🤖",
        "name_key": "help_mod_automod",
        "commands": {
            "automod": "help_cmd_automod",
        }
    },
    "Stream Alerts": {
        "emoji": NexusEmojis.Modules.BROADCAST,
        "name_key": "help_mod_stream_alerts",
        "commands": {
            "alert_add":    "help_cmd_alert_add",
            "alert_edit":   "help_cmd_alert_edit",
            "alert_list":   "help_cmd_alert_list",
            "alert_remove": "help_cmd_alert_remove",
            "alert_role":   "help_cmd_alert_role",
        }
    },
    "Tickets": {
        "emoji": NexusEmojis.Modules.TICKET,
        "name_key": "help_mod_tickets",
        "commands": {
            "add_category":     "help_cmd_add_category",
            "edit_category":    "help_cmd_edit_category",
            "ticket_panel":     "help_cmd_ticket_panel",
            "force_close":      "help_cmd_force_close",
            "ticket_settings":  "help_cmd_ticket_settings",
        }
    },
    "Giveaways": {
        "emoji": "🎉",
        "name_key": "help_mod_giveaways",
        "always": True,
        "commands": {
            "giveaways":    "help_cmd_giveaways",
        }
    }
}

ALWAYS_SHOWN = {
    "name_key": "help_always_general",
    "commands": {
        "setup":          "help_cmd_setup",
        "module":         "help_cmd_module",
        "settings":       "help_cmd_settings",
        "delete_my_data": "help_cmd_delete_my_data",
        "help":           "help_cmd_help",
        "info":           "help_cmd_info",
        "announcement":   "help_cmd_announcement",
        "invite":         "help_cmd_invite",
        "premium":        "help_cmd_premium",
        "diagnose":       "help_cmd_diagnose",
    }
}

# ─── View ────────────────────────────────────────────────────────────────────

class HelpView(NexusView):
    """Ein Dropdown statt Buttons: bis zu 25 Einträge, kein Zeilen-/Spalten-Limit wie bei Buttons."""

    def __init__(self, owner_id: int, lang: dict, color: int, active_modules: dict):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=120)
        self.color          = color
        self.active_modules = active_modules
        self.current_page   = "overview"
        self.select_module.placeholder = lang.get("help_select_placeholder", "Select a module …")
        self._refresh_options()

    def _is_active(self, module: str, info: dict) -> bool:
        if info.get("always"):
            return True
        return bool(self.active_modules.get(module.lower().replace(" ", "_")))

    @staticmethod
    def _plain(text: str, fallback: str) -> str:
        """Text ohne führendes Emoji; fällt auf fallback zurück, wenn nichts übrig bleibt."""
        return _LEADING_EMOJI.sub("", text).strip() or fallback

    def _refresh_options(self):
        """Baut die Optionen neu auf, damit der aktuelle Eintrag als Vorauswahl markiert ist."""
        active = [
            (module, info) for module, info in MODULE_INFO.items()
            if self._is_active(module, info) and info["commands"]
        ]
        total_commands = len(ALWAYS_SHOWN["commands"]) + sum(len(info["commands"]) for _, info in active)

        options = [
            discord.SelectOption(
                label=self._plain(self.lang.get("help_btn_overview", "Overview"), "Overview"),
                value="overview",
                description=self.lang.get(
                    "help_select_overview_count", "{modules} modules · {commands} commands"
                ).format(modules=len(active), commands=total_commands),
                emoji="📋",
                default=self.current_page == "overview",
            )
        ]
        for module, info in active:
            options.append(discord.SelectOption(
                label=self._plain(self.lang.get(info["name_key"], module), module)[:100],
                value=module,
                description=self.lang.get("help_select_count", "{count} commands").format(count=len(info["commands"])),
                emoji=info["emoji"],
                default=self.current_page == module,
            ))
        self.select_module.options = options[:25]

    @discord.ui.select(min_values=1, max_values=1)
    async def select_module(self, interaction: discord.Interaction, select: discord.ui.Select):
        choice = select.values[0]
        self.current_page = choice if choice in MODULE_INFO else "overview"
        self._refresh_options()
        embed = self._overview_embed() if self.current_page == "overview" else self._module_embed(self.current_page)
        await interaction.response.edit_message(embed=embed, view=self)

    def _overview_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.lang.get("help_title", "📖 Nexus — Help"),
            description=self.lang.get("help_overview_desc",
                "Select a module to see available commands.\n\n⭐ = Premium Feature"
            ),
            color=self.color,
        )

        # Immer verfügbar — Keys auflösen
        always_cmds = "\n".join(
            f"`/{cmd}` — {self.lang.get(key, cmd)}"
            for cmd, key in ALWAYS_SHOWN["commands"].items()
        )
        embed.add_field(
            name=self.lang.get(ALWAYS_SHOWN["name_key"], "⚙️ General").format(settings=NexusEmojis.Modules.SETTINGS),
            value=always_cmds,
            inline=False,
        )

        # Aktive Module
        for module, info in MODULE_INFO.items():
            if not self._is_active(module, info) or not info["commands"]:
                continue
            embed.add_field(
                name=self.lang.get(info["name_key"], f"{info['emoji']} {module}"),
                value=f"{self.lang.get('help_select_count', '{count} commands').format(count=len(info['commands']))} — {self.lang.get('help_click_details', 'Select in the menu')}",
                inline=True,
            )

        embed.set_footer(text=NEXUS_FOOTER)
        return embed

    def _module_embed(self, module: str) -> discord.Embed:
        info  = MODULE_INFO[module]
        embed = discord.Embed(
            title=self.lang.get(info["name_key"], f"{info['emoji']} {module}"),
            color=self.color,
        )
        cmds = "\n".join(
            f"`/{cmd}` — {self.lang.get(key, cmd)}"
            for cmd, key in info["commands"].items()
        )
        embed.description = cmds or self.lang.get("help_no_commands", "No commands available.")
        embed.set_footer(text=NEXUS_FOOTER)
        return embed

# ─── Cog ─────────────────────────────────────────────────────────────────────

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description=app_commands.locale_str("cmd_help_desc"))
    async def help(self, interaction: discord.Interaction):
        lang     = get_user_lang(interaction)
        color    = await db_settings.get_color(interaction.guild_id)
        settings = await db_settings.get_or_create_guild(interaction.guild_id)
        modules  = settings.get("modules", {})

        view  = HelpView(interaction.user.id, lang, color, modules)
        embed = view._overview_embed()

        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True,
        )
        view.bind(interaction)

    @app_commands.command(name="info", description=app_commands.locale_str("cmd_info_desc"))
    async def info(self, interaction: discord.Interaction):
        lang  = get_user_lang(interaction)
        color = await db_settings.get_color(interaction.guild_id)
        site_locale = LOCALE_MAP.get(str(interaction.locale), "en")
        if site_locale not in SITE_LOCALES:
            site_locale = "en"

        embed = discord.Embed(
            title=lang.get("info_title", "ℹ️ Nexus"),
            description=lang.get("info_desc",
                "Nexus is a modern, modular Discord bot that takes your server to the next level."
            ),
            color=color,
        )
        embed.add_field(
            name=lang.get("info_docu", "📚 Documentation"),
            value=f"[trynexus.de/wiki](https://trynexus.de/{site_locale}/wiki/)",
            inline=True,
        )
        embed.add_field(
            name=lang.get("info_support", "💬 Support"),
            value="[Support-Server](https://discord.gg/YFCrvBb6t3)",
            inline=True,
        )
        embed.add_field(
            name=lang.get("info_website", "🌐 Website"),
            value=f"[trynexus.de](https://trynexus.de/{site_locale}/)",
            inline=True,
        )
        embed.add_field(
            name=lang.get("info_premium", "⭐ Premium"),
            value=f"[trynexus.de/premium](https://trynexus.de/{site_locale}/#premium)",
            inline=True,
        )
        embed.add_field(
            name=lang.get("info_privacy", "📜 Privacy"),
            value=f"[trynexus.de/privacy](https://trynexus.de/{site_locale}/privacy/)",
            inline=True,
        )
        embed.add_field(
            name=lang.get("info_terms", "📋 Terms"),
            value=f"[trynexus.de/terms](https://trynexus.de/{site_locale}/terms/)",
            inline=True,
        )
        embed.set_footer(text=NEXUS_FOOTER)
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Help(bot))