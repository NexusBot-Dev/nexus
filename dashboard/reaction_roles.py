import asyncio
import discord
from .registry import register_action, get_guild_or_raise, parse_id
from database import db_settings, db_reaction_roles
from systems.premium import is_premium
from cogs.reaction_roles import _update_dropdown_message
from cogs.utils import get_lang
from systems.hierarchy import can_grant_role, can_set_up_self_roles, format_issue
from .discord_auth_check import get_acting_member
from .health import plain_mentions

@register_action("reaction_roles", "list-groups")
async def list_groups(guild_id: int, payload: dict) -> dict:
    groups = await db_reaction_roles.get_groups(guild_id)
    return {"groups": [{"id": g["id"], "name": g["name"]} for g in groups]}

@register_action("reaction_roles", "create-group")
async def create_group(guild_id: int, payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    if not name or len(name) > 100:
        raise ValueError("Gültiger Gruppenname wird benötigt")
    group_id = await db_reaction_roles.create_group(guild_id, name, "unique")
    return {"id": group_id, "name": name}

@register_action("reaction_roles", "list-messages")
async def list_messages(guild_id: int, payload: dict) -> dict:
    guild = get_guild_or_raise(guild_id)
    # Alle Einträge der Guild in einer Abfrage statt einer pro Nachricht
    premium, msg_refs, all_entries = await asyncio.gather(
        is_premium(guild_id),
        db_reaction_roles.get_all_reaction_role_messages(guild_id),
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
            "entries": entries,
        })

    return {"messages": messages, "premium_active": premium}

@register_action("reaction_roles", "create-message")
async def create_message(guild_id: int, payload: dict) -> dict:
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

    guild = get_guild_or_raise(guild_id)
    # Vor dem Posten prüfen — sonst bliebe eine Nachricht zurück, an die der User keine Rolle hängen darf
    actor = await get_acting_member(guild, payload)
    if not (check := can_set_up_self_roles(guild, actor)):
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
async def add_entry(guild_id: int, payload: dict) -> dict:
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

    guild = get_guild_or_raise(guild_id)
    role = guild.get_role(role_id)
    if role is None:
        raise ValueError("Rolle existiert nicht auf diesem Server")
    actor = await get_acting_member(guild, payload)
    if not (check := can_grant_role(guild, role, actor)):
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

@register_action("reaction_roles", "delete-entry")
async def delete_entry(guild_id: int, payload: dict) -> dict:
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

    guild = get_guild_or_raise(guild_id)
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
async def delete_message_entries(guild_id: int, payload: dict) -> dict:
    channel_id = payload.get("channel_id")
    message_id = payload.get("message_id")
    if not channel_id or not message_id:
        raise ValueError("channel_id und message_id werden benötigt")
    channel_id = parse_id(channel_id, "Ungültige channel_id")
    message_id = parse_id(message_id, "Ungültige message_id")

    guild = get_guild_or_raise(guild_id)

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