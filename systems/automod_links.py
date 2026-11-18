"""Link-Filter: eine Wortfilter-Regel in Discords AutoMod, die Einladungen auf fremde Server und gesperrte
Domains blockiert (systems/automod_rules.py, Art „links“).

Damit eigene Einladungen nicht mit geblockt werden, pflegt Nexus die erlaubten Wörter der Regel selbst.
Discord vergleicht sie mit dem ganzen „Wort“ (Text bis zum Leerzeichen), daher die Platzhalter:
  - Support-Server von Nexus:  */code*            (abschaltbar)
  - Vanity-URL des Servers:    */vanity, */vanity?*  (kein */vanity*: sonst ginge auch „vanity-nsfw“ durch)
  - Partnerlinks des Admins:   */code*
  - Einladungen des Servers:   */code*            (aus guild.invites(): ohne Ablauf zuerst, dann die am
                                                   längsten gültigen; über 100 fällt die bald ablaufende weg)
Abgeglichen wird bei neuen/gelöschten Einladungen (gebündelt), beim Speichern und alle paar Stunden —
Discord meldet abgelaufene Einladungen nicht.

Einstellungen stehen in automod_link_settings, die Regel wird daraus gebaut. Änderungen am Muster direkt
in Discord überschreibt Nexus beim nächsten Abgleich.
"""

import asyncio
import logging
import re
from urllib.parse import urlsplit

import discord

from cogs.utils import get_lang
from config import SUPPORT_INVITE_CODE
from database import db_automod, db_settings
from systems import automod_rules

log = logging.getLogger(__name__)

MAX_ALLOW_LIST = 100        # Discord: erlaubte Wörter bei Wortfilter-Regeln
MAX_REGEX = 10              # Discord: Muster pro Regel
MAX_REGEX_LENGTH = 260      # Discord: Zeichen pro Muster
MAX_PARTNERS = 25
SYNC_DELAY = 10.0           # Einladungs-Ereignisse so lange sammeln, dann einmal abgleichen

