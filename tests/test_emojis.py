"""Emojis zentral in emojis.py: umgestellte Bereiche dürfen keine Emojis mehr selbst in Texten haben.

Die Liste UMGESTELLT wächst Modul für Modul. Für jede Datei darin gilt:
  - im Code steht kein ❌ mehr (auch nicht in englischen Ersatztexten),
  - kein Text, den die Datei benutzt, beginnt in irgendeiner Sprache mit ❌ — das setzt error_msg davor.
"""
import pathlib
import re

import pytest

from emojis import NexusEmojis
from systems.ui import error_msg
from translations import TRANSLATIONS

ROOT = pathlib.Path(__file__).resolve().parents[1]

UMGESTELLT = [
    "cogs/stream_alerts.py",
    "cogs/levels.py",
    "cogs/mee6_import.py",
    "cogs/giveaways.py",
    "cogs/tickets.py",
    "cogs/reaction_roles.py",
    "cogs/embeds.py",
    "cogs/moderation.py",
    "cogs/welcome.py",
    "cogs/goodbye.py",
    "cogs/welcome_message.py",
    "cogs/boost_message.py",
    "cogs/boosts.py",
    "systems/message_panel.py",
    "cogs/announcements.py",
    "cogs/automod.py",
    "cogs/honeypot.py",
    "cogs/shield.py",
    "systems/honeypot.py",
    "cogs/admin_commands.py",
    "cogs/setup.py",
    "cogs/diagnose.py",
    "cogs/support.py",
    "systems/premium_ui.py",
    "cogs/owner_tools.py",
    "systems/hierarchy.py",
    "systems/ui.py",
    "bot.py",
]


def used_keys(source: str) -> set[str]:
    keys = TRANSLATIONS["en"].keys()
    return {k for k in re.findall(r"""["']([a-z0-9_]+)["']""", source) if k in keys}   # einfache und doppelte Anführungszeichen


@pytest.mark.parametrize("path", UMGESTELLT)
def test_kein_fehler_emoji_im_code(path):
    source = (ROOT / path).read_text()
    lines = [f"{i}: {line.strip()}" for i, line in enumerate(source.splitlines(), 1) if "❌" in line]
    assert lines == []


@pytest.mark.parametrize("path", UMGESTELLT)
def test_kein_fehler_emoji_in_den_texten(path):
    keys = used_keys((ROOT / path).read_text())
    wrong = sorted(f"{lang}:{key}" for lang, texts in TRANSLATIONS.items()
                   for key in keys if texts.get(key, "").lstrip().startswith("❌"))
    assert wrong == []


def test_error_msg_setzt_das_emoji_davor():
    assert error_msg("Etwas ging schief.") == f"{NexusEmojis.ERROR} Etwas ging schief."


def test_stream_alert_fehler_mit_emoji():
    from cogs.stream_alerts import alert_error_text
    from systems.stream_alert_service import AlertError
    text = alert_error_text(TRANSLATIONS["de"], AlertError("limit", limit=1), "shroud")
    assert text == f"{NexusEmojis.ERROR} " + TRANSLATIONS["de"]["alert_limit_reached"].format(limit=1)


def test_dashboard_bekommt_rechte_meldungen_ohne_emoji(guild):
    """Das Dashboard zeigt Bot-Texte als Klartext — ein eigenes Discord-Emoji wäre dort Rohtext (<:name:id>)."""
    from systems.hierarchy import can_use_channel, format_issue, issue_text
    channel = guild.add_text_channel("gesperrt", bot_denied={"send_messages"})
    check = can_use_channel(channel, "send_messages")
    lang = TRANSLATIONS["de"]
    assert not issue_text(check, lang).startswith(NexusEmojis.ERROR)
    assert format_issue(check, lang) == f"{NexusEmojis.ERROR} {issue_text(check, lang)}"


def test_nirgends_mehr_fehler_emoji_im_code():
    """Nach der Umstellung steht ❌ nur noch in emojis.py."""
    files = [p for p in ROOT.rglob("*.py")
             if p.parts[len(ROOT.parts)] in {"cogs", "systems", "dashboard", "integrations", "lists"} or p.name == "bot.py"]
    found = [f"{p.relative_to(ROOT)}:{i}" for p in files for i, line in enumerate(p.read_text().splitlines(), 1) if "❌" in line]
    assert found == []


def test_kein_text_beginnt_mit_fehler_emoji():
    wrong = sorted(f"{lang}:{key}" for lang, texts in TRANSLATIONS.items() for key, text in texts.items()
                   if text.lstrip().startswith("❌"))
    assert wrong == []


# ── Weitere Statuszeichen: ⚠️ Warnung, ⏳ Laden, 🔒 gesperrt, ⭐ Premium ─────────────────────────────

STATUS_EMOJIS = {"⚠️": "WARNING", "⏳": "LOADING", "🔒": "LOCKED", "⭐": "PREMIUM"}

# Texte, die ihr Emoji bewusst behalten — Discord zeigt dort nur Unicode-Emojis (kein eigenes aus emojis.py),
# oder der Text ist Inhalt statt Statuszeichen.
AUSNAHMEN = {
    "setup_restored_perm_hint": "Fußzeile — Legende zum ⚠️ an den Kanälen in /setup",
    "honeypot_modal_warntext_placeholder": "Platzhalter im Formular",
    "honeypot_channel_title": "Standardtitel der Honeypot-Nachricht: Inhalt, änderbar, auch im Dashboard",
    "rr_edit_role_deleted": "auch Beschriftung in einer Auswahlliste",
}


@pytest.mark.parametrize("emoji", STATUS_EMOJIS)
def test_kein_text_beginnt_mit_statuszeichen(emoji):
    wrong = sorted(f"{lang}:{key}" for lang, texts in TRANSLATIONS.items() for key, text in texts.items()
                   if text.lstrip().startswith(emoji) and key not in AUSNAHMEN)
    assert wrong == []


def test_statuszeichen_haben_einen_namen_in_emojis_py():
    """Welches Emoji dahintersteht, ist egal — es soll sich in emojis.py tauschen lassen."""
    for name in STATUS_EMOJIS.values():
        assert getattr(NexusEmojis, name)


def test_ausnahmen_gibt_es_noch():
    """Fliegt ein Text raus, soll er auch aus der Ausnahmeliste verschwinden."""
    assert all(key in TRANSLATIONS["de"] for key in AUSNAHMEN)
