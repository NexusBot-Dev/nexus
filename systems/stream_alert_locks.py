"""Sperren für das Anlegen und Entfernen von Stream-Alerts (Slash-Commands und Dashboard).

Zwischen Prüfen und Schreiben liegen Anfragen an Twitch/Kick/YouTube — ohne Sperre können sich
gleichzeitige Änderungen in die Quere kommen:
  - zwei Server fügen denselben neuen Streamer hinzu → zweites Webhook-Abo, Twitch lehnt ab (409)
  - Doppelklick auf demselben Server → doppelter Eintrag, Datenbank lehnt ab
  - zwei Streamer gleichzeitig → Limit wird überschritten
  - ein Server entfernt den letzten Alert, ein anderer fügt ihn gerade hinzu → dessen Alert verschwindet still

Reihenfolge immer: erst `guild_lock`, dann `streamer_lock` (nie umgekehrt) — so kann nichts verklemmen.
Die Sperren gelten innerhalb des Bot-Prozesses; bei mehreren Prozessen bräuchte es eine Sperre in der
Datenbank (z. B. GET_LOCK).
"""
import asyncio
import weakref

# Eine Sperre lebt nur, solange jemand sie hält oder auf sie wartet
_locks: "weakref.WeakValueDictionary[tuple, asyncio.Lock]" = weakref.WeakValueDictionary()


def _lock(key: tuple) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def guild_lock(guild_id: int, platform: str) -> asyncio.Lock:
    """Hinzufügen auf einem Server und einer Plattform — Limit-Prüfung bis zum Speichern."""
    return _lock(("guild", guild_id, platform))


def streamer_lock(platform: str, platform_channel_id: str) -> asyncio.Lock:
    """Alles, was den gemeinsamen Eintrag eines Streamers (und sein Webhook-Abo) anlegt oder löscht."""
    return _lock(("streamer", platform, str(platform_channel_id)))
