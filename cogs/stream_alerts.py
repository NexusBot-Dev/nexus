import logging
import discord
from discord import app_commands
from discord.ext import commands
from database import db_settings, db_stream_alerts, db_limits
from systems.premium import is_premium
from cogs.utils import get_lang, module_required
from emojis import NexusEmojis
from systems.platform_registry import PLATFORM_HANDLERS
from config import NEXUS_FOOTER
from systems.ui import NexusView, NexusModal

log = logging.getLogger(__name__)

PLATFORM_DISPLAY = {
    "twitch":  {"emoji": NexusEmojis.Integrations.TWITCH, "label": "Twitch"},
    "youtube": {"emoji": NexusEmojis.Integrations.YOUTUBE, "label": "YouTube"},
    "kick":    {"emoji": NexusEmojis.Integrations.KICK, "label": "Kick"},
}

PLATFORM_PLACEHOLDER_FALLBACKS = {
    "twitch":  "z.B. Username oder https://twitch.tv/Username",
    "youtube": "z.B. Username oder https://youtube.com/@Username",
    "kick":    "z.B. Username oder https://kick.com/Username",
}

class AlertMessageModal(NexusModal):
    def __init__(self, sub: dict, lang: dict, cog):
        super().__init__(lang, title=lang.get("alert_message_modal_title", "Edit Custom Message"))
        self.sub = sub
        self.cog = cog

        self.message_input = discord.ui.TextInput(
            label=lang.get("alert_message_modal_label", "Message (leave empty to remove)"),
            style=discord.TextStyle.paragraph,
            placeholder=lang.get("alert_add_modal_message_placeholder", "e.g., Let's go, team! 🎉"),
            default=self.sub.get("message") or "",
            max_length=200,
            required=False,
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_message = self.message_input.value.strip() or None

        await db_stream_alerts.update_subscription(
                    guild_id=interaction.guild_id,
                    monitored_channel_id=self.sub["monitored_channel_id"],
                    message=new_message,
                )

        self.sub["message"] = new_message
        color = await db_settings.get_color(interaction.guild_id)
        embed = self.cog._build_edit_embed(self.sub, self.lang, color)

        # Kein eigenes View-Objekt hier zum self.show()en — dieses Modal ist selbst
        # keine View, daher direktes edit_message (kein alter View-Timeout zu stoppen).
        await interaction.response.edit_message(
            embed=embed,
            view=AlertEditView(self.sub, self.lang, self.cog)
        )

class ChannelPickView(NexusView):
    def __init__(self, sub: dict, lang: dict, cog):
        super().__init__(lang, owner_id=None, require=None, timeout=60)
        self.sub = sub
        self.cog = cog
        self.select_channel.placeholder = lang.get("alert_edit_channel_placeholder", "Select new channel...")

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Select new channel...")
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        new_channel = select.values[0]
        await db_stream_alerts.update_subscription(
            guild_id=interaction.guild_id,
            monitored_channel_id=self.sub["monitored_channel_id"],
            channel_id=new_channel.id,
        )
        self.sub["channel_id"] = new_channel.id
        color = await db_settings.get_color(interaction.guild_id)
        embed = self.cog._build_edit_embed(self.sub, self.lang, color)

        feedback_text = self.lang.get(
            "alert_edit_channel_updated",
            "{nexus_checkmark} Alert-Channel was changed to {channel}."
        ).format(
            nexus_checkmark=NexusEmojis.CHECKMARK, 
            channel=new_channel.mention
        )

        self.stop()
        await interaction.response.edit_message(
            content=feedback_text, 
            embed=embed, 
            view=AlertEditView(self.sub, self.lang, self.cog)
        )


class AlertEditView(NexusView):
    def __init__(self, sub: dict, lang: dict, cog):
        super().__init__(lang, owner_id=None, require=None, timeout=120)
        self.sub = sub
        self.cog = cog
        self.edit_message_btn.label = lang.get("alert_edit_btn_message", "✏️ Change Message")
        self.edit_channel_btn.label = lang.get("alert_edit_btn_channel", "📺 Change Channel")

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def edit_message_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AlertMessageModal(self.sub, self.lang, self.cog))

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def edit_channel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show(
            interaction,
            ChannelPickView(self.sub, self.lang, self.cog),
            content=self.lang.get("alert_edit_pick_channel", "Select the new channel:"),
            embed=None,
        )


