"""Timeouts mit Sicherheitsabstand zu Discords Obergrenze von 28 Tagen.

Discord lehnt einen Timeout ab, der auch nur knapp über 28 Tagen endet. Weil das Ende hier mit der
eigenen Uhr berechnet wird und die Anfrage etwas unterwegs ist, kann genau „jetzt + 28 Tage“ bei
Discord schon zu lang sein. Deshalb höchstens 28 Tage minus 5 Minuten — die fehlen niemandem.
"""

from datetime import datetime, timedelta

import discord

DISCORD_MAX_TIMEOUT = timedelta(days=28)
MAX_TIMEOUT = DISCORD_MAX_TIMEOUT - timedelta(minutes=5)

# Für Eingaben in Minuten (z. B. /timeout): Wer „28 Tage“ angibt, bekommt sie — gekürzt um den Abstand
MAX_TIMEOUT_MINUTES = int(DISCORD_MAX_TIMEOUT.total_seconds() // 60)   # 40320


def timeout_until(duration: timedelta) -> datetime:
    """Endzeitpunkt für member.timeout(): jetzt + Dauer, höchstens MAX_TIMEOUT."""
    return discord.utils.utcnow() + min(duration, MAX_TIMEOUT)
