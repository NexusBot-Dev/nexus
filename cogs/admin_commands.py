import logging
import discord
import zoneinfo
import re
from discord import app_commands
from discord.ext import commands
from database import db_settings, db_support
from cogs.utils import toggle_module, CORE_MODULES, get_lang
from config import NEXUS_COLOR, DEV_GUILD_ID
from emojis import NexusEmojis
from translations import TRANSLATIONS
from systems.premium import is_premium
from systems.ui import NexusView, NexusModal

ALL_TIMEZONES = sorted(list(zoneinfo.available_timezones()))
COLOR_BETA_GUILDS = {DEV_GUILD_ID}

log = logging.getLogger(__name__)

def _format_module_name(module_key: str, lang: dict) -> str:
    """Wandelt snake_case in sauberes Title Case um oder nutzt den Key aus den Übersetzungen."""
    clean_title = module_key.replace("_", " ").title()
    return lang.get(f"module_name_{module_key}", clean_title)

class ModuleView(NexusView):
    def __init__(self, bot, guild: discord.Guild, modules: dict, owner_id: int, lang):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=120)
        self.bot      = bot
        self.guild    = guild
        self.modules  = modules
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()
        for module, enabled in self.modules.items():
            if module in CORE_MODULES:
                continue  # immer aktiv — kein Button, stattdessen Hinweis im Embed

            formatted_name = _format_module_name(module, self.lang)
            button = discord.ui.Button(
                label=formatted_name,
                style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
                emoji=discord.PartialEmoji.from_str(NexusEmojis.CHECKMARK) if enabled else "❌",
                custom_id=f"module_{module}",
            )
            button.callback = self._make_callback(module, enabled)
            self.add_item(button)

    def _make_callback(self, module: str, currently_enabled: bool):
        async def callback(interaction: discord.Interaction):
            new_state = not currently_enabled
            success, error = await toggle_module(self.bot, self.guild, module, new_state, self.lang)

            if not success:
                await interaction.response.send_message(error, ephemeral=True)
                return

            self.modules[module] = new_state
            self._update_buttons()
            await interaction.response.edit_message(
                embed=_modules_embed(self.modules, self.lang),
                view=self,
            )
        return callback

def _modules_embed(modules: dict, lang: dict) -> discord.Embed:
    embed = discord.Embed(
        title=lang.get("module_config", "{settings} Manage Modules").format(settings=NexusEmojis.Modules.SETTINGS),
        description=lang.get("module_config_desc", "Activate or deactivate Modules for this Server."),
        color=NEXUS_COLOR,
    )
    core_names = []
    for module, enabled in modules.items():
        if module in CORE_MODULES:
            core_names.append(_format_module_name(module, lang))
            continue
        status = f"{NexusEmojis.CHECKMARK} {lang.get('module_active', 'Active')}" if enabled else f"{lang.get('module_inactive', '❌ Disabled')}"
        embed.add_field(name=_format_module_name(module, lang), value=status, inline=True)

    if core_names:
        embed.add_field(
            name=lang.get("module_core_title", "🔒 Always active"),
            value=", ".join(core_names),
            inline=False,
        )

    embed.set_footer(text="Nexus • Module")
    return embed

