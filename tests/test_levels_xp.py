"""XP gutschreiben und abziehen (database/db_levels.py) — auch bei gleichzeitigen Änderungen."""
import asyncio

from database import db_levels
from systems.exp_system import xp_for_level
from tests.conftest import query_one


async def stored(guild, user_id) -> dict:
    return await query_one("SELECT xp, level FROM levels WHERE guild_id = %s AND user_id = %s", guild.id, user_id)


async def test_gleichzeitige_gutschriften_gehen_nicht_verloren(guild):
    """Chat-XP und Voice-XP im selben Moment: jede Gutschrift zählt."""
    await db_levels.add_xp(guild.id, guild.member.id, 10)
    await asyncio.gather(*(db_levels.add_xp(guild.id, guild.member.id, 10) for _ in range(20)))
    assert (await stored(guild, guild.member.id))["xp"] == 210


async def test_gleichzeitig_beim_ersten_mal(guild):
    """Auch wenn das Mitglied noch gar keinen Eintrag hat."""
    await asyncio.gather(*(db_levels.add_xp(guild.id, guild.admin.id, 7) for _ in range(10)))
    assert (await stored(guild, guild.admin.id))["xp"] == 70


async def test_mod_befehle_und_gutschriften_gleichzeitig(guild):
    await db_levels.add_xp(guild.id, guild.member.id, 1000)
    await asyncio.gather(
        *(db_levels.add_xp(guild.id, guild.member.id, 10) for _ in range(5)),
        *(db_levels.add_xp_mod(guild.id, guild.member.id, 100) for _ in range(3)),
        *(db_levels.remove_xp_mod(guild.id, guild.member.id, 50) for _ in range(2)),
    )
    assert (await stored(guild, guild.member.id))["xp"] == 1000 + 50 + 300 - 100


async def test_level_und_rueckgabe(guild):
    # Schon die erste Gutschrift kann ein Level-Up sein (z. B. hohe Voice-XP)
    result = await db_levels.add_xp(guild.id, guild.member.id, xp_for_level(2))
    assert result == {"old_level": 1, "new_level": 2, "xp": xp_for_level(2)}
    assert await stored(guild, guild.member.id) == {"xp": xp_for_level(2), "level": 2}

    result = await db_levels.remove_xp_mod(guild.id, guild.member.id, 10**6)   # nie unter 0
    assert result == {"old_level": 2, "new_level": 1, "xp": 0}
    assert await stored(guild, guild.member.id) == {"xp": 0, "level": 1}
