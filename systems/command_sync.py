"""Slash-Command-Sync beim Start — mit späteren Wiederholungen, wenn Discord gerade nicht will.

Schlägt der Sync beim Start fehl (Rate Limit, Discord-Störung, Netzwerk), wird er im Hintergrund
noch einige Male wiederholt, statt erst beim nächsten Neustart. Fehler, die eine Wiederholung nicht
behebt (z. B. 400 wegen einer ungültigen Command-Definition), werden nur geloggt.
"""
import asyncio
import logging

import aiohttp
import discord
from discord import app_commands

log = logging.getLogger(__name__)

# Wartezeiten vor den Wiederholungen (Sekunden): 1 min, 5 min, 15 min, 30 min, 1 h
RETRY_DELAYS: tuple[float, ...] = (60, 300, 900, 1800, 3600)

Target = discord.abc.Snowflake | None   # None = globale Commands


def target_name(target: Target) -> str:
    return "global" if target is None else str(target.id)


def _retryable(error: Exception) -> bool:
    if isinstance(error, discord.HTTPException):
        return error.status == 429 or error.status >= 500
    return isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError, OSError))


async def sync_targets(tree: app_commands.CommandTree, targets: list[Target]) -> list[Target]:
    """Synchronisiert alle Ziele; gibt die zurück, bei denen sich ein neuer Versuch lohnt."""
    retry = []
    for target in targets:
        name = target_name(target)
        try:
            await tree.sync(guild=target)
            log.info("Command-Sync (%s) ausgeführt.", name)
        except Exception as e:
            if _retryable(e):
                log.warning("Command-Sync (%s) fehlgeschlagen, wird später wiederholt: %r", name, e)
                retry.append(target)
            else:
                log.exception("Fehler beim Command-Sync (%s) — Wiederholen hilft hier nicht:", name)
    return retry


async def retry_sync(
    tree: app_commands.CommandTree,
    targets: list[Target],
    delays: tuple[float, ...] = RETRY_DELAYS,
) -> list[Target]:
    """Wiederholt den Sync für `targets` nach den Wartezeiten; gibt zurück, was am Ende noch fehlt."""
    for attempt, delay in enumerate(delays, start=1):
        await asyncio.sleep(delay)
        targets = await sync_targets(tree, targets)
        if not targets:
            log.info("Command-Sync im %d. Wiederholungsversuch nachgeholt.", attempt)
            return []
    log.error(
        "Command-Sync (%s) nach %d Wiederholungen aufgegeben — neue oder geänderte Commands erst nach dem nächsten Neustart.",
        ", ".join(target_name(t) for t in targets), len(delays),
    )
    return targets
