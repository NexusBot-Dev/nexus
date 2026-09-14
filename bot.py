import asyncio
import logging
import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
from database import init_pool, close_pool, db_settings
from config import _read_secret, DEV_GUILD_ID, SUPPORT_GUILD_ID, NEXUS_COLOR, INVITE_URL, NEXUS_FOOTER, APPLICATION_ID
from translations.command_locales import COMMAND_LOCALES
from translations import TRANSLATIONS, LOCALE_MAP
from datetime import datetime, timezone

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_dm_cooldowns: dict[int, datetime] = {}

DEV_MODE = False
UPLOAD_COMMANDS_DBL = False
UPLOAD_COMMANDS_TOPGG = False

class NexusTranslator(app_commands.Translator):
    async def translate(self, string: app_commands.locale_str, locale: discord.Locale, context: app_commands.TranslationContext):
        lang_code = locale.value.split("-")[0]
        lang_dict = COMMAND_LOCALES.get(lang_code, COMMAND_LOCALES["en"])
        return lang_dict.get(string.message, COMMAND_LOCALES["en"].get(string.message, string.message))

class DMResponseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

        self.add_item(discord.ui.Button(
            label="Invite Bot",
            style=discord.ButtonStyle.link,
            url=INVITE_URL,
            emoji="🔗"
        ))
        self.add_item(discord.ui.Button(
            label="Support Server",
            style=discord.ButtonStyle.link,
            url="https://discord.gg/YFCrvBb6t3",
            emoji="💬"
        ))