class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="module", description=app_commands.locale_str("cmd_module_desc"))
    @app_commands.default_permissions(manage_guild=True)
    async def module(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        settings = await db_settings.get_guild(interaction.guild_id)
        locale = settings.get("locale", "en")
        lang = TRANSLATIONS.get(locale, TRANSLATIONS["en"])
        modules  = settings.get("modules", {})

        view = ModuleView(self.bot, interaction.guild, modules, interaction.user.id, lang)
        await interaction.followup.send(embed=_modules_embed(modules, lang), view=view, ephemeral=True)
        view.bind(interaction)

    @app_commands.command(name="settings", description=app_commands.locale_str("cmd_settings_desc"))
    @app_commands.default_permissions(manage_guild=True)
    async def settings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        settings = await db_settings.get_guild(interaction.guild_id)
        locale = settings.get("locale", "en")
        lang = TRANSLATIONS.get(locale, TRANSLATIONS["en"])

        log_ch     = f"<#{settings['log_channel_id']}>"     if settings and settings["log_channel_id"]     else f"= {lang.get('settings_not_set', 'Not set')}"
        rules_ch   = f"<#{settings['rules_channel_id']}>"   if settings and settings["rules_channel_id"]   else f"= {lang.get('settings_not_set', 'Not set')}"
        welcome_ch = f"<#{settings['welcome_channel_id']}>" if settings and settings["welcome_channel_id"] else f"= {lang.get('settings_not_set', 'Not set')}"
        boost_ch   = f"<#{settings['boost_channel_id']}>"   if settings and settings["boost_channel_id"]   else f"= ↳ {lang.get('settings_not_set', 'Not set')}"

        color = await db_settings.get_color(interaction.guild_id)
        embed = discord.Embed(
            title=lang.get("settings_title", "{settings} Server Settings").format(settings=NexusEmojis.Modules.SETTINGS),
            color=color,
        )
        embed.add_field(name=f"{lang.get('settings_log', '📋 Log-Channel')}",          value=log_ch,     inline=True)
        embed.add_field(name=f"{lang.get('settings_rules', '📜 Rules-Channel')}",      value=rules_ch,   inline=True)
        embed.add_field(name=f"{lang.get('settings_welcome', '👋 Welcome-Channel')}",  value=welcome_ch, inline=True)
        embed.add_field(name=f"{NexusEmojis.Modules.BOOST} {lang.get('settings_boost', '🚀 Boost-Channel')}",      value=boost_ch,   inline=True)
        embed.set_footer(text=f"Nexus • {lang.get("settings", "Settings")}")

        view = SettingsView(interaction.user.id, lang, interaction.guild_id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.bind(interaction)

    # ─── Timezone ───────────────────────────────────────────────────────────

    @app_commands.command(name="timezone", description=app_commands.locale_str("cmd_timezone_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(tz=app_commands.locale_str("cmd_timezone_param_desc"))
    async def timezone_command(self, interaction: discord.Interaction, tz: str):
        lang = await get_lang(interaction.guild_id)
        if tz not in ALL_TIMEZONES:
            await interaction.response.send_message(
                lang.get("timezone_invalid", "❌ **Invalid timezone.** Please select a suggestion from the list (e.g., `Europe/Berlin`)."),
                ephemeral=True
            )
            return

        await db_settings.update_timezone(interaction.guild_id, tz)
        await interaction.response.send_message(
            lang.get("timezone_success", "{nexus_checkmark} Timezone successfully set to `{timezone}`!").format(
                nexus_checkmark=NexusEmojis.CHECKMARK,
                timezone=tz
            ), ephemeral=True
        )

    @timezone_command.autocomplete("tz")
    async def timezone_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        choices = []
        current_lower = current.lower()

        for tz in ALL_TIMEZONES:
            if current_lower in tz.lower():
                if len(choices) < 25:
                    choices.append(app_commands.Choice(name=tz, value=tz))
                else:
                    break

        return choices

    # ─── Support PIN ──────────────────────────────────────────────────────────

    @app_commands.command(name="support-pin", description=app_commands.locale_str("cmd_support_pin_desc"))
    @app_commands.default_permissions(manage_guild=True)
    async def support_pin(self, interaction: discord.Interaction):
        lang = await get_lang(interaction.guild_id)
        pin = await db_support.create_pin(interaction.guild_id)

        embed = discord.Embed(
            title=lang.get("support_pin_title", "🔐 Support-PIN"),
            description=lang.get(
                "support_pin_desc",
                "Share this PIN with Nexus support: **`{pin}`**\nValid for 15 minutes."
            ).format(pin=pin),
            color=NEXUS_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

class SettingsView(NexusView):
    def __init__(self, owner_id: int, lang: dict, guild_id: int):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=120)

        # Garantierte Strings erzwingen (Fallback, falls der Bot-Cache laggt)
        self.select_locale.placeholder = str(lang.get("choose_language", "🌍 Choose Language..."))

        self.change_boost.emoji        = NexusEmojis.Modules.BOOST
        self.change_log.label          = str(lang.get("log_cnl_label", "📋 Change Log-Channel"))
        self.change_rules.label        = str(lang.get("rules_cnl_label", "📜 Change Rules-Channel"))
        self.change_welcome.label      = str(lang.get("welcome_cnl_label", "👋 Change Welcome-Channel"))
        self.change_boost.label        = str(lang.get("boost_cnl_label", "Change Boost-Channel"))
        self.change_color.label        = str(lang.get("color_btn_label", "🎨 Embed color"))

        if guild_id not in COLOR_BETA_GUILDS:
            self.remove_item(self.change_color)

    @discord.ui.select(
        options=[
            discord.SelectOption(label="Deutsch", value="de", emoji="🇩🇪"),
            discord.SelectOption(label="English", value="en", emoji="🇬🇧"),
            discord.SelectOption(label="Español", value="es", emoji="🇪🇸"),
            discord.SelectOption(label="Português", value="pt", emoji="🇧🇷"),
        ],
        row=0
    )
    async def select_locale(self, interaction: discord.Interaction, select: discord.ui.Select):
        locale = select.values[0]
        await db_settings.set_locale(interaction.guild_id, locale)

        msg = {
            "de": "Sprache auf **Deutsch** gesetzt.",
            "en": "Language was set to **English**.",
            "es": "Idioma cambiado a **Español**.",
            "pt": "Idioma alterado para **Português**.",
        }.get(locale, "Language was set to **English**.")

        await interaction.response.send_message(f"{NexusEmojis.CHECKMARK} {msg}", ephemeral=True)

    # row=1: Alle Buttons wandern gemeinsam sauber in die zweite Zeile
    @discord.ui.button(label="Log-Channel ändern", style=discord.ButtonStyle.secondary, row=1)
    async def change_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ChannelSelectView(interaction.user.id, self.lang, "log_channel_id", "Log-Channel")
        await interaction.response.send_message(
            self.lang.get("log_cnl_new", "Choose a new Log-Channel:"),
            view=view,
            ephemeral=True,
        )
        view.bind(interaction)

    @discord.ui.button(label="Regel-Channel ändern", style=discord.ButtonStyle.secondary, row=1)
    async def change_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ChannelSelectView(interaction.user.id, self.lang, "rules_channel_id", "Rules-Channel")
        await interaction.response.send_message(
            self.lang.get("rules_cnl_new", "Choose a new Rules-Channel:"),
            view=view,
            ephemeral=True,
        )
        view.bind(interaction)

    @discord.ui.button(label="Willkommens-Channel ändern", style=discord.ButtonStyle.secondary, row=1)
    async def change_welcome(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ChannelSelectView(interaction.user.id, self.lang, "welcome_channel_id", "Welcome-Channel")
        await interaction.response.send_message(
            self.lang.get("welcome_cnl_new", "Choose a new Welcome-Channel:"),
            view=view,
            ephemeral=True,
        )
        view.bind(interaction)

    @discord.ui.button(label="Boost-Channel ändern", style=discord.ButtonStyle.secondary, row=1)
    async def change_boost(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ChannelSelectView(interaction.user.id, self.lang, "boost_channel_id", "Boost-Channel")
        await interaction.response.send_message(
            self.lang.get("boost_cnl_new", "Choose a new Boost-Channel:"),
            view=view,
            ephemeral=True,
        )
        view.bind(interaction)

    @discord.ui.button(label="Embed color", style=discord.ButtonStyle.secondary, row=1)
    async def change_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = await db_settings.get_guild(interaction.guild_id) or {}
        stored = settings.get("color") # check:ignore
        stored = stored if stored and stored != NEXUS_COLOR else None

        if not await is_premium(interaction.guild_id):
            text = self.lang.get(
                "color_premium_required",
                "⭐ A custom embed color is a Premium feature. See `/premium` for details.",
            )
            if stored:
                text += "\n" + self.lang.get(
                    "color_premium_stored_hint", "Your saved color {color} becomes active again with Premium.",
                ).format(color=f"`#{stored:06X}`")
            await interaction.response.send_message(text, ephemeral=True)
            return

        await interaction.response.send_modal(EmbedColorModal(self.lang, stored))

class EmbedColorModal(NexusModal):
    def __init__(self, lang: dict, current: int | None):
        super().__init__(lang, title=lang.get("color_modal_title", "Embed color"))
        self.color_input = discord.ui.TextInput(
            label=lang.get("color_modal_label", "Hex color (empty = Nexus default)"),
            placeholder="#00F5D4",
            default=f"#{current:06X}" if current else None,
            required=False,
            max_length=7,
        )
        self.add_item(self.color_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.color_input.value.strip().lstrip("#")
        if not raw:
            value = NEXUS_COLOR
        else:
            if len(raw) == 3:
                raw = "".join(c * 2 for c in raw)
            if not re.fullmatch(r"[0-9a-fA-F]{6}", raw) or int(raw, 16) == 0:   # 000000 = "keine Farbe" bei Discord
                await interaction.response.send_message(
                    self.lang.get("color_invalid", "❌ Please enter a valid hex color like `#00F5D4`."),
                    ephemeral=True,
                )
                return
            value = int(raw, 16)

        if not await db_settings.set_color(interaction.guild_id, value):   # prüft Premium erneut
            await interaction.response.send_message(
                self.lang.get("color_premium_required", "⭐ A custom embed color is a Premium feature. See `/premium` for details."),
                ephemeral=True,
            )
            return

        if value == NEXUS_COLOR:
            text = self.lang.get("color_reset", "{nexus_checkmark} Embed color reset to the Nexus default.")
        else:
            text = self.lang.get("color_saved", "{nexus_checkmark} Embed color set to **{color}**. This is how your embeds look now.")
        embed = discord.Embed(
            description=text.format(nexus_checkmark=NexusEmojis.CHECKMARK, color=f"#{value:06X}"),
            color=value,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ChannelSelectView(NexusView):
    def __init__(self, owner_id: int, lang: dict, setting_key: str, label: str):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=60)
        self.setting_key = setting_key
        self.label       = label

        self.select_channel.placeholder = lang.get("setup_placeholder", "📋 Choose an existing Channel...")

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
    )
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = select.values[0]
        await db_settings.update_guild(interaction.guild_id, **{self.setting_key: channel.id})
        self.stop()
        await interaction.response.edit_message(
            content=self.lang.get("channel_switched", "{nexus_checkmark} **{label}** was set to {channel}.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK,
                label=self.label,
                channel=channel.mention
            ),
            view=None,
        )

async def setup(bot):
    await bot.add_cog(AdminCommands(bot))