import logging
import re
import io
import discord
from discord import app_commands
from discord.ext import commands, tasks
from database import db_settings, db_ticket_categories, db_tickets, db_limits, db_support, db_ticket_settings
from systems.premium import is_premium
from cogs.utils import get_lang, module_required, get_member_safe
from config import NEXUS_COLOR, NEXUS_FOOTER
from emojis import NexusEmojis
from systems.hierarchy import can_apply_overwrites, can_use_channel, format_issue
from systems.ticket_panel import PanelError, publish_panel, refresh_panel, import_panel
from systems.ui import NexusView, NexusModal, report_interaction_error

log = logging.getLogger(__name__)

# ─── Fristen (Anzeige) ───────────────────────────────────────────────────────

DURATION_FALLBACKS = {1: "1 hour", 12: "12 hours", 24: "24 hours", 48: "48 hours", 72: "72 hours", 168: "7 days"}
CLOSE_TYPE_FALLBACKS = {
    "user_confirmed": "Confirmed by the user",
    "auto_timeout":   "Closed automatically (no response)",
    "support_forced": "Force-closed by the team",
}

def _duration(hours: int, lang: dict) -> str:
    return lang.get(f"ticket_dur_{hours}", DURATION_FALLBACKS.get(hours, f"{hours} h"))

def _panel_error_text(err: PanelError, lang: dict, channel=None) -> str:
    if err.code == "no_categories":
        return lang.get("ticket_panel_no_categories", "❌ No ticket category exists yet. Create one first with `/add_category`.")
    if err.code == "no_channel":
        return lang.get("ticket_panel_need_channel", "❌ There is no panel yet — please choose a channel.")
    if err.code == "no_permission":
        return lang.get("ticket_panel_no_permission", "❌ I can't send messages in {channel}.").format(
            channel=channel.mention if channel else "—"
        )
    if err.code == "not_a_panel":
        return lang.get("ticket_panel_not_a_panel", "❌ This message isn't a Nexus ticket panel.")
    return lang.get("error_generic", "Something went wrong. Please try again later.")

async def _set_ticket_write_access(channel: discord.TextChannel, member: discord.Member, can_send: bool) -> bool:
    """Schreibrechte des Ticket-Erstellers setzen. Scheitert still (mit Log), damit der Ticket-Flow nicht abbricht."""
    try:
        await channel.set_permissions(
            member, view_channel=True, send_messages=can_send,
            reason="Nexus Ticketsystem — Schreibrechte angepasst",
        )
        return True
    except discord.HTTPException as e:
        log.warning("Konnte Ticket-Rechte in %s für %s nicht setzen: %s", channel.id, member.id, e)
        return False

