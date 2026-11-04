"""Diagnose für "Unknown interaction" (10062): Discord gibt 3 Sekunden für die erste Antwort.

Zwei Messungen unterscheiden die beiden möglichen Ursachen:
  - Event-Loop-Wächter: ein eigener Thread prüft jede Sekunde, ob die Event-Loop noch reagiert.
    Hängt sie länger als LOOP_BLOCK_THRESHOLD, wird die Codestelle geloggt, an der sie gerade steht —
    dann blockiert etwas im Bot. (discord.py selbst meldet erst ab ca. 10 s "heartbeat blocked".)
  - Alter eingehender Interaktionen: Ist eine Interaktion schon beim Eintreffen älter als
    INTERACTION_AGE_THRESHOLD, obwohl die Loop frei war, kam sie verspätet von Discord bzw. übers Netz.
"""
import asyncio
import logging
import sys
import threading
import time
import traceback

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

LOOP_BLOCK_THRESHOLD = 1.5      # Sekunden ohne Reaktion der Event-Loop, ab denen gewarnt wird
INTERACTION_AGE_THRESHOLD = 2.0 # Sekunden zwischen Erstellen (Discord) und Eintreffen im Bot
CHECK_INTERVAL = 1.0


class _LoopWatchdog(threading.Thread):
    def __init__(self, loop, loop_thread_id: int):
        super().__init__(name="nexus-loop-watchdog", daemon=True)
        self.loop = loop
        self.loop_thread_id = loop_thread_id
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.is_set():
            answered = threading.Event()
            start = time.monotonic()
            try:
                self.loop.call_soon_threadsafe(answered.set)
            except RuntimeError:   # Loop geschlossen (Shutdown)
                return
            if not answered.wait(LOOP_BLOCK_THRESHOLD):
                frame = sys._current_frames().get(self.loop_thread_id)
                stack = "".join(traceback.format_stack(frame)) if frame else "(Stack nicht verfügbar)"
                log.warning("[DIAG] Event-Loop reagiert seit über %.1f s nicht — aktuelle Stelle:\n%s",
                            LOOP_BLOCK_THRESHOLD, stack)
                answered.wait()
                log.warning("[DIAG] Event-Loop wieder frei nach %.1f s.", time.monotonic() - start)
            self.stopped.wait(CHECK_INTERVAL)


class Diagnostics(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._watchdog: _LoopWatchdog | None = None

    async def cog_load(self):
        # cog_load läuft in der Event-Loop: ihr Thread ist der, den der Wächter beobachtet
        self._watchdog = _LoopWatchdog(asyncio.get_running_loop(), threading.get_ident())
        self._watchdog.start()

    async def cog_unload(self):
        if self._watchdog:
            self._watchdog.stopped.set()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
        if age < INTERACTION_AGE_THRESHOLD:
            return
        name = interaction.command.qualified_name if interaction.command else (
            (interaction.data or {}).get("custom_id") or (interaction.data or {}).get("name") or "?"
        )
        log.warning(
            "[DIAG] Interaktion kam %.1f s nach dem Erstellen an (Typ %s, %s, Guild %s, Shard %s, Gateway-Latenz %.0f ms).",
            age, interaction.type.name, name, interaction.guild_id,
            interaction.guild.shard_id if interaction.guild else "-",
            self.bot.latency * 1000,
        )


async def setup(bot):
    await bot.add_cog(Diagnostics(bot))
