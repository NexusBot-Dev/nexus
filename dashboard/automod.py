import discord
from .registry import register_action, DashCtx, DashboardError, InvalidRequest
from database import db_automod, db_honeypot
from cogs.utils import get_lang
from systems import automod_escalation, automod_links, automod_rules, shield_whitelist
from systems import honeypot as hp_system
from systems.hierarchy import issue_text

async def _rule_data(guild, kind: str, rule) -> dict:
    content = await automod_links.read_content(guild, rule) if kind == "links" else automod_rules.read_content(kind, rule)
    return {**automod_rules.read_settings(kind, rule), **content}


@register_action("automod", "status")
async def status(ctx: DashCtx) -> dict:
    guild, guild_id = ctx.guild, ctx.guild_id
    overview = await automod_rules.get_overview(guild, await get_lang(guild_id))
    active_rules = overview.nexus
    shield_enabled = bool(await db_automod.get_antinuke(guild_id))
    honeypot_channel = await hp_system.get_channel(guild)   # von Hand gelöscht → kein Honeypot
    honeypot = await db_honeypot.get_honeypot_settings(guild_id)
    # Server-Sprache: der Standardtitel steht so in Discord. Liest beim ersten Mal die bestehende Nachricht aus.
    honeypot_title, honeypot_text = await hp_system.load_texts(guild, honeypot_channel, honeypot, await get_lang(guild_id))

    keywords_rule = active_rules.get("keywords")
    return {
        # Einstellungen je Regel (None: Regel noch nicht angelegt) und die Grenzen von Discord fürs Formular
        # Testphase AutoMod 2.0: False → nur Anti-Spam, Mention-Spam, Wortfilter mit Schalter und Wortliste
        "v2": automod_rules.v2_available(guild_id),
        "rules": {kind: await _rule_data(guild, kind, rule) if (rule := active_rules.get(kind)) else None
                  for kind in automod_rules.RULE_KINDS},
        # Fremde Regeln, die einen Platz belegen, den Discord nur einmal vergibt — Nexus kann sie übernehmen
        "foreign_rules": {kind: rule.name for kind, rule in overview.foreign.items()},
        # Eskalation (Testphase wie AutoMod 2.0): Stufen, Grenze Free/Premium und Auswahlwerte fürs Formular
        "escalation": await automod_escalation.read(guild) if automod_rules.v2_available(guild_id) else None,
        "presets": list(automod_rules.PRESETS),
        "rule_limits": {
            "custom_message": automod_rules.MAX_CUSTOM_MESSAGE,
            "exempt_roles": automod_rules.MAX_EXEMPT_ROLES,
            "exempt_channels": automod_rules.MAX_EXEMPT_CHANNELS,
            "mention_limit": automod_rules.MAX_MENTION_LIMIT,
            "timeout_minutes": automod_rules.MAX_RULE_TIMEOUT_MINUTES,
            "timeout_kinds": sorted(automod_rules.TIMEOUT_KINDS),
            "words": automod_rules.MAX_WORDS,
            "allow_list": automod_rules.MAX_ALLOW_LIST,
            "word_length": automod_rules.MAX_WORD_LENGTH,
            "partners": automod_links.MAX_PARTNERS,
            "link_allow_list": automod_links.MAX_ALLOW_LIST,
        },
        "spam_enabled": "spam" in active_rules and active_rules["spam"].enabled,
        "mentions_enabled": "mentions" in active_rules and active_rules["mentions"].enabled,
        "keywords_enabled": keywords_rule is not None and keywords_rule.enabled,
        "keywords": list(keywords_rule.trigger.keyword_filter) if keywords_rule else [],
        "shield_enabled": shield_enabled,
        "shield_can_disable": shield_whitelist.is_owner(guild, ctx.actor),   # ausschalten: nur der Besitzer
        "honeypot_channel_id": str(honeypot_channel.id) if honeypot_channel else None,
        "honeypot_timeout_days": honeypot["timeout_days"],
        "honeypot_catches": honeypot["catches"],
        "honeypot_title": honeypot_title,
        "honeypot_text": honeypot_text,
        "honeypot_punish_members": honeypot["punish_members"],
        "honeypot_punish_bots": honeypot["punish_bots"],
        "honeypot_day_options": list(db_honeypot.TIMEOUT_DAY_OPTIONS),
    }

