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
from datetime import datetime, timezone, timedelta
from cachetools import TTLCache
from cogs.setup import SetupView, RestoreView
from systems.premium import remove_premium, sync_entitlements, set_premium_from_discord

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_dm_cooldowns: TTLCache[int, bool] = TTLCache(maxsize=10_000, ttl=1800)

DEV_MODE = False
UPLOAD_COMMANDS_DBL = False
UPLOAD_COMMANDS_TOPGG = True

class NexusTranslator(app_commands.Translator):
    async def translate(self, string: app_commands.locale_str, locale: discord.Locale, context: app_commands.TranslationContext):
        lang_code = locale.value.split("-")[0]
        lang_dict = COMMAND_LOCALES.get(lang_code, COMMAND_LOCALES["en"])
        return lang_dict.get(string.message, COMMAND_LOCALES["en"].get(string.message, string.message))

class NexusTree(app_commands.CommandTree):
    """Bot-weite Absicherung: verhindert die Ausführung JEDES Commands in DMs,
    unabhängig davon, ob der einzelne Command explizit als guild_only markiert wurde.
    Läuft vor jedem einzelnen Command-Aufruf, für alle Cogs."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Nexus-Commands funktionieren nur auf Servern, nicht in DMs.",
                ephemeral=True,
            )
            return False
        return True

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

        super().__init__(
            command_prefix="!",
            intents=intents,
            chunk_guilds_at_startup=False,
            application_id=APPLICATION_ID,
            tree_cls=NexusTree,
        )
        self.dev_guild = discord.Object(id=DEV_GUILD_ID)
        self.support_guild = discord.Object(id=SUPPORT_GUILD_ID)
        self._initial_sync_done = False
        self.started_at = datetime.now(timezone.utc)

    # ─── Loop Tasks ─────────────────────────────────────────────────────

    @tasks.loop(hours=3)
    async def cleanup_old_guilds(self):
        await db_settings.delete_old_guilds(days=7)

    @tasks.loop(hours=24)
    async def sync_premium_entitlements(self):
        try:
            removed = await sync_entitlements(self)
            if removed:
                log.info("[PREMIUM-SYNC] %s Guilds mit abgelaufenem Premium bereinigt.", removed)
        except Exception:
            log.exception("[PREMIUM-SYNC] Fehler beim täglichen Entitlement-Abgleich.")

    # ─── Bot Tasks ──────────────────────────────────────────────────────

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

        for command in self.tree.walk_commands():
            command.guild_only = True
        for command in self.tree.walk_commands(guild=self.support_guild):
            command.guild_only = True
        log.info("Alle Commands als guild_only markiert (nicht sichtbar/nutzbar in DMs).")

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is not None:
            return

        user_id = message.author.id
        if user_id in _dm_cooldowns:
            return

        _dm_cooldowns[user_id] = True

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

    async def on_ready(self):
        if not self.is_ready():
            return

        # 1. Background-Tasks idempotenten Starten (laufen dauerhaft im Hintergrund)
        if not self.cleanup_old_guilds.is_running():
            self.cleanup_old_guilds.start()
        if not self.sync_premium_entitlements.is_running():
            self.sync_premium_entitlements.start()

        log.info("Nexus ist online als %s (Shards aktiv: %s)", self.user, self.shard_count)

        # 2. Reconnect-Schutz: Alles darunter darf strikt nur EINMAL beim echten Kaltstart laufen
        if self._initial_sync_done:
            log.info("Reconnect erkannt — Sync und API-Uploads übersprungen.")
            return

        # 3. Externe API-Uploads nur beim initialen Start ausführen
        if UPLOAD_COMMANDS_TOPGG:
            topgg_token = _read_secret("TOPGG_TOKEN_FILE", "/run/secrets/topgg_token")
            if topgg_token:
                try:
                    from lists.topgg_stats import post_commands_to_topgg
                    await post_commands_to_topgg(self, topgg_token)
                except Exception:
                    log.exception("Fehler beim Upload der Commands zu Top.gg")

        if UPLOAD_COMMANDS_DBL:
            dbl_token = _read_secret("DBL_TOKEN_FILE", "/run/secrets/dbl_token")
            if dbl_token:
                try:
                    from lists.discordbotlist import post_commands_to_dbl
                    await post_commands_to_dbl(self, dbl_token)
                except Exception:
                    log.exception("Fehler beim Upload der Commands zu DiscordBotList")

        # 4. Command-Sync mit resilientem Fehler-Handling
        if DEV_MODE:
            self.tree.copy_global_to(guild=self.dev_guild)
            try:
                await self.tree.sync(guild=self.dev_guild)
                log.info("Dev-Mode: Commands für Dev-Guild synchronisiert.")
            except discord.HTTPException as e:
                if e.status == 429:
                    log.warning("Rate Limited beim Dev-Sync — überspringe.")
                else:
                    log.exception("Fehler beim Dev-Guild Command-Sync:")
        else:
            try:
                await self.tree.sync()
                log.info("Globaler Command-Sync ausgeführt.")
            except discord.HTTPException as e:
                if e.status == 429:
                    log.warning("Rate Limited beim globalen Sync — Commands bereits aktuell, überspringe.")
                else:
                    log.exception("Fehler beim globalen Command-Sync:")

            try:
                await self.tree.sync(guild=self.support_guild)
                log.info("Support-Guild-Commands synchronisiert.")
            except discord.HTTPException as e:
                if e.status == 429:
                    log.warning("Rate Limited beim Support-Guild-Sync — überspringe.")
                else:
                    log.exception("Fehler beim Support-Guild Command-Sync:")

        self._initial_sync_done = True

    async def on_guild_join(self, guild: discord.Guild):
        locale = LOCALE_MAP.get(str(guild.preferred_locale), "en")
        lang_dict = TRANSLATIONS.get(locale, TRANSLATIONS["en"])

        # Nickname setzen (Fehler abfangen, falls Permissions fehlen)
        try:
            await guild.me.edit(nick="Nexus")
        except discord.Forbidden:
            log.warning("Konnte Nickname auf Guild '%s' nicht ändern (Rechte fehlen).", guild.name)

        settings = await db_settings.get_guild(guild.id)

        embed_title = lang_dict.get("setup_hello_title")
        embed_desc = lang_dict.get("setup_hello_description")
        chosen_view_class = SetupView
        grant_early_bird = False

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
            expires_at = datetime.now(timezone.utc) + timedelta(days=90)
            await set_premium_from_discord(self, guild.id, entitlement=None, reason="Early-Bird 90 Days Gift", expires_at=expires_at, source="gift")
        # Embed final zusammenbauen
        embed = discord.Embed(
            title=embed_title,
            description=embed_desc,
            color=NEXUS_COLOR,
        )
        embed.set_footer(text="Nexus • Setup")

        target_channel = guild.system_channel
        if not target_channel or not (target_channel.permissions_for(guild.me).send_messages and target_channel.permissions_for(guild.me).embed_links):
            target_channel = next(
                (
                    ch for ch in guild.text_channels 
                    if ch.permissions_for(guild.me).send_messages and ch.permissions_for(guild.me).embed_links
                ),
                None
            )

        message_sent = False

        if target_channel:
            try:
                await target_channel.send(
                    embed=embed,
                    view=chosen_view_class(guild.owner_id, lang_dict, target_channel),
                )
                message_sent = True
            except discord.HTTPException as e:
                log.error("Fehler beim Senden der Setup-Nachricht auf Guild %s: %s", guild.name, e)

        if not message_sent:
            log.warning("Kein beschreibbarer Textkanal auf '%s' gefunden — versuche Fallback-DM an Owner.", guild.name)

            owner = guild.owner
            if owner is None:
                try:
                    owner = await guild.fetch_member(guild.owner_id)
                except (discord.NotFound, discord.HTTPException):
                    try:
                        owner = await self.fetch_user(guild.owner_id)
                    except (discord.NotFound, discord.HTTPException):
                        owner = None

            if owner:
                dm_embed = discord.Embed(
                    title=f"👋 Nexus — {guild.name}",
                    description=lang_dict.get(
                        "setup_no_channel_dm",
                        "Danke, dass du mich auf **{guild}** eingeladen hast!\n\n"
                        "⚠️ Ich konnte **in keinem Kanal eine Begrüßungsnachricht posten**, "
                        "weil mir die Berechtigungen `Kanal anzeigen`, `Nachrichten senden` oder `Links einbetten` fehlen.\n\n"
                        "Bitte weise mir auf dem Server entsprechende Rechte zu und nutze `/settings`, um mich einzurichten."
                    ).format(guild=guild.name),
                    color=NEXUS_COLOR,
                )
                dm_embed.set_footer(text="Nexus • Setup")

                try:
                    await owner.send(embed=dm_embed)
                    log.info("Setup-Hinweis erfolgreich per DM an Owner %s (%s) für Guild '%s' gesendet.", owner, owner.id, guild.name)
                except discord.Forbidden:
                    log.warning("Konnte Owner %s (%s) keine DM schicken (DMs blockiert/deaktiviert).", owner, owner.id)
                except discord.HTTPException as e:
                    log.error("Fehler beim Senden der DM an Owner %s: %s", owner, e)

        log.info("Nexus ist %s beigetreten — Guild-Settings angelegt.", guild.name)

    async def on_guild_remove(self, guild: discord.Guild):
        await db_settings.update_guild(guild.id, left_at=datetime.now(timezone.utc))
        log.info("Nexus wurde von %s entfernt.", guild.name)

    async def on_application_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        original = getattr(error, "original", error)

        # 1. Cog hat eigenen Handler → der hat schon geantwortet, hier nur loggen
        cog = interaction.command.binding if interaction.command else None
        if isinstance(cog, commands.Cog) and cog.has_app_command_error_handler():
            if not isinstance(original, discord.Forbidden) and not isinstance(error, app_commands.CheckFailure):
                log.exception("Command-Fehler in %s:", cog.qualified_name, exc_info=error)
            return

        locale = LOCALE_MAP.get(str(interaction.guild.preferred_locale), "en") if interaction.guild else "en"
        lang = TRANSLATIONS.get(locale, TRANSLATIONS["en"])

        # 2. User fehlt ein Recht → freundliche Meldung
        if isinstance(error, app_commands.MissingPermissions):
            msg = lang.get("no_permission", "❌ You don't have permission to use this command.")

        # 3. Andere Checks (z.B. module_required) antworten selbst → still
        elif isinstance(error, app_commands.CheckFailure):
            return

        # 4. Nexus fehlt ein Recht / Hierarchie
        elif isinstance(original, discord.Forbidden):
            log.warning(
                "Forbidden in /%s auf %s: %s",
                interaction.command.qualified_name if interaction.command else "?",
                interaction.guild_id, original.text,
            )
            msg = lang.get(
                "error_forbidden",
                "❌ I'm missing permissions or my role is too low for this. "
                "Run `/diagnose` to see what's wrong.",
            )

        # 5. Alles andere
        else:
            log.exception("Unbehandelter Command-Fehler:", exc_info=error)
            msg = lang.get("error_generic", "❌ Something went wrong. Please try again later.")

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_entitlement_create(self, entitlement: discord.Entitlement):
        """Wird aufgerufen, wenn jemand ein Premium-Abo im Discord Store kauft."""
        if entitlement.guild_id is None:
            log.warning("Ein User hat ein User-Abo gekauft. Nexus unterstützt aktuell nur Server-Premium.")
            return

        log.info("🎉 Neues Discord-Entitlement erstellt für Guild ID: %s", entitlement.guild_id)

        await set_premium_from_discord(self, entitlement.guild_id, entitlement, reason="Discord Store Kauf", source="discord")

    async def on_entitlement_delete(self, entitlement: discord.Entitlement):
        """Wird aufgerufen, wenn ein Abo ausläuft, gekündigt oder von Discord rückabgewickelt wird."""
        if entitlement.guild_id is None:
            return

        log.info("😢 Discord-Entitlement gelöscht/abgelaufen für Guild ID: %s", entitlement.guild_id)
        await remove_premium(self, entitlement.guild_id, reason="Discord Store Abo beendet")

    async def on_entitlement_update(self, entitlement: discord.Entitlement):
        """Wird aufgerufen, wenn ein Abo tatsächlich endet (ends_at erreicht, nach Kündigung)."""
        if entitlement.guild_id is None:
            return

        log.info("💳 Discord-Entitlement beendet (ends_at erreicht) für Guild ID: %s", entitlement.guild_id)
        await remove_premium(self, entitlement.guild_id, reason="Discord Store Abo ausgelaufen")

    async def close(self):
        await close_pool()
        await super().close()

bot = Nexus()
bot.run(_read_secret("DISCORD_TOKEN_FILE", "/run/secrets/discord_token"))