class StreamerSelectView(NexusView):
    def __init__(self, subs: list[dict], lang: dict, cog):
        super().__init__(lang, owner_id=None, require=None, timeout=60)
        self.subs = subs
        self.cog = cog
        self.select_streamer.placeholder = lang.get("alert_edit_select_placeholder", "Select a channel...")
        self.select_streamer.options = [
            discord.SelectOption(label=s["streamer_name"], value=str(s["monitored_channel_id"]))
            for s in subs
        ][:25]

    @discord.ui.select(placeholder="Select a channel...")
    async def select_streamer(self, interaction: discord.Interaction, select: discord.ui.Select):
        chosen_id = int(select.values[0])
        sub = next(s for s in self.subs if s["monitored_channel_id"] == chosen_id)
        lang = await get_lang(interaction.guild_id)
        color = await db_settings.get_color(interaction.guild_id)
        embed = self.cog._build_edit_embed(sub, lang, color)
        await self.show(interaction, AlertEditView(sub, lang, self.cog), content=None, embed=embed)

class RoleQuickSetView(NexusView):
    def __init__(self, lang: dict, guild_id: int, platform_value: str):
        super().__init__(lang, owner_id=None, require=None, timeout=60)
        self.guild_id = guild_id
        self.platform_value = platform_value
        self._interaction: discord.Interaction | None = None
        self._message_id: int | None = None
        self.select_role.placeholder = lang.get("alert_role_prompt_placeholder", "Select a role (optional)...")

    def bind_followup(self, interaction: discord.Interaction, message: discord.Message) -> "RoleQuickSetView":
        """Diese View lebt auf einer eigenen ephemeren Follow-up-Nachricht, nicht der
        @original-Antwort — deshalb ein eigener Timeout-Pfad statt bind()."""
        self._interaction = interaction
        self._message_id = message.id
        return self

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self._interaction and self._message_id:
            try:
                await self._interaction.followup.edit_message(self._message_id, view=self)
            except discord.HTTPException:
                pass

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Select a role (optional)...", min_values=0, max_values=1)
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not select.values:
            await interaction.response.edit_message(
                content=self.lang.get("alert_role_prompt_skipped", "Alright, no ping configured."),
                view=None,
            )
            return

        role = select.values[0]
        try:
            confirmation_text = self.lang.get("alert_role_set", "{nexus_checkmark} {role} will now be pinged for {platform}.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK,
                role=role.mention,
                platform=self.lang.get("alert_role_all_platforms", "all platforms"),
            )
        except KeyError:
            log.exception("Formatierungsfehler bei alert_role_set — Platzhalter fehlt.")
            confirmation_text = f"{NexusEmojis.CHECKMARK} {role.mention}"  # simpler Fallback, kann nicht crashen
        await db_stream_alerts.set_ping_role(self.guild_id, "all", role.id)
        await interaction.response.edit_message(content=confirmation_text, view=None)