class Nexus(commands.AutoShardedBot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents, chunk_guilds_at_startup=False, application_id=APPLICATION_ID)
        self.dev_guild = discord.Object(id=DEV_GUILD_ID)
        self.support_guild = discord.Object(id=SUPPORT_GUILD_ID)
        self._initial_sync_done = False

    async def setup_hook(self):
        await init_pool()

        await self.tree.set_translator(NexusTranslator())
        log.info("NexusTranslator erfolgreich für den CommandTree registriert.")
        self.tree.on_error = self.on_application_command_error
        from cogs.announcements import AnnouncementDisableView
        self.add_view(AnnouncementDisableView())

        for filename in os.listdir("./cogs"):
            if filename.endswith(".py") and filename not in ("__init__.py", "utils.py"):
                await self.load_extension(f"cogs.{filename[:-3]}")
                log.info("Cog geladen: %s", filename)

        for filename in os.listdir("./integrations"):
            if filename.endswith(".py") and filename != "__init__.py":
                await self.load_extension(f"integrations.{filename[:-3]}")
                log.info("Integration geladen: %s", filename)

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is not None:
            return

        user_id = message.author.id
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        last_interaction = _dm_cooldowns.get(user_id)
        if last_interaction and (now - last_interaction).total_seconds() < 60:
            return
        _dm_cooldowns[user_id] = now

        embed = discord.Embed(
            title="🤖 Nexus Support",
            description=(
                "Sadly I am just a bot and can't reply to you directly.\n\n"
                "If you need assistance or want to invite me to your server, please use the buttons below:"
            ),
            color=NEXUS_COLOR
        )
        embed.set_footer(text=NEXUS_FOOTER)
        try:
            await message.channel.send(embed=embed, view=DMResponseView())
        except discord.Forbidden:
            pass
        return

    @tasks.loop(hours=3)
    async def cleanup_old_guilds(self):
        await db_settings.delete_old_guilds(days=7)

    async def on_ready(self):
        if not self.is_ready():
            return
        if not self.cleanup_old_guilds.is_running():
            self.cleanup_old_guilds.start()

        log.info("Nexus ist online als %s (Shards aktiv: %s)", self.user, self.shard_count)
        if UPLOAD_COMMANDS_TOPGG:
            topgg_token = _read_secret("TOPGG_TOKEN_FILE", "/run/secrets/topgg_token")
            if topgg_token:
                from lists.topgg_stats import post_commands_to_topgg
                await post_commands_to_topgg(self, topgg_token)
        if UPLOAD_COMMANDS_DBL:
            dbl_token = _read_secret("DBL_TOKEN_FILE", "/run/secrets/dbl_token")
            if dbl_token:
                from lists.discordbotlist import post_commands_to_dbl
                await post_commands_to_dbl(self, dbl_token)

        if self._initial_sync_done:
            log.info("Reconnect erkannt — Sync übersprungen (Commands bereits aktuell).")
            return

        if DEV_MODE:
            self.tree.copy_global_to(guild=self.dev_guild)
            try:
                await self.tree.sync(guild=self.dev_guild)
                log.info("Dev-Mode: Commands für Dev-Guild synchronisiert.")
            except discord.HTTPException as e:
                if e.status == 429:
                    log.warning("Rate Limited beim Dev-Sync — überspringe.")
                else:
                    raise
        else:
            try:
                await self.tree.sync()
                log.info("Globaler Command-Sync ausgeführt.")
            except discord.HTTPException as e:
                if e.status == 429:
                    log.warning("Rate Limited beim globalen Sync — Commands bereits aktuell, überspringe.")
                else:
                    raise

            try:
                await self.tree.sync(guild=self.support_guild)
                log.info("Support-Guild-Commands synchronisiert.")
            except discord.HTTPException as e:
                if e.status == 429:
                    log.warning("Rate Limited beim Support-Guild-Sync — überspringe.")
                else:
                    raise

        self._initial_sync_done = True
        bot.started_at = datetime.now(timezone.utc)

    async def on_guild_join(self, guild: discord.Guild):
        locale = LOCALE_MAP.get(str(guild.preferred_locale), "en")
        lang_dict = TRANSLATIONS.get(locale, TRANSLATIONS["en"])

        # Nickname setzen (Fehler abfangen, falls Permissions fehlen)
        try:
            await guild.me.edit(nick="Nexus")
        except discord.Forbidden:
            log.warning("Konnte Nickname auf Guild '%s' nicht ändern (Rechte fehlen).", guild.name)

        from cogs.setup import SetupView, RestoreView
        settings = await db_settings.get_guild(guild.id)

        embed_title = lang_dict.get("setup_hello_title")
        embed_desc = lang_dict.get("setup_hello_description")
        chosen_view_class = SetupView
        grant_early_bird = False
        from datetime import datetime, timezone, timedelta

        if settings and settings["left_at"]:
            left_at = settings["left_at"].replace(tzinfo=timezone.utc)
            days_gone = (datetime.now(timezone.utc) - left_at).days

            if days_gone < 7:
                # Reaktivieren: Altes Setup behalten
                await db_settings.update_guild(guild.id, left_at=None)
                embed_title = lang_dict.get("setup_welcome_back_title", "👋 Welcome back!")
                embed_desc = lang_dict.get(
                    "setup_welcome_back_desc",
                    "Nexus was already on this server.\nWould you like to restore your old settings?"
                )
                chosen_view_class = RestoreView
            else:
                await db_settings.reset_guild(guild.id)
                grant_early_bird = True
        else:
            # Komplett neu
            await db_settings.get_or_create_guild(guild.id)
            grant_early_bird = True

        if grant_early_bird:
            from systems.premium import set_premium_from_discord
            expires_at=datetime.now(timezone.utc) + timedelta(days=90)
            await set_premium_from_discord(self, guild.id, entitlement=None, reason="Early-Bird 90 Days Gift", expires_at=expires_at)

        # Embed final zusammenbauen
        embed = discord.Embed(
            title=embed_title,
            description=embed_desc,
            color=NEXUS_COLOR,
        )
        embed.set_footer(text="Nexus • Setup")

        target_channel = guild.system_channel
        if not target_channel or not target_channel.permissions_for(guild.me).send_messages:
            target_channel = next(
                (ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages),
                None
            )

        if target_channel:
            try:
                await target_channel.send(
                    embed=embed,
                    view=chosen_view_class(guild.owner_id, lang_dict, target_channel),
                )
            except discord.HTTPException as e:
                log.error("Fehler beim Senden der Setup-Nachricht auf Guild %s: %s", guild.name, e)
        else:
            log.warning("Konnte keine Setup-Nachricht auf Guild %s senden (kein Textkanal beschreibbar).", guild.name)

        log.info("Nexus ist %s beigetreten — Guild-Settings angelegt.", guild.name)

    async def on_guild_remove(self, guild: discord.Guild):
       from datetime import datetime, timezone
       await db_settings.update_guild(guild.id, left_at=datetime.now(timezone.utc))
       log.info("Nexus wurde von %s entfernt.", guild.name)

    async def on_application_command_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ):
        if isinstance(error, app_commands.CheckFailure) and not isinstance(error, app_commands.MissingPermissions):
            return

        locale = LOCALE_MAP.get(str(interaction.guild.preferred_locale), "en")
        lang_dict = TRANSLATIONS.get(locale, TRANSLATIONS["en"])

        log.exception("Unbehandelter Command-Fehler:", exc_info=error)
        try:
            await interaction.response.send_message(lang_dict.get("error_occurred", "An error occurred: {error}").format(error=error), ephemeral=True)
        except discord.InteractionResponded:
            pass

    async def on_entitlement_create(self, entitlement: discord.Entitlement):
        """Wird aufgerufen, wenn jemand ein Premium-Abo im Discord Store kauft."""
        if entitlement.guild_id is None:
            log.warning("Ein User hat ein User-Abo gekauft. Nexus unterstützt aktuell nur Server-Premium.")
            return

        from systems.premium import set_premium_from_discord

        log.info("🎉 Neues Discord-Entitlement erstellt für Guild ID: %s", entitlement.guild_id)

        await set_premium_from_discord(self, entitlement.guild_id, entitlement, reason="Discord Store Kauf")

    async def on_entitlement_delete(self, entitlement: discord.Entitlement):
        """Wird aufgerufen, wenn ein Abo ausläuft, gekündigt oder von Discord rückabgewickelt wird."""
        if entitlement.guild_id is None:
            return

        from systems.premium import remove_premium

        log.info("😢 Discord-Entitlement gelöscht/abgelaufen für Guild ID: %s", entitlement.guild_id)
        await remove_premium(self, entitlement.guild_id, reason="Discord Store Abo beendet")

    async def close(self):
        await close_pool()
        await super().close()

bot = Nexus()
bot.run(_read_secret("DISCORD_TOKEN_FILE", "/run/secrets/discord_token"))