@register_action("automod", "toggle-rule")
async def toggle_rule(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    rule_key = payload.get("rule_key")
    if rule_key not in automod_rules.RULE_KINDS:
        raise InvalidRequest(f"Unbekannte Regel: {rule_key!r}")

    lang = await get_lang(guild_id)
    rule = (await automod_rules.get_nexus_rules(guild, lang)).get(rule_key)

    if rule and rule.enabled:
        await rule.edit(enabled=False, reason="Nexus Dashboard — deaktiviert")
        enabled = False
    else:
        try:
            await automod_rules.enable_rule(guild, rule_key, lang, reason="Nexus Dashboard — aktiviert")
        except automod_rules.ForeignRuleError:
            raise DashboardError("dash_err_am_foreign_rule") from None
        except automod_rules.SettingsError as e:
            raise _settings_error(e) from None
        enabled = True

    # Neuer Zustand, damit das Dashboard ihn übernimmt statt nur seine Anzeige umzudrehen
    # (die Regel kann seit dem Laden der Seite in Discord geändert worden sein)
    return {"rule_key": rule_key, "enabled": enabled}

@register_action("automod", "set-keywords")
async def set_keywords(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    keywords = payload.get("keywords")
    if not isinstance(keywords, list):
        raise InvalidRequest("keywords muss eine Liste sein")
    keywords = [str(k).strip().lower() for k in keywords if str(k).strip()][:1000]

    lang = await get_lang(guild_id)
    rule = (await automod_rules.get_nexus_rules(guild, lang)).get("keywords")

    if rule:
        await rule.edit(
            trigger=discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword, keyword_filter=keywords),
            reason="Nexus Dashboard — Keywords aktualisiert",
        )
        enabled = rule.enabled
    else:
        await automod_rules.create_rule(
            guild, "keywords", lang, reason="Nexus Dashboard — Keywords erstellt", keywords=keywords,
        )
        enabled = True   # neu angelegte Regel ist sofort aktiv

    return {"keywords": keywords, "enabled": enabled}

_SETTING_FIELDS = ("custom_message", "exempt_role_ids", "exempt_channel_ids", "timeout_minutes",
                   "mention_limit", "raid_protection")


@register_action("automod", "rule-settings")
async def rule_settings(ctx: DashCtx) -> dict:
    """Einstellungen einer Regel ändern. Nur die mitgeschickten Felder werden geändert."""
    payload = ctx.payload
    rule_key = payload.get("rule_key")
    if rule_key not in automod_rules.RULE_KINDS:
        raise InvalidRequest(f"Unbekannte Regel: {rule_key!r}")
    fields = {name: payload[name] for name in _SETTING_FIELDS if name in payload}
    if not fields:
        raise InvalidRequest("Keine Einstellung übergeben")
    try:
        rule = await automod_rules.update_settings(
            ctx.guild, rule_key, await get_lang(ctx.guild_id), reason="Nexus Dashboard — Einstellungen", **fields,
        )
    except automod_rules.SettingsError as e:
        raise _settings_error(e) from None
    return {"rule_key": rule_key, **automod_rules.read_settings(rule_key, rule)}

def _settings_error(e: automod_rules.SettingsError) -> DashboardError:
    return InvalidRequest(e.key) if e.invalid else DashboardError(e.key, **e.params)


@register_action("automod", "rule-content")
async def rule_content(ctx: DashCtx) -> dict:
    """Inhalt ändern: fertige Wortlisten (presets, allow_list), Namensfilter (keywords) oder Link-Filter
    (block_invites, allow_support, blocked_domains, partner_links)."""
    payload = ctx.payload
    rule_key = payload.get("rule_key")
    lang = await get_lang(ctx.guild_id)
    try:
        if rule_key == "links":
            fields = {n: payload[n] for n in ("block_invites", "allow_support", "blocked_domains", "partner_links") if n in payload}
            if not fields:
                raise InvalidRequest("Keine Einstellung übergeben")
            rule = await automod_links.update_content(ctx.guild, lang, **fields)
            return {"rule_key": rule_key, **await automod_links.read_content(ctx.guild, rule)}
        if rule_key not in ("presets", "names"):
            raise InvalidRequest(f"Kein Inhalt für {rule_key!r}")
        fields = {name: payload[name] for name in ("keywords", "presets", "allow_list") if name in payload}
        rule = await automod_rules.update_content(ctx.guild, rule_key, lang, reason="Nexus Dashboard — Inhalt", **fields)
    except automod_rules.SettingsError as e:
        raise _settings_error(e) from None
    return {"rule_key": rule_key, **automod_rules.read_content(rule_key, rule)}


@register_action("automod", "escalation")
async def escalation(ctx: DashCtx) -> dict:
    """Eskalation ändern: {enabled?, steps?} — nur die mitgeschickten Felder."""
    fields = {name: ctx.payload[name] for name in ("enabled", "steps") if name in ctx.payload}
    if not fields:
        raise InvalidRequest("Keine Einstellung übergeben")
    try:
        return await automod_escalation.update(ctx.guild, **fields)
    except automod_rules.SettingsError as e:
        raise _settings_error(e) from None


@register_action("automod", "adopt-rule")
async def adopt_rule(ctx: DashCtx) -> dict:
    """Fremde Regel auf einem Einzelplatz von Nexus übernehmen lassen (das Dashboard fragt vorher nach)."""
    rule_key = ctx.payload.get("rule_key")
    if rule_key not in automod_rules.SINGLE_PER_GUILD:
        raise InvalidRequest(f"Nicht übernehmbar: {rule_key!r}")
    try:
        rule = await automod_rules.adopt_rule(
            ctx.guild, rule_key, await get_lang(ctx.guild_id), reason="Nexus Dashboard — übernommen",
        )
    except automod_rules.SettingsError as e:
        raise _settings_error(e) from None
    return {"rule_key": rule_key, **automod_rules.read_settings(rule_key, rule), **automod_rules.read_content(rule_key, rule)}


@register_action("automod", "toggle-shield")
async def toggle_shield(ctx: DashCtx) -> dict:
    guild_id = ctx.guild_id
    current = bool(await db_automod.get_antinuke(guild_id))
    # Einschalten darf jeder mit Dashboard-Zugriff, ausschalten nur der Besitzer (systems/shield_whitelist.py)
    if current and not shield_whitelist.is_owner(ctx.guild, ctx.actor):
        raise DashboardError("dash_err_shield_owner_only")
    new_status = await db_automod.set_antinuke(guild_id, not current)
    return {"shield_enabled": bool(new_status)}


@register_action("automod", "shield-whitelist")
async def shield_whitelist_status(ctx: DashCtx) -> dict:
    """Bots auf dem Server mit Whitelist-Status, eingetragene Rollen und was der Eingeloggte ändern darf.
    Eigene Aktion statt in „status“: lädt beim ersten Mal die Mitgliederliste nach."""
    return await shield_whitelist.overview(ctx.guild, ctx.actor)


def _snowflake(value) -> int:
    if not isinstance(value, str) or not value.isdigit():
        raise InvalidRequest(f"Ungültige ID: {value!r}")
    return int(value)


@register_action("automod", "shield-bot")
async def shield_bot(ctx: DashCtx) -> dict:
    """Einen Bot ein- oder austragen: {bot_id, whitelisted}."""
    bot_id = _snowflake(ctx.payload.get("bot_id"))
    whitelisted = ctx.payload.get("whitelisted")
    if not isinstance(whitelisted, bool):
        raise InvalidRequest("whitelisted muss true/false sein")
    try:
        await shield_whitelist.set_bot(ctx.guild, ctx.actor, bot_id, whitelisted)
    except shield_whitelist.WhitelistError as e:
        raise DashboardError(e.key, **e.params) from None
    return await shield_whitelist.overview(ctx.guild, ctx.actor)


@register_action("automod", "shield-roles")
async def shield_roles(ctx: DashCtx) -> dict:
    """Rollen-Whitelist ersetzen: {role_ids: [...]}."""
    role_ids = ctx.payload.get("role_ids")
    if not isinstance(role_ids, list):
        raise InvalidRequest("role_ids muss eine Liste sein")
    try:
        await shield_whitelist.set_roles(ctx.guild, ctx.actor, [_snowflake(r) for r in role_ids])
    except shield_whitelist.WhitelistError as e:
        raise DashboardError(e.key, **e.params) from None
    return await shield_whitelist.overview(ctx.guild, ctx.actor)

def _honeypot_texts(payload: dict) -> tuple[str, str]:
    """Titel (optional — leer = Standardtitel in der Server-Sprache) und Warntext aus dem Request."""
    title = str(payload.get("title") or "").strip()
    text = str(payload.get("warn_text") or "").strip()
    if len(title) > db_honeypot.WARN_TITLE_MAX:
        raise DashboardError("dash_err_am_honeypot_title_too_long")
    if len(text) > db_honeypot.WARN_TEXT_MAX:
        raise DashboardError("dash_err_am_honeypot_text_too_long")
    return title, text

@register_action("automod", "create-honeypot")
async def create_honeypot(ctx: DashCtx) -> dict:
    guild, payload = ctx.guild, ctx.payload
    name = (payload.get("name") or "").strip()
    title, warn_text = _honeypot_texts(payload)
    if not name or not warn_text:
        raise DashboardError("dash_err_am_honeypot_text_required")

    server_lang = await get_lang(guild.id)   # Warn-Nachricht steht in Discord
    try:
        channel = await hp_system.create(guild, name, title, warn_text, server_lang)
    except hp_system.HoneypotError as e:
        if e.code == "perms":
            raise ValueError(issue_text(e.check, ctx.lang)) from None
        if e.code == "exists":
            raise DashboardError("dash_err_am_honeypot_exists") from None
        raise DashboardError("dash_err_missing_manage_channels") from None

    return {
        "channel_id": str(channel.id),
        "channel_name": channel.name,
        "title": title or hp_system.default_title(server_lang),
        "text": warn_text,
    }

@register_action("automod", "set-honeypot-text")
async def set_honeypot_text(ctx: DashCtx) -> dict:
    """Titel/Text der Warn-Nachricht speichern und die Nachricht im Channel anpassen."""
    guild = ctx.guild
    title, warn_text = _honeypot_texts(ctx.payload)
    if not warn_text:
        raise DashboardError("dash_err_am_honeypot_warn_text_required")
    if await hp_system.get_channel(guild) is None:
        raise DashboardError("dash_err_am_honeypot_missing")

    server_lang = await get_lang(guild.id)
    status = await hp_system.update_texts(guild, title, warn_text, server_lang)
    # status: "updated" | "reposted" (Nachricht fehlte) | "saved_only" (Nachricht nicht anpassbar)
    return {"title": title or hp_system.default_title(server_lang), "text": warn_text, "status": status}

@register_action("automod", "set-honeypot-punishment")
async def set_honeypot_punishment(ctx: DashCtx) -> dict:
    """Bestrafung für Mitglieder und Bots sowie die Timeout-Dauer (einzeln oder zusammen)."""
    payload, fields = ctx.payload, {}
    for key, column in (("members", "punish_members"), ("bots", "punish_bots")):
        if key in payload:
            if payload[key] not in db_honeypot.PUNISHMENTS:
                raise InvalidRequest(f"Ungültige Bestrafung für {key}: {payload[key]!r}")
            fields[column] = payload[key]
    if "days" in payload:
        days = payload["days"]
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= db_honeypot.MAX_TIMEOUT_DAYS:
            raise DashboardError("dash_err_am_honeypot_days_range", max=db_honeypot.MAX_TIMEOUT_DAYS)
        fields["timeout_days"] = days
    if not fields:
        raise InvalidRequest("Keine Änderung angegeben")

    hp = await db_honeypot.update_honeypot_settings(ctx.guild_id, **fields)
    return {"members": hp["punish_members"], "bots": hp["punish_bots"], "days": hp["timeout_days"]}

@register_action("automod", "delete-honeypot")
async def delete_honeypot(ctx: DashCtx) -> dict:
    channel = await hp_system.get_channel(ctx.guild)
    if channel is None:
        raise DashboardError("dash_err_am_honeypot_missing")
    await hp_system.cleanup(ctx.guild, channel.id)   # Fänge, Texte und Bestrafung bleiben gespeichert
    return {"deleted": True}

@register_action("automod", "set-honeypot-timeout")
async def set_honeypot_timeout(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    try:
        days = int(payload.get("days"))
    except (TypeError, ValueError):
        raise DashboardError("dash_err_am_honeypot_days_range", max=db_honeypot.MAX_TIMEOUT_DAYS) from None
    if not 1 <= days <= db_honeypot.MAX_TIMEOUT_DAYS:
        raise DashboardError("dash_err_am_honeypot_days_range", max=db_honeypot.MAX_TIMEOUT_DAYS)

    await db_honeypot.update_honeypot_settings(guild_id, timeout_days=days)
    return {"timeout_days": days}