class AddChannelModal(NexusModal):
    def __init__(self, lang: dict, platform_value: str, platform_label: str, handler, channel: discord.TextChannel):
        super().__init__(lang, title=lang.get("alert_add_modal_title", "Add {platform} Channel").format(platform=platform_label))
        self.platform_value = platform_value
        self.handler = handler
        self.channel = channel

        fallback = PLATFORM_PLACEHOLDER_FALLBACKS.get(platform_value, "z.B. Username")
        placeholder_key = f"alert_add_modal_streamer_{platform_value}_placeholder"

        self.streamer_input = discord.ui.TextInput(
            label=lang.get("alert_add_modal_streamer_label", "Username or Link"),
            placeholder=lang.get(placeholder_key, fallback),
            required=True,
            max_length=200,
        )
        self.message_input = discord.ui.TextInput(
            label=lang.get("alert_add_modal_message_label", "Custom Message (optional)"),
            style=discord.TextStyle.paragraph,
            placeholder=lang.get("alert_add_modal_message_placeholder", "e.g., Let's go, team! 🎉"),
            required=False,
            max_length=200,
        )
        self.add_item(self.streamer_input)
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = self.lang

        login = self.handler.normalize_input(self.streamer_input.value)
        if login is None:
            await interaction.followup.send(
                lang.get("alert_invalid_username", "❌ That doesn't look like a valid Username or Link."),
                ephemeral=True,
            )
            return

        current_count = await db_stream_alerts.count_subscriptions_for_guild(interaction.guild_id, self.platform_value)
        premium = await is_premium(interaction.guild_id)
        limit = await db_limits.get_limit(interaction.guild_id, f"stream_alerts_{self.platform_value}", premium)
        if current_count >= limit:
            await interaction.followup.send(
                lang.get("alert_limit_reached", "❌ Limit of {limit} monitored channels reached.").format(limit=limit),
                ephemeral=True,
            )
            return

        user = await self.handler.resolve_user(login)
        if user is None:
            await interaction.followup.send(
                lang.get("alert_user_not_found", "❌ User `{login}` was not found.").format(login=login),
                ephemeral=True,
            )
            return

        external_id = user["id"]
        monitored = await db_stream_alerts.get_monitored_channel(self.platform_value, external_id)

        if monitored is None:
            sub_ids = {}
            if self.handler.create_subscriptions:
                try:
                    sub_ids = await self.handler.create_subscriptions(external_id)
                except Exception:
                    log.exception("Subscription-Anlage für %s (%s) fehlgeschlagen.", login, self.platform_value)
                    await interaction.followup.send(
                        lang.get("alert_subscription_failed", "❌ Subscription could not be created. Please try again later."),
                        ephemeral=True,
                    )
                    return

            monitored = await db_stream_alerts.get_or_create_monitored_channel(
                self.platform_value, external_id, user["display_name"]
            )
            if sub_ids:
                await db_stream_alerts.set_eventsub_ids(
                    monitored["id"], sub_ids.get("online_id"), sub_ids.get("offline_id")
                )
            if self.handler.on_channel_created:
                try:
                    await self.handler.on_channel_created(monitored["id"], user)
                except Exception:
                    log.exception("on_channel_created-Hook für %s (%s) fehlgeschlagen.", login, self.platform_value)
        else:
            existing_sub = await db_stream_alerts.get_subscription(interaction.guild_id, monitored["id"])
            if existing_sub is not None:
                await interaction.followup.send(
                    lang.get("alert_already_added", "❌ `{login}` is already being monitored on this server.").format(login=login),
                    ephemeral=True,
                )
                return

        message = self.message_input.value.strip() or None
        await db_stream_alerts.add_subscription(
            guild_id=interaction.guild_id,
            channel_id=self.channel.id,
            monitored_channel_id=monitored["id"],
            message=message,
        )

        await interaction.followup.send(
            lang.get("alert_add_success", "{nexus_checkmark} `{login}` is now being monitored — alerts will be posted in {channel}.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK, login=login, channel=self.channel.mention,
            ),
            ephemeral=True,
        )
        log.info("%s-Alert für %s auf Guild %s eingerichtet.", self.platform_value, login, interaction.guild_id)

        existing_role = await db_stream_alerts.get_ping_role(interaction.guild_id, self.platform_value)
        if existing_role is None:
            role_view = RoleQuickSetView(lang, interaction.guild_id, self.platform_value)
            role_msg = await interaction.followup.send(
                lang.get("alert_role_prompt", "🔔 Would you like to set a role to be pinged during live events?"),
                view=role_view,
                ephemeral=True,
                wait=True,
            )
            role_view.bind_followup(interaction, role_msg)