def _category_slug(name: str) -> str:
    """Wandelt einen Kategorienamen in einen Discord-tauglichen Channel-Namen-Präfix um.
    Discord erlaubt in Channel-Namen praktisch nur Kleinbuchstaben, Zahlen und Bindestriche —
    Emojis und Sonderzeichen werden entfernt statt zu crashen."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug, flags=re.UNICODE)  # Emojis/Sonderzeichen raus
    slug = re.sub(r"[\s_]+", "-", slug)                      # Leerzeichen -> Bindestrich
    slug = re.sub(r"-+", "-", slug).strip("-")                # doppelte Bindestriche glätten
    return slug or "ticket"  # Fallback, falls der Name nur aus Emojis bestand

def _assignable_roles(roles) -> list[discord.Role]:
    """Rollen, die als Ticket-Zugriff gespeichert werden dürfen. @everyone würde das
    view_channel=False im Ticket überschreiben und alle Tickets öffentlich machen,
    Bot-/Integrationsrollen ergeben keinen Sinn — gleicher Filter wie im Dashboard."""
    return [role for role in roles if not role.is_default() and not role.managed]

async def _drop_if_channel_gone(guild: discord.Guild, ticket: dict | None) -> dict | None:
    """Offenes Ticket, dessen Channel nicht mehr existiert: Eintrag entfernen, None zurückgeben."""
    if ticket is None or guild.get_channel(ticket["channel_id"]) is not None:
        return ticket
    await db_support.revoke_access(ticket["channel_id"])
    await db_tickets.delete_ticket_record(ticket["id"])
    log.info("Ticket #%s auf %s: Channel existiert nicht mehr, Eintrag entfernt.", ticket["ticket_number"], guild.name)
    return None

def _category_gone_text(lang: dict) -> str:
    return lang.get("ticket_category_not_found", "❌ This category was not found.")

# ─── /add_category — Schritt 1: Modal für den Namen ──────────────────────────

class AddCategoryModal(NexusModal):
    def __init__(self, lang: dict, cog):
        super().__init__(lang, title=lang.get("ticket_add_category_modal_title", "Create Ticket Category"))
        self.cog  = cog

        self.name_input = discord.ui.TextInput(
            label=lang.get("ticket_add_category_name_label", "Category Name"),
            placeholder=lang.get("ticket_add_category_name_placeholder", "e.g. 🐞 Bug Reports"),
            min_length=1,
            max_length=100,  # Discord-Kategorienamen sind auf 100 Zeichen begrenzt
            required=True,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        name = self.name_input.value.strip()

        bot_member = interaction.guild.me or await interaction.guild.fetch_member(interaction.client.user.id)

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                attach_files=True,
                read_message_history=True,
                manage_channels=True,
            ),
        }

        # Ohne Admin: vorab prüfen, ob Nexus diese Overwrites überhaupt setzen darf
        check = can_apply_overwrites(interaction.guild, overwrites)
        if not check:
            await interaction.followup.send(format_issue(check, self.lang), ephemeral=True)
            return

        try:
            discord_category = await interaction.guild.create_category(
                name=name,
                overwrites=overwrites,
                reason=f"Nexus Ticketsystem — Kategorie erstellt von {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                self.lang.get("ticket_no_permission_category", "❌ I don't have permission to manage categories."),
                ephemeral=True,
            )
            return

        category_id = await db_ticket_categories.create_category(
            guild_id=interaction.guild_id,
            name=name,
            category_channel_id=discord_category.id,
        )
        await refresh_panel(interaction.guild)

        embed = discord.Embed(
            title=self.lang.get("ticket_category_created_title", "{nexus_checkmark} Category created!").format(
                nexus_checkmark=NexusEmojis.CHECKMARK
            ),
            description=self.lang.get(
                "ticket_category_created_desc",
                "The category **{name}** has been created.\n\nWould you like to grant access to specific roles "
                "for tickets in this category right away?\n\n"
                "*(Can always be set up later via `/edit_category`.)*",
            ).format(name=name),
            color=NEXUS_COLOR,
        )

        view = RoleSetupPromptView(category_id, self.lang)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.bind(interaction)
        log.info("Ticket-Kategorie '%s' (ID %s) auf %s erstellt von %s.", name, category_id, interaction.guild.name, interaction.user)

# ─── /add_category — Schritt 2: Ja/Nein für Rollen-Zuordnung ─────────────────

class RoleSetupPromptView(NexusView):
    def __init__(self, category_id: int, lang: dict):
        super().__init__(lang, owner_id=None, require=None, timeout=180)
        self.category_id = category_id
        self.yes_btn.label = lang.get("ticket_roles_prompt_yes", "Yes, select roles")
        self.yes_btn.emoji = NexusEmojis.CHECKMARK
        self.no_btn.label  = lang.get("ticket_roles_prompt_no", "No, done")

    @discord.ui.button(label="Yes, select roles", style=discord.ButtonStyle.success)
    async def yes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show(
            interaction,
            RoleSelectView(self.category_id, self.lang),
            content=self.lang.get("ticket_roles_select_prompt", "Select the roles that should get access:"),
            embed=None,
        )

    @discord.ui.button(label="Nein, fertig", style=discord.ButtonStyle.secondary)
    async def no_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(view=None)

# ─── /add_category — Schritt 3 (optional): Rollen-Auswahl ────────────────────

class RoleSelectView(NexusView):
    def __init__(self, category_id: int, lang: dict):
        super().__init__(lang, owner_id=None, require=None, timeout=180)
        self.category_id = category_id
        self.select_roles.placeholder = lang.get("ticket_roles_select_placeholder", "Select roles...")

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Select roles...",
        min_values=0,
        max_values=25,
    )
    async def select_roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        roles    = _assignable_roles(select.values)
        role_ids = [role.id for role in roles]
        await db_ticket_categories.set_category_roles(self.category_id, role_ids)

        if role_ids:
            mentions = ", ".join(role.mention for role in roles)
            content = self.lang.get(
                "ticket_roles_saved", "{nexus_checkmark} These roles now have access: {roles}"
            ).format(nexus_checkmark=NexusEmojis.CHECKMARK, roles=mentions)
        else:
            content = self.lang.get(
                "ticket_roles_saved_none",
                "{nexus_checkmark} No roles selected — only server admins automatically see all tickets.",
            ).format(nexus_checkmark=NexusEmojis.CHECKMARK)

        self.stop()
        await interaction.response.edit_message(content=content, view=None)
        log.info("Rollen für Ticket-Kategorie %s aktualisiert: %s", self.category_id, role_ids)

# ─── /edit_category — Übersichts-Embed + Hauptmenü ───────────────────────────

def _category_overview_embed(category: dict, role_ids: list[int], lang: dict, color: int) -> discord.Embed:
    embed = discord.Embed(
        title=lang.get("ticket_edit_category_title", "{setting} Edit category: {name}").format(setting=NexusEmojis.Modules.SETTINGS, name=category["name"]),
        color=color,
    )
    if role_ids:
        roles_display = "\n".join(f"<@&{role_id}>" for role_id in role_ids)
    else:
        roles_display = lang.get("ticket_edit_no_roles", "*None — only server admins automatically see all tickets.*")
    embed.add_field(
        name=lang.get("ticket_edit_roles_field", "🎭 Authorized Roles"),
        value=roles_display,
        inline=False,
    )
    embed.set_footer(text=f"Nexus • Ticket-Kategorie #{category['id']}")
    return embed

class CategoryEditView(NexusView):
    """
    Wiederverwendete Hub-View: Rename und Edit Roles reichen SICH SELBST (self) als
    parent_view weiter und kommen über _update_view() wieder hierher zurück, statt eine
    neue Instanz zu erzeugen. Ihre eigene Timeout-Uhr läuft deshalb schon seit dem
    allerersten Anzeigen, auch während Kind-Views sichtbar sind.

    Deshalb bewusst KEIN NexusView-Standard-Timeout hier: Würde er feuern, während
    z.B. EditRoleSelectView gerade angezeigt wird, würde er diese mit einer
    deaktivierten CategoryEditView überschreiben. on_timeout bleibt daher inert,
    genau wie das ursprüngliche discord.ui.View-Verhalten (Buttons bleiben nach
    Ablauf zwar technisch klickbar, das ist aber unverändert zum Vorher-Zustand).
    """
    def __init__(self, category_id: int, lang: dict):
        super().__init__(lang, owner_id=None, require=None, timeout=180)
        self.category_id = category_id
        self.rename_btn.label       = lang.get("ticket_edit_btn_rename", "Rename")
        self.rename_btn.emoji       = "✏️"
        self.roles_btn.label        = lang.get("ticket_edit_btn_roles", "Edit Roles")
        self.roles_btn.emoji        = "🎭"
        self.delete_btn.label       = lang.get("ticket_edit_btn_delete", "Delete")
        self.delete_btn.emoji       = "🗑️"

    async def on_timeout(self) -> None:
        pass

    async def _update_view(self, interaction: discord.Interaction):
        """Lädt Kategorie + Rollen frisch aus der DB und aktualisiert das Embed —
        wird nach jeder Änderung aufgerufen, damit die View offen bleiben kann
        und der Admin mehrere Dinge in einem Zug bearbeiten kann."""
        category = await db_ticket_categories.get_category(self.category_id, interaction.guild_id)
        if category is None:
            self.stop()
            await interaction.edit_original_response(content=_category_gone_text(self.lang), embed=None, view=None)
            return
        role_ids = await db_ticket_categories.get_category_roles(self.category_id)
        color    = await db_settings.get_color(interaction.guild_id)
        await interaction.edit_original_response(
            embed=_category_overview_embed(category, role_ids, self.lang, color),
            view=self,
        )

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.secondary)
    async def rename_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        category = await db_ticket_categories.get_category(self.category_id, interaction.guild_id)
        if category is None:
            await interaction.response.send_message(_category_gone_text(self.lang), ephemeral=True)
            return
        await interaction.response.send_modal(RenameCategoryModal(self.category_id, category["name"], self.lang, self))

    @discord.ui.button(label="Edit Roles", style=discord.ButtonStyle.secondary)
    async def roles_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_role_ids = await db_ticket_categories.get_category_roles(self.category_id)
        await interaction.response.edit_message(
            content=self.lang.get("ticket_roles_select_prompt", "Select the roles that should get access:"),
            embed=None,
            view=EditRoleSelectView(self.category_id, self.lang, self, current_role_ids),
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        has_open = await db_tickets.has_open_tickets_for_category(self.category_id)
        if has_open:
            await interaction.response.send_message(
                self.lang.get(
                    "ticket_delete_blocked",
                    "❌ This category cannot be deleted while open tickets still exist. "
                    "Please close all tickets in this category first.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            content=self.lang.get(
                "ticket_delete_confirm_prompt",
                "⚠️ **Are you sure?** The Discord category and its configuration will be permanently deleted.",
            ),
            embed=None,
            view=DeleteCategoryConfirmView(self.category_id, self.lang),
        )

class RenameCategoryModal(NexusModal):
    def __init__(self, category_id: int, current_name: str, lang: dict, parent_view: CategoryEditView):
        super().__init__(lang, title=lang.get("ticket_rename_modal_title", "Rename Category"))
        self.category_id = category_id
        self.parent_view  = parent_view

        self.name_input = discord.ui.TextInput(
            label=lang.get("ticket_add_category_name_label", "Category Name"),
            default=current_name,
            min_length=1,
            max_length=100,
            required=True,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        new_name = self.name_input.value.strip()

        category = await db_ticket_categories.get_category(self.category_id, interaction.guild_id)
        if category is None:
            await interaction.followup.send(_category_gone_text(self.lang), ephemeral=True)
            return
        discord_category = interaction.guild.get_channel(category["category_channel_id"])

        if discord_category is None:
            await interaction.followup.send(
                self.lang.get(
                    "ticket_rename_channel_missing",
                    "❌ The linked Discord category was not found (manually deleted?). "
                    "Rename aborted — please delete and recreate the category instead.",
                ),
                ephemeral=True,
            )
            return

        try:
            await discord_category.edit(name=new_name, reason=f"Nexus Ticketsystem — umbenannt von {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send(
                self.lang.get("ticket_no_permission_category", "❌ I don't have permission to manage categories."),
                ephemeral=True,
            )
            return

        await db_ticket_categories.rename_category(self.category_id, interaction.guild_id, new_name)
        await refresh_panel(interaction.guild)
        await self.parent_view._update_view(interaction)

class EditRoleSelectView(NexusView):
    def __init__(self, category_id: int, lang: dict, parent_view: CategoryEditView, current_role_ids: list[int]):
        super().__init__(lang, owner_id=None, require=None, timeout=180)
        self.category_id = category_id
        self.parent_view  = parent_view
        self.select_roles.placeholder = lang.get("ticket_roles_select_placeholder", "Select roles...")
        self.select_roles.default_values = [
            discord.SelectDefaultValue(id=role_id, type=discord.SelectDefaultValueType.role)
            for role_id in current_role_ids
        ]

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Select roles...",
        min_values=0,
        max_values=25,
    )
    async def select_roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if await db_ticket_categories.get_category(self.category_id, interaction.guild_id) is None:
            self.stop()
            await interaction.response.edit_message(content=_category_gone_text(self.lang), embed=None, view=None)
            return
        role_ids = [role.id for role in _assignable_roles(select.values)]
        await db_ticket_categories.set_category_roles(self.category_id, role_ids)
        self.stop()  # sonst überschreibt unser eigener Timeout später den zurückgekehrten Hub
        await interaction.response.defer()
        await self.parent_view._update_view(interaction)
        log.info("Rollen für Ticket-Kategorie %s aktualisiert: %s", self.category_id, role_ids)

class DeleteCategoryConfirmView(NexusView):
    def __init__(self, category_id: int, lang: dict):
        super().__init__(lang, owner_id=None, require=None, timeout=60)
        self.category_id = category_id
        self.confirm_btn.label = lang.get("ticket_delete_confirm_btn", "Yes, delete permanently")
        self.confirm_btn.emoji = "⚠️"
        self.cancel_btn.label  = lang.get("rr_btn_cancel", "Abort")

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        has_open = await db_tickets.has_open_tickets_for_category(self.category_id)
        if has_open:
            self.stop()
            await interaction.edit_original_response(
                content=self.lang.get(
                    "ticket_delete_blocked",
                    "❌ This category cannot be deleted while open tickets still exist. "
                    "Please close all tickets in this category first.",
                ),
                embed=None,
                view=None,
            )
            return

        category = await db_ticket_categories.get_category(self.category_id, interaction.guild_id)
        if category is None:
            # Inzwischen woanders gelöscht (z.B. Dashboard) — nicht fälschlich "gelöscht" melden
            self.stop()
            await interaction.edit_original_response(content=_category_gone_text(self.lang), embed=None, view=None)
            return
        discord_category = interaction.guild.get_channel(category["category_channel_id"])

        if discord_category is not None:
            try:
                await discord_category.delete(reason=f"Nexus Ticketsystem — gelöscht von {interaction.user}")
            except discord.Forbidden:
                await interaction.followup.send(
                    self.lang.get("ticket_no_permission_category", "❌ I don't have permission to manage categories."),
                    ephemeral=True,
                )
                return

        try:
            await db_ticket_categories.delete_category(self.category_id, interaction.guild_id)
        except Exception as e:
            log.exception("DB-Löschung der Kategorie %s auf %s fehlgeschlagen, obwohl die Discord-Kategorie schon gelöscht war: %s", self.category_id, interaction.guild.name, e)
            await interaction.followup.send(
                self.lang.get(
                    "ticket_delete_db_orphaned",
                    "⚠️ The Discord category was deleted, but an internal database error occurred. "
                    "Please contact bot support so the configuration can be cleaned up.",
                ),
                ephemeral=True,
            )
            return

        await refresh_panel(interaction.guild)

        self.stop()
        await interaction.edit_original_response(
            content=self.lang.get("ticket_delete_success", "{nexus_checkmark} Category has been deleted.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK
            ),
            embed=None,
            view=None,
        )
        log.info("Ticket-Kategorie %s auf %s gelöscht von %s.", self.category_id, interaction.guild.name, interaction.user)

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        category = await db_ticket_categories.get_category(self.category_id, interaction.guild_id)
        if category is None:
            await interaction.response.send_message(_category_gone_text(self.lang), ephemeral=True)
            return
        role_ids = await db_ticket_categories.get_category_roles(self.category_id)
        color    = await db_settings.get_color(interaction.guild_id)
        await self.show(
            interaction,
            CategoryEditView(self.category_id, self.lang),
            content=None,
            embed=_category_overview_embed(category, role_ids, self.lang, color),
        )

# ─── Ticket-Panel — Text speichern, bestehendes Panel bearbeiten ─────────────

class TicketPanelModal(NexusModal):
    def __init__(self, lang: dict, channel: discord.TextChannel | None, title_default: str | None, desc_default: str | None):
        super().__init__(lang, title=lang.get("ticket_panel_modal_title", "Create Ticket Panel"))
        self.channel = channel

        self.title_input = discord.ui.TextInput(
            label=lang.get("ticket_panel_title_label", "Title"),
            placeholder=lang.get("ticket_panel_title_default", "🎫 Support"),
            default=title_default,
            max_length=256,
            required=True,
        )
        self.desc_input = discord.ui.TextInput(
            label=lang.get("ticket_panel_desc_label", "Description"),
            style=discord.TextStyle.paragraph,
            placeholder=lang.get("ticket_panel_desc_default", "Select a category below to create a ticket."),
            default=desc_default,
            max_length=1000,
            required=True,
        )
        self.add_item(self.title_input)
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            message, edited = await publish_panel(
                interaction.guild, self.title_input.value, self.desc_input.value, self.channel,
            )
        except PanelError as e:
            await interaction.followup.send(_panel_error_text(e, self.lang, self.channel), ephemeral=True)
            return

        if edited:
            text = self.lang.get("ticket_panel_updated", "{nexus_checkmark} Panel updated in {channel}.")
        else:
            text = self.lang.get("ticket_panel_posted", "{nexus_checkmark} Panel has been posted in {channel}.")
        await interaction.followup.send(
            text.format(nexus_checkmark=NexusEmojis.CHECKMARK, channel=message.channel.mention),
            ephemeral=True,
        )

# ─── /ticket_settings — die zwei Fristen ─────────────────────────────────────

def _ticket_settings_embed(settings: dict, lang: dict, color: int) -> discord.Embed:
    embed = discord.Embed(title=lang.get("ticket_settings_title", "⚙️ Ticket settings"), color=color)
    embed.add_field(
        name=lang.get("ticket_settings_closure_field", "⏳ Auto-close without response"),
        value=_duration(settings["closure_timeout_hours"], lang),
        inline=True,
    )
    embed.add_field(
        name=lang.get("ticket_settings_deletion_field", "🗑️ Delete channel after closing"),
        value=_duration(settings["deletion_delay_hours"], lang),
        inline=True,
    )
    embed.set_footer(text=NEXUS_FOOTER)
    return embed

class TicketSettingsView(NexusView):
    def __init__(self, lang: dict, settings: dict):
        super().__init__(lang, timeout=180)
        self.closure_select.placeholder  = lang.get("ticket_settings_closure_placeholder", "Auto-close after …")
        self.deletion_select.placeholder = lang.get("ticket_settings_deletion_placeholder", "Delete channel after …")
        self._sync(settings)

    def _sync(self, settings: dict):
        self.closure_select.options = [
            discord.SelectOption(label=_duration(h, self.lang), value=str(h), emoji="⏳",
                                 default=h == settings["closure_timeout_hours"])
            for h in db_ticket_settings.CLOSURE_OPTIONS
        ]
        self.deletion_select.options = [
            discord.SelectOption(label=_duration(h, self.lang), value=str(h), emoji="🗑️",
                                 default=h == settings["deletion_delay_hours"])
            for h in db_ticket_settings.DELETION_OPTIONS
        ]

    async def _save(self, interaction: discord.Interaction, **fields):
        settings = await db_ticket_settings.update_ticket_settings(interaction.guild_id, **fields)
        self._sync(settings)
        color = await db_settings.get_color(interaction.guild_id)
        await interaction.response.edit_message(embed=_ticket_settings_embed(settings, self.lang, color), view=self)

    @discord.ui.select(cls=discord.ui.Select, min_values=1, max_values=1, row=0)
    async def closure_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self._save(interaction, closure_timeout_hours=int(select.values[0]))

    @discord.ui.select(cls=discord.ui.Select, min_values=1, max_values=1, row=1)
    async def deletion_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self._save(interaction, deletion_delay_hours=int(select.values[0]))

# ─── Ticket-Erstellung — Problem-Modal + Channel-Erstellung ──────────────────

class TicketProblemModal(NexusModal):
    def __init__(self, category_id: int, category_name: str, lang: dict):
        super().__init__(lang, title=lang.get("ticket_problem_modal_title", "Create Ticket"))
        self.category_id   = category_id
        self.category_name = category_name

        self.description_input = discord.ui.TextInput(
            label=lang.get("ticket_problem_label", "Describe your issue"),
            style=discord.TextStyle.paragraph,
            placeholder=lang.get("ticket_problem_placeholder", "What can we help you with?"),
            max_length=1000,
            required=True,
        )
        self.add_item(self.description_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key = (interaction.guild_id, interaction.user.id, self.category_id)
        if key in _creating_tickets:
            await interaction.followup.send(
                self.lang.get("ticket_creation_in_progress", "❌ Your ticket is already being created."),
                ephemeral=True,
            )
            return
        _creating_tickets.add(key)
        try:
            await self._create_ticket(interaction)
        finally:
            _creating_tickets.discard(key)

    async def _create_ticket(self, interaction: discord.Interaction):
        lang  = self.lang
        guild = interaction.guild

        category_data = await db_ticket_categories.get_category(self.category_id, guild.id)
        if category_data is None:
            await interaction.followup.send(
                lang.get("ticket_category_not_found", "❌ This category was not found."), ephemeral=True
            )
            return

        existing = await db_tickets.get_open_ticket(guild.id, interaction.user.id, self.category_id)
        existing = await _drop_if_channel_gone(interaction.guild, existing)
        if existing is not None:
            await interaction.followup.send(
                lang.get(
                    "ticket_already_open",
                    "❌ You already have an open ticket in this category: <#{channel_id}>",
                ).format(channel_id=existing["channel_id"]),
                ephemeral=True,
            )
            return

        discord_category = guild.get_channel(category_data["category_channel_id"])
        if discord_category is None:
            await interaction.followup.send(
                lang.get(
                    "ticket_category_channel_missing",
                    "❌ The Discord category no longer exists. Please inform an admin.",
                ),
                ephemeral=True,
            )
            return

        bot_member = guild.me or await guild.fetch_member(interaction.client.user.id)
        role_ids = await db_ticket_categories.get_category_roles(self.category_id)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                attach_files=True,
                read_message_history=True,
                manage_channels=True,
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                attach_files=True,
                read_message_history=True,
            ),
        }
        for role_id in role_ids:
            role = guild.get_role(role_id)
            # @everyone nie überschreiben — auch nicht für Altdaten aus der DB
            if role and not role.is_default():
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    embed_links=True,
                    attach_files=True,
                    read_message_history=True,
                )

        # Ohne Admin: gegen die Kategorie prüfen, dort gelten Nexus' effektive Rechte
        check = can_apply_overwrites(discord_category, overwrites)
        if not check:
            log.warning("Ticket-Overwrites auf %s nicht möglich: %s %s", guild.id, check.issue.value, check.missing)
            await interaction.followup.send(format_issue(check, lang), ephemeral=True)
            return

        ticket_number = await db_ticket_categories.get_next_ticket_number(self.category_id)
        channel_name  = f"{_category_slug(self.category_name)}-{ticket_number:04d}"
        try:
            ticket_channel = await guild.create_text_channel(
                name=channel_name,
                category=discord_category,
                overwrites=overwrites,
                reason=f"Nexus Ticketsystem — geöffnet von {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                lang.get("ticket_no_permission_channel", "❌ I don't have permission to create the ticket channel."),
                ephemeral=True,
            )
            return

        try:
            ticket_id = await db_tickets.create_ticket(
                guild_id=guild.id, channel_id=ticket_channel.id, user_id=interaction.user.id,
                category_id=self.category_id, ticket_number=ticket_number,
            )
        except Exception:
            try:
                await ticket_channel.delete(reason="Nexus Ticketsystem — Anlegen fehlgeschlagen")
            except discord.HTTPException:
                pass
            raise

        intro_embed = discord.Embed(
            title=lang.get("ticket_created_intro_title", "🎫 Ticket #{number}").format(number=ticket_number),
            description=self.description_input.value,
            color=await db_settings.get_color(guild.id),
            timestamp=discord.utils.utcnow(),
        )
        intro_embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        intro_embed.set_footer(text=self.category_name)

        role_mentions = " ".join(f"<@&{rid}>" for rid in role_ids)
        try:
            await ticket_channel.send(
                content=f"{interaction.user.mention} {role_mentions}".strip(),
                embed=intro_embed,
                view=_build_close_request_view(ticket_id, lang),
                allowed_mentions=discord.AllowedMentions(users=True, roles=True),
            )
        except discord.HTTPException as e:
            # Ticket steht trotzdem — ohne Close-Button bleibt /force_close als Weg zum Schließen
            log.warning("Intro-Nachricht in Ticket-Channel %s nicht gepostet: %s", ticket_channel.id, e)

        await interaction.followup.send(
            lang.get("ticket_created_success", "{nexus_checkmark} Your ticket has been created: {channel}").format(
                nexus_checkmark=NexusEmojis.CHECKMARK, channel=ticket_channel.mention
            ),
            ephemeral=True,
        )
        log.info("Ticket #%s (%s) für %s in Kategorie '%s' auf %s erstellt.",
                  ticket_number, ticket_channel.id, interaction.user, self.category_name, guild.name)

# (guild_id, user_id, category_id), für die gerade ein Ticket angelegt wird — zwei
# gleichzeitig offene Modals (z.B. Handy + PC) dürfen nicht zwei Tickets erzeugen
_creating_tickets: set[tuple[int, int, int]] = set()

def _ticket_matches(ticket: dict | None, interaction: discord.Interaction) -> bool:
    """Die Ticket-ID kommt aus der custom_id — sicherstellen, dass das Ticket wirklich
    zu diesem Server und diesem Channel gehört, bevor darauf gehandelt wird."""
    return (
        ticket is not None
        and ticket["guild_id"] == interaction.guild_id
        and ticket["channel_id"] == interaction.channel_id
    )

# ─── Support-Berechtigung ─────────────────────────────────────────────────────

async def _is_support_for_category(member: discord.Member, category_id: int) -> bool:
    """Admin ODER Inhaber einer der Kategorie zugeordneten Rolle darf das Ticket
    schließen/force-closen. Bewusst NICHT über default_permissions(manage_guild)
    gelöst, da das Support-Trainees ohne Server-weite Mod-Rechte aussperren würde —
    genau das sollte die Rollen-Zuordnung pro Kategorie ja vermeiden."""
    if member.guild_permissions.administrator:
        return True
    role_ids = await db_ticket_categories.get_category_roles(category_id)
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & set(role_ids))

# ─── Transcript ────────────────────────────────────────────────────────────────

def _embed_to_transcript_text(embed: discord.Embed) -> str:
    """Wandelt ein Embed in lesbaren Text um — nötig, weil Ticket-Erstellung,
    Schließ-Anfrage und Wiedereröffnung alle als Embed gepostet werden, nicht
    als reiner Nachrichtentext. Ohne diese Funktion würden genau diese
    System-Ereignisse im Transcript als leere Zeilen erscheinen."""
    parts = []
    if embed.title:
        parts.append(embed.title)
    if embed.description:
        parts.append(embed.description)
    for field in embed.fields:
        parts.append(f"{field.name}: {field.value}")
    return " | ".join(parts)

async def _generate_transcript_text(channel: discord.TextChannel) -> str:
    """clean_content statt content — löst Mentions/Rollen lesbar auf,
    keine rohen <@id>-Fragmente im Transcript. Embeds werden zusätzlich
    mit ausgelesen (siehe _embed_to_transcript_text), da die Ticket-Erst-
    nachricht, Schließ-Anfragen und Wiedereröffnungs-Begründungen alle als
    Embed statt reiner Text gepostet werden."""
    lines = []
    async for msg in channel.history(limit=None, oldest_first=True):
        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author    = msg.author.display_name
        content   = msg.clean_content or ""

        for embed in msg.embeds:
            embed_text = _embed_to_transcript_text(embed)
            if embed_text:
                content += f" [System] {embed_text}" if content else f"[System] {embed_text}"

        if msg.attachments:
            attach_urls = ", ".join(a.url for a in msg.attachments)
            content += f" [Anhang: {attach_urls}]" if content else f"[Anhang: {attach_urls}]"

        if not content:
            continue  # z.B. reine View-Nachrichten ohne Embed/Text/Anhang — nichts Sinnvolles zu protokollieren

        lines.append(f"[{timestamp}] {author}: {content}")
    return "\n".join(lines) if lines else "(Keine Nachrichten)"

def _transcript_file(text: str, channel_name: str) -> discord.File:
    """Erzeugt ein frisches discord.File-Objekt — muss pro Zustellort (Channel,
    DM, Log) neu gebaut werden, da ein discord.File nur einmal versendet werden kann."""
    buffer = io.BytesIO(text.encode("utf-8"))
    return discord.File(buffer, filename=f"transcript-{channel_name}.txt")

# ─── Zentrale Schließ-Logik — genutzt von User-Bestätigung, Auto-Timeout, Force-Close ──

async def _finalize_close(
    bot,
    guild: discord.Guild,
    ticket: dict,
    lang: dict,
    closed_by_type: str,
    closed_by_user_id: int | None = None,
    close_reason: str | None = None,
) -> bool:
    """Schließt das Ticket endgültig. False, wenn es schon geschlossen war.

    Der Status wird ZUERST in der DB gesetzt: Transcript, DM und Log dauern einige
    Sekunden, und in dieser Zeit darf kein zweiter Close (Doppelklick, Auto-Timeout)
    und kein offenes "Ablehnen"-Modal mehr greifen."""
    ticket_settings = await db_ticket_settings.get_ticket_settings(guild.id)
    deletion_hours = ticket_settings["deletion_delay_hours"]
    if not await db_tickets.close_ticket(
        ticket["id"], closed_by_type, closed_by_user_id, close_reason, deletion_hours=deletion_hours,
    ):
        return False

    channel = guild.get_channel(ticket["channel_id"])
    if channel is None:
        # Channel existiert schon nicht mehr (z.B. manuell gelöscht) — kein Transcript möglich
        return True

    # Erst sperren, dann Transcript — sonst fehlt, was der User in der Zwischenzeit schreibt
    member = await get_member_safe(guild, ticket["user_id"])
    if member is not None:
        await _set_ticket_write_access(channel, member, False)   # auch beim Force-Close aus "open"

    transcript_text = await _generate_transcript_text(channel)

    closing_embed = discord.Embed(
        title=lang.get("ticket_closed_title", "🔒 Ticket closed"),
        description=lang.get(
            "ticket_closed_desc",
            "This channel will be automatically deleted in **{duration}**. You can find the transcript attached.",
        ).format(duration=_duration(deletion_hours, lang)),
        color=discord.Color.dark_grey(),
    )
    if closed_by_type == "support_forced" and close_reason:
        closing_embed.add_field(
            name=lang.get("ticket_closed_reason_field", "Reason"),
            value=close_reason,
            inline=False,
        )

    try:
        await channel.send(embed=closing_embed, file=_transcript_file(transcript_text, channel.name))
    except discord.HTTPException as e:
        log.exception("Konnte Schluss-Nachricht in Ticket-Channel %s nicht posten: %s", channel.id, e)

    # DM an den User — best effort, analog dm_user() in utils.py
    if member is not None:
        try:
            await member.send(
                content=lang.get("ticket_transcript_dm_note", "Here's the transcript of your ticket from **{guild}**:").format(guild=guild.name),
                file=_transcript_file(transcript_text, channel.name),
            )
        except discord.Forbidden:
            log.info("Konnte %s keine Transcript-DM schicken (DMs deaktiviert).", member)
        except discord.HTTPException as e:
            log.warning("Transcript-DM an %s fehlgeschlagen: %s", member, e)

    # Log-Channel für Support/Admins — eigener ticket_log_channel_id mit Fallback auf log_channel_id
    settings = await db_settings.get_guild(guild.id)
    log_channel_id = (settings.get("ticket_log_channel_id") or settings.get("log_channel_id")) if settings else None
    if log_channel_id:
        log_channel = guild.get_channel(log_channel_id)
        if log_channel is not None:
            log_embed = discord.Embed(
                title=lang.get("ticket_log_closed_title", "🎫 Ticket closed — #{number}").format(number=ticket["ticket_number"]),
                color=discord.Color.dark_grey(),
            )
            log_embed.add_field(name=lang.get("log_user", "User"), value=f"<@{ticket['user_id']}>", inline=True)
            closed_by = lang.get(f"ticket_close_type_{closed_by_type}", CLOSE_TYPE_FALLBACKS.get(closed_by_type, closed_by_type))
            if closed_by_user_id:
                closed_by += f"\n<@{closed_by_user_id}>"
            log_embed.add_field(name=lang.get("ticket_log_closed_type", "Closed by"), value=closed_by, inline=True)
            if close_reason:
                log_embed.add_field(name=lang.get("ticket_closed_reason_field", "Reason"), value=close_reason, inline=False)
            try:
                await log_channel.send(embed=log_embed, file=_transcript_file(transcript_text, channel.name))
            except discord.Forbidden:
                check = can_use_channel(log_channel, "send_messages", "embed_links", "attach_files")
                fehlt = ", ".join(check.missing) if check.missing else ("view_channel" if not check.ok else "unbekannt")
                log.warning(
                    "Ticket-Transcript auf %s (%s) nicht in Log-Channel %s gepostet: Missing Permissions (fehlt: %s).",
                    guild.name, guild.id, log_channel_id, fehlt,
                )
            except discord.HTTPException as e:
                log.exception("Konnte Ticket-Transcript nicht in Log-Channel %s posten: %s", log_channel_id, e)

    log.info("Ticket #%s (%s) auf %s geschlossen (%s).", ticket["ticket_number"], channel.id, guild.name, closed_by_type)
    return True

async def _reject_stale_decision(
    interaction: discord.Interaction, ticket: dict | None, lang: dict, *, declining: bool,
) -> None:
    """Bestätigen/Ablehnen auf einer Schließ-Anfrage, die nicht mehr gilt: passende
    Meldung je nach aktuellem Status, und die veralteten Buttons verschwinden."""
    if ticket is not None and ticket["status"] == "closed":
        if declining:
            text = lang.get("ticket_reopen_invalid", "❌ This ticket can no longer be reopened.")
        else:
            text = lang.get("ticket_already_closed", "❌ This ticket is already closed.")
    else:
        # Ticket wurde inzwischen wieder geöffnet (oder existiert nicht mehr)
        text = lang.get("ticket_close_request_outdated", "❌ This close request is no longer active.")
    await interaction.response.send_message(text, ephemeral=True)

    # Nur Buttons im eigenen Ticket-Channel anfassen — bei fremder channel_id ist die
    # custom_id manipuliert bzw. kopiert, die Nachricht gehört dann nicht hierher
    if ticket is None or ticket["channel_id"] == interaction.channel_id:
        await _remove_buttons(interaction.message)

async def _remove_buttons(message: discord.Message | None) -> None:
    """Bestätigen/Ablehnen-Buttons nach der Entscheidung entfernen — sonst bleiben sie
    unter alten Schließ-Anfragen klickbar. Best effort."""
    if message is None:
        return
    try:
        await message.edit(view=None)
    except discord.HTTPException:
        pass

# ─── Schließen-Flow — Views & Modals ──────────────────────────────────────────

def _build_close_request_view(ticket_id: int, lang: dict) -> discord.ui.View:
    """Button in der Ticket-Erstnachricht, für Support klickbar. Wie beim Panel:
    roher on_interaction-Listener statt persistenter View-Klasse, custom_id trägt
    die ticket_id fest codiert, übersteht Neustarts."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label=lang.get("ticket_close_btn_label", "Close Ticket"),
        emoji= "🔒",
        style=discord.ButtonStyle.secondary,
        custom_id=f"nexus_ticket_close_request_{ticket_id}",
    ))
    return view

