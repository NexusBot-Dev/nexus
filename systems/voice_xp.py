"""Voice-XP: wer im Voice-Kanal gerade XP bekommt.

Einmal pro Minute (Takt im Levels-Cog) bekommt jedes aktive Mitglied in einem Voice-Kanal XP —
aber nur, wenn dort mindestens MIN_ACTIVE aktive Mitglieder sitzen. Aktiv heißt: kein Bot,
nicht stumm und nicht taub (selbst oder vom Server). Wer sich kurz stummschaltet, verpasst
höchstens die eine Minute, in die die Prüfung fällt.
"""

TICK_SECONDS = 60
MIN_ACTIVE = 2
# Höchstwert pro Minute (Chat: 1000 pro Nachricht) — Voice läuft stundenlang, 1000/min wären 60.000 XP pro Stunde
MAX_XP_PER_MINUTE = 150


def is_active(member) -> bool:
    voice = member.voice
    if member.bot or voice is None:
        return False
    return not (voice.self_mute or voice.mute or voice.self_deaf or voice.deaf)


def eligible_members(channel, *, afk_channel_id: int | None, excluded: frozenset[int]) -> list:
    """Mitglieder, die in diesem Kanal für die aktuelle Minute XP bekommen."""
    if channel.id == afk_channel_id:
        return []
    if {channel.id, channel.category_id} & excluded:
        return []
    active = [m for m in channel.members if is_active(m)]
    return active if len(active) >= MIN_ACTIVE else []
