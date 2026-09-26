import discord
from .registry import register_action, get_bot
from database import db_settings, db_reaction_roles
from systems.premium import is_premium
from cogs.reaction_roles import parse_emoji_for_select, _update_dropdown_message

def _guild_or_raise(guild_id: int) -> discord.Guild:
    guild = get_bot().get_guild(guild_id)
    if guild is None:
        raise ValueError("Bot ist nicht (mehr) auf diesem Server")
    return guild

async def _check_role_valid(guild: discord.Guild, role: discord.Role, acting_user_id: str | None):
    if role.managed:
        raise ValueError("Diese Rolle wird von einer Integration verwaltet und kann nicht genutzt werden")
    if role >= guild.me.top_role:
        raise ValueError("Diese Rolle steht über der Bot-Rolle — Hierarchie anpassen")

    if acting_user_id and acting_user_id.isdigit() and guild.owner_id != int(acting_user_id):
        member = guild.get_member(int(acting_user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(acting_user_id))
            except discord.NotFound:
                member = None
        if member and role >= member.top_role:
            raise ValueError("Diese Rolle steht über deiner eigenen höchsten Rolle")

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
    guild = _guild_or_raise(guild_id)
    premium = await is_premium(guild_id)
    msg_refs = await db_reaction_roles.get_all_reaction_role_messages(guild_id)

    messages = []
    for ref in msg_refs:
        channel = guild.get_channel(ref["channel_id"])
        entries_raw = await db_reaction_roles.get_reaction_roles(guild_id, ref["message_id"])
        entries = []
        for e in entries_raw:
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
    channel_id = payload.get("channel_id")
    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()
    display_mode = payload.get("display_mode", "reaction")

    if not channel_id or not str(channel_id).isdigit():
        raise ValueError("Gültiger Channel wird benötigt")
    if not title or not description:
        raise ValueError("Titel und Beschreibung werden benötigt")
    if display_mode not in ("reaction", "dropdown"):
        raise ValueError("Ungültiger Anzeige-Modus")

    premium = await is_premium(guild_id)
    if display_mode == "dropdown" and not premium:
        raise ValueError("Dropdown-Modus ist ein Premium-Feature")

    guild = _guild_or_raise(guild_id)
    channel = guild.get_channel(int(channel_id))
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
    acting_user_id = payload.get("acting_user_id")

    if not all([channel_id, message_id, role_id, emoji]):
        raise ValueError("channel_id, message_id, role_id und emoji werden benötigt")
    if mode not in ("toggle", "unique"):
        raise ValueError("Ungültiger Modus")

    guild = _guild_or_raise(guild_id)
    role = guild.get_role(int(role_id))
    if role is None:
        raise ValueError("Rolle existiert nicht auf diesem Server")
    await _check_role_valid(guild, role, acting_user_id)

    channel = guild.get_channel(int(channel_id))
    if channel is None:
        raise ValueError("Channel existiert nicht (mehr)")
    try:
        message = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        raise ValueError("Nachricht existiert nicht (mehr)")

    existing_emoji = await db_reaction_roles.get_reaction_role(guild_id, int(message_id), emoji)
    if existing_emoji is not None:
        raise ValueError("Dieses Emoji ist auf dieser Nachricht bereits vergeben")

    all_current = await db_reaction_roles.get_reaction_roles(guild_id, int(message_id))
    if any(e["role_id"] == int(role_id) for e in all_current):
        raise ValueError("Diese Rolle ist auf dieser Nachricht bereits vergeben")

    premium = await is_premium(guild_id)
    is_custom_emoji = emoji.startswith("<") and emoji.endswith(">")
    if is_custom_emoji and not premium:
        raise ValueError("Custom-Emojis sind ein Premium-Feature")
    if mode == "unique" and not premium:
        raise ValueError("Unique-Modus ist ein Premium-Feature")

    msg_ref = await db_reaction_roles.get_message_ref(guild_id, int(message_id))
    display_mode = msg_ref["display_mode"] if msg_ref else "reaction"

    group_id_int = int(group_id) if group_id else None

    if display_mode == "reaction":
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            raise ValueError("Emoji konnte nicht zur Nachricht hinzugefügt werden — ungültiges Emoji?")

    new_id = await db_reaction_roles.add_reaction_role(
        guild_id=guild_id, channel_id=int(channel_id), message_id=int(message_id),
        role_id=int(role_id), emoji=emoji, mode=mode, group_id=group_id_int,
    )

    if display_mode == "dropdown":
        lang = {}
        await _update_dropdown_message(message, guild_id, int(message_id), lang)

    return {"entry_id": new_id, "role_id": role_id, "role_name": role.name, "emoji": emoji, "mode": mode}

@register_action("reaction_roles", "delete-entry")
async def delete_entry(guild_id: int, payload: dict) -> dict:
    entry_id = payload.get("entry_id")
    channel_id = payload.get("channel_id")
    message_id = payload.get("message_id")
    emoji = payload.get("emoji")

    if not all([entry_id, channel_id, message_id, emoji]):
        raise ValueError("entry_id, channel_id, message_id und emoji werden benötigt")

    deleted = await db_reaction_roles.delete_reaction_role(int(entry_id), guild_id)
    if not deleted:
        raise ValueError("Eintrag wurde nicht gefunden")

    guild = _guild_or_raise(guild_id)
    channel = guild.get_channel(int(channel_id))
    if channel is not None:
        try:
            message = await channel.fetch_message(int(message_id))
            msg_ref = await db_reaction_roles.get_message_ref(guild_id, int(message_id))
            display_mode = msg_ref["display_mode"] if msg_ref else "reaction"
            if display_mode == "dropdown":
                lang = {}
                await _update_dropdown_message(message, guild_id, int(message_id), lang)
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

    guild = _guild_or_raise(guild_id)
    channel = guild.get_channel(int(channel_id))
    if channel is not None:
        try:
            message = await channel.fetch_message(int(message_id))
            msg_ref = await db_reaction_roles.get_message_ref(guild_id, int(message_id))
            display_mode = msg_ref["display_mode"] if msg_ref else "reaction"
            if display_mode == "dropdown":
                await message.edit(view=None)
            else:
                await message.clear_reactions()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    entries = await db_reaction_roles.get_reaction_roles(guild_id, int(message_id))
    for e in entries:
        await db_reaction_roles.delete_reaction_role(e["id"], guild_id)

    await db_reaction_roles.delete_message_ref(guild_id, int(message_id))

    return {"message_id": message_id}