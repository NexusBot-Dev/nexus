"""Support-Ansichten und Premium-Status (cogs/support.py, systems/premium_ui.py) — Emojis aus emojis.py."""
from datetime import datetime, timedelta, timezone

from cogs.support import _channel_status, _format_premium, _state
from emojis import NexusEmojis
from systems.premium import PremiumInfo
from systems.premium_ui import describe_status
from translations import TRANSLATIONS


def test_support_status():
    assert _state(False) == f"{NexusEmojis.OFF} Disabled"
    assert _format_premium(None) == f"{NexusEmojis.OFF} No"


def test_kanal_ohne_schreibrecht(guild):
    readonly = guild.add_text_channel("logs", bot_denied={"send_messages"})
    assert _channel_status(guild, readonly.id) == f"#logs — Can send: {NexusEmojis.OFF}"


def test_premium_abgelaufen():
    expired = PremiumInfo(active=False, expires_at=datetime.now(timezone.utc) - timedelta(days=1), source="gift")
    text, show_buy, _ = describe_status(expired, TRANSLATIONS["de"])
    assert text.startswith(f"{NexusEmojis.OFF} ") and show_buy
