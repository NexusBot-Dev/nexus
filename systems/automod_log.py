"""Nexus-Log für Eingriffe der Nexus-AutoMod-Regeln (ersetzt Discords eigenen Alarm) und Einstieg für die
Eskalation (systems/automod_escalation.py: ein Strike pro Verstoß).

Discord meldet jeden ausgeführten Eingriff einzeln (on_automod_action). Eine Regel mit „blockieren +
Timeout“ löst also zwei Meldungen aus — sie werden kurz gesammelt und als EIN Log-Eintrag geschrieben.
Fremde Regeln (von Admins angelegt) loggt Nexus nicht; dort gilt, was der Admin eingestellt hat.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

import discord

from cogs.utils import get_lang
from database import db_settings
from emojis import NexusEmojis
from systems import automod_escalation, automod_rules
from systems.ui import with_emoji

log = logging.getLogger(__name__)

# So lange werden Meldungen zum selben Verstoß gesammelt (Blockieren und Timeout kommen getrennt)
COLLECT_SECONDS = 1.5
MAX_CONTENT = 1000   # Sicherheitsgrenze fürs Embed-Feld (Discord: höchstens 1024 Zeichen)
CONTEXT = 100        # Zeichen vor und nach der Fundstelle im Ausschnitt
NO_MATCH_LENGTH = 300   # ohne Fundstelle (Spam, Mention-Spam): so viel vom Anfang
WORD_SNAP = 20       # Schnitt so weit zur nächsten Wortgrenze verschieben, damit kein Wort halbiert wird
MAX_LINK_LENGTH = 200   # sehr lange Links im Ausschnitt kürzen

# Links im Ausschnitt stehen als Code: nicht anklickbar (Spam-Einladung im Mod-Log) und ohne `**` mitten im
# Link — das machte Discord sonst zu „%2A%2A“ in der Adresse
_LINKISH = re.compile(r"(?i)(?:https?://|discord(?:app)?\.com/invite/|discord\.gg/|www\.)")
_TOKENS = re.compile(r"\S+|\s+")

_ACTION_ORDER = (
    discord.AutoModRuleActionType.block_message,
    discord.AutoModRuleActionType.timeout,
    discord.AutoModRuleActionType.block_member_interactions,
)


@dataclass
class _Pending:
    execution: discord.AutoModAction
    actions: dict[discord.AutoModRuleActionType, discord.AutoModRuleAction] = field(default_factory=dict)


_pending: dict[tuple, _Pending] = {}


def _key(execution: discord.AutoModAction) -> tuple:
    return (execution.guild_id, execution.rule_id, execution.user_id, execution.channel_id, execution.content)


async def handle_action(execution: discord.AutoModAction) -> None:
    """Einstieg aus cogs/automod.py (on_automod_action)."""
    # Discords Alarm (alte Regeln vor dem Umstellen) ist keine eigene Maßnahme
    if execution.action.type == discord.AutoModRuleActionType.send_alert_message:
        return
    key = _key(execution)
    pending = _pending.get(key)
    if pending is not None:
        pending.actions[execution.action.type] = execution.action
        return
    _pending[key] = _Pending(execution, {execution.action.type: execution.action})
    try:
        await asyncio.sleep(COLLECT_SECONDS)
    finally:
        pending = _pending.pop(key)
    try:
        await _process(pending)
    except Exception:
        log.exception("AutoMod-Log auf %s fehlgeschlagen.", execution.guild_id)


def _duration(delta: timedelta, lang: dict) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes and minutes % (60 * 24) == 0:
        days = minutes // (60 * 24)
        return lang.get("automod_log_day_one", "1 d") if days == 1 else lang.get("automod_log_days", "{n} d").format(n=days)
    if minutes and minutes % 60 == 0:
        return lang.get("automod_log_hours", "{n} h").format(n=minutes // 60)
    return lang.get("automod_log_minutes", "{n} min").format(n=max(minutes, 1))


def _measures(actions: dict, lang: dict) -> str:
    lines = []
    for action_type in _ACTION_ORDER:
        action = actions.get(action_type)
        if action is None:
            continue
        if action_type == discord.AutoModRuleActionType.block_message:
            lines.append(lang.get("automod_log_blocked", "Message blocked"))
        elif action_type == discord.AutoModRuleActionType.timeout:
            lines.append(lang.get("automod_log_timeout", "Timeout ({duration})").format(
                duration=_duration(action.duration, lang),
            ))
        else:
            lines.append(lang.get("automod_log_interactions", "Interactions blocked"))
    return "\n".join(lines)


def _normalize(text: str) -> str:
    """Leerzeilen am Stück zu einer zusammenfassen, Leerzeilen am Anfang und Ende weg — sonst bestünde der
    Ausschnitt bei „HALLO“ + 20 Leerzeilen + „Kekse“ fast nur aus leeren Zeilen."""
    lines: list[str] = []
    for line in text.splitlines():
        line = line.rstrip()
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip("\n")


def _snap_start(text: str, start: int) -> int:
    """Anfang des Ausschnitts auf den nächsten Wortanfang schieben (höchstens WORD_SNAP Zeichen)."""
    if start == 0 or text[start - 1].isspace():
        return start
    for i in range(start, min(start + WORD_SNAP, len(text))):
        if text[i].isspace():
            return i + 1
    return start


def _snap_end(text: str, end: int) -> int:
    """Ende des Ausschnitts auf das vorige Wortende schieben (höchstens WORD_SNAP Zeichen)."""
    if end >= len(text) or text[end].isspace():
        return end
    for i in range(end, max(end - WORD_SNAP, 0), -1):
        if text[i - 1].isspace():
            return i - 1
    return end


def _render(text: str, start: int, end: int, hit: tuple[int, int] | None) -> str:
    """text[start:end] für Discord: Markdown entschärft, Links als Code, die Fundstelle fett."""
    esc = discord.utils.escape_markdown
    out: list[str] = []
    for m in _TOKENS.finditer(text, start, end):
        token, a, b = m.group(), m.start(), m.end()
        if token.isspace():
            out.append(token)
            continue
        overlaps = hit is not None and a < hit[1] and b > hit[0]
        if _LINKISH.search(token):
            if len(token) > MAX_LINK_LENGTH:
                token = token[:MAX_LINK_LENGTH - 1] + "…"
            code = "`" + token.replace("`", "ˋ") + "`"
            out.append(f"**{code}**" if overlaps else code)
        elif overlaps:
            s, e = max(hit[0], a) - a, min(hit[1], b) - a
            out.append(esc(token[:s]) + "**" + esc(token[s:e]) + "**" + esc(token[e:]))
        else:
            out.append(esc(token))
    return "".join(out)


def _excerpt(content: str, matched: str | None) -> str:
    """Ausschnitt fürs Log: rund CONTEXT Zeichen vor und nach der Fundstelle, die Fundstelle fett.
    Ohne Fundstelle die ersten NO_MATCH_LENGTH Zeichen. Markdown entschärft, Links als Code, als Zitat."""
    text = _normalize(content)
    pos = text.lower().find(matched.lower()) if matched else -1
    if pos >= 0:
        hit = (pos, pos + len(matched))
        start = _snap_start(text, max(0, pos - CONTEXT))
        end = _snap_end(text, min(len(text), hit[1] + CONTEXT))
        start, end = min(start, pos), max(end, hit[1])   # die Fundstelle selbst nie abschneiden
    else:
        hit = None
        start, end = 0, _snap_end(text, min(len(text), NO_MATCH_LENGTH))
    body = _render(text, start, end, hit)
    body = ("…" if start > 0 else "") + body + ("…" if end < len(text) else "")
    return _quote(body)


def _quote(text: str) -> str:
    """Als Zitat („> “ je Zeile). Gekürzt wird erst ganz am Ende: über 1024 Zeichen lehnt Discord das
    ganze Embed ab. Ein einzelner \\ am Schnitt würde das „…“ entschärfen, deshalb fällt er weg."""
    quoted = "\n".join(f"> {line}" if line else ">" for line in text.splitlines()) or ">"
    if len(quoted) > MAX_CONTENT:
        quoted = quoted[:MAX_CONTENT - 1].rstrip("\\") + "…"
    return quoted


def _trigger_text(kind: str, keyword: str | None, lang: dict) -> str | None:
    """Auslöser fürs Log. Beim Link-Filter meldet Discord das Muster (Regex) — das versteht niemand,
    deshalb dort ein Klartext: fremde Einladung oder gesperrte Domain."""
    if not keyword:
        return None
    if kind == "links":
        if keyword == automod_rules.INVITE_PATTERN:
            return lang.get("automod_log_trigger_invite", "Invite to another server")
        return lang.get("automod_log_trigger_domain", "Blocked domain")
    return discord.utils.escape_markdown(keyword)[:256]


async def _process(pending: _Pending) -> None:
    """Ein Verstoß: Eskalation prüfen, dann loggen (beides nur für Nexus-Regeln)."""
    execution = pending.execution
    guild = execution.guild
    if guild is None or guild.me is None:
        return
    if not await db_settings.is_module_enabled(guild.id, "automod"):
        return
    lang = await get_lang(guild.id)
    kind = await automod_rules.nexus_rule_kind(guild, execution.rule_id, lang)
    if kind is None:
        return   # fremde Regel: Nexus loggt und zählt nur seine eigenen
    escalation = None
    try:   # auch ohne Log-Channel
        escalation = await automod_escalation.on_violation(guild, execution.user_id, kind, lang)
    except Exception:
        log.exception("AutoMod-Eskalation auf %s fehlgeschlagen.", guild.id)
    await _write_log(pending, kind, lang, escalation)


def _escalation_text(result: automod_escalation.Result, lang: dict) -> str:
    if result.action == "timeout":
        measure = lang.get("automod_escalation_timeout", "Timeout ({duration})").format(
            duration=automod_escalation.window_label(result.timeout_minutes, lang),
        )
    else:
        measure = lang.get(f"automod_escalation_{result.action}", result.action)
    lines = [
        lang.get("automod_escalation_step", "Level {step}/{total}: {measure}").format(
            step=result.step, total=result.total, measure=measure,
        ),
        lang.get("automod_escalation_cause", "{count} violations within {window}").format(
            count=result.strikes, window=automod_escalation.window_label(result.window_minutes, lang),
        ),
    ]
    if not result.ok:
        lines.append(with_emoji(NexusEmojis.WARNING, lang.get(
            "automod_escalation_failed", "Failed — missing permissions, role too low or user gone.",
        )))
    return "\n".join(lines)


async def _write_log(pending: _Pending, kind: str, lang: dict, escalation: "automod_escalation.Result | None") -> None:
    execution = pending.execution
    guild = execution.guild
    settings = await db_settings.get_guild(guild.id)
    channel = guild.get_channel(settings.get("log_channel_id")) if settings and settings.get("log_channel_id") else None
    if channel is None:
        return
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        log.info("AutoMod-Log: kein Schreibrecht im Log-Channel %s auf %s.", channel.id, guild.id)
        return

    embed = discord.Embed(
        title=f"{NexusEmojis.SHIELD} " + lang.get("automod_log_title", "AutoMod stepped in"),
        color=await db_settings.get_color(guild.id),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name=lang.get("automod_log_user", "User"), value=f"<@{execution.user_id}>", inline=True)
    if execution.channel_id:
        embed.add_field(name=lang.get("automod_log_channel", "Channel"), value=f"<#{execution.channel_id}>", inline=True)
    embed.add_field(name=lang.get("automod_log_rule", "Rule"), value=automod_rules.rule_name(kind, lang), inline=True)
    if trigger := _trigger_text(kind, execution.matched_keyword, lang):
        embed.add_field(name=lang.get("automod_log_keyword", "Matched"), value=trigger, inline=True)
    embed.add_field(name=lang.get("automod_log_measures", "Action"), value=_measures(pending.actions, lang), inline=True)
    if escalation is not None:
        embed.add_field(name=lang.get("automod_escalation_title", "Escalation"), value=_escalation_text(escalation, lang), inline=False)
    if execution.content:
        embed.add_field(name=lang.get("automod_log_message", "Message"), value=_excerpt(execution.content, execution.matched_content), inline=False)
    embed.set_footer(text=f"User-ID: {execution.user_id}")

    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.warning("AutoMod-Log in %s auf %s nicht gesendet: %s", channel.id, guild.id, e)
