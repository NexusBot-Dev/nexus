"""Stream-Alert hinzufügen und entfernen — ein Ablauf für Slash-Commands und Dashboard.

Die Aufrufer prüfen vorher ihre Eingaben (Plattform, Name, Kanal, Nachricht) und übersetzen
einen `AlertError` in ihre eigene Meldung; hier passiert alles ab der Limit-Prüfung.
"""
import logging
from dataclasses import dataclass

from database import db_limits, db_stream_alerts
from systems.platform_registry import PLATFORM_HANDLERS
from systems.premium import is_premium
from systems.stream_alert_locks import guild_lock, streamer_lock

log = logging.getLogger(__name__)


class AlertError(Exception):
    """Erwarteter Abbruch. reason: "limit" (limit=), "user_not_found", "subscription_failed",
    "already_monitored" oder "not_monitored"."""

    def __init__(self, reason: str, **params):
        super().__init__(reason)
        self.reason = reason
        self.params = params


@dataclass(frozen=True)
class AddedAlert:
    monitored_channel_id: int
    subscription_id: int
    streamer_name: str   # Anzeigename von der Plattform


async def add_alert(
    guild_id: int, platform: str, login: str, channel_id: int, message: str | None,
) -> AddedAlert:
    """Richtet den Alert ein. `login` ist bereits normalisiert (handler.normalize_input)."""
    handler = PLATFORM_HANDLERS[platform]

    # Gesperrt bis zum Speichern: Limit, gemeinsamer Streamer-Eintrag und Webhook-Abo (siehe stream_alert_locks)
    async with guild_lock(guild_id, platform):
        current_count = await db_stream_alerts.count_subscriptions_for_guild(guild_id, platform)
        limit = await db_limits.get_limit(guild_id, f"stream_alerts_{platform}", await is_premium(guild_id))
        if current_count >= limit:
            raise AlertError("limit", limit=limit)

        user = await handler.resolve_user(login)
        if user is None:
            raise AlertError("user_not_found")

        external_id = user["id"]
        async with streamer_lock(platform, external_id):
            monitored = await db_stream_alerts.get_monitored_channel(platform, external_id)

            if monitored is None:
                sub_ids = {}
                if handler.create_subscriptions:
                    try:
                        sub_ids = await handler.create_subscriptions(external_id)
                    except Exception:
                        log.exception("Subscription-Anlage für %s (%s) fehlgeschlagen.", login, platform)
                        raise AlertError("subscription_failed") from None

                monitored = await db_stream_alerts.get_or_create_monitored_channel(
                    platform, external_id, user["display_name"]
                )
                if sub_ids:
                    await db_stream_alerts.set_eventsub_ids(
                        monitored["id"], sub_ids.get("online_id"), sub_ids.get("offline_id")
                    )
                if handler.on_channel_created:
                    try:
                        await handler.on_channel_created(monitored["id"], user)
                    except Exception:
                        log.exception("on_channel_created-Hook für %s (%s) fehlgeschlagen.", login, platform)
            elif await db_stream_alerts.get_subscription(guild_id, monitored["id"]) is not None:
                raise AlertError("already_monitored")

            subscription_id = await db_stream_alerts.add_subscription(
                guild_id=guild_id, channel_id=channel_id, monitored_channel_id=monitored["id"], message=message,
            )

    log.info("%s-Alert für %s auf Guild %s eingerichtet.", platform, login, guild_id)
    return AddedAlert(monitored["id"], subscription_id, user["display_name"])


async def remove_alert(guild_id: int, monitored: dict) -> None:
    """Entfernt den Alert dieses Servers. War es der letzte Server, werden auch das Webhook-Abo
    bei der Plattform und der gemeinsame Streamer-Eintrag gelöscht."""
    async with streamer_lock(monitored["platform"], monitored["platform_channel_id"]):
        if not await db_stream_alerts.remove_subscription(guild_id, monitored["id"]):
            raise AlertError("not_monitored")

        if await db_stream_alerts.is_channel_still_referenced(monitored["id"]):
            return

        handler = PLATFORM_HANDLERS.get(monitored["platform"])
        if handler and handler.delete_subscriptions:
            try:
                await handler.delete_subscriptions(monitored)
            except Exception:
                log.exception("Subscription-Löschung für %s fehlgeschlagen — DB-Eintrag wird trotzdem entfernt.",
                              monitored["streamer_name"])
        await db_stream_alerts.delete_monitored_channel(monitored["id"])
        log.info("Letzter Server hat %s entfernt — Abos und Kanal-Eintrag bereinigt.", monitored["streamer_name"])
