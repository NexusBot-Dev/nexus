import discord
from .registry import register_action, get_bot
from database import db_settings, db_automod
from cogs.automod import (
    _get_nexus_rules, _get_existing_rule_by_type, _build_actions,
    AUTOMOD_RULES, _slugify_channel_name,
)

def _guild_or_raise(guild_id: int) -> discord.Guild:
    guild = get_bot().get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")
    return guild

@register_action("automod", "status")
async def status(guild_id: int, payload: dict) -> dict:
    guild = _guild_or_raise(guild_id)
    active_rules = await _get_nexus_rules(guild)
    settings = await db_settings.get_guild(guild_id)
    shield_enabled = bool(await db_automod.get_antinuke(guild_id))
    honeypot_channel_id = settings.get("honeypot_channel_id") if settings else None

    keywords_rule = active_rules.get("keywords")
    return {
        "spam_enabled": "spam" in active_rules and active_rules["spam"].enabled,
        "mentions_enabled": "mentions" in active_rules and active_rules["mentions"].enabled,
        "keywords_enabled": keywords_rule is not None and keywords_rule.enabled,
        "keywords": list(keywords_rule.trigger.keyword_filter) if keywords_rule else [],
        "shield_enabled": shield_enabled,
        "honeypot_channel_id": str(honeypot_channel_id) if honeypot_channel_id else None,
    }

@register_action("automod", "toggle-rule")
async def toggle_rule(guild_id: int, payload: dict) -> dict:
    rule_key = payload.get("rule_key")
    if rule_key not in AUTOMOD_RULES:
        raise ValueError("Unbekannte Regel")

    guild = _guild_or_raise(guild_id)
    active_rules = await _get_nexus_rules(guild)
    rule = active_rules.get(rule_key)

    if rule and rule.enabled:
        await rule.edit(enabled=False, reason="Nexus Dashboard — deaktiviert")
    else:
        rule_conf = AUTOMOD_RULES[rule_key]
        existing = await _get_existing_rule_by_type(guild, rule_conf["trigger"].type)

        if existing and not existing.name.startswith("Nexus —"):
            raise ValueError("Eine fremde AutoMod-Regel dieses Typs existiert bereits — bitte manuell in Discord auflösen")

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

    return {"rule_key": rule_key}

@register_action("automod", "set-keywords")
async def set_keywords(guild_id: int, payload: dict) -> dict:
    keywords = payload.get("keywords")
    if not isinstance(keywords, list):
        raise ValueError("keywords muss eine Liste sein")
    keywords = [str(k).strip().lower() for k in keywords if str(k).strip()][:1000]

    guild = _guild_or_raise(guild_id)
    active_rules = await _get_nexus_rules(guild)
    rule = active_rules.get("keywords")

    if rule:
        await rule.edit(
            trigger=discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword, keyword_filter=keywords),
            reason="Nexus Dashboard — Keywords aktualisiert",
        )
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

    return {"keywords": keywords}

@register_action("automod", "toggle-shield")
async def toggle_shield(guild_id: int, payload: dict) -> dict:
    current = bool(await db_automod.get_antinuke(guild_id))
    new_status = await db_automod.set_antinuke(guild_id, not current)
    return {"shield_enabled": bool(new_status)}

@register_action("automod", "create-honeypot")
async def create_honeypot(guild_id: int, payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    warn_text = (payload.get("warn_text") or "").strip()
    if not name or not warn_text:
        raise ValueError("Name und Warntext werden benötigt")
    if len(warn_text) > 1000:
        raise ValueError("Warntext zu lang (max. 1000 Zeichen)")

    guild = _guild_or_raise(guild_id)
    settings = await db_settings.get_guild(guild_id)
    if settings and settings.get("honeypot_channel_id"):
        raise ValueError("Es existiert bereits ein Honeypot — zuerst löschen")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
    }
    channel = await guild.create_text_channel(
        name=_slugify_channel_name(name), overwrites=overwrites, reason="Nexus Dashboard — Honeypot erstellt",
    )
    try:
        await channel.edit(position=0)
    except discord.HTTPException:
        pass

    color = await db_settings.get_color(guild_id)
    embed = discord.Embed(title="⚠️ Warning", description=warn_text, color=color)
    embed.set_footer(text="Nexus • Honeypot")
    await channel.send(embed=embed)

    await db_settings.update_guild(guild_id, honeypot_channel_id=channel.id)
    return {"channel_id": str(channel.id), "channel_name": channel.name}

@register_action("automod", "delete-honeypot")
async def delete_honeypot(guild_id: int, payload: dict) -> dict:
    guild = _guild_or_raise(guild_id)
    settings = await db_settings.get_guild(guild_id)
    channel_id = settings.get("honeypot_channel_id") if settings else None
    if not channel_id:
        raise ValueError("Kein Honeypot vorhanden")

    channel = guild.get_channel(channel_id)
    if channel is not None:
        try:
            await channel.delete(reason="Nexus Dashboard — Honeypot gelöscht")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

    await db_settings.update_guild(guild_id, honeypot_channel_id=None)
    return {"deleted": True}