class StreamAlerts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="alert_add", description=app_commands.locale_str("cmd_alert_add_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("stream_alerts")
    @app_commands.describe(
        platform=app_commands.locale_str("cmd_alert_platform_desc"),
        channel=app_commands.locale_str("cmd_alert_channel_desc"),
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="Twitch", value="twitch"),
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Kick", value="kick"),
    ])
    async def alert_add(
        self,
        interaction: discord.Interaction,
        platform: app_commands.Choice[str],
        channel: discord.TextChannel,
    ):
        lang = await get_lang(interaction.guild_id)
        handler = PLATFORM_HANDLERS.get(platform.value)
        if handler is None:
            await interaction.response.send_message(
                lang.get("alert_platform_unavailable", "❌ This platform isn't available yet."),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            AddChannelModal(lang, platform.value, platform.name, handler, channel)
        )

    @app_commands.command(name="alert_remove", description=app_commands.locale_str("cmd_alert_remove_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("stream_alerts")
    @app_commands.describe(platform=app_commands.locale_str("cmd_alert_platform_desc"))
    @app_commands.choices(platform=[
        app_commands.Choice(name="Twitch", value="twitch"),
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Kick", value="kick"),
    ])
    async def alert_remove(
        self,
        interaction: discord.Interaction,
        platform: app_commands.Choice[str],
        streamer: str,
    ):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)

        handler = PLATFORM_HANDLERS.get(platform.value)
        if handler is None:
            await interaction.followup.send(
                lang.get("alert_platform_unavailable", "❌ This platform isn't available yet."),
                ephemeral=True,
            )
            return

        login = handler.normalize_input(streamer)
        if login is None:
            await interaction.followup.send(
                lang.get("alert_invalid_username", "❌ That doesn't look like a valid Twitch username."),
                ephemeral=True,
            )
            return

        user = await handler.resolve_user(login)
        if user is None:
            await interaction.followup.send(
                lang.get("alert_user_not_found", "❌ User `{login}` was not found.").format(login=login),
                ephemeral=True,
            )
            return

        external_id = user["id"]
        monitored = await db_stream_alerts.get_monitored_channel(platform.value, external_id)
        if monitored is None:
            await interaction.followup.send(
                lang.get("alert_not_monitored", "❌ `{login}` is not being monitored on this server.").format(login=login),
                ephemeral=True,
            )
            return

        removed = await db_stream_alerts.remove_subscription(interaction.guild_id, monitored["id"])
        if not removed:
            await interaction.followup.send(
                lang.get("alert_not_monitored", "❌ `{login}` is not being monitored on this server.").format(login=login),
                ephemeral=True,
            )
            return

        still_referenced = await db_stream_alerts.is_channel_still_referenced(monitored["id"])
        if not still_referenced:
            if handler.delete_subscriptions:
                try:
                    await handler.delete_subscriptions(monitored)
                except Exception:
                    log.exception("Subscription-Löschung für %s fehlgeschlagen — DB-Eintrag wird trotzdem entfernt.", login)

            await db_stream_alerts.delete_monitored_channel(monitored["id"])
            log.info("Letzter Server hat %s entfernt — Abos und Kanal-Eintrag bereinigt.", login)

        await interaction.followup.send(
            lang.get("alert_remove_success", "{nexus_checkmark} `{login}` is no longer being monitored.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK, login=login,
            ),
            ephemeral=True,
        )

    @alert_remove.autocomplete("streamer")
    async def alert_remove_autocomplete(self, interaction: discord.Interaction, current: str):
        selected_platform = interaction.namespace.platform or "twitch"
        subs = await db_stream_alerts.get_subscriptions_for_guild(interaction.guild_id)
        return [
            app_commands.Choice(name=f"{s['streamer_name']} ({s['platform']})", value=s['streamer_name'])
            for s in subs
            if s["platform"] == selected_platform and current.lower() in s["streamer_name"].lower()
        ][:25]

    @app_commands.command(name="alert_list", description=app_commands.locale_str("cmd_alert_list_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("stream_alerts")
    async def alert_list(self, interaction: discord.Interaction):
        lang = await get_lang(interaction.guild_id)
        subs = await db_stream_alerts.get_subscriptions_for_guild(interaction.guild_id)

        if not subs:
            await interaction.response.send_message(
                lang.get("alert_list_empty", "No monitored channels on this server."),
                ephemeral=True,
            )
            return

        grouped: dict[str, list[dict]] = {}
        for s in subs:
            grouped.setdefault(s["platform"], []).append(s)

        color = await db_settings.get_color(interaction.guild_id)
        embed = discord.Embed(
            title=lang.get("alert_list_title", "{broadcast} Überwachte Kanäle").format(broadcast=NexusEmojis.Modules.BROADCAST),
            color=color,
        )

        for platform in ("twitch", "youtube", "kick"):
            entries = grouped.get(platform)
            if not entries:
                continue

            premium = await is_premium(interaction.guild_id)
            limit = await db_limits.get_limit(interaction.guild_id, f"stream_alerts_{platform}", premium)
            sorted_entries = sorted(entries, key=lambda s: s["created_at"])

            display = PLATFORM_DISPLAY.get(platform, {"emoji": "❔", "label": platform.capitalize()})
            
            # Einträge mit Blockquotes (>) als saubere Kacheln formatieren
            entry_blocks = []
            for i, s in enumerate(sorted_entries):
                if i >= limit:
                    status_text = lang.get("alert_inactive_premium", "⏸️ Inaktiv (Premium-Limit)")
                else:
                    status_text = lang.get("alert_live", "🔴 Live") if s["live"] else lang.get("alert_offline", "⚫ Offline")
                
                # Mit "> " formatieren für den Card-Look
                entry_blocks.append(f"> **{s['streamer_name']}** — {status_text} — <#{s['channel_id']}>")

            embed.add_field(
                name=f"{display['emoji']} {display['label']}",
                value="\n".join(entry_blocks),
                inline=False,
            )

        embed.set_footer(text=NEXUS_FOOTER)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="alert_role", description=app_commands.locale_str("cmd_alert_role_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("stream_alerts")
    @app_commands.describe(role=app_commands.locale_str("cmd_alert_role_role_desc"))
    @app_commands.describe(platform=app_commands.locale_str("cmd_alert_role_platform_desc"))
    @app_commands.choices(platform=[
        app_commands.Choice(name="Twitch", value="twitch"),
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Kick", value="kick"),
    ])
    async def alert_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role = None,
        platform: app_commands.Choice[str] = None,
    ):
        lang = await get_lang(interaction.guild_id)
        premium = await is_premium(interaction.guild_id)

        fallback_notice = ""
        if platform is not None and not premium:
            platform = None  # Fällt zurück auf 'all'
            fallback_notice = lang.get(
                "alert_role_platform_premium",
                "\n*Note: Platform-specific roles require Premium. Setting role for all platforms instead.*"
            )

        target_platform = platform.value if platform else "all"
        label = platform.name if platform else lang.get("alert_role_all_platforms", "all platforms")
        if role is None:
            await db_stream_alerts.remove_ping_role(interaction.guild_id, target_platform)
            await interaction.response.send_message(
                lang.get("alert_role_removed", "{nexus_checkmark} Ping role for {platform} has been removed.").format(
                    nexus_checkmark=NexusEmojis.CHECKMARK, platform=label
                ) + fallback_notice,
                ephemeral=True,
            )
            return
        bot_member = interaction.guild.me
        can_ping = role.mentionable or bot_member.guild_permissions.mention_everyone

        await db_stream_alerts.set_ping_role(interaction.guild_id, target_platform, role.id)

        if not can_ping:
            await interaction.response.send_message(
                lang.get(
                    "alert_role_not_pingable",
                    "⚠️ {role} was saved, but is currently not marked as mentionable by everyone.\n"
                    "For the ping to actually notify (not just display), enable "
                    "**'Allow anyone to @mention this role'** in Server Settings → Roles → {role}, "
                    "or grant me the 'Mention @everyone, @here, and All Roles' permission.",
                ).format(role=role.mention) + fallback_notice,
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            lang.get("alert_role_set", "{nexus_checkmark} {role} will now be pinged for {platform}.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK, 
                role=role.mention, 
                platform=label
            ) + fallback_notice,
            ephemeral=True,
        )

    def _build_edit_embed(self, sub: dict, lang: dict, color: int) -> discord.Embed:
        display = PLATFORM_DISPLAY.get(sub["platform"], {"emoji": "❔", "label": sub["platform"].capitalize()})
        embed = discord.Embed(
            title=f"{display['emoji']} {sub['streamer_name']} ({display['label']})",
            color=color,
        )
        embed.add_field(name=lang.get("alert_edit_channel_field", "📺 Channel"), value=f"<#{sub['channel_id']}>", inline=True)
        embed.add_field(
            name=lang.get("alert_edit_message_field", "💬 Message"),
            value=sub.get("message") or lang.get("alert_edit_no_message", "*None set*"),
            inline=False,
        )
        embed.set_footer(text=lang.get("alert_edit_role_hint", "Change ping role: /alert_role"))
        return embed

    @app_commands.command(name="alert_edit", description=app_commands.locale_str("cmd_alert_edit_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("stream_alerts")
    @app_commands.describe(platform=app_commands.locale_str("cmd_alert_platform_desc"))
    @app_commands.choices(platform=[
        app_commands.Choice(name="Twitch", value="twitch"),
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Kick", value="kick"),
    ])
    async def alert_edit(self, interaction: discord.Interaction, platform: app_commands.Choice[str]):
        lang = await get_lang(interaction.guild_id)
        all_subs = await db_stream_alerts.get_subscriptions_for_guild(interaction.guild_id)
        subs = [s for s in all_subs if s["platform"] == platform.value]

        if not subs:
            await interaction.response.send_message(
                lang.get("alert_edit_none_for_platform", "❌ Nothing is currently being monitored on this platform."),
                ephemeral=True,
            )
            return

        color = await db_settings.get_color(interaction.guild_id)

        if len(subs) == 1:
            embed = self._build_edit_embed(subs[0], lang, color)
            view = AlertEditView(subs[0], lang, self)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            view.bind(interaction)
            return

        view = StreamerSelectView(subs, lang, self)
        await interaction.response.send_message(
            lang.get("alert_edit_pick_streamer", "Which channel would you like to edit?"),
            view=view,
            ephemeral=True,
        )
        view.bind(interaction)

async def setup(bot):
    await bot.add_cog(StreamAlerts(bot))