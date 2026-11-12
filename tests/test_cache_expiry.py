"""TTL-Caches: Ein Eintrag, der genau zwischen „ist drin?“ und „hol ihn“ abläuft, darf keinen KeyError werfen.

Vorkommen im Betrieb: Voice-XP-Loop → db_settings.is_module_enabled → get_guild → KeyError (Eintrag lief nach 15 min ab).
Die Uhr unten springt bei jedem Blick ein Stück weiter — beim ersten Blick ist der Eintrag noch gültig, beim zweiten nicht.
"""
import pytest
from cachetools import TTLCache

from database import db_automod, db_levels, db_settings
from systems import premium

TTL = 900


class Clock:
    def __init__(self):
        self.now = 0.0
        self.step = 0.0

    def __call__(self):
        self.now += self.step
        return self.now


@pytest.mark.parametrize("module, cache_name, read, value", [
    (db_settings, "_settings_cache", db_settings.get_guild, {"guild_id": 1, "modules": {"levels": True}}),
    (db_levels, "_levels_settings_cache", db_levels.get_levels_settings, {"guild_id": 1, "currency_name": "Coins"}),
    (db_automod, "_antinuke_cache", db_automod.get_antinuke, 1),
    (premium, "_premium_cache", premium.is_premium, True),
])
async def test_eintrag_laeuft_zwischen_pruefen_und_lesen_ab(monkeypatch, module, cache_name, read, value):
    clock = Clock()
    cache = TTLCache(maxsize=10, ttl=TTL, timer=clock)
    cache[1] = value                              # eingetragen bei 0, gültig bis 900
    monkeypatch.setattr(module, cache_name, cache)
    clock.now, clock.step = TTL - 0.5, 0.4        # nächste Blicke: 899,9 (gültig), 900,3 (abgelaufen)

    assert await read(1) == value                 # ein einziger Blick in den Cache: noch gültig, kein KeyError
