"""Diagnose für "Unknown interaction" (10062): Discord gibt 3 Sekunden für die erste Antwort.

Zwei Messungen unterscheiden die beiden möglichen Ursachen:
  - Event-Loop-Wächter: ein eigener Thread prüft jede Sekunde, ob die Event-Loop noch reagiert.
    Hängt sie länger als LOOP_BLOCK_THRESHOLD, wird die Codestelle geloggt, an der sie gerade steht —
    dann blockiert etwas im Bot. (discord.py selbst meldet erst ab ca. 10 s "heartbeat blocked".)
  - Alter eingehender Interaktionen: Ist eine Interaktion schon beim Eintreffen älter als
    INTERACTION_AGE_THRESHOLD, obwohl die Loop frei war, kam sie verspätet von Discord bzw. übers Netz.
  - Zeitmessung der ersten Antwort: Jede erste Antwort (defer, send_message, send_modal, ...) läuft durch
    create_interaction_response. Geloggt wird, wie alt die Interaktion beim Absenden war und wie lange der
    Request dauerte — trennt "unser Code war zu langsam" von "der Request an Discord hing".
    Langsame Requests werden zerlegt (aiohttp-Tracing): Warten auf eine freie Verbindung, DNS,
    Verbindungsaufbau (TCP+TLS), Warten auf Discords Antwort, Rest — plus die größte Verzögerung
    der Event-Loop währenddessen (ein Taktgeber misst alle 100 ms, ob sie pünktlich drankommt).
Alle Altersangaben hängen an der Server-Uhr: geht sie falsch, sind sie um diesen Betrag daneben.
"""
import asyncio
import collections
import contextvars
import logging
import sys
import threading
import time
import traceback

import aiohttp
import discord
from discord.ext import commands
from discord.webhook.async_ import AsyncWebhookAdapter

log = logging.getLogger(__name__)

LOOP_BLOCK_THRESHOLD = 1.5      # Sekunden ohne Reaktion der Event-Loop, ab denen gewarnt wird
INTERACTION_AGE_THRESHOLD = 2.0 # Sekunden zwischen Erstellen (Discord) und Eintreffen im Bot
CHECK_INTERVAL = 1.0
RESPONSE_SLOW_THRESHOLD = 1.0   # Sekunden für den Antwort-Request selbst
LAG_TICK = 0.1                  # Taktgeber für die Loop-Verzögerung

# Zeitpunkte des gerade laufenden Antwort-Requests (aiohttp-Tracing schreibt hinein)
_current_timings: contextvars.ContextVar[dict | None] = contextvars.ContextVar("nexus_diag_timings", default=None)
_loop_lag: collections.deque = collections.deque(maxlen=1200)   # (Zeitpunkt, Verzögerung), ca. 2 Minuten


async def _lag_sampler():
    """Misst, wie verspätet die Loop einen 100-ms-Takt ausführt. Findet auch viele kleine Hänger,
    die der Wächter-Thread (ab 1,5 s) nicht sieht."""
    while True:
        start = time.monotonic()
        await asyncio.sleep(LAG_TICK)
        now = time.monotonic()
        _loop_lag.append((now, now - start - LAG_TICK))


def _max_lag(since: float, until: float) -> float:
    return max((lag for t, lag in _loop_lag if since <= t <= until + LAG_TICK), default=0.0)


def _make_trace_config() -> aiohttp.TraceConfig:
    def mark(key):
        async def handler(_session, _ctx, _params):
            timings = _current_timings.get()
            if timings is not None:
                timings[key] = time.monotonic()
                if key == "request_start":
                    timings["attempts"] = timings.get("attempts", 0) + 1
                    for k in ("reused", "dns_start", "dns_end", "conn_start", "conn_end",
                              "queue_start", "queue_end", "sent", "request_end"):
                        timings.pop(k, None)   # bei Wiederholung zählt der letzte Versuch
        return handler

    tc = aiohttp.TraceConfig()
    tc.on_request_start.append(mark("request_start"))
    tc.on_connection_queued_start.append(mark("queue_start"))
    tc.on_connection_queued_end.append(mark("queue_end"))
    tc.on_dns_resolvehost_start.append(mark("dns_start"))
    tc.on_dns_resolvehost_end.append(mark("dns_end"))
    tc.on_connection_create_start.append(mark("conn_start"))
    tc.on_connection_create_end.append(mark("conn_end"))
    tc.on_connection_reuseconn.append(mark("reused"))
    tc.on_request_headers_sent.append(mark("sent"))
    tc.on_request_chunk_sent.append(mark("sent"))
    tc.on_request_end.append(mark("request_end"))
    tc.freeze()
    return tc


