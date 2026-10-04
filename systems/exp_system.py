import bisect

XP_COOLDOWN = 60  # Sekunden
MAX_PRECOMPUTED_LEVEL = 250

def xp_for_level(level: int) -> int:
    """
    Berechnet die kumulierten Gesamt-XP, die man braucht, um dieses Level zu erreichen.
    Entspricht der MEE6-Formel: Summe von (5*L^2 + 50*L + 100) fuer L von 0 bis level-2.
    """
    if level <= 1:
        return 0

    n = level - 1
    # Exakte mathematische Summenformel
    return int((5 / 6) * n * (2 * n**2 + 27 * n + 91))

# Einmalig beim Start vorberechnen
_LEVEL_THRESHOLDS = [xp_for_level(lvl) for lvl in range(MAX_PRECOMPUTED_LEVEL + 1)]

def calculate_level(xp: int) -> int:
    """Berechnet das aktuelle Level basierend auf den Gesamt-XP via Binaersuche."""
    if xp < 100:
        return 1

    # Findet den passenden Level-Index in O(log n)
    level = bisect.bisect_right(_LEVEL_THRESHOLDS, xp) - 1

    # Fallback falls jemand jemals ueber Level 250 hinausgrindet
    if level >= MAX_PRECOMPUTED_LEVEL:
        while xp_for_level(level + 1) <= xp:
            level += 1

    return max(1, level)

def xp_to_next_level(xp: int) -> tuple[int, int]:
    """Gibt (XP bis zum naechsten Level, naechstes Level) zurueck."""
    current_level = calculate_level(xp)
    next_level = current_level + 1

    xp_needed_for_next = xp_for_level(next_level)
    needed = xp_needed_for_next - xp

    return max(0, needed), next_level