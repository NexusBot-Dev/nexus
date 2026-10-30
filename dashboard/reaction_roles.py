import asyncio
import discord
from .registry import register_action, parse_id, DashCtx
from database import db_settings, db_reaction_roles
from systems.premium import is_premium
from cogs.reaction_roles import _entry_cap, _update_dropdown_message, repost_panel, save_panel_text
from cogs.utils import get_lang
from systems.hierarchy import can_grant_role, can_set_up_self_roles, format_issue
from .health import plain_mentions

@register_action("reaction_roles", "list-groups")
async def list_groups(ctx: DashCtx) -> dict:
    groups = await db_reaction_roles.get_groups(ctx.guild_id)
    return {"groups": [{"id": g["id"], "name": g["name"]} for g in groups]}

@register_action("reaction_roles", "create-group")
async def create_group(ctx: DashCtx) -> dict:
    guild_id, payload = ctx.guild_id, ctx.payload
    name = (payload.get("name") or "").strip()
    if not name or len(name) > 100:
        raise ValueError("Gültiger Gruppenname wird benötigt")
    group_id = await db_reaction_roles.create_group(guild_id, name, "unique")
    return {"id": group_id, "name": name}

@register_action("reaction_roles", "list-messages")
async def list_messages(ctx: DashCtx) -> dict:
    guild, guild_id = ctx.guild, ctx.guild_id
    # Alle Einträge der Guild in einer Abfrage statt einer pro Nachricht
    # Auch Nachrichten ohne Rollen (frisch angelegt oder alle entfernt) — sonst wären sie nicht mehr bearbeitbar
    premium, msg_refs, all_entries = await asyncio.gather(
        is_premium(guild_id),
        db_reaction_roles.get_all_panels(guild_id),
        db_reaction_roles.get_reaction_roles_for_guild(guild_id),
    )
    entries_by_message: dict[int, list[dict]] = {}
    for e in all_entries:
        entries_by_message.setdefault(e["message_id"], []).append(e)

    messages = []
    for ref in msg_refs:
        channel = guild.get_channel(ref["channel_id"])
        entries = []
        for e in entries_by_message.get(ref["message_id"], []):
            role = guild.get_role(e["role_id"])
            entries.append({
                "id": e["id"],
                "emoji": e["emoji"],
                "role_id": str(e["role_id"]),
                "role_name": role.name if role else "(gelöschte Rolle)",
                "mode": e["mode"],
                "group_id": e["group_id"],
            })
        messages.append({
            "message_id": str(ref["message_id"]),
            "channel_id": str(ref["channel_id"]),
            "channel_name": channel.name if channel else None,
            "title": ref["title"],
            "description": ref["description"],
            "display_mode": ref.get("display_mode", "reaction"),
            "max_entries": _entry_cap(ref.get("display_mode", "reaction")),
            "entries": entries,
        })

    return {"messages": messages, "premium_active": premium}

@register_action("reaction_roles", "create-message")
async def create_message(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    channel_id = parse_id(payload.get("channel_id"), "Gültiger Channel wird benötigt")
    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()
    display_mode = payload.get("display_mode", "reaction")

    if not title or not description:
        raise ValueError("Titel und Beschreibung werden benötigt")
    if display_mode not in ("reaction", "dropdown"):
        raise ValueError("Ungültiger Anzeige-Modus")

    premium = await is_premium(guild_id)
    if display_mode == "dropdown" and not premium:
        raise ValueError("Dropdown-Modus ist ein Premium-Feature")

    # Vor dem Posten prüfen — sonst bliebe eine Nachricht zurück, an die der User keine Rolle hängen darf
    if not (check := can_set_up_self_roles(guild, ctx.actor)):
        raise ValueError(format_issue(check, await get_lang(guild_id)))

    channel = guild.get_channel(channel_id)
    if channel is None:
        raise ValueError("Channel existiert nicht (mehr)")

    color = await db_settings.get_color(guild_id)
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=guild.name)

    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        raise ValueError("Fehlende Berechtigung zum Posten in diesem Channel")

    await db_reaction_roles.create_message_ref(
        guild_id, channel.id, msg.id, title, description, display_mode=display_mode
    )

    return {"message_id": str(msg.id), "channel_id": str(channel.id), "channel_name": channel.name, "display_mode": display_mode}