_TRACE_CONFIG = _make_trace_config()


def _attach_trace(session) -> None:
    """Hängt das Tracing an die Session von discord.py (die wird bei einem Neu-Login neu erzeugt)."""
    configs = getattr(session, "_trace_configs", None)
    if isinstance(configs, list) and _TRACE_CONFIG not in configs:
        configs.append(_TRACE_CONFIG)


def _breakdown(t: dict, start: float, end: float) -> str:
    def span(a, b):
        return t[b] - t[a] if a in t and b in t else None
    parts = []
    if (queue := span("queue_start", "queue_end")) is not None:
        parts.append(f"Warten auf freie Verbindung {queue:.2f}")
    if "reused" in t:
        parts.append("Verbindung wiederverwendet")
    elif (conn := span("conn_start", "conn_end")) is not None:
        dns = span("dns_start", "dns_end")
        parts.append(f"Verbindung neu {conn:.2f}" + (f" (davon DNS {dns:.2f})" if dns is not None else " (DNS aus Cache)"))
    if (wait := span("sent", "request_end")) is not None:
        parts.append(f"Warten auf Discord {wait:.2f}")
    if "request_end" in t:
        parts.append(f"Rest {end - t['request_end']:.2f}")
    if t.get("attempts", 1) > 1:
        parts.append(f"Versuche {t['attempts']}")
    parts.append(f"Loop max. {_max_lag(start, end):.2f} verzögert")
    return " | ".join(parts) + " (Sekunden)"


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


def _instrument_interaction_responses():
    """Misst jede erste Antwort auf eine Interaktion. Gibt das Original zum Zurücksetzen zurück."""
    original = AsyncWebhookAdapter.create_interaction_response

    def timed(self, interaction_id, token, **kwargs):
        try:
            _attach_trace(kwargs.get("session"))
        except Exception:
            pass   # Diagnose darf die Antwort nie verhindern
        request = original(self, interaction_id, token, **kwargs)

        async def run():
            age = (discord.utils.utcnow() - discord.utils.snowflake_time(interaction_id)).total_seconds()
            timings: dict = {}
            reset = _current_timings.set(timings)
            start = time.monotonic()
            try:
                result = await request
            except discord.NotFound as e:
                if e.code == 10062:
                    end = time.monotonic()
                    log.warning(
                        "[DIAG] 10062: Antwort wurde %.2f s nach dem Erstellen abgeschickt, Request dauerte %.2f s — %s",
                        age, end - start, _breakdown(timings, start, end),
                    )
                raise
            finally:
                _current_timings.reset(reset)
            end = time.monotonic()
            took = end - start
            if age >= INTERACTION_AGE_THRESHOLD or took >= RESPONSE_SLOW_THRESHOLD:
                log.warning(
                    "[DIAG] Langsame erste Antwort: abgeschickt %.2f s nach dem Erstellen, Request dauerte %.2f s — %s",
                    age, took, _breakdown(timings, start, end),
                )
            return result

        return run()

    AsyncWebhookAdapter.create_interaction_response = timed
    return original


class Diagnostics(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._watchdog: _LoopWatchdog | None = None
        self._original_response = None
        self._lag_task: asyncio.Task | None = None

    async def cog_load(self):
        # cog_load läuft in der Event-Loop: ihr Thread ist der, den der Wächter beobachtet
        self._watchdog = _LoopWatchdog(asyncio.get_running_loop(), threading.get_ident())
        self._watchdog.start()
        self._original_response = _instrument_interaction_responses()
        self._lag_task = asyncio.create_task(_lag_sampler())

    async def cog_unload(self):
        if self._watchdog:
            self._watchdog.stopped.set()
        if self._original_response:
            AsyncWebhookAdapter.create_interaction_response = self._original_response
        if self._lag_task:
            self._lag_task.cancel()

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
