"""Eskalation für Nexus-AutoMod (opt-in): Wer wiederholt gegen Nexus-Regeln verstößt, wird stufenweise härter
bestraft — z. B. 3 Verstöße in 1 Std. → Verwarnung, 5 in 24 Std. → Timeout, 8 in 7 Tagen → Bann.

- Strike: jeder Verstoß gegen eine Nexus-Regel, außer dem Namensfilter (der greift wegen des Profils, nicht
  wegen etwas, das jemand schreibt). Eine Nachricht mit „blockieren + Timeout“ zählt einmal — systems/automod_log.py
  sammelt Discords Einzelmeldungen und ruft on_violation einmal pro Verstoß.
- Stufe: {strikes, window_minutes, action, timeout_minutes}. Sie greift, sobald ihre Schwelle im eigenen
  Zeitfenster genau erreicht wird. Strikes laufen danach weiter (nach der Verwarnung folgt bei weiterem
  Fehlverhalten die nächste Stufe). Greifen mehrere Stufen gleichzeitig, nur die letzte (strengste).
- Free: 1 Stufe, Premium: bis zu 5 (feature_limits „automod_escalation_steps“). Läuft Premium aus, bleiben die
  Stufen gespeichert, aktiv ist nur noch die erste.
- Strikes werden nach STRIKE_RETENTION_DAYS gelöscht (cogs/automod.py, täglich).
- Testphase: wie AutoMod 2.0 nur, wo automod_rules.v2_available.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from cogs.utils import dm_user, get_member_safe
from database import db_automod, db_limits, db_warnings
from emojis import NexusEmojis
from systems import automod_rules
from systems.premium import is_premium
from systems.timeouts import timeout_until
from systems.ui import with_emoji

log = logging.getLogger(__name__)

ACTIONS = ("warn", "timeout", "kick", "ban")
COUNTED_KINDS = frozenset(automod_rules.RULE_KINDS) - {"names"}
LIMIT_KEY = "automod_escalation_steps"
STRIKE_RETENTION_DAYS = 30
MAX_STRIKES = 50
# Zeitfenster in Minuten: 10 Min., 1 Std., 6 Std., 24 Std., 7 Tage, 30 Tage (länger: Strikes sind dann weg)
WINDOW_OPTIONS = (10, 60, 360, 1440, 10080, 43200)
# Timeout-Dauer in Minuten (Discord: höchstens 28 Tage)
TIMEOUT_OPTIONS = (5, 10, 60, 360, 1440, 10080, 40320)


@dataclass
class Result:
    """Was eine Stufe getan hat — für den Log-Eintrag (systems/automod_log.py)."""
    step: int          # 1-basiert
    total: int         # aktive Stufen
    action: str
    strikes: int
    window_minutes: int
    timeout_minutes: int | None
    ok: bool


# ─── Einstellungen ───────────────────────────────────────────────────────────

async def max_steps(guild_id: int) -> int:
    return await db_limits.get_limit(guild_id, LIMIT_KEY, await is_premium(guild_id))


async def read(guild: discord.Guild) -> dict:
    settings = await db_automod.get_escalation(guild.id)
    return {
        "enabled": settings["enabled"],
        "steps": settings["steps"],
        "max_steps": await max_steps(guild.id),
        "premium_max_steps": await db_limits.get_limit(guild.id, LIMIT_KEY, True),
        "actions": list(ACTIONS),
        "window_options": list(WINDOW_OPTIONS),
        "timeout_options": list(TIMEOUT_OPTIONS),
        "max_strikes": MAX_STRIKES,
    }


def _step(raw) -> dict:
    """Eine Stufe prüfen. Wirft SettingsError (invalid: über die Oberfläche nicht erreichbar)."""
    SettingsError = automod_rules.SettingsError
    if not isinstance(raw, dict):
        raise SettingsError("Stufe muss ein Objekt sein", invalid=True)
    strikes, window, action = raw.get("strikes"), raw.get("window_minutes"), raw.get("action")
    if not isinstance(strikes, int) or isinstance(strikes, bool) or not 1 <= strikes <= MAX_STRIKES:
        raise SettingsError("dash_err_am_escalation_strikes", max=MAX_STRIKES)
    if window not in WINDOW_OPTIONS:
        raise SettingsError(f"Ungültiges Zeitfenster: {window!r}", invalid=True)
    if action not in ACTIONS:
        raise SettingsError(f"Ungültige Maßnahme: {action!r}", invalid=True)
    step = {"strikes": strikes, "window_minutes": window, "action": action}
    if action == "timeout":
        if raw.get("timeout_minutes") not in TIMEOUT_OPTIONS:
            raise SettingsError(f"Ungültige Timeout-Dauer: {raw.get('timeout_minutes')!r}", invalid=True)
        step["timeout_minutes"] = raw["timeout_minutes"]
    return step


_UNSET = object()


async def update(guild: discord.Guild, *, enabled=_UNSET, steps=_UNSET) -> dict:
    """Einstellungen prüfen und speichern. Wirft automod_rules.SettingsError."""
    SettingsError = automod_rules.SettingsError
    automod_rules.require_v2(guild.id)
    current = await db_automod.get_escalation(guild.id)
    new_enabled, new_steps = current["enabled"], current["steps"]

    if enabled is not _UNSET:
        if not isinstance(enabled, bool):
            raise SettingsError("enabled muss true/false sein", invalid=True)
        new_enabled = enabled
    if steps is not _UNSET:
        if not isinstance(steps, list):
            raise SettingsError("steps muss eine Liste sein", invalid=True)
        new_steps = [_step(raw) for raw in steps]
        limit = await max_steps(guild.id)
        if len(new_steps) > limit:
            raise SettingsError("dash_err_am_escalation_too_many_steps", max=limit)
    if new_enabled and not new_steps:
        raise SettingsError("dash_err_am_escalation_no_steps")

    await db_automod.save_escalation(guild.id, new_enabled, new_steps)
    return await read(guild)


# ─── Verstoß verarbeiten ─────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)   # wie in der DB: UTC ohne Zeitzone


async def on_violation(guild: discord.Guild, user_id: int, kind: str, lang: dict) -> Result | None:
    """Strike speichern und prüfen, ob eine Stufe greift. Aufruf aus systems/automod_log.py."""
    if kind not in COUNTED_KINDS or not automod_rules.v2_available(guild.id):
        return None
    settings = await db_automod.get_escalation(guild.id)
    if not settings["enabled"] or not settings["steps"]:
        return None
    steps = settings["steps"][:await max_steps(guild.id)]   # Premium ausgelaufen → nur noch die erste

    now = _now()
    await db_automod.add_strike(guild.id, user_id, kind, now)
    longest = max(step["window_minutes"] for step in steps)
    times = await db_automod.get_strike_times(guild.id, user_id, now - timedelta(minutes=longest))

    fired = None
    for index, step in enumerate(steps):
        count = sum(1 for t in times if t > now - timedelta(minutes=step["window_minutes"]))
        if count == step["strikes"]:
            fired = (index, step)
    if fired is None:
        return None

    index, step = fired
    ok = await _apply(guild, user_id, step, lang)
    log.info("[AUTOMOD] Eskalation Stufe %s (%s) für %s auf %s: %s",
             index + 1, step["action"], user_id, guild.id, "ok" if ok else "fehlgeschlagen")
    return Result(
        step=index + 1, total=len(steps), action=step["action"], strikes=step["strikes"],
        window_minutes=step["window_minutes"], timeout_minutes=step.get("timeout_minutes"), ok=ok,
    )


def window_label(minutes: int, lang: dict) -> str:
    from systems.automod_log import _duration   # hier: automod_log importiert dieses Modul
    return _duration(timedelta(minutes=minutes), lang)


def reason_text(step: dict, lang: dict) -> str:
    return lang.get("automod_escalation_reason", "AutoMod: {count} violations within {window}").format(
        count=step["strikes"], window=window_label(step["window_minutes"], lang),
    )


async def _apply(guild: discord.Guild, user_id: int, step: dict, lang: dict) -> bool:
    action, reason = step["action"], reason_text(step, lang)
    member = await get_member_safe(guild, user_id)
    try:
        if action == "warn":
            count = await db_warnings.add_warning(guild.id, user_id, guild.me.id, reason)
            if member is not None:
                await dm_user(
                    member, title=with_emoji(NexusEmojis.WARNING, lang.get("dm_warned", "You were warned")),
                    reason=reason, color=discord.Color.yellow(), lang=lang, footer=guild.name,
                    extra=lang.get("dm_warn_count", "Total warnings: {count}").format(count=count),
                )
            return True
        if action == "ban":
            if member is not None:
                await dm_user(member, title=lang.get("dm_banned", "You were banned"), reason=reason,
                              color=discord.Color.red(), lang=lang, footer=guild.name)
            await guild.ban(discord.Object(id=user_id), reason=reason, delete_message_seconds=0)
            return True
        if member is None:
            return False   # Timeout/Kick: nicht mehr auf dem Server
        if action == "timeout":
            await member.timeout(timeout_until(timedelta(minutes=step["timeout_minutes"])), reason=reason)
            return True
        await dm_user(member, title=lang.get("dm_kicked", "You were kicked"), reason=reason,
                      color=discord.Color.red(), lang=lang, footer=guild.name)
        await guild.kick(member, reason=reason)
        return True
    except discord.HTTPException as e:
        log.warning("[AUTOMOD] Eskalation %s für %s auf %s fehlgeschlagen: %s", action, user_id, guild.id, e)
        return False


async def cleanup() -> int:
    """Strikes älter als STRIKE_RETENTION_DAYS löschen."""
    return await db_automod.delete_strikes_before(_now() - timedelta(days=STRIKE_RETENTION_DAYS))