# Hostname: Labels aus Buchstaben, Ziffern, Bindestrich; mindestens eine Endung
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$")
_CODE_RE = re.compile(r"^[a-zA-Z0-9-]{2,32}$")
_INVITE_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg)/([a-zA-Z0-9-]+)/?(?:[?#].*)?$")

_DOMAIN_PREFIX = r"(?i)https?://(?:[a-z0-9-]+\.)*(?:"
_DOMAIN_SUFFIX = r")(?:[/:?#]|$)"


# ─── Eingaben ────────────────────────────────────────────────────────────────

def normalize_domain(raw: str) -> str | None:
    """„https://www.Example.com/pfad“ → „example.com“. None, wenn es keine gültige Domain ist."""
    text = raw.strip().lower()
    if not text:
        return None
    host = urlsplit(text if "://" in text else f"http://{text}").hostname or ""
    host = host.removeprefix("www.")
    return host if _DOMAIN_RE.match(host) else None


def parse_invite_code(raw: str) -> str | None:
    """Einladungslink oder Code → Code. None, wenn es keiner ist."""
    text = raw.strip()
    if match := _INVITE_URL_RE.match(text):
        return match.group(1)
    return text if _CODE_RE.match(text) else None


def domain_patterns(domains: list[str]) -> list[str]:
    """Muster für gesperrte Domains, so viele Domains pro Muster wie in 260 Zeichen passen.
    Trifft die Domain und ihre Subdomains (sub.example.com), aber nicht notexample.com."""
    patterns: list[str] = []
    current: list[str] = []
    for domain in domains:
        candidate = current + [re.escape(domain)]
        if len(_DOMAIN_PREFIX + "|".join(candidate) + _DOMAIN_SUFFIX) > MAX_REGEX_LENGTH and current:
            patterns.append(_DOMAIN_PREFIX + "|".join(current) + _DOMAIN_SUFFIX)
            current = [re.escape(domain)]
        else:
            current = candidate
    if current:
        patterns.append(_DOMAIN_PREFIX + "|".join(current) + _DOMAIN_SUFFIX)
    return patterns


# ─── Regel bauen ─────────────────────────────────────────────────────────────

async def _server_invites(guild: discord.Guild) -> list[discord.Invite]:
    """Gültige Einladungen des Servers: ohne Ablauf zuerst, dann die am längsten gültigen."""
    if not guild.me.guild_permissions.manage_guild:
        return []
    try:
        invites = await guild.invites()
    except discord.HTTPException as e:
        log.warning("Einladungen von %s nicht gelesen: %s", guild.id, e)
        return []
    now = discord.utils.utcnow()
    valid = [
        i for i in invites
        if (i.expires_at is None or i.expires_at > now) and not (i.max_uses and (i.uses or 0) >= i.max_uses)
    ]
    permanent = [i for i in valid if i.expires_at is None]
    temporary = sorted((i for i in valid if i.expires_at is not None), key=lambda i: i.expires_at, reverse=True)
    return permanent + temporary


async def allow_entries(guild: discord.Guild, settings: dict) -> list[str]:
    if not settings["block_invites"]:
        return []
    entries: list[str] = []
    if settings["allow_support"]:
        entries.append(f"*/{SUPPORT_INVITE_CODE}*")
    if vanity := guild.vanity_url_code:
        entries += [f"*/{vanity}", f"*/{vanity}?*"]
    entries += [f"*/{code}*" for code in settings["partner_codes"]]
    entries += [f"*/{invite.code}*" for invite in await _server_invites(guild)]
    return list(dict.fromkeys(e.lower() for e in entries))[:MAX_ALLOW_LIST]


def build_trigger(settings: dict, allow_list: list[str]) -> discord.AutoModTrigger:
    patterns = ([automod_rules.INVITE_PATTERN] if settings["block_invites"] else []) + domain_patterns(settings["blocked_domains"])
    return discord.AutoModTrigger(
        type=discord.AutoModRuleTriggerType.keyword, keyword_filter=[], regex_patterns=patterns, allow_list=allow_list,
    )


def _same(trigger: discord.AutoModTrigger, wanted: discord.AutoModTrigger) -> bool:
    return (
        list(trigger.regex_patterns) == list(wanted.regex_patterns)
        and [w.lower() for w in trigger.allow_list] == list(wanted.allow_list)
        and not trigger.keyword_filter
    )


async def sync(guild: discord.Guild, lang: dict | None = None) -> bool:
    """Link-Filter mit den Einstellungen und den aktuellen Einladungen abgleichen. True, wenn geändert."""
    lang = lang or await get_lang(guild.id)
    rule = (await automod_rules.get_nexus_rules(guild, lang)).get("links")
    if rule is None:
        return False
    settings = await db_automod.get_link_settings(guild.id)
    wanted = build_trigger(settings, await allow_entries(guild, settings))
    if _same(rule.trigger, wanted):
        return False
    await rule.edit(trigger=wanted, reason=lang.get("automod_links_synced", "Nexus AutoMod — link filter updated"))
    return True


# ─── Einladungs-Ereignisse und regelmäßiger Abgleich ─────────────────────────

_scheduled: set[int] = set()


async def _has_active_link_rule(guild_id: int) -> bool:
    """Nur Server mit Nexus-Link-Filter und eingeschaltetem AutoMod-Modul abgleichen. Die Einladungs-Ereignisse
    kommen von jedem Server; ohne diese Prüfung fragt sync() überall die AutoMod-Regeln ab (403 ohne „Server verwalten“)."""
    if "links" not in (await db_automod.get_rule_kinds(guild_id)).values():
        return False
    return await db_settings.is_module_enabled(guild_id, "automod")   # aus: Regeln sind pausiert


async def _sync_logged(guild: discord.Guild) -> None:
    try:
        await sync(guild)
    except discord.Forbidden:
        # Recht „Server verwalten“ entzogen: kein Stacktrace bei jeder Einladung, der Filter bleibt wie er ist
        log.warning("Link-Filter auf %s nicht abgeglichen: Nexus fehlt das Recht „Server verwalten“.", guild.id)
    except Exception:
        log.exception("Link-Filter auf %s nicht abgeglichen.", guild.id)


async def _delayed_sync(guild: discord.Guild) -> None:
    try:
        await asyncio.sleep(SYNC_DELAY)
    finally:
        _scheduled.discard(guild.id)
    if await _has_active_link_rule(guild.id):
        await _sync_logged(guild)


def schedule_sync(guild: discord.Guild) -> None:
    """Nach on_invite_create/delete: in SYNC_DELAY Sekunden einmal abgleichen (viele Ereignisse → ein Abgleich)."""
    if guild.id in _scheduled:
        return
    _scheduled.add(guild.id)
    asyncio.create_task(_delayed_sync(guild))


async def sync_all(bot, *, pause: float = 2.0) -> None:
    """Alle Server mit Link-Filter abgleichen — fängt abgelaufene Einladungen auf."""
    for guild_id in await db_automod.get_guilds_with_link_rule():
        guild = bot.get_guild(guild_id)
        if guild is None or guild.unavailable or guild.me is None:
            continue
        if not await db_settings.is_module_enabled(guild_id, "automod"):
            continue   # Regeln sind pausiert
        await _sync_logged(guild)
        await asyncio.sleep(pause)


# ─── Einstellungen (Dashboard) ───────────────────────────────────────────────

_UNSET = object()


async def read_content(guild: discord.Guild, rule: discord.AutoModRule) -> dict:
    settings = await db_automod.get_link_settings(guild.id)
    return {
        "block_invites": settings["block_invites"],
        "allow_support": settings["allow_support"],
        "blocked_domains": settings["blocked_domains"],
        "partner_codes": settings["partner_codes"],
        "allow_list_count": len(rule.trigger.allow_list),   # wie viele der 100 Plätze belegt sind
    }


async def update_content(
    guild: discord.Guild, lang: dict, *,
    block_invites=_UNSET, allow_support=_UNSET, blocked_domains=_UNSET, partner_links=_UNSET,
) -> discord.AutoModRule:
    """Einstellungen prüfen, speichern und die Regel sofort abgleichen. Wirft automod_rules.SettingsError."""
    SettingsError = automod_rules.SettingsError
    automod_rules.require_v2(guild.id)
    rule = (await automod_rules.get_nexus_rules(guild, lang)).get("links")
    if rule is None:
        raise SettingsError("dash_err_am_rule_missing")
    settings = await db_automod.get_link_settings(guild.id)

    for name, value in (("block_invites", block_invites), ("allow_support", allow_support)):
        if value is not _UNSET:
            if not isinstance(value, bool):
                raise SettingsError(f"{name} muss true/false sein", invalid=True)
            settings[name] = value

    if blocked_domains is not _UNSET:
        if not isinstance(blocked_domains, list) or not all(isinstance(d, str) for d in blocked_domains):
            raise SettingsError("blocked_domains muss eine Liste von Texten sein", invalid=True)
        domains = []
        for raw in blocked_domains:
            if not raw.strip():
                continue
            if (domain := normalize_domain(raw)) is None:
                raise SettingsError("dash_err_am_domain_invalid", domain=raw.strip()[:60])
            domains.append(domain)
        domains = list(dict.fromkeys(domains))
        if len(domain_patterns(domains)) > MAX_REGEX - 1:   # ein Muster bleibt für Einladungen
            raise SettingsError("dash_err_am_too_many_domains")
        settings["blocked_domains"] = domains

    if partner_links is not _UNSET:
        if not isinstance(partner_links, list) or not all(isinstance(p, str) for p in partner_links):
            raise SettingsError("partner_links muss eine Liste von Texten sein", invalid=True)
        codes = []
        for raw in partner_links:
            if not raw.strip():
                continue
            if (code := parse_invite_code(raw)) is None:
                raise SettingsError("dash_err_am_partner_invalid", link=raw.strip()[:60])
            codes.append(code)
        codes = list(dict.fromkeys(codes))
        if len(codes) > MAX_PARTNERS:
            raise SettingsError("dash_err_am_too_many_partners", max=MAX_PARTNERS)
        settings["partner_codes"] = codes

    await db_automod.save_link_settings(guild.id, settings)
    await sync(guild, lang)
    return (await automod_rules.get_nexus_rules(guild, lang))["links"]