@register_action("reaction_roles", "add-entry")
async def add_entry(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    channel_id = payload.get("channel_id")
    message_id = payload.get("message_id")
    role_id = payload.get("role_id")
    emoji = (payload.get("emoji") or "").strip()
    mode = payload.get("mode", "toggle")
    group_id = payload.get("group_id")

    if not all([channel_id, message_id, role_id, emoji]):
        raise ValueError("channel_id, message_id, role_id und emoji werden benötigt")
    if mode not in ("toggle", "unique"):
        raise ValueError("Ungültiger Modus")
    channel_id = parse_id(channel_id, "Ungültige channel_id")
    message_id = parse_id(message_id, "Ungültige message_id")
    role_id = parse_id(role_id, "Ungültige role_id")
    group_id = parse_id(group_id, "Ungültige group_id") if group_id else None

    role = guild.get_role(role_id)
    if role is None:
        raise ValueError("Rolle existiert nicht auf diesem Server")
    if not (check := can_grant_role(guild, role, ctx.actor)):
        raise ValueError(plain_mentions(guild, format_issue(check, await get_lang(guild_id))))

    channel = guild.get_channel(channel_id)
    if channel is None:
        raise ValueError("Channel existiert nicht (mehr)")
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        raise ValueError("Nachricht existiert nicht (mehr)")

    msg_ref = await db_reaction_roles.get_message_ref(guild_id, message_id)
    if msg_ref is None:
        raise ValueError("Diese Nachricht ist keine bekannte Reaction-Role-Nachricht")

    existing_emoji = await db_reaction_roles.get_reaction_role(guild_id, message_id, emoji)
    if existing_emoji is not None:
        raise ValueError("Dieses Emoji ist auf dieser Nachricht bereits vergeben")

    all_current = await db_reaction_roles.get_reaction_roles(guild_id, message_id)
    if any(e["role_id"] == role_id for e in all_current):
        raise ValueError("Diese Rolle ist auf dieser Nachricht bereits vergeben")
    cap = _entry_cap(msg_ref["display_mode"])
    if len(all_current) >= cap:
        raise ValueError(f"Diese Nachricht hat bereits das Maximum von {cap} Rollen")

    premium = await is_premium(guild_id)
    is_custom_emoji = emoji.startswith("<") and emoji.endswith(">")
    if is_custom_emoji and not premium:
        raise ValueError("Custom-Emojis sind ein Premium-Feature")
    if mode == "unique" and not premium:
        raise ValueError("Unique-Modus ist ein Premium-Feature")

    display_mode = msg_ref["display_mode"]

    if display_mode == "reaction":
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            raise ValueError("Emoji konnte nicht zur Nachricht hinzugefügt werden — ungültiges Emoji?")

    new_id = await db_reaction_roles.add_reaction_role(
        guild_id=guild_id, channel_id=channel_id, message_id=message_id,
        role_id=role_id, emoji=emoji, mode=mode, group_id=group_id,
    )

    if display_mode == "dropdown":
        await _update_dropdown_message(message, guild_id, message_id, await get_lang(guild_id))

    return {"entry_id": new_id, "role_id": str(role_id), "role_name": role.name, "emoji": emoji, "mode": mode}

@register_action("reaction_roles", "update-message")
async def update_message(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    message_id = parse_id(payload.get("message_id"), "Ungültige message_id")
    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()

    if not title or not description:
        raise ValueError("Titel und Beschreibung werden benötigt")
    if len(title) > 256:
        raise ValueError("Titel zu lang (max. 256 Zeichen)")
    if len(description) > 4000:
        raise ValueError("Beschreibung zu lang (max. 4000 Zeichen)")

    if not (check := can_set_up_self_roles(guild, ctx.actor)):
        raise ValueError(format_issue(check, await get_lang(guild_id)))

    status = await save_panel_text(guild, message_id, title, description)
    if status == "gone":
        raise ValueError("Diese Nachricht ist keine bekannte Reaction-Role-Nachricht")

    # Text ist in jedem Fall gespeichert — status sagt dem Dashboard, ob Discord nachgezogen wurde:
    # "updated", "saved_only" (Nachricht nicht erreichbar), "not_own" (fremde Nachricht), "failed"
    return {
        "message_id": str(message_id),
        "title": title,
        "description": description,
        "message_updated": status == "updated",
        "status": status,
    }

@register_action("reaction_roles", "update-entry")
async def update_entry(ctx: DashCtx) -> dict:
    """Tauscht die Rolle eines Eintrags aus — Emoji, Modus und Gruppe bleiben."""
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    entry_id = parse_id(payload.get("entry_id"), "Ungültige entry_id")
    message_id = parse_id(payload.get("message_id"), "Ungültige message_id")
    role_id = parse_id(payload.get("role_id"), "Ungültige role_id")

    role = guild.get_role(role_id)
    if role is None:
        raise ValueError("Rolle existiert nicht auf diesem Server")
    if not (check := can_grant_role(guild, role, ctx.actor)):
        raise ValueError(plain_mentions(guild, format_issue(check, await get_lang(guild_id))))

    entries = await db_reaction_roles.get_reaction_roles(guild_id, message_id)
    entry = next((e for e in entries if e["id"] == entry_id), None)
    if entry is None:
        raise ValueError("Eintrag wurde nicht gefunden")

    # Gleiche Rolle: nichts zu tun (UPDATE würde 0 Zeilen melden und wie ein Fehler aussehen)
    if role_id != entry["role_id"]:
        if any(e["role_id"] == role_id for e in entries):
            raise ValueError("Diese Rolle ist auf dieser Nachricht bereits vergeben")
        await db_reaction_roles.update_reaction_role_role(entry_id, guild_id, role_id)

    return {
        "entry_id": entry_id,
        "role_id": str(role_id),
        "role_name": role.name,
        "emoji": entry["emoji"],
        "mode": entry["mode"],
    }

@register_action("reaction_roles", "repost-message")
async def repost_message(ctx: DashCtx) -> dict:
    """Postet eine Nachricht neu, deren Discord-Nachricht oder Channel gelöscht wurde — alle Rollen ziehen mit um."""
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    message_id = parse_id(payload.get("message_id"), "Ungültige message_id")
    channel_id = parse_id(payload.get("channel_id"), "Gültiger Channel wird benötigt")

    lang = await get_lang(guild_id)
    if not (check := can_set_up_self_roles(guild, ctx.actor)):
        raise ValueError(format_issue(check, lang))

    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError("Channel existiert nicht (mehr)")

    result = await repost_panel(guild, message_id, channel, lang)
    if result.status == "gone":
        raise ValueError("Diese Nachricht ist keine bekannte Reaction-Role-Nachricht")
    if result.status == "still_there":
        raise ValueError(
            "Die Nachricht existiert noch oder Nexus kann ihren Channel nicht lesen — "
            "Neu-Posten ist nur für gelöschte Nachrichten möglich"
        )
    if result.status == "no_perms":
        raise ValueError(plain_mentions(guild, format_issue(result.check, lang)))
    if result.status == "send_failed":
        raise ValueError("Discord hat das Senden abgelehnt — bitte später erneut versuchen")

    return {
        "old_message_id": str(message_id),
        "message_id": str(result.message.id),
        "channel_id": str(channel.id),
        "channel_name": channel.name,
        "failed": result.failed,  # Emojis (bzw. der Dropdown-Button), die nicht wiederhergestellt werden konnten
    }

@register_action("reaction_roles", "delete-entry")
async def delete_entry(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    entry_id = payload.get("entry_id")
    channel_id = payload.get("channel_id")
    message_id = payload.get("message_id")
    emoji = payload.get("emoji")

    if not all([entry_id, channel_id, message_id, emoji]):
        raise ValueError("entry_id, channel_id, message_id und emoji werden benötigt")
    entry_id = parse_id(entry_id, "Ungültige entry_id")
    channel_id = parse_id(channel_id, "Ungültige channel_id")
    message_id = parse_id(message_id, "Ungültige message_id")

    deleted = await db_reaction_roles.delete_reaction_role(entry_id, guild_id)
    if not deleted:
        raise ValueError("Eintrag wurde nicht gefunden")

    channel = guild.get_channel(channel_id)
    if channel is not None:
        try:
            msg_ref = await db_reaction_roles.get_message_ref(guild_id, message_id)
            if msg_ref is not None:
                message = await channel.fetch_message(message_id)
                display_mode = msg_ref["display_mode"]
                if display_mode == "dropdown":
                    await _update_dropdown_message(message, guild_id, message_id, await get_lang(guild_id))
                else:
                    await message.clear_reaction(emoji)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    return {"entry_id": entry_id}

@register_action("reaction_roles", "delete-message")
async def delete_message_entries(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    channel_id = payload.get("channel_id")
    message_id = payload.get("message_id")
    if not channel_id or not message_id:
        raise ValueError("channel_id und message_id werden benötigt")
    channel_id = parse_id(channel_id, "Ungültige channel_id")
    message_id = parse_id(message_id, "Ungültige message_id")

    msg_ref = await db_reaction_roles.get_message_ref(guild_id, message_id)
    if msg_ref is None:
        raise ValueError("Diese Nachricht ist keine bekannte Reaction-Role-Nachricht")

    channel = guild.get_channel(channel_id)
    if channel is not None:
        try:
            message = await channel.fetch_message(message_id)
            display_mode = msg_ref["display_mode"]
            if display_mode == "dropdown":
                await message.edit(view=None)
            else:
                await message.clear_reactions()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    await db_reaction_roles.delete_reaction_roles_for_message(guild_id, message_id)

    await db_reaction_roles.delete_message_ref(guild_id, message_id)

    return {"message_id": str(message_id)}  # Snowflakes als String, JS-Numbers sind dafür zu ungenau
