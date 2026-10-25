import logging
import discord
from discord.ext import commands, tasks
from datetime import datetime, timezone
from cogs.setup import SetupView, RestoreView
from config import NEXUS_COLOR
from database import db_settings
from systems.premium import grant_early_bird_premium
from translations import TRANSLATIONS, LOCALE_MAP

log = logging.getLogger(__name__)

GUILD_RETENTION_DAYS = 7

class GuildEvents(commands.Cog):
    """Lebenszyklus des Bots auf Servern: Beitritt, Austritt und Aufräumen alter Guilds."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.cleanup_old_guilds.start()

    async def cog_unload(self):
        self.cleanup_old_guilds.cancel()

    # ─── Loop ───────────────────────────────────────────────────────────

    @tasks.loop(hours=3)
    async def cleanup_old_guilds(self):
        try:
            await db_settings.delete_old_guilds(days=GUILD_RETENTION_DAYS)
        except Exception:
            log.exception("[GUILD-CLEANUP] Fehler beim Löschen alter Guilds.")

    @cleanup_old_guilds.before_loop
    async def _before_cleanup_old_guilds(self):
        await self.bot.wait_until_ready()

    # ─── Events ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        locale = LOCALE_MAP.get(str(guild.preferred_locale), "en")
        lang_dict = TRANSLATIONS.get(locale, TRANSLATIONS["en"])

        # Nickname setzen (Fehler abfangen, falls Permissions fehlen)
        try:
            await guild.me.edit(nick="Nexus")
        except discord.Forbidden:
            log.warning("Konnte Nickname auf Guild '%s' nicht ändern (Rechte fehlen).", guild.name)

        try:
            settings = await db_settings.get_guild(guild.id)
        except Exception as e:
            log.error("Fehler beim Initialisieren von Guild %s in der DB: %s", guild.id, e, exc_info=True)
            return

        embed_title = lang_dict.get("setup_hello_title")
        embed_desc = lang_dict.get("setup_hello_description")
        chosen_view_class = SetupView
        grant_early_bird = False

        if settings and settings["left_at"]:
            left_at = settings["left_at"].replace(tzinfo=timezone.utc)
            days_gone = (datetime.now(timezone.utc) - left_at).days

            if days_gone < GUILD_RETENTION_DAYS:
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
            # Vergibt nur bis EARLY_BIRD_END und überschreibt nie ein längeres/aktives Premium
            await grant_early_bird_premium(self.bot, guild.id)
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
                if chosen_view_class is RestoreView:
                    setup_view = RestoreView(guild.owner_id, lang_dict, setup_channel=target_channel)
                else:
                    setup_view = SetupView(guild.owner_id, lang_dict, guild.id, setup_channel=target_channel)
                await target_channel.send(embed=embed, view=setup_view)
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
                        owner = await self.bot.fetch_user(guild.owner_id)
                    except (discord.NotFound, discord.HTTPException):
                        owner = None

            if owner:
                dm_embed = discord.Embed(
                    title=f"👋 Nexus — {guild.name}",
                    description=lang_dict.get(
                        "setup_no_channel_dm",
                        "Thanks for inviting me to **{guild}**!\n\n"
                        "⚠️ I couldn't **post a welcome message in any channel** "
                        "because I'm missing the `View Channel`, `Send Messages`, or `Embed Links` permissions.\n\n"
                        "Please grant me the required permissions on your server and run `/setup` to get started.",
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

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        try:
            await db_settings.update_guild(guild.id, left_at=datetime.now(timezone.utc))
            log.info("Nexus wurde von %s entfernt.", guild.name)
        except Exception:
            log.exception("left_at für Guild %s (%s) konnte nicht gesetzt werden – wird beim nächsten Sync nachgezogen.", guild.name, guild.id)

async def setup(bot: commands.Bot):
    await bot.add_cog(GuildEvents(bot))