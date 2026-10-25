import logging
import discord
from datetime import datetime, timezone
from cachetools import TTLCache
from discord.ext import commands, tasks

from systems.premium import (
    remove_premium, sync_entitlements, set_premium_from_discord,
    apply_subscription_update, describe_subscription,
)

log = logging.getLogger(__name__)

class PremiumEvents(commands.Cog):
    """Discord-Store-Abos: Entitlement-/Subscription-Events und der tägliche Abgleich."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._entitlement_dedup = TTLCache[int, bool](maxsize=1_000, ttl=60)
        self._subscription_dedup = TTLCache[int, bool](maxsize=1_000, ttl=60)

    async def cog_load(self):
        self.sync_premium_entitlements.start()

    async def cog_unload(self):
        self.sync_premium_entitlements.cancel()

    # ─── Loop ───────────────────────────────────────────────────────────

    @tasks.loop(hours=24)
    async def sync_premium_entitlements(self):
        try:
            removed = await sync_entitlements(self.bot)
            if removed:
                log.info("[PREMIUM-SYNC] %s Guilds mit abgelaufenem Premium bereinigt.", removed)
        except Exception:
            log.exception("[PREMIUM-SYNC] Fehler beim täglichen Entitlement-Abgleich.")

    @sync_premium_entitlements.before_loop
    async def _before_sync_premium_entitlements(self):
        await self.bot.wait_until_ready()

    # ─── Entitlements ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_entitlement_create(self, entitlement: discord.Entitlement):
        """Wird aufgerufen, wenn jemand ein Premium-Abo im Discord Store kauft."""
        if entitlement.id in self._entitlement_dedup:
            return
        self._entitlement_dedup[entitlement.id] = True

        if entitlement.guild_id is None:
            log.warning("Ein User hat ein User-Abo gekauft. Nexus unterstützt aktuell nur Server-Premium.")
            return

        log.info("🎉 Neues Discord-Entitlement erstellt für Guild ID: %s (Entitlement-ID: %s)", entitlement.guild_id, entitlement.id)
        await set_premium_from_discord(self.bot, entitlement.guild_id, entitlement, reason="Discord Store Kauf")

    @commands.Cog.listener()
    async def on_entitlement_delete(self, entitlement: discord.Entitlement):
        """Wird aufgerufen, wenn ein Abo ausläuft, gekündigt oder von Discord rückabgewickelt wird."""
        if entitlement.id in self._entitlement_dedup:
            return
        self._entitlement_dedup[entitlement.id] = True

        if entitlement.guild_id is None:
            return

        log.info("😢 Discord-Entitlement gelöscht/abgelaufen für Guild ID: %s", entitlement.guild_id)
        await remove_premium(self.bot, entitlement.guild_id, reason="Discord Store Abo beendet")

    @commands.Cog.listener()
    async def on_entitlement_update(self, entitlement: discord.Entitlement):
        """Laut Discord-Doku nur, wenn ein Abo-Entitlement tatsächlich endet (ends_at gesetzt)."""
        if entitlement.id in self._entitlement_dedup:
            return
        self._entitlement_dedup[entitlement.id] = True

        if entitlement.guild_id is None:
            return
        ends = entitlement.ends_at
        log.info("💳 Entitlement-Update für Guild %s: id=%s ends_at=%s", entitlement.guild_id, entitlement.id, ends)
        if ends is not None and ends <= datetime.now(timezone.utc):
            await remove_premium(self.bot, entitlement.guild_id, reason="Discord Store Abo ausgelaufen")
        elif ends is not None:
            await set_premium_from_discord(self.bot, entitlement.guild_id, entitlement, reason="Discord Abo aktualisiert")

    # ─── Subscriptions ──────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_subscription_create(self, subscription: discord.Subscription):
        log.info("🧾 Subscription erstellt: %s", describe_subscription(subscription))

    @commands.Cog.listener()
    async def on_subscription_update(self, subscription: discord.Subscription):
        """Kündigung (ENDING bzw. in der Praxis INACTIVE -> Periodenende) und Wiederaufnahme (ACTIVE) landen in premium.expires_at."""
        if subscription.id in self._subscription_dedup:
            return
        self._subscription_dedup[subscription.id] = True

        log.info("🧾 Subscription aktualisiert: %s", describe_subscription(subscription))
        await apply_subscription_update(self.bot, subscription)

    @commands.Cog.listener()
    async def on_subscription_delete(self, subscription: discord.Subscription):
        log.info("🧾 Subscription gelöscht: %s", describe_subscription(subscription))

async def setup(bot: commands.Bot):
    await bot.add_cog(PremiumEvents(bot))