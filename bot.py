import logging
import discord
import asyncio
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from database import init_pool, close_pool
from config import DISCORD_TOKEN, DEV_GUILD_ID, SUPPORT_GUILD_ID, NEXUS_COLOR, INVITE_URL, NEXUS_FOOTER, APPLICATION_ID
from translations import TRANSLATIONS, LOCALE_MAP, COMMAND_LOCALES
from datetime import datetime, timezone
from cachetools import TTLCache
from lists.topgg import post_commands_to_topgg
from lists.discordbotlist import post_commands_to_dbl

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_dm_cooldowns = TTLCache[int, bool](maxsize=2_000, ttl=1800)

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
    """Bot-Kern: Start, Extension-Loader, Command-Sync und globale Fehlerbehandlung.

    Feature-Logik gehört in Cogs:
      - cogs/guild_events.py   — Guild-Beitritt/-Austritt, Aufräumen alter Guilds
      - cogs/premium_events.py — Entitlement-/Subscription-Events, Premium-Abgleich
      - cogs/botlists.py       — Stats-Push an Bot-Listen
    """

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
        self._upload_task: asyncio.Task | None = None
        self.started_at = datetime.now(timezone.utc)

    # ─── Startup ────────────────────────────────────────────────────────

    async def setup_hook(self):
        await init_pool()

        await self.tree.set_translator(NexusTranslator())
        log.info("NexusTranslator erfolgreich für den CommandTree registriert.")
        self.tree.on_error = self.on_application_command_error

        MODULE_DIRS = ("cogs", "integrations")

        failed = []
        for directory in MODULE_DIRS:
            for path in sorted(Path(directory).glob("*.py")):
                if path.name.startswith("_") or path.name == "utils.py":
                    continue
                ext = f"{directory}.{path.stem}"
                try:
                    await self.load_extension(ext)
                    log.info("Geladen: %s", ext)
                except commands.ExtensionError:
                    log.exception("Fehler beim Laden von %s", ext)
                    failed.append(ext)

        if failed:
            log.error("%d Extension(s) NICHT geladen: %s", len(failed), ", ".join(failed))

        for command in self.tree.walk_commands():
            command.guild_only = True
        for guild in (self.support_guild, self.dev_guild):
            for command in self.tree.walk_commands(guild=guild):
                command.guild_only = True
        log.info("Alle Commands als guild_only markiert (nicht sichtbar/nutzbar in DMs).")

    async def on_ready(self):
        if not self.is_ready():
            return

        log.info("Nexus ist online als %s (Shards aktiv: %s)", self.user, self.shard_count)

        # Reconnect-Schutz: Alles darunter darf strikt nur EINMAL beim echten Kaltstart laufen
        if self._initial_sync_done:
            log.info("Reconnect erkannt — Sync und API-Uploads übersprungen.")
            return
        self._initial_sync_done = True

        # Command-Sync mit resilientem Fehler-Handling
        targets = [None] + [g for g in (self.support_guild, self.dev_guild) if g]
        for target in targets:
            name = "global" if target is None else str(target.id)
            try:
                await self.tree.sync(guild=target)
                log.info("Command-Sync (%s) ausgeführt.", name)
            except discord.HTTPException as e:
                if e.status == 429:
                    log.warning("Rate Limited beim Sync (%s) — überspringe.", name)
                else:
                    log.exception("Fehler beim Command-Sync (%s):", name)

        # Externe Uploads völlig entkoppelt im Hintergrund feuern ("Fire & Forget")
        self._upload_task = asyncio.create_task(self._upload_commands_background())

    async def _upload_commands_background(self):
        """Läuft asynchron im Hintergrund, blockiert den Bot-Start nicht."""
        # Kurz warten, damit Discord-intern alles stabil steht
        await asyncio.sleep(5)

        try:
            await post_commands_to_topgg(self)
            log.info("Top.gg Commands erfolgreich hochgeladen.")
        except Exception:
            log.exception("Fehler beim Upload der Commands zu Top.gg (nicht kritisch)")

        try:
            await post_commands_to_dbl(self)
            log.info("DiscordBotList Commands erfolgreich hochgeladen.")
        except Exception:
            log.exception("Fehler beim Upload der Commands zu DiscordBotList (nicht kritisch)")

    # ─── DMs ────────────────────────────────────────────────────────────

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

    # ─── Fehlerbehandlung ───────────────────────────────────────────────

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

    # ─── Shutdown ───────────────────────────────────────────────────────

    async def close(self):
        await close_pool()
        await super().close()

bot = Nexus()
bot.run(DISCORD_TOKEN)