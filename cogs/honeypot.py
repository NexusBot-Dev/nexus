import asyncio
import logging
import discord
import time
from datetime import timedelta
from discord.ext import commands, tasks
from database import db_settings, db_honeypot
from cogs.utils import get_lang
from systems.hierarchy import can_act_on_member

log = logging.getLogger(__name__)

# ─── Honeypot ───────────────────────────────────────────────────────────────
HONEYPOT_COOLDOWN_SECONDS = 60
COUNTER_UPDATE_DELAY = 5
_honeypot_seen: dict[tuple[int, int], float] = {}
_counter_pending: set[int] = set()

# ─── Honeypot Helpers ────────────────────────────────────────────────────────

def _slugify_channel_name(name: str) -> str:
    slug = name.strip().lower().replace(" ", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    slug = slug.strip("-") or "honeypot"
    return slug[:90]

async def _kick_member(guild: discord.Guild, member: discord.Member, reason: str) -> bool:
    try:
        await guild.kick(member, reason=reason)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False

async def cleanup_honeypot(guild: discord.Guild, honeypot_channel_id: int | None) -> None:
    if honeypot_channel_id is None:
        return
    channel = guild.get_channel(honeypot_channel_id)
    if channel is not None:
        try:
            await channel.delete(reason="[Nexus Honeypot] Deaktiviert")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.warning("[HONEYPOT] Konnte Honeypot-Channel %s auf %s nicht löschen.", honeypot_channel_id, guild.name)
    await db_settings.update_guild(guild.id, honeypot_channel_id=None)
    await db_honeypot.update_honeypot_settings(guild.id, message_id=None)

async def _send_honeypot_log(
    bot,
    guild: discord.Guild,
    lang: dict,
    member: discord.Member,
    message_content: str,
    timeout_applied: bool,
    days: int,
    total: int,
):
    settings = await db_settings.get_guild(guild.id)
    if settings is None or not settings.get("log_channel_id"):
        return
    channel = bot.get_channel(settings["log_channel_id"])
    if channel is None:
        return

    embed = discord.Embed(
        title=lang.get("honeypot_log_title", "🍯 Honeypot Triggered"),
        description=lang.get(
            "honeypot_log_desc",
            "{user} posted in the honeypot channel.",
        ).format(user=member.mention, days=days),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name=lang.get("honeypot_log_message", "💬 Message"),
        value=(message_content[:1000] or "*(leer/Anhang)*"),
        inline=False,
    )
    status = lang.get("honeypot_log_timeout_success", "{days} day timeout applied").format(days=days) if timeout_applied else lang.get("honeypot_log_timeout_failed", "Timeout failed (Hierarchy/Permissions?)")
    embed.add_field(name=lang.get("honeypot_log_status", "Status"), value=status, inline=False)
    embed.set_footer(text=lang.get("honeypot_log_footer", "Nexus • Honeypot • Catch #{count}").format(count=f"{total:,}"))

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning("[HONEYPOT] Keine Berechtigung, Log-Channel %s zu schreiben.", channel)
    except discord.HTTPException:
        log.exception("[HONEYPOT] Fehler beim Schreiben in Log-Channel %s.", channel)

async def _update_counter(bot, guild: discord.Guild, channel_id: int, lang: dict) -> None:
    """Schreibt den Fang-Zähler in den Footer des Warn-Embeds im Honeypot-Channel.
    Höchstens eine Aktualisierung alle COUNTER_UPDATE_DELAY Sekunden pro Server (Edit-Limits bei Spam-Wellen)."""
    if guild.id in _counter_pending:
        return
    _counter_pending.add(guild.id)
    try:
        await asyncio.sleep(COUNTER_UPDATE_DELAY)
        channel = guild.get_channel(channel_id)
        if channel is None:
            return

        hp = await db_honeypot.get_honeypot_settings(guild.id)
        message = None
        if hp.get("message_id"):
            try:
                message = await channel.fetch_message(hp["message_id"])
            except discord.NotFound:
                message = None

        if message is None:
            async for candidate in channel.history(limit=10, oldest_first=True):
                if candidate.author.id == bot.user.id and candidate.embeds:
                    message = candidate
                    await db_honeypot.update_honeypot_settings(guild.id, message_id=candidate.id)
                    break
        if message is None or not message.embeds:
            return

        embed = message.embeds[0].copy()
        embed.set_footer(text=lang.get("honeypot_counter", "Nexus • Honeypot • 🍯 {count} caught").format(
            count=f"{hp['catches']:,}",
        ))
        await message.edit(embed=embed)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        log.warning("[HONEYPOT] Zähler auf %s konnte nicht aktualisiert werden.", guild.name)
    except Exception:
        log.exception("[HONEYPOT] Unerwarteter Fehler beim Aktualisieren des Zählers auf %s.", guild.name)
    finally:
        _counter_pending.discard(guild.id)

# ─── Cog ─────────────────────────────────────────────────────────────────────

class Honeypot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._tasks: set[asyncio.Task] = set()
        self._cleanup_seen.start()

    def cog_unload(self):
        self._cleanup_seen.cancel()

    @tasks.loop(minutes=10)
    async def _cleanup_seen(self):
        try:
            mono = time.monotonic()
            for k in [k for k, t in _honeypot_seen.items() if mono - t >= HONEYPOT_COOLDOWN_SECONDS]:
                del _honeypot_seen[k]
        except Exception:
            log.exception("[HONEYPOT-CLEANUP] Fehler beim Bereinigen des Cooldown-Caches.")

    @_cleanup_seen.before_loop
    async def _before_cleanup_seen(self):
        await self.bot.wait_until_ready()

    # ─── Honeypot Listener ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.id == self.bot.user.id:
            return

        settings = await db_settings.get_guild(message.guild.id)
        if settings is None or not settings.get("honeypot_channel_id"):
            return
        if message.channel.id != settings["honeypot_channel_id"]:
            return

        member = message.author
        if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
            return

        key = (message.guild.id, member.id)
        now = time.monotonic()
        first_hit = now - _honeypot_seen.get(key, -HONEYPOT_COOLDOWN_SECONDS) >= HONEYPOT_COOLDOWN_SECONDS
        if first_hit:
            _honeypot_seen[key] = now

        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

        if not first_hit:
            return

        total = await db_honeypot.increment_catches(message.guild.id)
        hp = await db_honeypot.get_honeypot_settings(message.guild.id)
        days = hp["timeout_days"]

        action_applied = False
        if member.bot:
            check = can_act_on_member(message.guild, member, perms=("kick_members",))
            if check:
                action_applied = await _kick_member(
                    message.guild,
                    member,
                    reason="[Nexus Honeypot] Bot hat im Honeypot-Channel gepostet"
                )
            if action_applied:
                log.info("[HONEYPOT] Bot %s auf %s gekickt.", member, message.guild.name)
            else:
                log.warning("[HONEYPOT] Kick für Bot %s auf %s nicht möglich (%s).", member, message.guild.name, check.issue.value)
        else:
            check = can_act_on_member(message.guild, member, perms=("moderate_members",), is_timeout=True)
            if not check:
                log.warning("[HONEYPOT] Timeout für %s auf %s nicht möglich (%s).", member, message.guild.name, check.issue.value)
            else:
                try:
                    until = discord.utils.utcnow() + timedelta(days=days)
                    await member.timeout(until, reason="[Nexus Honeypot] Nachricht im Honeypot-Channel gepostet")
                    action_applied = True
                    log.info("[HONEYPOT] %s auf %s für %s Tage getimed out.", member, message.guild.name, days)
                except discord.NotFound:
                    log.info("[HONEYPOT] %s war auf %s schon nicht mehr auf dem Server — kein Timeout nötig.", member, message.guild.name)
                except discord.Forbidden:
                    log.warning("[HONEYPOT] Timeout für %s auf %s fehlgeschlagen (Hierarchie/Rechte?).", member, message.guild.name)
                except discord.HTTPException:
                    log.exception("[HONEYPOT] Fehler beim Timeout für %s auf %s.", member, message.guild.name)

        lang = await get_lang(message.guild.id)
        await _send_honeypot_log(self.bot, message.guild, lang, member, message.content, action_applied, days, total)

        task = asyncio.create_task(_update_counter(self.bot, message.guild, message.channel.id, lang))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

async def setup(bot):
    await bot.add_cog(Honeypot(bot))