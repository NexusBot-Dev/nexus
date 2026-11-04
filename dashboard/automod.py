import discord
from .registry import register_action, DashCtx, DashboardError, InvalidRequest
from database import db_settings, db_automod, db_honeypot
from cogs.automod import (
       _get_nexus_rules, _get_existing_rule_by_type, _build_actions,
       AUTOMOD_RULES,
   )
from cogs.utils import get_lang
from systems import honeypot as hp_system
from systems.hierarchy import format_issue

@register_action("automod", "status")
async def status(ctx: DashCtx) -> dict:
    guild, guild_id = ctx.guild, ctx.guild_id
    active_rules = await _get_nexus_rules(guild)
    shield_enabled = bool(await db_automod.get_antinuke(guild_id))
    honeypot_channel = await hp_system.get_channel(guild)   # von Hand gelöscht → kein Honeypot
    honeypot = await db_honeypot.get_honeypot_settings(guild_id)
    # Server-Sprache: der Standardtitel steht so in Discord. Liest beim ersten Mal die bestehende Nachricht aus.
    honeypot_title, honeypot_text = await hp_system.load_texts(guild, honeypot_channel, honeypot, await get_lang(guild_id))

    keywords_rule = active_rules.get("keywords")
    return {
        "spam_enabled": "spam" in active_rules and active_rules["spam"].enabled,
        "mentions_enabled": "mentions" in active_rules and active_rules["mentions"].enabled,
        "keywords_enabled": keywords_rule is not None and keywords_rule.enabled,
        "keywords": list(keywords_rule.trigger.keyword_filter) if keywords_rule else [],
        "shield_enabled": shield_enabled,
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
    if rule_key not in AUTOMOD_RULES:
        raise InvalidRequest(f"Unbekannte Regel: {rule_key!r}")

    active_rules = await _get_nexus_rules(guild)
    rule = active_rules.get(rule_key)

    if rule and rule.enabled:
        await rule.edit(enabled=False, reason="Nexus Dashboard — deaktiviert")
        enabled = False
    else:
        rule_conf = AUTOMOD_RULES[rule_key]
        existing = await _get_existing_rule_by_type(guild, rule_conf["trigger"].type)

        if existing and not existing.name.startswith("Nexus —"):
            raise DashboardError("dash_err_am_foreign_rule")

        if existing and existing.name.startswith("Nexus —"):
            await existing.edit(enabled=True, reason="Nexus Dashboard — aktiviert")
        else:
            settings = await db_settings.get_guild(guild_id)
            log_ch_id = settings.get("log_channel_id") if settings else None
            actions = await _build_actions(guild, log_ch_id)
            await guild.create_automod_rule(
                name=rule_conf["name"],
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=rule_conf["trigger"],
                actions=actions,
                enabled=True,
                reason="Nexus Dashboard — aktiviert",
            )
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

    active_rules = await _get_nexus_rules(guild)
    rule = active_rules.get("keywords")

    if rule:
        await rule.edit(
            trigger=discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword, keyword_filter=keywords),
            reason="Nexus Dashboard — Keywords aktualisiert",
        )
        enabled = rule.enabled
    else:
        settings = await db_settings.get_guild(guild_id)
        log_ch_id = settings.get("log_channel_id") if settings else None
        actions = await _build_actions(guild, log_ch_id)
        await guild.create_automod_rule(
            name=AUTOMOD_RULES["keywords"]["name"],
            event_type=discord.AutoModRuleEventType.message_send,
            trigger=discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword, keyword_filter=keywords),
            actions=actions,
            enabled=True,
            reason="Nexus Dashboard — Keywords erstellt",
        )
        enabled = True   # neu angelegte Regel ist sofort aktiv

    return {"keywords": keywords, "enabled": enabled}

@register_action("automod", "toggle-shield")
async def toggle_shield(ctx: DashCtx) -> dict:
    guild_id = ctx.guild_id
    current = bool(await db_automod.get_antinuke(guild_id))
    new_status = await db_automod.set_antinuke(guild_id, not current)
    return {"shield_enabled": bool(new_status)}

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
            raise ValueError(format_issue(e.check, ctx.lang)) from None
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
