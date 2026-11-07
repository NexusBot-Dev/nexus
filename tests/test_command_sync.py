"""Command-Sync beim Start und seine Wiederholungen (systems/command_sync.py)."""
import asyncio
import logging

import aiohttp
import discord

from systems.command_sync import retry_sync, sync_targets
from tests.fakes import _http_error, bad_request

SUPPORT = discord.Object(id=1)
DEV = discord.Object(id=2)
NO_WAIT = (0, 0, 0)


class FakeTree:
    """Liefert je Ziel der Reihe nach die vorgegebenen Fehler, danach Erfolg."""

    def __init__(self, **errors):
        self.errors = {name: list(errs) for name, errs in errors.items()}
        self.calls: list[str] = []

    async def sync(self, *, guild=None):
        name = "global" if guild is None else str(guild.id)
        self.calls.append(name)
        if self.errors.get(name):
            raise self.errors[name].pop(0)
        return []


def unavailable():
    return _http_error(discord.DiscordServerError, 503, 0, "Service Unavailable")


def rate_limited():
    return _http_error(discord.HTTPException, 429, 0, "Too Many Requests")


async def test_alles_klappt_beim_ersten_mal():
    tree = FakeTree()
    assert await sync_targets(tree, [None, SUPPORT, DEV]) == []
    assert tree.calls == ["global", "1", "2"]


async def test_voruebergehende_fehler_werden_wiederholt():
    """Rate Limit, Discord-Störung und Netzwerkfehler: beim Start fehlgeschlagen, später nachgeholt."""
    tree = FakeTree(**{"global": [rate_limited(), unavailable()], "2": [aiohttp.ClientConnectionError()]})
    failed = await sync_targets(tree, [None, SUPPORT, DEV])
    assert failed == [None, DEV]

    assert await retry_sync(tree, failed, NO_WAIT) == []
    # Nur die fehlgeschlagenen Ziele werden wiederholt, und nur so oft wie nötig
    assert tree.calls == ["global", "1", "2", "global", "2", "global"]


async def test_dauerhafte_fehler_werden_nicht_wiederholt(caplog):
    tree = FakeTree(**{"1": [bad_request(50035, "Invalid Form Body")]})
    with caplog.at_level(logging.ERROR, logger="systems.command_sync"):
        assert await sync_targets(tree, [None, SUPPORT]) == []
    assert "Command-Sync (1)" in caplog.text


async def test_gibt_nach_allen_versuchen_auf(caplog):
    tree = FakeTree(**{"global": [unavailable() for _ in range(10)]})
    with caplog.at_level(logging.ERROR, logger="systems.command_sync"):
        assert await retry_sync(tree, [None], NO_WAIT) == [None]
    assert tree.calls == ["global"] * len(NO_WAIT)
    assert "aufgegeben" in caplog.text


async def test_wartet_zwischen_den_versuchen(monkeypatch):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    tree = FakeTree(**{"global": [unavailable(), unavailable()]})
    assert await retry_sync(tree, [None], (60, 300, 900)) == []
    assert waits == [60, 300, 900]
    assert len(tree.calls) == 3
