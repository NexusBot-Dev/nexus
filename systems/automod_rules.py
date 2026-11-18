"""Die AutoMod-Regeln, die Nexus in Discords eigener AutoMod-Engine anlegt.

Nexus erkennt seine Regeln an der Ersteller-ID (creator_id), nicht am Namen. Admins dürfen sie also
umbenennen, und bleiben sie nach einem Kick liegen, tragen sie neutrale Namen statt „Nexus — …“.
Übernimmt Nexus eine fremde Regel (adopt_rule), steht sie in der Tabelle automod_rules und zählt ab
dann ebenfalls als Nexus-Regel.

Meldungen schreibt Nexus selbst ins Log (systems/automod_log.py), Discords eigener Alarm ist aus.
Ältere Nexus-Regeln (alter Name „Nexus — …“, Discord-Alarm) bringt Nexus beim nächsten Kontakt auf
den aktuellen Stand.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

import discord
from cachetools import TTLCache

from config import DEV_GUILD_ID
from database import db_automod

log = logging.getLogger(__name__)

RULE_KINDS = ("spam", "mentions", "keywords", "presets", "names", "links")

# Testphase AutoMod 2.0: Solange RELEASED aus ist, gibt es die neuen Regeln, die Einstellungen je Regel und
# das Übernehmen fremder Regeln nur auf dem Dev-Server. Anderswo bleibt es bei Anti-Spam, Mention-Spam und
# Wortfilter mit Schalter und Wortliste. Freigabe für alle: True setzen und den Bot neu starten.
RELEASED = False
V2_KINDS = frozenset({"presets", "names", "links"})


def v2_available(guild_id: int) -> bool:
    return RELEASED or guild_id == DEV_GUILD_ID


def require_v2(guild_id: int) -> None:
    """Sperre der Testphase — im Bot, nicht nur in der Oberfläche. Wirft SettingsError."""
    if not v2_available(guild_id):
        raise SettingsError("dash_err_am_v2_unavailable")

_TRIGGER_TYPES = {
    "spam": discord.AutoModRuleTriggerType.spam,
    "mentions": discord.AutoModRuleTriggerType.mention_spam,
    "keywords": discord.AutoModRuleTriggerType.keyword,
    "presets": discord.AutoModRuleTriggerType.keyword_preset,
    "names": discord.AutoModRuleTriggerType.member_profile,
    "links": discord.AutoModRuleTriggerType.keyword,   # Wortfilter-Regel mit Mustern (systems/automod_links.py)
}
# Regeltyp → Art. Der Link-Filter fehlt bewusst: Er ist eine Wortfilter-Regel wie „keywords“ und wird nur
# über die Tabelle automod_rules erkannt (create_rule trägt ihn dort ein).
_KIND_BY_TRIGGER = {trigger: kind for kind, trigger in _TRIGGER_TYPES.items() if kind != "links"}

# Von diesen Typen erlaubt Discord nur eine Regel pro Server — eine fremde blockiert also den Platz
SINGLE_PER_GUILD = frozenset({"spam", "mentions", "presets", "names"})

# Namen bis AutoMod 2.0. Nur Nexus-Regeln mit genau diesem Namen werden umbenannt.
LEGACY_NAMES = {
    "Nexus — Anti-Spam": "spam",
    "Nexus — Mention-Spam": "mentions",
    "Nexus — Keyword-Filter": "keywords",
}

_DEFAULT_NAMES = {
    "spam": "Anti-Spam", "mentions": "Mention spam", "keywords": "Word filter",
    "presets": "Word lists", "names": "Name filter", "links": "Link filter",
}
DEFAULT_MENTION_LIMIT = 5

# Fertige Wortlisten von Discord. Beim Anlegen nur Slurs an, der Rest zum Dazuwählen.
PRESETS = ("profanity", "sexual_content", "slurs")
DEFAULT_PRESETS = ("slurs",)

# Einladungen auf Discord-Server (so auf einem Testserver geprüft). Erlaubte Einladungen stehen als
# erlaubte Wörter in der Regel (systems/automod_links.py).
INVITE_PATTERN = r"(?:discord(?:app)?\.com/invite|discord\.gg)/[a-zA-Z0-9-]+"


# Grenzen laut Discord-Doku (Auto Moderation)
MAX_CUSTOM_MESSAGE = 150      # Hinweis an den Nutzer beim Blockieren
MAX_EXEMPT_ROLES = 20
MAX_EXEMPT_CHANNELS = 50
MAX_MENTION_LIMIT = 50
# Timeout als Aktion der Regel: Discord führt ihn selbst aus, deshalb genau 4 Wochen (kein Sicherheitsabstand
# wie bei systems/timeouts.py nötig). Discord erlaubt ihn nur bei Wortfilter- und Mention-Spam-Regeln.
MAX_RULE_TIMEOUT_MINUTES = 4 * 7 * 24 * 60   # 40320
TIMEOUT_KINDS = frozenset({"keywords", "mentions", "links"})   # Link-Filter ist eine Wortfilter-Regel
MAX_WORDS = 1000              # Wortfilter und Namensfilter
MAX_ALLOW_LIST = 1000         # erlaubte Wörter bei den fertigen Wortlisten
MAX_WORD_LENGTH = 60


class ForeignRuleError(Exception):
    """Der Platz für diese Regel ist durch eine Regel belegt, die nicht von Nexus stammt."""


class SettingsError(Exception):
    """Einstellung nicht übernommen. key: Übersetzungs-Key (dash_err_am_*), params für den Text.
    invalid=True: Anfrage falsch aufgebaut (über die Oberfläche nicht erreichbar)."""

    def __init__(self, key: str, *, invalid: bool = False, **params):
        super().__init__(key)
        self.key, self.invalid, self.params = key, invalid, params


def rule_name(kind: str, lang: dict) -> str:
    return lang.get(f"automod_rule_name_{kind}", _DEFAULT_NAMES[kind])


def default_trigger(kind: str, keywords: list[str] | None = None) -> discord.AutoModTrigger:
    trigger_type = _TRIGGER_TYPES[kind]
    if kind == "mentions":
        return discord.AutoModTrigger(type=trigger_type, mention_limit=DEFAULT_MENTION_LIMIT)
    if kind in ("keywords", "names"):
        return discord.AutoModTrigger(type=trigger_type, keyword_filter=keywords or [])
    if kind == "presets":
        return discord.AutoModTrigger(type=trigger_type, presets=discord.AutoModPresets(**{p: True for p in DEFAULT_PRESETS}))
    if kind == "links":
        return discord.AutoModTrigger(type=trigger_type, keyword_filter=[], regex_patterns=[INVITE_PATTERN])
    return discord.AutoModTrigger(type=trigger_type)


def build_actions(kind: str) -> list[discord.AutoModRuleAction]:
    """Nur blockieren — die Meldung im Log schreibt Nexus selbst (systems/automod_log.py). Beim Namensfilter
    gibt es nichts zu blockieren: Discord sperrt Interaktionen, bis der Name geändert ist."""
    if kind == "names":
        return [discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_member_interactions)]
    return [discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)]


def _event_type(kind: str) -> discord.AutoModRuleEventType:
    return discord.AutoModRuleEventType.member_update if kind == "names" else discord.AutoModRuleEventType.message_send


def is_nexus_rule(guild: discord.Guild, rule: discord.AutoModRule, adopted: dict[int, str] | None = None) -> bool:
    """Von Nexus angelegt oder von Nexus übernommen (adopted: rule_id → Art aus der Tabelle automod_rules)."""
    return rule.creator_id == guild.me.id or rule.id in (adopted or {})


def _without_alert(rule: discord.AutoModRule) -> list[discord.AutoModRuleAction] | None:
    """Aktionen ohne Discords Alarm — None, wenn die Regel keinen hat."""
    if not any(a.type == discord.AutoModRuleActionType.send_alert_message for a in rule.actions):
        return None
    return [a for a in rule.actions if a.type != discord.AutoModRuleActionType.send_alert_message]


def needs_upgrade(guild: discord.Guild, rule: discord.AutoModRule) -> bool:
    """Nexus-Regel mit altem Namen oder mit Discords Alarm (wird durch das Nexus-Log ersetzt)."""
    return is_nexus_rule(guild, rule) and (rule.name in LEGACY_NAMES or _without_alert(rule) is not None)


async def upgrade_rules(
    guild: discord.Guild, rules: list[discord.AutoModRule], lang: dict,
) -> list[discord.AutoModRule]:
    """Bringt ältere Nexus-Regeln auf den aktuellen Stand: neutraler Name statt „Nexus — …“ und kein
    Discord-Alarm mehr (sonst stünde jede Meldung doppelt im Log). Gibt die Liste mit den aktualisierten
    Regeln zurück. Fremde Regeln und von Admins umbenannte Namen bleiben unangetastet."""
    result = []
    for rule in rules:
        if needs_upgrade(guild, rule):
            changes: dict = {}
            if kind := LEGACY_NAMES.get(rule.name):
                changes["name"] = rule_name(kind, lang)
            if (actions := _without_alert(rule)) is not None:
                changes["actions"] = actions
            try:
                rule = await rule.edit(**changes, reason=lang.get("automod_rule_upgraded", "Nexus AutoMod — updated"))
                log.info("AutoMod-Regel %s auf %s angepasst (%s).", rule.id, guild.id, ", ".join(changes))
            except discord.HTTPException as e:
                log.warning("AutoMod-Regel %s auf %s nicht angepasst: %s", rule.id, guild.id, e)
        result.append(rule)
    return result


# Regel-ID → Art bei Nexus-Regeln, None bei fremden. Für das Nexus-Log, das bei jedem Eingriff fragt —
# ohne jedes Mal Discord abzufragen. Auch fremde Regeln stehen drin: Sonst löste jede Admin-Regel bei jedem
# Eingriff eine Anfrage aus. IDs ändern sich nie; neue trägt create_rule ein, übernommene adopt_rule.
_rule_kinds: TTLCache[int, dict[int, str | None]] = TTLCache(maxsize=2_000, ttl=900)


@dataclass(frozen=True)
class RuleOverview:
    nexus: dict[str, discord.AutoModRule]     # Nexus-Regeln nach Art
    foreign: dict[str, discord.AutoModRule]   # fremde Regeln, die einen Einzelplatz belegen (übernehmbar)


async def get_overview(guild: discord.Guild, lang: dict) -> RuleOverview:
    """Alle Regeln des Servers mit EINEM Abruf: Nexus-Regeln nach Art und fremde Regeln auf Einzelplätzen.
    Ältere Nexus-Regeln werden dabei angepasst, Einträge gelöschter Regeln aus der Tabelle entfernt."""
    adopted = await db_automod.get_rule_kinds(guild.id)
    rules = await upgrade_rules(guild, await guild.fetch_automod_rules(), lang)
    if stale := set(adopted) - {r.id for r in rules}:
        await db_automod.delete_rule_kinds(guild.id, stale)
    nexus: dict[str, discord.AutoModRule] = {}
    foreign: dict[str, discord.AutoModRule] = {}
    kinds: dict[int, str | None] = {}
    for rule in sorted(rules, key=lambda r: r.id):   # bei mehreren Nexus-Wortfiltern gilt die älteste
        trigger_kind = _KIND_BY_TRIGGER.get(rule.trigger.type)
        if is_nexus_rule(guild, rule, adopted):
            kind = adopted.get(rule.id, trigger_kind)
            kinds[rule.id] = kind
            if kind:
                nexus.setdefault(kind, rule)
        else:
            kinds[rule.id] = None
            if trigger_kind in SINGLE_PER_GUILD:
                foreign.setdefault(trigger_kind, rule)
    _rule_kinds[guild.id] = kinds
    return RuleOverview(nexus=nexus, foreign=foreign)


async def get_nexus_rules(guild: discord.Guild, lang: dict) -> dict[str, discord.AutoModRule]:
    """Die Nexus-Regeln des Servers nach Art. Ältere Regeln werden dabei angepasst."""
    return (await get_overview(guild, lang)).nexus


async def nexus_rule_kind(guild: discord.Guild, rule_id: int, lang: dict) -> str | None:
    """Art der Nexus-Regel mit dieser ID, None bei fremden Regeln. Fragt Discord nur, wenn der Server
    noch nicht (oder nicht mehr) im Zwischenspeicher ist oder die Regel dort fehlt."""
    kinds = _rule_kinds.get(guild.id)
    if kinds is None or rule_id not in kinds:
        await get_nexus_rules(guild, lang)
        kinds = _rule_kinds.setdefault(guild.id, {})
        kinds.setdefault(rule_id, None)   # inzwischen gelöscht: nicht bei jedem Eingriff neu fragen
    return kinds[rule_id]


def _remember(guild: discord.Guild, rule: discord.AutoModRule, kind: str) -> None:
    if (kinds := _rule_kinds.get(guild.id)) is not None:
        kinds[rule.id] = kind


async def create_rule(
    guild: discord.Guild, kind: str, lang: dict, *, reason: str, keywords: list[str] | None = None,
) -> discord.AutoModRule:
    if kind in V2_KINDS:
        require_v2(guild.id)
    if kind in SINGLE_PER_GUILD and (await get_overview(guild, lang)).foreign.get(kind):
        raise ForeignRuleError(kind)
    rule = await guild.create_automod_rule(
        name=rule_name(kind, lang),
        event_type=_event_type(kind),
        trigger=default_trigger(kind, keywords),
        actions=build_actions(kind),
        enabled=True,
        reason=reason,
    )
    _remember(guild, rule, kind)
    if kind == "links":
        # Ohne Eintrag hielte Nexus den Link-Filter für einen zweiten Wortfilter
        await db_automod.set_rule_kind(guild.id, rule.id, kind)
        from systems import automod_links   # spät: automod_links baut auf diesem Modul auf
        await automod_links.sync(guild, lang)   # eigene Einladungen, Support-Server usw. sofort erlauben
        rule = (await get_nexus_rules(guild, lang))[kind]
    return rule


async def adopt_rule(guild: discord.Guild, kind: str, lang: dict, *, reason: str) -> discord.AutoModRule:
    """Fremde Regel auf einem Einzelplatz übernehmen: Ab dann verwaltet Nexus sie (Name, Log statt Discord-Alarm).
    Inhalt und übrige Einstellungen bleiben, wie der Admin sie hatte. Wirft SettingsError, wenn es nichts gibt."""
    require_v2(guild.id)
    rule = (await get_overview(guild, lang)).foreign.get(kind)
    if rule is None:
        raise SettingsError("dash_err_am_nothing_to_adopt")
    changes: dict = {"name": rule_name(kind, lang)}
    if (actions := _without_alert(rule)) is not None:
        changes["actions"] = actions
    rule = await rule.edit(**changes, reason=reason)
    await db_automod.set_rule_kind(guild.id, rule.id, kind)
    _remember(guild, rule, kind)
    log.info("AutoMod-Regel %s (%s) auf %s übernommen.", rule.id, kind, guild.id)
    return rule


async def enable_rule(guild: discord.Guild, kind: str, lang: dict, *, reason: str) -> discord.AutoModRule:
    """Schaltet die Nexus-Regel ein oder legt sie an. ForeignRuleError, wenn der Platz fremd belegt ist.
    Ausschalten geht immer (auch in der Testphase), einschalten neuer Regelarten nur mit require_v2."""
    if kind in V2_KINDS:
        require_v2(guild.id)
    rule = (await get_nexus_rules(guild, lang)).get(kind)
    if rule is None:
        return await create_rule(guild, kind, lang, reason=reason)
    if not rule.enabled:
        rule = await rule.edit(enabled=True, reason=reason)
    return rule


# ─── Modul aus/an ────────────────────────────────────────────────────────────

async def pause_for_module(guild: discord.Guild, lang: dict) -> None:
    """AutoMod-Modul ausgeschaltet: aktive Nexus-Regeln deaktivieren und merken (nicht löschen —
    Wortlisten und Einstellungen bleiben). Fehler bei einzelnen Regeln brechen nicht ab."""
    for rule in (await get_nexus_rules(guild, lang)).values():
        if not rule.enabled:
            continue
        try:
            await rule.edit(enabled=False, reason=lang.get("automod_module_paused", "Nexus AutoMod — module disabled"))
        except discord.HTTPException as e:
            log.warning("AutoMod-Regel %s auf %s nicht pausiert: %s", rule.id, guild.id, e)
            continue
        await db_automod.add_paused_rule(guild.id, rule.id)


async def resume_for_module(guild: discord.Guild, lang: dict) -> None:
    """AutoMod-Modul wieder eingeschaltet: genau die Regeln reaktivieren, die beim Ausschalten liefen."""
    paused = await db_automod.pop_paused_rules(guild.id)
    if not paused:
        return
    for rule in (await get_nexus_rules(guild, lang)).values():
        if rule.id in paused and not rule.enabled:
            try:
                await rule.edit(enabled=True, reason=lang.get("automod_module_resumed", "Nexus AutoMod — module enabled"))
            except discord.HTTPException as e:
                log.warning("AutoMod-Regel %s auf %s nicht reaktiviert: %s", rule.id, guild.id, e)


async def on_module_toggled(guild: discord.Guild, enabled: bool, lang: dict) -> None:
    """Gemeinsamer Weg für /modules und das Dashboard. Fehler (z. B. fehlendes Recht) loggen statt
    das Umschalten des Moduls scheitern zu lassen — das ist zu diesem Zeitpunkt schon gespeichert."""
    try:
        if enabled:
            await resume_for_module(guild, lang)
        else:
            await pause_for_module(guild, lang)
    except discord.HTTPException as e:
        log.warning("AutoMod-Regeln auf %s nicht %s: %s", guild.id, "reaktiviert" if enabled else "pausiert", e)


# ─── Einstellungen je Regel ──────────────────────────────────────────────────
# Alles steht in der Discord-Regel selbst — Nexus speichert nichts davon. Ändert ein Admin die Regel in
# den Servereinstellungen von Discord, sieht das Dashboard sofort den neuen Stand.

_UNSET = object()


def _action(rule: discord.AutoModRule, action_type) -> discord.AutoModRuleAction | None:
    return next((a for a in rule.actions if a.type == action_type), None)


def read_settings(kind: str, rule: discord.AutoModRule) -> dict:
    block = _action(rule, discord.AutoModRuleActionType.block_message)
    timeout = _action(rule, discord.AutoModRuleActionType.timeout)
    settings = {
        "enabled": rule.enabled,
        "custom_message": (block.custom_message or None) if block else None,
        "exempt_role_ids": [str(i) for i in sorted(rule.exempt_role_ids)],
        "exempt_channel_ids": [str(i) for i in sorted(rule.exempt_channel_ids)],
        "timeout_minutes": int(timeout.duration.total_seconds() // 60) if timeout and timeout.duration else 0,
    }
    if kind == "mentions":
        settings["mention_limit"] = rule.trigger.mention_limit
        settings["raid_protection"] = bool(rule.trigger.mention_raid_protection)
    return settings


def _ids(raw, field: str) -> list[int]:
    if not isinstance(raw, list) or not all(isinstance(i, (str, int)) and not isinstance(i, bool) and str(i).isdigit() for i in raw):
        raise SettingsError(f"{field} muss eine Liste gültiger IDs sein", invalid=True)
    return sorted({int(i) for i in raw})


def _int(raw, field: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise SettingsError(f"{field} muss eine ganze Zahl sein", invalid=True)
    return raw


async def update_settings(
    guild: discord.Guild, kind: str, lang: dict, *, reason: str,
    custom_message=_UNSET, exempt_role_ids=_UNSET, exempt_channel_ids=_UNSET,
    timeout_minutes=_UNSET, mention_limit=_UNSET, raid_protection=_UNSET,
) -> discord.AutoModRule:
    """Ändert die übergebenen Einstellungen der Nexus-Regel (nicht übergebene bleiben, wie sie sind).
    Erst wird alles geprüft, dann in EINEM Aufruf an Discord geschrieben. Wirft SettingsError."""
    require_v2(guild.id)
    rule = (await get_nexus_rules(guild, lang)).get(kind)
    if rule is None:
        raise SettingsError("dash_err_am_rule_missing")
    if kind == "names" and any(v is not _UNSET for v in (custom_message, timeout_minutes, exempt_channel_ids)):
        # Namen stehen in keinem Channel, und Discord sperrt hier Interaktionen statt zu blockieren
        raise SettingsError("dash_err_am_setting_unsupported")
    changes: dict = {}

    block = _action(rule, discord.AutoModRuleActionType.block_message)
    timeout = _action(rule, discord.AutoModRuleActionType.timeout)
    message = block.custom_message if block else None
    duration = timeout.duration if timeout else None

    if custom_message is not _UNSET:
        if custom_message is not None and not isinstance(custom_message, str):
            raise SettingsError("custom_message muss Text sein", invalid=True)
        message = (custom_message or "").strip() or None
        if message and len(message) > MAX_CUSTOM_MESSAGE:
            raise SettingsError("dash_err_am_message_too_long", max=MAX_CUSTOM_MESSAGE)

    if timeout_minutes is not _UNSET:
        minutes = _int(timeout_minutes, "timeout_minutes")
        if minutes and kind not in TIMEOUT_KINDS:
            raise SettingsError("dash_err_am_timeout_unsupported")
        if not 0 <= minutes <= MAX_RULE_TIMEOUT_MINUTES:
            raise SettingsError("dash_err_am_timeout_range", max=MAX_RULE_TIMEOUT_MINUTES)
        if minutes and not guild.me.guild_permissions.moderate_members:
            raise SettingsError("dash_err_am_timeout_no_permission")
        duration = timedelta(minutes=minutes) if minutes else None

    if custom_message is not _UNSET or timeout_minutes is not _UNSET:
        # Andere Aktionen (z. B. Interaktionen sperren, von Admins in Discord gesetzt) bleiben erhalten
        others = [a for a in rule.actions if a.type not in (
            discord.AutoModRuleActionType.block_message, discord.AutoModRuleActionType.timeout,
            discord.AutoModRuleActionType.send_alert_message,
        )]
        actions = [discord.AutoModRuleAction(custom_message=message) if message
                   else discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)]
        if duration:
            actions.append(discord.AutoModRuleAction(duration=duration))
        changes["actions"] = actions + others

    if exempt_role_ids is not _UNSET:
        role_ids = _ids(exempt_role_ids, "exempt_role_ids")
        if len(role_ids) > MAX_EXEMPT_ROLES:
            raise SettingsError("dash_err_am_too_many_roles", max=MAX_EXEMPT_ROLES)
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None or role.is_default():   # @everyone ausnehmen hieße: Regel greift bei niemandem
                raise SettingsError("dash_err_am_unknown_role")
        changes["exempt_roles"] = [discord.Object(id=i) for i in role_ids]

    if exempt_channel_ids is not _UNSET:
        channel_ids = _ids(exempt_channel_ids, "exempt_channel_ids")
        if len(channel_ids) > MAX_EXEMPT_CHANNELS:
            raise SettingsError("dash_err_am_too_many_channels", max=MAX_EXEMPT_CHANNELS)
        if any(guild.get_channel(i) is None for i in channel_ids):
            raise SettingsError("dash_err_am_unknown_channel")
        changes["exempt_channels"] = [discord.Object(id=i) for i in channel_ids]

    if mention_limit is not _UNSET or raid_protection is not _UNSET:
        if kind != "mentions":
            raise SettingsError("mention_limit/raid_protection gibt es nur bei mentions", invalid=True)
        limit = rule.trigger.mention_limit
        raid = bool(rule.trigger.mention_raid_protection)
        if mention_limit is not _UNSET:
            limit = _int(mention_limit, "mention_limit")
            if not 1 <= limit <= MAX_MENTION_LIMIT:
                raise SettingsError("dash_err_am_mention_limit_range", max=MAX_MENTION_LIMIT)
        if raid_protection is not _UNSET:
            if not isinstance(raid_protection, bool):
                raise SettingsError("raid_protection muss true/false sein", invalid=True)
            raid = raid_protection
        changes["trigger"] = discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.mention_spam, mention_limit=limit, mention_raid_protection=raid,
        )

    if not changes:
        return rule
    return await rule.edit(**changes, reason=reason)


# ─── Inhalt je Regel (Wörter, fertige Wortlisten) ────────────────────────────

def read_content(kind: str, rule: discord.AutoModRule) -> dict:
    trigger = rule.trigger
    if kind in ("keywords", "names"):
        return {"keywords": list(trigger.keyword_filter)}
    if kind == "presets":
        return {"presets": [p for p in PRESETS if getattr(trigger.presets, p)], "allow_list": list(trigger.allow_list)}
    return {}


def _words(raw, field: str, max_items: int) -> list[str]:
    """Wörter wie Discord sie vergleicht: klein, ohne Leerraum außen, ohne Doppelte (Reihenfolge bleibt)."""
    if not isinstance(raw, list) or not all(isinstance(w, str) for w in raw):
        raise SettingsError(f"{field} muss eine Liste von Texten sein", invalid=True)
    words = list(dict.fromkeys(w.strip().lower() for w in raw if w.strip()))
    if len(words) > max_items:
        raise SettingsError("dash_err_am_too_many_words", max=max_items)
    if any(len(w) > MAX_WORD_LENGTH for w in words):
        raise SettingsError("dash_err_am_word_too_long", max=MAX_WORD_LENGTH)
    return words


async def update_content(
    guild: discord.Guild, kind: str, lang: dict, *, reason: str,
    keywords=_UNSET, presets=_UNSET, allow_list=_UNSET,
) -> discord.AutoModRule:
    """Inhalt des Namensfilters (keywords) oder der fertigen Wortlisten (presets, allow_list) ändern.
    Den Wortfilter ändert weiterhin set_keywords im Dashboard. Wirft SettingsError."""
    require_v2(guild.id)
    rule = (await get_nexus_rules(guild, lang)).get(kind)
    if rule is None:
        raise SettingsError("dash_err_am_rule_missing")
    trigger = rule.trigger

    if kind == "names":
        if presets is not _UNSET or allow_list is not _UNSET or keywords is _UNSET:
            raise SettingsError("Namensfilter: nur keywords", invalid=True)
        new_trigger = discord.AutoModTrigger(
            type=_TRIGGER_TYPES[kind], keyword_filter=_words(keywords, "keywords", MAX_WORDS),
            regex_patterns=list(trigger.regex_patterns), allow_list=list(trigger.allow_list),
        )
    elif kind == "presets":
        if keywords is not _UNSET or (presets is _UNSET and allow_list is _UNSET):
            raise SettingsError("Wortlisten: presets und/oder allow_list", invalid=True)
        flags = {p: bool(getattr(trigger.presets, p)) for p in PRESETS}
        if presets is not _UNSET:
            if not isinstance(presets, list) or not set(presets) <= set(PRESETS):
                raise SettingsError(f"presets nur aus {PRESETS}", invalid=True)
            if not presets:
                raise SettingsError("dash_err_am_presets_empty")   # Discord verlangt mindestens eine Liste
            flags = {p: p in presets for p in PRESETS}
        allowed = list(trigger.allow_list) if allow_list is _UNSET else _words(allow_list, "allow_list", MAX_ALLOW_LIST)
        new_trigger = discord.AutoModTrigger(
            type=_TRIGGER_TYPES[kind], presets=discord.AutoModPresets(**flags), allow_list=allowed,
        )
    else:
        raise SettingsError(f"Kein Inhalt für {kind}", invalid=True)

    return await rule.edit(trigger=new_trigger, reason=reason)

