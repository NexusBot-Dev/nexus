import enum
import logging
import discord
from discord import app_commands
from discord.ext import commands
from database import db_settings, db_warnings
from cogs.utils import post_logging, dm_user, get_lang
from emojis import NexusEmojis
from systems.hierarchy import can_act_on_member, can_manage_role, format_issue, Issue

log = logging.getLogger(__name__)

class SlowmodeDuration(enum.Enum):
    deaktiviert = (0,     "Deaktiviert")
    s5          = (5,     "5 Sekunden")
    s30         = (30,    "30 Sekunden")
    min1        = (60,    "1 Minute")
    min5        = (300,   "5 Minuten")
    min30       = (1800,  "30 Minuten")
    h1          = (3600,  "1 Stunde")
    h3          = (10800, "3 Stunden")
    h6          = (21600, "6 Stunden")

    def __new__(cls, seconds, label):
        obj = object.__new__(cls)
        obj._value_ = seconds
        obj.label = label
        return obj

class ModAction(enum.Enum):
    KICK  = ("dm_kicked",  discord.Color.red())
    BAN   = ("dm_banned",  discord.Color.red())
    UNBAN = ("dm_unbanned", discord.Color.green())
    WARN  = ("dm_warned",  discord.Color.yellow())

    def __init__(self, key, color):
        self.key   = key
        self.color = color

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ─── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _hierarchy_error(
        interaction: discord.Interaction,
        member: discord.Member,
        lang: dict,
        key: str,
        fallback: str,
        *,
        perms: tuple[str, ...] = (),
        is_timeout: bool = False,
    ) -> str | None:
        """None wenn ok, sonst die passende Fehlermeldung."""
        check = can_act_on_member(
            interaction.guild, member,
            actor=interaction.user, perms=perms, is_timeout=is_timeout,
        )
        if check:
            return None
        # User steht zu tief → bestehende, command-spezifische Meldung
        if check.issue is Issue.ACTOR_HIERARCHY:
            return lang.get(key, fallback)
        return format_issue(check, lang)

    # ─── Kick / Ban / Unban ──────────────────────────────────────────────────────

    @app_commands.command(name="kick", description=app_commands.locale_str("cmd_kick_desc"))
    @app_commands.default_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = None,
    ):
        lang = await get_lang(interaction.guild_id)
        if err := self._hierarchy_error(
            interaction, member, lang,
            "kick_hierarchy", "You cannot kick users who are ranked equal or higher than you.",
            perms=("kick_members",),
        ):
            await interaction.response.send_message(err, ephemeral=True)
            return

        if reason is None:
            reason = lang.get("no_reason", "No reason given")
        await interaction.response.defer(ephemeral=True)  # DM + Kick + Log können >3s dauern

        # DM VOR dem Kick — danach teilt Nexus meist keinen Server mehr mit dem User
        await dm_user(
            member,
            title=lang.get(ModAction.KICK.key, "👢 You were kicked"),
            reason=reason,
            color=ModAction.KICK.color,
            lang=lang,
            footer=interaction.guild.name,
        )
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            await interaction.followup.send(
                lang.get("kick_no_permission", "❌ I don't have permission to kick this user."),
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            log.exception("Fehler beim Kick von %s: %s", member, e)
            await interaction.followup.send(
                lang.get("kick_error", "❌ Something went wrong while kicking this user."),
                ephemeral=True,
            )
            return
        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=lang.get("log_kicked", "{target} was kicked by {mod}.").format(
                target=member,
                mod=interaction.user
            ),
            moderator=interaction.user,
            target=member,
            lang = lang,
            reason=reason,
        )
        await interaction.followup.send(
            lang.get("kick_success", "{user} was kicked.").format(user=member.mention),
            ephemeral=True,
        )

    @app_commands.command(name="ban", description=app_commands.locale_str("cmd_ban_desc"))
    @app_commands.default_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = None,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ):
        lang = await get_lang(interaction.guild_id)
        if err := self._hierarchy_error(
            interaction, member, lang,
            "ban_hierarchy", "You cannot ban users who are ranked equal or higher than you.",
            perms=("ban_members",),
        ):
            await interaction.response.send_message(err, ephemeral=True)
            return
        if reason is None:
            reason = lang.get("no_reason", "No reason given")

        await interaction.response.defer(ephemeral=True)
        # DM VOR dem Ban — danach teilt Nexus meist keinen Server mehr mit dem User

        try:
            await member.ban(reason=reason, delete_message_days=delete_message_days)
            await dm_user(
                        member,
                        title=lang.get(ModAction.BAN.key, "🔨 You were banned"),
                        reason=reason,
                        color=ModAction.BAN.color,
                        lang=lang,
                        footer=interaction.guild.name,
                    )
        except discord.Forbidden:
            await interaction.followup.send(
                lang.get("ban_no_permission", "❌ I don't have permission to ban this user."),
                ephemeral=True,
            )
            return
        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=lang.get("log_banned", "{target} was banned by {mod}.").format(
                target=member,
                mod=interaction.user
            ),
            moderator=interaction.user,
            target=member,
            lang = lang,
            reason=reason,
        )
        await interaction.followup.send(lang.get("ban_success", "{user} was banned.").format(user=member.mention), ephemeral=True)

    @app_commands.command(name="unban", description=app_commands.locale_str("cmd_unban_desc"))
    @app_commands.default_permissions(ban_members=True)
    async def unban(
        self,
        interaction: discord.Interaction,
        user: str,
        reason: str = None,
    ):
        lang = await get_lang(interaction.guild_id)

        if not user.isdigit():
            await interaction.response.send_message(
                lang.get("unban_invalid_user", "❌ Please select a user from the suggestions."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)  # fetch_user + unban + DM + Log können >3s dauern

        try:
            user_obj = await self.bot.fetch_user(int(user))
            if reason is None:
                reason = lang.get("no_reason", "No reason given")
            await interaction.guild.unban(user_obj, reason=reason)
            from emojis import NexusEmojis
            await dm_user(
                user_obj,
                title=lang.get(ModAction.UNBAN.key, "{nexus_checkmark} You were unbanned").format(nexus_checkmark=NexusEmojis.CHECKMARK),
                reason=reason,
                color=ModAction.UNBAN.color,
                lang = lang,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                lang.get("unban_no_permission", "❌ I don't have permission to unban this user."),
                ephemeral=True,
            )
            return
        except discord.NotFound:
            await interaction.followup.send(
                lang.get("unban_not_found", "❌ This user is not banned anymore."),
                ephemeral=True,
            )
            return
        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=lang.get("log_unbanned", "{target} was unbanned by {mod}.").format(
                target=user_obj,
                mod=interaction.user
            ),
            moderator=interaction.user,
            target=user_obj,
            lang = lang,
            reason=reason,
            color=discord.Color.green(),
        )
        await interaction.followup.send(
            lang.get("unban_success", "{user} was unbanned.").format(user=user_obj.mention), ephemeral=True)

    @unban.autocomplete("user")
    async def unban_autocomplete(self, interaction: discord.Interaction, current: str):
        bans = [ban async for ban in interaction.guild.bans()]
        return [
            app_commands.Choice(name=ban.user.name, value=str(ban.user.id))
            for ban in bans
            if current.lower() in ban.user.name.lower()
        ][:25]

    # ─── Rollen ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="add_role", description=app_commands.locale_str("cmd_add_role_desc"))
    @app_commands.default_permissions(manage_roles=True)
    async def add_role(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: str,
    ):
        lang = await get_lang(interaction.guild_id)
        if err := self._hierarchy_error(
            interaction, member, lang,
            "add_role_hierarchy", "You cannot assign roles to users who are ranked equal or higher than you.",
            perms=("manage_roles",),
        ):
            await interaction.response.send_message(err, ephemeral=True)
            return
        role_obj = interaction.guild.get_role(int(role)) if role.isdigit() else None
        if role_obj is None:
            await interaction.response.send_message(lang.get("role_not_found", "Role not found."), ephemeral=True)
            return
        # Die ROLLE selbst muss unter Nexus UND unter dem ausführenden User stehen
        # (sonst könnte ein Mod sich oder anderen höhere Rollen geben)
        if not (role_check := can_manage_role(interaction.guild, role_obj, actor=interaction.user)):
            await interaction.response.send_message(format_issue(role_check, lang), ephemeral=True)
            return
        try:
            await member.add_roles(role_obj)
            await post_logging(
                self.bot,
                guild_id=interaction.guild_id,
                title=lang.get("log_add_role", "{target} received the role {role}.").format(
                    target=member,
                    role=role_obj,
                ),
                moderator=interaction.user,
                lang = lang,
                target=member,
                color=discord.Color.green(),
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                lang.get("add_role_no_permission", "❌ I don't have permission to grant a role."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(lang.get("add_role_success", "{user} received {role}.").format(
            user=member.mention,
            role=role_obj.mention
        ), ephemeral=True)

    @add_role.autocomplete("role")
    async def add_role_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=role.name, value=str(role.id))
            for role in interaction.guild.roles
            if current.lower() in role.name.lower() and role.name != "@everyone"
        ][:25]

    @app_commands.command(name="remove_role", description=app_commands.locale_str("cmd_remove_role_desc"))
    @app_commands.default_permissions(manage_roles=True)
    async def remove_role(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: str,
    ):
        lang = await get_lang(interaction.guild_id)
        if err := self._hierarchy_error(
            interaction, member, lang,
            "remove_role_hierarchy", "You cannot remove roles from users who are ranked equal or higher than you.",
            perms=("manage_roles",),
        ):
            await interaction.response.send_message(err, ephemeral=True)
            return
        role_obj = interaction.guild.get_role(int(role)) if role.isdigit() else None
        if role_obj is None:
            await interaction.response.send_message(lang.get("role_not_found", "Role not found."), ephemeral=True)
            return

        if not (role_check := can_manage_role(interaction.guild, role_obj, actor=interaction.user)):
            await interaction.response.send_message(format_issue(role_check, lang), ephemeral=True)
            return
        try:
            await member.remove_roles(role_obj)
            await post_logging(
                self.bot,
                guild_id=interaction.guild_id,
                title=lang.get("log_remove_role", "{target} lost the role {role}.").format(target=member, role=role_obj),
                moderator=interaction.user,
                target=member,
                lang=lang
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                lang.get("add_role_no_permission", "❌ I don't have permission to grant a role."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            lang.get("remove_role_success", "{user} lost {role}.").format(user=member.mention, role=role_obj.mention),
            ephemeral=True,
        )

    @remove_role.autocomplete("role")
    async def remove_role_autocomplete(self, interaction: discord.Interaction, current: str):
        member_ref = interaction.namespace.member
        if not member_ref:
            return []
        member = interaction.guild.get_member(member_ref.id)
        if not member:
            return []
        return [
            app_commands.Choice(name=role.name, value=str(role.id))
            for role in member.roles
            if current.lower() in role.name.lower() and role.name != "@everyone"
        ][:25]

    # ─── Slowmode ────────────────────────────────────────────────────────────────

    @app_commands.command(name="set_slowmode", description=app_commands.locale_str("cmd_set_slowmode_desc"))
    @app_commands.default_permissions(manage_channels=True)
    async def set_slowmode(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        duration: int,
    ):
        lang = await get_lang(interaction.guild_id)
        try:
            await channel.edit(slowmode_delay=duration)
        except discord.Forbidden:
            await interaction.response.send_message(
                lang.get("slowmode_no_permission", "❌ I don't have permission to edit this channel."),
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            log.exception("Fehler beim Setzen des Slowmodes in %s: %s", channel, e)
            await interaction.response.send_message(
                lang.get("error_occurred", "An error occurred: {error}").format(error=e),
                ephemeral=True,
            )
            return

        label = next((d.label for d in SlowmodeDuration if d.value == duration), f"{duration}s")
        if duration == 0:
            msg       = lang.get("slowmode_disabled", "Slowmode in {channel} has been disabled.").format(channel=channel.mention)
            log_title = lang.get("log_slowmode_disabled", "Slowmode in #{channel} disabled by {mod}.").format(channel=channel.name, mod=interaction.user)
            color     = discord.Color.green()
        else:
            msg       = lang.get("slowmode_set", "Slowmode in {channel} has been set to {duration}.").format(channel=channel.mention, duration=label)
            log_title = lang.get("log_slowmode_set", "Slowmode in #{channel} set to {duration} by {mod}.").format(channel=channel.name, duration=label, mod=interaction.user)
            color     = discord.Color.orange()

        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=log_title,
            moderator=interaction.user,
            target=interaction.user,
            color=color,
            lang=lang
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @set_slowmode.autocomplete("duration")
    async def slowmode_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=d.label, value=d.value)
            for d in SlowmodeDuration
            if current.lower() in d.label.lower()
        ]

    @app_commands.command(name="timeout", description=app_commands.locale_str("cmd_timeout_desc"))
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration: int,
        reason: str = None,
    ):
        lang = await get_lang(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)

        if err := self._hierarchy_error(
            interaction, member, lang,
            "timeout_hierarchy", "You cannot timeout users ranked equal or higher than you.",
            perms=("moderate_members",),
            is_timeout=True,
        ):
            await interaction.followup.send(err, ephemeral=True)
            return
        if reason is None:
            reason = lang.get("no_reason", "No reason given")
        if duration < 0:
            await interaction.followup.send(
                lang.get("timeout_negative", "❌ The duration cannot be negative."),
                ephemeral=True,
            )
            return
        elif duration == 0:
            try:
                await member.timeout(None, reason=reason)
                log_title = lang.get("log_timeout_removed", "{mod} has removed the timeout for {target}.").format(
                    mod=interaction.user, target=member
                )
                color = discord.Color.green()
                msg   = lang.get("timeout_removed", "{nexus_checkmark} Timeout for {user} has been removed.").format(
                    nexus_checkmark=NexusEmojis.CHECKMARK,
                    user=member.mention
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    lang.get("timeout_no_permission", "❌ I don't have permission to timeout this user."),
                    ephemeral=True,
                )
                return
            except discord.HTTPException as e:
                log.exception("Fehler beim Timeout für %s", member)
                await interaction.followup.send(
                    lang.get("error_occurred", "An error occurred: {error}").format(error=e),
                    ephemeral=True,
                )
                return
        else:
            from datetime import timedelta
            until = discord.utils.utcnow() + timedelta(minutes=duration)
            try:
                await member.timeout(until, reason=reason)
                log_title = lang.get("log_timeout_set", "{mod} timed out {target} for {duration} minutes.").format(
                    mod=interaction.user,
                    target=member,
                    duration=duration
                )
                color = discord.Color.orange()
                msg   = lang.get("timeout_success", "{nexus_checkmark} {user} has been timed out for {duration} minutes.").format(
                    nexus_checkmark=NexusEmojis.CHECKMARK,
                    user=member.mention,
                    duration=duration
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    lang.get("timeout_no_permission", "❌ I don't have permission to timeout this user."),
                    ephemeral=True,
                )
                return
            except discord.HTTPException as e:
                log.exception("Fehler beim Timeout für %s", member)
                await interaction.followup.send(
                    lang.get("error_occurred", "An error occurred: {error}").format(error=e),
                    ephemeral=True,
                )
                return

        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=log_title,
            moderator=interaction.user,
            target=member,
            lang=lang,
            reason=reason,
            color=color,
        )
        await interaction.followup.send(msg, ephemeral=True)

    @timeout.autocomplete("duration")
    async def duration_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        options = [
            {"name": "5 Minuten",  "value": 5},
            {"name": "10 Minuten", "value": 10},
            {"name": "1 Stunde",   "value": 60},
            {"name": "6 Stunden",  "value": 360},
            {"name": "12 Stunden", "value": 720},
            {"name": "1 Tag",      "value": 1440},
            {"name": "1 Woche",    "value": 10080},
            {"name": "Aufheben",   "value": 0},
        ]
        return [
            app_commands.Choice(name=opt["name"], value=opt["value"])
            for opt in options
            if current.lower() in opt["name"].lower()
        ][:25]

    # ─── Messages ────────────────────────────────────────────────────────────────

    @app_commands.command(name="purge_messages", description=app_commands.locale_str("cmd_purge_desc"))
    @app_commands.default_permissions(manage_messages=True)
    async def purge_messages(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        amount: app_commands.Range[int, 1, 500],
    ):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        color = await db_settings.get_color(interaction.guild_id)

        from datetime import datetime, timedelta, timezone
        two_weeks_ago = datetime.now(timezone.utc) - timedelta(days=14)

        def only_new_messages(message: discord.Message):
            return message.created_at > two_weeks_ago

        try:
            deleted = await channel.purge(limit=amount, bulk=True, check=only_new_messages)
            if len(deleted) < amount:
                await interaction.followup.send(lang.get("purge_part_successful", "Successfully deleted {deleted} of {amount} Messages in {channel}").format(
                    deleted=len(deleted),
                    amount=amount,
                    channel=channel.mention,
                    ), ephemeral=True)
            else:
                await interaction.followup.send(lang.get("purge_successful", "Successfully deleted {amount} Messages in {channel}").format(
                    amount=amount,
                    channel=channel.mention,
                ), ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                lang.get("purge_no_permission", "❌ I don't have permission to delete messages in {channel}.").format(
                    channel=channel.mention,
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            log.exception("Fehler beim Purge in %s: %s", channel, e)
            await interaction.followup.send(
                lang.get("error_occurred", "An error occurred: {error}").format(error=e),
                ephemeral=True,
            )
            return

        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=lang.get("log_purge", "{amount} Messages in #{channel} deleted.").format(
                amount=len(deleted),
                channel=channel
            ),
            moderator=interaction.user,
            target=interaction.user,
            lang=lang,
            color=color,
        )

    # ─── Warns ───────────────────────────────────────────────────────────────────

    @app_commands.command(name="warn", description=app_commands.locale_str("cmd_warn_desc"))
    @app_commands.default_permissions(moderate_members=True)
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = None,
    ):
        lang = await get_lang(interaction.guild_id)
        if err := self._hierarchy_error(
            interaction, member, lang,
            "warn_hierarchy", "You cannot warn users who are ranked equal or higher than you.",
        ):
            await interaction.response.send_message(err, ephemeral=True)
            return
        if reason is None:
            reason = lang.get("no_reason", "No reason given")
        count = await db_warnings.add_warning(
            guild_id=interaction.guild_id,
            user_id=member.id,
            moderator_id=interaction.user.id,
            reason=reason,
        )
        await dm_user(
            member,
            title=lang.get(ModAction.WARN.key, "⚠️ You were warned"),
            reason=reason,
            color=ModAction.WARN.color,
            lang=lang,
            footer=interaction.guild.name,
            extra=lang.get("dm_warn_count", "Total warnings: {count}").format(count=count),
        )
        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=lang.get("log_warned", "{target} was warned by {mod}. ({count} warnings)").format(
                target=member,
                mod=interaction.user,
                count=count
            ),
            moderator=interaction.user,
            target=member,
            reason=reason,
            lang=lang,
            color=discord.Color.yellow(),
        )
        await interaction.response.send_message(
            lang.get("warn_success", "{user} was warned. They now have **{count}** warning(s).").format(
                user=member.mention,
                count=count
            ),
            ephemeral=True,
        )

    @app_commands.command(name="warnings", description=app_commands.locale_str("cmd_warnings_desc"))
    @app_commands.default_permissions(moderate_members=True)
    async def warnings(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        lang = await get_lang(interaction.guild_id)
        warns = await db_warnings.get_warnings(
            guild_id=interaction.guild_id,
            user_id=member.id,
        )
        if not warns:
            await interaction.response.send_message(
                lang.get("warnings_none", "{user} has no warnings.").format(user=member.mention),
                ephemeral=True,
            )
            return

        color = await db_settings.get_color(interaction.guild_id)
        embed = discord.Embed(
            title=lang.get("warn_title", "Warnings for {member}").format(member=member),
            color=color,
        )
        for w in warns:
            moderator     = interaction.guild.get_member(w["moderator_id"])
            moderator_str = moderator.mention if moderator else f"<@{w['moderator_id']}>"
            embed.add_field(
                name=f"#{w['id']} — {w['created_at'].strftime('%d.%m.%Y %H:%M')}",
                value=f"**{lang.get('log_reason', 'Reason')}:** {w['reason']}\n"
                      f"**{lang.get('log_moderator', 'Moderator')}:** {moderator_str}",
                inline=False,
            )
        embed.set_footer(text=lang.get("warn_count", "Overall {amount} warning(s)").format(amount=len(warns)))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="unwarn", description=app_commands.locale_str("cmd_unwarn_desc"))
    @app_commands.default_permissions(moderate_members=True)
    async def unwarn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        warning_id: int,
    ):
        lang = await get_lang(interaction.guild_id)
        removed = await db_warnings.remove_warning(
            warning_id=warning_id,
            guild_id=interaction.guild_id,
        )
        if not removed:
            await interaction.response.send_message(lang.get("unwarn_not_found", "Warning #{id} not found.").format(
                id=warning_id
                ), ephemeral=True)
            return
        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=lang.get("log_unwarn", "Warning #{id} from {target} was removed.").format(
                id=warning_id,
                target=member
            ),
            moderator=interaction.user,
            target=member,
            lang=lang,
            color=discord.Color.green(),
        )
        await interaction.response.send_message(lang.get("unwarn_success", "Warning #{id} from {user} has been removed.").format(
            id=warning_id,
            user=member.mention
            ), ephemeral=True,)

    @app_commands.command(name="clearwarnings", description=app_commands.locale_str("cmd_clearwarnings_desc"))
    @app_commands.default_permissions(administrator=True)
    async def clearwarnings(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        lang = await get_lang(interaction.guild_id)
        count = await db_warnings.clear_warnings(
            guild_id=interaction.guild_id,
            user_id=member.id,
        )
        if count == 0:
            await interaction.response.send_message(lang.get("clearwarnings_none", "{user} has no warnings.").format(
                user=member.mention
            ), ephemeral=True)
            return
        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=lang.get("log_clearwarnings", "All warnings from {target} were cleared. ({count} entries)").format(
                target=member,
                count=count
            ),
            moderator=interaction.user,
            target=member,
            lang=lang,
            color=discord.Color.green(),
        )
        await interaction.response.send_message(lang.get("clearwarnings_success", "All **{count}** warning(s) from {user} have been removed.").format(
            user=member.mention,
            count=count), ephemeral=True)

async def setup(bot):
    await bot.add_cog(Moderation(bot))