def _build_confirm_decline_view(ticket_id: int, lang: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label=lang.get("ticket_confirm_btn_label", "Confirm"), style=discord.ButtonStyle.success,
        emoji= NexusEmojis.CHECKMARK,
        custom_id=f"nexus_ticket_confirm_{ticket_id}",
    ))
    view.add_item(discord.ui.Button(
        label=lang.get("ticket_decline_btn_label", "Decline"), style=discord.ButtonStyle.secondary,
        emoji="❌",
        custom_id=f"nexus_ticket_decline_{ticket_id}",
    ))
    return view

class DeclineReasonModal(NexusModal):
    def __init__(self, ticket: dict, lang: dict):
        super().__init__(lang, title=lang.get("ticket_decline_modal_title", "Why isn't the issue resolved?"))
        self.ticket = ticket

        self.reason_input = discord.ui.TextInput(
            label=lang.get("ticket_decline_reason_label", "Reason"),
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        lang = self.lang

        # Zwischen Klick und Absenden kann das Ticket per /force_close oder Timeout
        # geschlossen worden sein — dann darf es hier nicht wieder aufgehen
        if not await db_tickets.reopen_ticket(self.ticket["id"]):
            await _remove_buttons(interaction.message)
            await interaction.followup.send(
                lang.get("ticket_reopen_invalid", "❌ This ticket can no longer be reopened."), ephemeral=True
            )
            return
        await _remove_buttons(interaction.message)

        channel = interaction.channel
        member = await get_member_safe(interaction.guild, self.ticket["user_id"])
        if member is not None:
            # send_messages wieder freigeben — Gegenstück zum Entzug beim close_request
            await _set_ticket_write_access(channel, member, True)

        role_ids      = await db_ticket_categories.get_category_roles(self.ticket["category_id"])
        role_mentions = " ".join(f"<@&{rid}>" for rid in role_ids)

        reopen_embed = discord.Embed(
            title=lang.get("ticket_reopened_title", "🔓 Ticket reopened"),
            description=self.reason_input.value,
            color=discord.Color.orange(),
        )
        await interaction.followup.send(content=role_mentions or None, embed=reopen_embed)
        log.info("Ticket #%s auf %s wieder geöffnet (User lehnte Schließung ab).", self.ticket["ticket_number"], interaction.guild.name)

class ForceCloseReasonModal(NexusModal):
    def __init__(self, ticket: dict, lang: dict):
        super().__init__(lang, title=lang.get("ticket_force_close_modal_title", "Force-close Ticket"))
        self.ticket = ticket

        self.reason_input = discord.ui.TextInput(
            label=lang.get("ticket_force_close_reason_label", "Reason (required)"),
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = self.lang

        closed = await _finalize_close(
            interaction.client, interaction.guild, self.ticket, lang,
            closed_by_type="support_forced",
            closed_by_user_id=interaction.user.id,
            close_reason=self.reason_input.value,
        )
        if not closed:
            await interaction.followup.send(
                lang.get("ticket_already_closed", "❌ This ticket is already closed."), ephemeral=True
            )
            return
        await interaction.followup.send(
            lang.get("ticket_force_closed_success", "{nexus_checkmark} Ticket has been permanently closed.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK
            ),
            ephemeral=True,
        )

# ─── Cog ─────────────────────────────────────────────────────────────────────

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_overdue_closures.start()
        self.check_due_deletions.start()

        # Kontextmenü (Rechtsklick auf Nachricht → Apps): altes Panel übernehmen.
        # Kontextmenüs lassen sich nicht per Decorator in einem Cog definieren, daher hier.
        self.panel_menu = app_commands.ContextMenu(name="Use as ticket panel", callback=self.use_as_ticket_panel)
        self.bot.tree.add_command(self.panel_menu)

    async def cog_unload(self):
        self.check_overdue_closures.cancel()
        self.check_due_deletions.cancel()
        self.bot.tree.remove_command(self.panel_menu.name, type=self.panel_menu.type)

    # ─── Listener ─────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        ticket = await db_tickets.get_ticket_by_channel(channel.id)
        if ticket is None or ticket["status"] == "closed":
            return
        await db_support.revoke_access(channel.id)
        await db_tickets.delete_ticket_record(ticket["id"])
        log.info("Ticket #%s (%s) auf %s: Channel manuell gelöscht, Eintrag entfernt.",
                 ticket["ticket_number"], channel.id, channel.guild.name)

    # ─── Timeout: pending_closure ohne Reaktion automatisch schließen ─────────
    @tasks.loop(hours=1)
    async def check_overdue_closures(self):
        try:
            overdue = await db_tickets.get_overdue_pending_closures()
            for ticket in overdue:
                guild = self.bot.get_guild(ticket["guild_id"])
                if guild is None:
                    await db_tickets.close_ticket(ticket["id"], "auto_timeout")
                    continue

                lang = await get_lang(guild.id)
                try:
                    await _finalize_close(self.bot, guild, ticket, lang, closed_by_type="auto_timeout")
                except Exception:
                    log.exception("Fehler beim Auto-Timeout-Close für Ticket %s auf %s.", ticket["id"], guild.name)
        except Exception:
            log.exception("Fehler im check_overdue_closures-Loop.")

    @check_overdue_closures.before_loop
    async def before_check_overdue_closures(self):
        await self.bot.wait_until_ready()

    # ─── Löschung: geschlossene Ticket-Channels nach Ablauf der Frist aufräumen ──
    @tasks.loop(hours=1)
    async def check_due_deletions(self):
        try:
            due = await db_tickets.get_due_for_deletion()
            for ticket in due:
                guild = self.bot.get_guild(ticket["guild_id"])
                if guild is None:
                    await db_support.revoke_access(ticket["channel_id"])
                    await db_tickets.delete_ticket_record(ticket["id"])
                    continue

                channel = guild.get_channel(ticket["channel_id"])
                if channel is not None:
                    try:
                        await channel.delete(reason="Nexus Ticketsystem — Frist nach Schließung abgelaufen")
                    except discord.HTTPException as e:
                        log.exception("Konnte Ticket-Channel %s nicht löschen: %s", ticket["channel_id"], e)
                        continue  # Zeile bleibt stehen, nächster Loop-Durchlauf versucht es erneut

                await db_support.revoke_access(ticket["channel_id"])
                await db_tickets.delete_ticket_record(ticket["id"])
                log.info("Ticket #%s (%s) auf %s nach Ablauf der Frist gelöscht.", ticket["ticket_number"], ticket["channel_id"], guild.name)
        except Exception:
            log.exception("Fehler im check_due_deletions-Loop.")

    @check_due_deletions.before_loop
    async def before_check_due_deletions(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="add_category", description=app_commands.locale_str("cmd_add_category_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("tickets")
    async def add_category(self, interaction: discord.Interaction):
        lang = await get_lang(interaction.guild_id)

        current_count = await db_ticket_categories.get_category_count(interaction.guild_id)
        premium = await is_premium(interaction.guild_id)
        limit   = await db_limits.get_limit(interaction.guild_id, "ticket_categories", premium)

        if current_count >= limit:
            premium_limit = await db_limits.get_limit(interaction.guild_id, "ticket_categories", True)
            await interaction.response.send_message(
                lang.get(
                    "ticket_category_limit_reached",
                    "❌ Limit of {limit} ticket categories reached.{premium_hint}",
                ).format(
                    limit=limit,
                    premium_hint="" if premium else lang.get(
                        "ticket_category_limit_premium_hint", " With Premium, up to {max} are possible."
                    ).format(max=premium_limit),
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(AddCategoryModal(lang, self))

    @app_commands.command(name="edit_category", description=app_commands.locale_str("cmd_edit_category_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("tickets")
    @app_commands.describe(category=app_commands.locale_str("cmd_edit_category_param_desc"))
    async def edit_category(self, interaction: discord.Interaction, category: int):
        lang = await get_lang(interaction.guild_id)

        category_data = await db_ticket_categories.get_category(category, interaction.guild_id)
        if category_data is None:
            await interaction.response.send_message(
                lang.get("ticket_category_not_found", "❌ This category was not found."),
                ephemeral=True,
            )
            return

        role_ids = await db_ticket_categories.get_category_roles(category)
        color    = await db_settings.get_color(interaction.guild_id)

        view = CategoryEditView(category, lang)
        await interaction.response.send_message(
            embed=_category_overview_embed(category_data, role_ids, lang, color),
            view=view,
            ephemeral=True,
        )
        view.bind(interaction)

    @edit_category.autocomplete("category")
    async def edit_category_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        categories = await db_ticket_categories.get_categories(interaction.guild_id)
        return [
            app_commands.Choice(name=cat["name"], value=cat["id"])
            for cat in categories
            if current.lower() in cat["name"].lower()
        ][:25]

    @app_commands.command(name="ticket_panel", description=app_commands.locale_str("cmd_ticket_panel_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("tickets")
    @app_commands.describe(channel=app_commands.locale_str("cmd_ticket_panel_channel_desc"))
    async def ticket_panel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        lang = await get_lang(interaction.guild_id)
        categories = await db_ticket_categories.get_categories(interaction.guild_id)

        if not categories:
            await interaction.response.send_message(
                lang.get(
                    "ticket_panel_no_categories",
                    "❌ No ticket category exists yet. Create one first with `/add_category`.",
                ),
                ephemeral=True,
            )
            return

        settings = await db_ticket_settings.get_ticket_settings(interaction.guild_id)
        if channel is None and not settings.get("panel_message_id"):
            await interaction.response.send_message(
                lang.get("ticket_panel_need_channel", "❌ There is no panel yet — please choose a channel."),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            TicketPanelModal(lang, channel, settings.get("panel_title"), settings.get("panel_description"))
        )

    @app_commands.command(name="ticket_settings", description=app_commands.locale_str("cmd_ticket_settings_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("tickets")
    async def ticket_settings(self, interaction: discord.Interaction):
        lang     = await get_lang(interaction.guild_id)
        settings = await db_ticket_settings.get_ticket_settings(interaction.guild_id)
        color    = await db_settings.get_color(interaction.guild_id)

        view = TicketSettingsView(lang, settings)
        await interaction.response.send_message(
            embed=_ticket_settings_embed(settings, lang, color), view=view, ephemeral=True,
        )
        view.bind(interaction)

    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @module_required("tickets")
    async def use_as_ticket_panel(self, interaction: discord.Interaction, message: discord.Message):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        try:
            await import_panel(interaction.guild, message, self.bot.user.id)
        except PanelError as e:
            await interaction.followup.send(_panel_error_text(e, lang), ephemeral=True)
            return
        await interaction.followup.send(
            lang.get(
                "ticket_panel_imported",
                "{nexus_checkmark} Panel adopted. Text and buttons are now managed by Nexus — "
                "edit it anytime with `/ticket_panel`.",
            ).format(nexus_checkmark=NexusEmojis.CHECKMARK),
            ephemeral=True,
        )
        log.info("Ticket-Panel %s auf %s übernommen von %s.", message.id, interaction.guild.name, interaction.user)

    @app_commands.command(name="force_close", description=app_commands.locale_str("cmd_force_close_desc"))
    @app_commands.default_permissions(moderate_members=True)
    @module_required("tickets")
    async def force_close(self, interaction: discord.Interaction):
        lang = await get_lang(interaction.guild_id)

        ticket = await db_tickets.get_ticket_by_channel(interaction.channel_id)
        if ticket is None:
            await interaction.response.send_message(
                lang.get("ticket_not_a_ticket_channel", "❌ This is not a ticket channel."), ephemeral=True
            )
            return
        if ticket["status"] == "closed":
            await interaction.response.send_message(
                lang.get("ticket_already_closed", "❌ This ticket is already closed."), ephemeral=True
            )
            return
        # default_permissions lässt sich pro Server umstellen — Support-Rolle trotzdem selbst prüfen
        if not await _is_support_for_category(interaction.user, ticket["category_id"]):
            await interaction.response.send_message(
                lang.get("no_permission", "You don't have permission to use this command."), ephemeral=True
            )
            return

        await interaction.response.send_modal(ForceCloseReasonModal(ticket, lang))

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("nexus_ticket_"):
            return

        lang = await get_lang(interaction.guild_id)
        try:
            await self._handle_ticket_component(interaction, custom_id, lang)
        except Exception as e:
            await report_interaction_error(interaction, e, lang, where=f"Tickets/{custom_id}")

    async def _handle_ticket_component(self, interaction: discord.Interaction, custom_id: str, lang: dict):
        # ── Panel-Button: neues Ticket öffnen ──
        if custom_id.startswith("nexus_ticket_open_"):
            category_id = int(custom_id.removeprefix("nexus_ticket_open_"))

            # Alte Panels dürfen bei deaktiviertem Modul keine neuen Tickets mehr öffnen.
            # Schließen bestehender Tickets bleibt bewusst möglich.
            guild_settings = await db_settings.get_guild(interaction.guild_id)
            if not (guild_settings and guild_settings.get("modules", {}).get("tickets", False)):
                await interaction.response.send_message(
                    lang.get("ticket_module_disabled", "❌ The ticket system is currently disabled on this server."),
                    ephemeral=True,
                )
                return

            category_data = await db_ticket_categories.get_category(category_id, interaction.guild_id)
            if category_data is None:
                await interaction.response.send_message(
                    lang.get("ticket_category_not_found", "❌ This category was not found."), ephemeral=True
                )
                return

            existing = await db_tickets.get_open_ticket(interaction.guild_id, interaction.user.id, category_id)
            existing = await _drop_if_channel_gone(interaction.guild, existing)
            if existing is not None:
                await interaction.response.send_message(
                    lang.get(
                        "ticket_already_open",
                        "❌ You already have an open ticket in this category: <#{channel_id}>",
                    ).format(channel_id=existing["channel_id"]),
                    ephemeral=True,
                )
                return

            await interaction.response.send_modal(TicketProblemModal(category_id, category_data["name"], lang))
            return

        # ── Support klickt "Ticket schließen" (Schließ-Anfrage stellen) ──
        if custom_id.startswith("nexus_ticket_close_request_"):
            ticket_id = int(custom_id.removeprefix("nexus_ticket_close_request_"))
            ticket    = await db_tickets.get_ticket(ticket_id)

            if not _ticket_matches(ticket, interaction) or ticket["status"] != "open":
                await interaction.response.send_message(
                    lang.get("ticket_close_request_invalid", "❌ This ticket cannot be closed right now."),
                    ephemeral=True,
                )
                return

            is_support = await _is_support_for_category(interaction.user, ticket["category_id"])
            if not is_support:
                await interaction.response.send_message(
                    lang.get("no_permission", "You don't have permission to use this command."), ephemeral=True
                )
                return

            if not await db_tickets.request_closure(ticket_id):
                # Ein anderes Teammitglied war schneller
                await interaction.response.send_message(
                    lang.get("ticket_close_request_invalid", "❌ This ticket cannot be closed right now."),
                    ephemeral=True,
                )
                return

            member = await get_member_safe(interaction.guild, ticket["user_id"])
            if member is not None:
                await _set_ticket_write_access(interaction.channel, member, False)

            ticket_settings = await db_ticket_settings.get_ticket_settings(interaction.guild_id)
            description = lang.get(
                "ticket_pending_closure_desc",
                "<@{user_id}>, is your issue resolved? Please confirm or decline.",
            ).format(user_id=ticket["user_id"])
            description += "\n\n" + lang.get(
                "ticket_pending_closure_auto",
                "Without a response, this ticket closes automatically after **{duration}**.",
            ).format(duration=_duration(ticket_settings["closure_timeout_hours"], lang))

            embed = discord.Embed(
                title=lang.get("ticket_pending_closure_title", "🔒 Ticket is being closed"),
                description=description,
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, view=_build_confirm_decline_view(ticket_id, lang))
            return

        # ── User bestätigt die Schließung ──
        if custom_id.startswith("nexus_ticket_confirm_"):
            ticket_id = int(custom_id.removeprefix("nexus_ticket_confirm_"))
            ticket    = await db_tickets.get_ticket(ticket_id)

            if not _ticket_matches(ticket, interaction) or ticket["status"] != "pending_closure":
                await _reject_stale_decision(interaction, ticket, lang, declining=False)
                return
            if interaction.user.id != ticket["user_id"]:
                await interaction.response.send_message(
                    lang.get("ticket_only_creator", "❌ Only the creator of this ticket can confirm this."),
                    ephemeral=True,
                )
                return

            await interaction.response.defer()
            await _remove_buttons(interaction.message)
            if not await _finalize_close(self.bot, interaction.guild, ticket, lang, closed_by_type="user_confirmed"):
                await interaction.followup.send(
                    lang.get("ticket_already_closed", "❌ This ticket is already closed."), ephemeral=True
                )
            return

        # ── User lehnt die Schließung ab ──
        if custom_id.startswith("nexus_ticket_decline_"):
            ticket_id = int(custom_id.removeprefix("nexus_ticket_decline_"))
            ticket    = await db_tickets.get_ticket(ticket_id)

            if not _ticket_matches(ticket, interaction) or ticket["status"] != "pending_closure":
                await _reject_stale_decision(interaction, ticket, lang, declining=True)
                return
            if interaction.user.id != ticket["user_id"]:
                await interaction.response.send_message(
                    lang.get("ticket_only_creator", "❌ Only the creator of this ticket can confirm this."),
                    ephemeral=True,
                )
                return

            await interaction.response.send_modal(DeclineReasonModal(ticket, lang))
            return

async def setup(bot):
    await bot.add_cog(Tickets(bot))
