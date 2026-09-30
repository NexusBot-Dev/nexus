import logging
import discord

log = logging.getLogger(__name__)


# ─── Gemeinsame Fehlerausgabe ────────────────────────────────────────────────

async def send_ephemeral(interaction: discord.Interaction, msg: str) -> None:
    """Antwortet ephemeral — egal ob die Interaction schon beantwortet/deferred ist."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def report_interaction_error(
    interaction: discord.Interaction,
    error: Exception,
    lang: dict,
    *,
    where: str = "?",
) -> None:
    """Wird vom globalen Command-Handler UND von NexusView/NexusModal genutzt."""
    original = getattr(error, "original", error)

    if isinstance(original, discord.NotFound) and original.code == 10062:
        return  # Interaction abgelaufen — niemandem mehr etwas zu sagen

    if isinstance(original, discord.Forbidden):
        log.warning("Forbidden in %s auf %s: %s", where, interaction.guild_id, original.text)
        msg = lang.get(
            "error_forbidden",
            "❌ I'm missing permissions or my role is too low for this. Run `/diagnose` to see what's wrong.",
        )
    else:
        log.exception("Unbehandelter Fehler in %s:", where, exc_info=error)
        msg = lang.get("error_generic", "❌ Something went wrong. Please try again later.")

    await send_ephemeral(interaction, msg)


# ─── Views ──────────────────────────────────────────────────────────────────

class NexusView(discord.ui.View):
    """Basis für alle Nexus-Views."""

    # Antworttypen, bei denen @original sicher die View-Nachricht ist
    _UPDATE_TYPES = frozenset({
        discord.InteractionResponseType.message_update,
        discord.InteractionResponseType.deferred_message_update,
    })
    _MAX_TRACKED = 5
    DENY_KEY = "no_permission"   # Übersetzungs-Key für "dir fehlt das Recht" (Subklassen dürfen ihn überschreiben)

    def __init__(
        self,
        lang: dict,
        *,
        owner_id: int | None = None,
        require: str | None = "manage_guild",
        timeout: float | None = 300,
    ):
        super().__init__(timeout=timeout)
        self.lang     = lang
        self.owner_id = owner_id
        self.require  = require
        self.message: discord.Message | None = None       # normale Nachrichten: Bot-Token, kein Ablauf
        self._anchor: discord.Interaction | None = None   # Interaction, die die View gesendet hat
        self._clicks: list[discord.Interaction] = []      # ephemeral: Token, gefiltert auf Updates

    def bind(self, interaction: discord.Interaction) -> "NexusView":
        """Nach dem ersten Senden aufrufen, damit on_timeout die Nachricht findet."""
        self._anchor = interaction
        if interaction.message:   # bei Component-Interactions: die Nachricht mit der View
            self.message = interaction.message
        return self

    # 1. Zugriff ─────────────────────────────────────────────────────────────
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await send_ephemeral(interaction, self.lang.get(
                "view_not_yours", "❌ This menu belongs to someone else. Run the command yourself."
            ))
            return False

        if self.require and not getattr(interaction.user.guild_permissions, self.require, False):
            await send_ephemeral(interaction, self.lang.get(
                self.DENY_KEY, "❌ You don't have permission to use this."
            ))
            return False

        if interaction.message:
            self.message = interaction.message
        self._clicks = (self._clicks + [interaction])[-self._MAX_TRACKED:]
        return True

    # 2. Fehler ──────────────────────────────────────────────────────────────
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_interaction_error(
            interaction, error, self.lang,
            where=f"{type(self).__name__}/{getattr(item, 'custom_id', '?')}",
        )

    # 3. Timeout ─────────────────────────────────────────────────────────────
    def _edit_target(self) -> discord.Interaction | None:
        """Letzte Interaction, deren @original sicher die View-Nachricht ist.

        Klicks, die mit send_message geantwortet haben, zeigen mit @original auf
        DIE NEUE Nachricht — die überspringen wir.
        """
        for inter in reversed(self._clicks):
            if inter.response.type in self._UPDATE_TYPES:
                return inter
        return self._anchor

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        try:
            # 1. Normale Nachricht: Bot-Token, läuft nie ab
            if self.message and not self.message.flags.ephemeral:
                await self.message.edit(view=self)
                return
            # 2. Ephemeral: nur über Interaction-Token, gefiltert auf Update-Antworten
            target = self._edit_target()
            if target:
                await target.edit_original_response(view=self)
        except discord.HTTPException:
            pass  # Token abgelaufen (>15 min), Nachricht gelöscht o.ä.

    # Helfer ─────────────────────────────────────────────────────────────────
    async def show(self, interaction: discord.Interaction, next_view: "NexusView", **kwargs) -> None:
        """Nächsten Schritt in derselben Nachricht anzeigen und die View korrekt binden."""
        self.stop()  # alte View nicht mehr timeouten lassen, sie ist ersetzt
        await interaction.response.edit_message(view=next_view, **kwargs)
        next_view.bind(interaction)


# ─── Modals ─────────────────────────────────────────────────────────────────

class NexusModal(discord.ui.Modal):
    """Basis für alle Nexus-Modals: einheitliche Fehlerbehandlung."""

    def __init__(self, lang: dict, *, title: str, timeout: float | None = 300):
        super().__init__(title=title[:45], timeout=timeout)  # Discord-Limit: 45 Zeichen
        self.lang = lang

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_interaction_error(interaction, error, self.lang, where=type(self).__name__)


# ─── Fertige Bausteine ──────────────────────────────────────────────────────

class ConfirmView(NexusView):
    """
    Ja/Nein-Dialog.

        view = ConfirmView(lang, owner_id=interaction.user.id)
        await interaction.response.send_message("Wirklich löschen?", view=view, ephemeral=True)
        view.bind(interaction)
        if await view.wait_result():
            ...
    """

    def __init__(self, lang: dict, *, owner_id: int, require: str | None = None, timeout: float = 60):
        super().__init__(lang, owner_id=owner_id, require=require, timeout=timeout)
        self.result: bool | None = None
        self.confirm.label = lang.get("confirm_yes", "Confirm")
        self.cancel.label  = lang.get("confirm_no", "Cancel")

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        await self._finish(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        await self._finish(interaction)

    async def _finish(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def wait_result(self) -> bool:
        """True bei Bestätigung, False bei Abbruch oder Timeout."""
        await self.wait()
        return bool(self.result)