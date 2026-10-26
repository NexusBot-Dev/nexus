import asyncio
import discord
import logging
from .registry import register_action, parse_id, DashCtx
from database import db_settings, db_ticket_categories, db_tickets, db_limits
from systems.premium import is_premium
from cogs.tickets import _build_panel_embed, _build_panel_view
from systems.hierarchy import can_apply_overwrites, format_issue
from cogs.utils import get_lang

log = logging.getLogger(__name__)

@register_action("tickets", "list-categories")
async def list_categories(ctx: DashCtx) -> dict:
    guild, guild_id = ctx.guild, ctx.guild_id
    categories, roles_by_category, premium, limit_free, limit_premium = await asyncio.gather(
        db_ticket_categories.get_categories(guild_id),
        db_ticket_categories.get_category_roles_for_guild(guild_id),
        is_premium(guild_id),
        db_limits.get_limit(guild_id, "ticket_categories", False),
        db_limits.get_limit(guild_id, "ticket_categories", True),
    )

    result = []
    for cat in categories:
        role_ids = roles_by_category.get(cat["id"], [])
        role_names = []
        for rid in role_ids:
            role = guild.get_role(rid)
            role_names.append({"id": str(rid), "name": role.name if role else "(gelöschte Rolle)"})
        result.append({
            "id": cat["id"],
            "name": cat["name"],
            "role_ids": [str(r) for r in role_ids],
            "roles": role_names,
        })

    return {"categories": result, "limit_free": limit_free, "limit_premium": limit_premium, "premium_active": premium}

@register_action("tickets", "create-category")
async def create_category(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    name = (payload.get("name") or "").strip()
    role_ids = payload.get("role_ids") or []

    if not name or len(name) > 100:
        raise ValueError("Name ist erforderlich (max. 100 Zeichen)")
    if not isinstance(role_ids, list):
        raise ValueError("role_ids muss eine Liste sein")

    current_count = await db_ticket_categories.get_category_count(guild_id)
    premium = await is_premium(guild_id)
    limit = await db_limits.get_limit(guild_id, "ticket_categories", premium)
    if current_count >= limit:
        raise ValueError(f"Limit erreicht ({limit} Kategorien)")

    valid_role_ids = []
    for rid in role_ids:
        if not str(rid).isdigit():
            continue
        role = guild.get_role(int(rid))
        if role and not role.managed and not role.is_default():
            valid_role_ids.append(int(rid))

    bot_member = guild.me
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        bot_member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, embed_links=True,
            attach_files=True, read_message_history=True, manage_channels=True,
        ),
    }
    check = can_apply_overwrites(guild, overwrites)
    if not check:
        raise ValueError(format_issue(check, await get_lang(guild_id)))

    try:
        discord_category = await guild.create_category(
            name=name, overwrites=overwrites, reason="Nexus Dashboard — Kategorie erstellt",
        )
    except discord.Forbidden:
        raise ValueError("Fehlende Berechtigung zum Erstellen von Kategorien")
    except discord.HTTPException:
        log.exception("Dashboard: Kategorie erstellen auf %s fehlgeschlagen", guild_id)
        raise ValueError("Discord hat die Anfrage abgelehnt — bitte später erneut versuchen")

    category_id = await db_ticket_categories.create_category(
        guild_id=guild_id, name=name, category_channel_id=discord_category.id,
    )
    if valid_role_ids:
        await db_ticket_categories.set_category_roles(category_id, valid_role_ids)

    return {"id": category_id, "name": name}

@register_action("tickets", "rename-category")
async def rename_category(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    category_id = parse_id(payload.get("category_id"), "category_id und ein gültiger Name werden benötigt")
    new_name = (payload.get("name") or "").strip()
    if not new_name or len(new_name) > 100:
        raise ValueError("category_id und ein gültiger Name werden benötigt")

    category = await db_ticket_categories.get_category(category_id, guild_id)
    if category is None:
        raise ValueError("Kategorie wurde nicht gefunden")

    discord_category = guild.get_channel(category["category_channel_id"])
    if discord_category is not None:
        try:
            await discord_category.edit(name=new_name, reason="Nexus Dashboard — umbenannt")
        except discord.Forbidden:
            raise ValueError("Fehlende Berechtigung zum Umbenennen")
        except discord.HTTPException:
            log.exception("Dashboard: Kategorie umbenennen auf %s fehlgeschlagen", guild_id)   # beim Löschen entsprechend
            raise ValueError("Discord hat die Anfrage abgelehnt — bitte später erneut versuchen")

    await db_ticket_categories.rename_category(category_id, guild_id, new_name)
    return {"id": category_id, "name": new_name}

@register_action("tickets", "set-category-roles")
async def set_category_roles(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    category_id = parse_id(payload.get("category_id"), "category_id wird benötigt")
    role_ids = payload.get("role_ids") or []
    if not isinstance(role_ids, list):
        raise ValueError("role_ids muss eine Liste sein")

    category = await db_ticket_categories.get_category(category_id, guild_id)
    if category is None:
        raise ValueError("Kategorie wurde nicht gefunden")

    valid_role_ids = []
    role_names = []
    for rid in role_ids:
        if not str(rid).isdigit():
            continue
        role = guild.get_role(int(rid))
        if role and not role.managed and not role.is_default():
            valid_role_ids.append(int(rid))
            role_names.append({"id": str(rid), "name": role.name})

    await db_ticket_categories.set_category_roles(category_id, valid_role_ids)
    return {"id": category_id, "roles": role_names}

@register_action("tickets", "delete-category")
async def delete_category(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    category_id = parse_id(payload.get("category_id"), "category_id wird benötigt")

    category = await db_ticket_categories.get_category(category_id, guild_id)
    if category is None:
        raise ValueError("Kategorie wurde nicht gefunden")

    # Erst nach der Guild-Prüfung — sonst ließe sich über fremde IDs abfragen, ob dort Tickets offen sind
    if await db_tickets.has_open_tickets_for_category(category_id):
        raise ValueError("Kategorie hat noch offene Tickets — zuerst schließen")

    discord_category = guild.get_channel(category["category_channel_id"])
    if discord_category is not None:
        try:
            await discord_category.delete(reason="Nexus Dashboard — gelöscht")
        except discord.Forbidden:
            raise ValueError("Fehlende Berechtigung zum Löschen")
        except discord.HTTPException:
            log.exception("Dashboard: Kategorie löschen auf %s fehlgeschlagen", guild_id)
            raise ValueError("Discord hat die Anfrage abgelehnt — bitte später erneut versuchen")

    await db_ticket_categories.delete_category(category_id, guild_id)
    return {"id": category_id}

@register_action("tickets", "list-open")
async def list_open(ctx: DashCtx) -> dict:
    guild, guild_id = ctx.guild, ctx.guild_id
    tickets, category_rows = await asyncio.gather(
        db_tickets.get_open_tickets_for_guild(guild_id),
        db_ticket_categories.get_categories(guild_id),
    )
    categories = {c["id"]: c["name"] for c in category_rows}

    result = []
    for t in tickets:
        member = guild.get_member(t["user_id"])
        result.append({
            "id": t["id"],
            "ticket_number": t["ticket_number"],
            "status": t["status"],
            "category_name": categories.get(t["category_id"], "(gelöschte Kategorie)"),
            "user_tag": member.display_name if member else f"Unbekannt ({t['user_id']})",
            "channel_id": str(t["channel_id"]),
        })
    return {"tickets": result}

@register_action("tickets", "post-panel")
async def post_panel(ctx: DashCtx) -> dict:
    guild, guild_id, payload = ctx.guild, ctx.guild_id, ctx.payload
    channel_id = payload.get("channel_id")
    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()

    if not channel_id or not str(channel_id).isdigit():
        raise ValueError("Gültiger Channel wird benötigt")
    if not title or not description:
        raise ValueError("Titel und Beschreibung werden benötigt")

    channel = guild.get_channel(int(channel_id))
    if channel is None:
        raise ValueError("Channel existiert nicht (mehr)")

    categories = await db_ticket_categories.get_categories(guild_id)
    if not categories:
        raise ValueError("Es existiert noch keine Ticket-Kategorie")

    color = await db_settings.get_color(guild_id)
    embed = _build_panel_embed(title, description, color)
    view = _build_panel_view(categories)

    try:
        await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        raise ValueError("Fehlende Berechtigung zum Posten in diesem Channel")

    return {"channel_id": channel_id}
