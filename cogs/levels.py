import logging
import discord
import random
from typing import Optional, Union
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from database import db_settings, db_levels
from systems.exp_system import XP_COOLDOWN
from cogs.utils import post_logging, get_lang, module_required
from config import NEXUS_FOOTER, NEXUS_COLOR
from emojis import NexusEmojis

log = logging.getLogger(__name__)

# Cooldown-Cache: Key=(guild_id, user_id), Value=Timestamp
_xp_cooldown: dict[tuple[int, int], datetime] = {}


class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─── Helpers ────────────────────────────────────────────────────────────────

    async def _handle_level_up(
        self,
        guild: discord.Guild,
        member: discord.Member,
        channel: Optional[Union[discord.TextChannel, discord.Thread, discord.VoiceChannel]],
        result: dict,
        lang: dict,
    ):
        new_level = result["new_level"]
        old_level = result["old_level"]

        # 1. Rollen vergeben bei Aufstieg
        if new_level > old_level:
            for level in range(old_level + 1, new_level + 1):
                role_id = await db_levels.get_level_role(guild.id, level)
                if role_id:
                    role = guild.get_role(role_id)
                    if role and role not in member.roles:
                        if role >= guild.me.top_role or role.managed:
                            log.warning(
                                "Kann Rolle %s auf %s nicht vergeben (Hierarchie/Managed).",
                                role.name, guild.name
                            )
                            continue
                        try:
                            await member.add_roles(role)
                            log.info("%s hat Level %s erreicht und Rolle %s erhalten.", member, level, role)
                        except discord.Forbidden:
                            log.warning("Fehlende Berechtigung (Forbidden) für Rolle %s auf %s.", role.name, guild.name)
                        except discord.HTTPException as e:
                            log.error("HTTP-Fehler beim Zuweisen von Rolle %s: %s", role.name, e)

        # 2. Rollen entfernen bei Abstieg (Mod Actions)
        elif new_level < old_level:
            for level in range(new_level + 1, old_level + 1):
                role_id = await db_levels.get_level_role(guild.id, level)
                if role_id:
                    role = guild.get_role(role_id)
                    if role and role in member.roles:
                        if role >= guild.me.top_role or role.managed:
                            log.warning(
                                "Kann Rolle %s auf %s nicht entfernen (Hierarchie/Managed).",
                                role.name, guild.name
                            )
                            continue
                        try:
                            await member.remove_roles(role)
                            log.info("%s ist auf Level %s gefallen und hat Rolle %s verloren.", member, new_level, role)
                        except discord.Forbidden:
                            log.warning("Fehlende Berechtigung (Forbidden) beim Entfernen von Rolle %s auf %s.", role.name, guild.name)
                        except discord.HTTPException as e:
                            log.error("HTTP-Fehler beim Entfernen von Rolle %s: %s", role.name, e)

        # 3. Embed-Nachricht senden (nur bei echtem Level-Up nach oben)
        if new_level > old_level and channel and hasattr(channel, "send"):
            try:
                color = await db_settings.get_color(guild.id) or NEXUS_COLOR
                embed = discord.Embed(
                    title="🎉 Level Up!",
                    description=lang.get("level_up", "{user} reached **Level {level}**!").format(
                        user=member.mention,
                        level=new_level
                    ),
                    color=color,
                )
                embed.set_thumbnail(url=member.display_avatar.url)
                embed.set_footer(text=NEXUS_FOOTER)
                await channel.send(embed=embed, delete_after=30)
            except discord.Forbidden:
                log.warning("Keine Berechtigung, Level-Up-Embed in %s auf %s zu senden.", channel, guild.name)
            except discord.HTTPException as e:
                log.error("HTTP-Fehler beim Senden des Level-Up-Embeds: %s", e)

    # ─── Events ──────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            if message.author.bot or not message.guild:
                return
            if not await db_settings.is_module_enabled(message.guild.id, "levels"):
                return

            key = (message.guild.id, message.author.id)
            now = datetime.now(timezone.utc).replace(tzinfo=None)

            # Cooldown prüfen
            last = _xp_cooldown.get(key)
            if last and (now - last).total_seconds() < XP_COOLDOWN:
                return

            _xp_cooldown[key] = now

            if len(_xp_cooldown) > 1000:
                expired = [
                    k for k, t in _xp_cooldown.items()
                    if (now - t).total_seconds() >= XP_COOLDOWN
                ]
                for k in expired:
                    _xp_cooldown.pop(k, None)

            xp_min, xp_max = await db_settings.get_xp_range(message.guild.id)
            amount = random.randint(min(xp_min, xp_max), max(xp_min, xp_max))

            result = await db_levels.add_xp(
                guild_id=message.guild.id,
                user_id=message.author.id,
                amount=amount,
            )
            if result is None:
                return

            if result["new_level"] > result["old_level"]:
                lang = await get_lang(message.guild.id)
                await self._handle_level_up(
                    message.guild,
                    message.author,
                    message.channel,
                    result,
                    lang,
                )
        except Exception:
            log.exception("Fehler in on_message für %s auf %s", message.author, message.guild.name)

    # ─── Commands ────────────────────────────────────────────────────────────────

    @app_commands.command(name="leaderboard", description=app_commands.locale_str("cmd_leaderboard_desc"))
    @module_required("levels")
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)

        entries = await db_levels.get_leaderboard(interaction.guild_id, limit=10)
        if not entries:
            await interaction.followup.send(lang.get("leaderboard_empty", "No entries yet."), ephemeral=True)
            return

        color = await db_settings.get_color(interaction.guild_id) or NEXUS_COLOR
        embed = discord.Embed(
            title=lang.get("leaderboard_title", "🏆 Leaderboard"),
            color=color,
        )
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, entry in enumerate(entries, start=1):
            m = interaction.guild.get_member(entry["user_id"])
            name = m.display_name if m else f"<@{entry['user_id']}>"
            embed.add_field(
                name=f"{medals.get(i, f'`#{i}`')} {name}",
                value=f"Level {entry['level']} — {entry['xp']} XP",
                inline=False,
            )
        embed.set_footer(text=NEXUS_FOOTER)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─── Mod Commands ────────────────────────────────────────────────────────────

    @app_commands.command(name="set_level", description=app_commands.locale_str("cmd_set_level_desc"))
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        level=app_commands.locale_str("cmd_level_desc"),
        member=app_commands.locale_str("cmd_member_desc")
    )
    @module_required("levels")
    async def set_level(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        level: app_commands.Range[int, 1, 1000],
    ):
        await interaction.response.defer(ephemeral=True)
        old_data = await db_levels.get_user_xp(interaction.guild_id, member.id)
        data = await db_levels.set_level(interaction.guild_id, member.id, level)
        lang = await get_lang(interaction.guild_id)

        await self._handle_level_up(
            interaction.guild, member, interaction.channel,
            {"old_level": old_data["level"], "new_level": data["level"]}, lang,
        )
        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=lang.get("log_set_level", "{mod} set {target} to Level {level}.").format(
                mod=interaction.user,
                target=member,
                level=data["level"]
            ),
            moderator=interaction.user,
            target=member,
            lang=lang,
            color=discord.Color.blue(),
        )
        await interaction.followup.send(
            lang.get("set_level_success", "{user} is now Level **{level}** ({xp} XP).").format(
                user=member.mention,
                level=data["level"],
                xp=data["xp"]
            ),
            ephemeral=True,
        )

    @app_commands.command(name="add_xp", description=app_commands.locale_str("cmd_add_xp_desc"))
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        amount=app_commands.locale_str("cmd_amount_desc"),
        member=app_commands.locale_str("cmd_member_desc")
    )
    @module_required("levels")
    async def add_xp(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000],
    ):
        await interaction.response.defer(ephemeral=True)
        data = await db_levels.add_xp_mod(interaction.guild_id, member.id, amount)
        lang = await get_lang(interaction.guild_id)

        await self._handle_level_up(
            interaction.guild, member, interaction.channel,
            {"old_level": data["old_level"], "new_level": data["new_level"]}, lang
        )

        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=lang.get("log_add_xp", "{mod} added {amount} EXP to {target}.").format(
                mod=interaction.user,
                target=member,
                amount=amount,
            ),
            lang=lang,
            moderator=interaction.user,
            target=member,
            color=discord.Color.blue(),
        )

        await interaction.followup.send(
            lang.get("add_xp_success", "{user} received **{amount} XP**. Now: {xp} XP (Level {level}).").format(
                user=member.mention,
                amount=amount,
                xp=data["xp"],
                level=data["new_level"],
            ),
            ephemeral=True,
        )

    @app_commands.command(name="remove_xp", description=app_commands.locale_str("cmd_remove_xp_desc"))
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        amount=app_commands.locale_str("cmd_remove_amount_desc"),
        member=app_commands.locale_str("cmd_member_desc")
    )
    @module_required("levels")
    async def remove_xp(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000],
    ):
        await interaction.response.defer(ephemeral=True)
        data = await db_levels.remove_xp_mod(interaction.guild_id, member.id, amount)
        lang = await get_lang(interaction.guild_id)

        await self._handle_level_up(
            interaction.guild, member, interaction.channel,
            {"old_level": data["old_level"], "new_level": data["new_level"]}, lang,
        )
        
        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=lang.get("log_remove_xp", "{mod} removed {amount} XP from {target}.").format(
                mod=interaction.user,
                target=member,
                amount=amount 
            ),
            lang=lang,
            moderator=interaction.user,
            target=member,
            color=discord.Color.orange(),
        )
        await interaction.followup.send(
            lang.get("remove_xp_success", "{user} lost **{amount} EXP**.").format(
                user=member.mention,
                amount=amount,
                xp=data["xp"],
                level=data["new_level"]
            ),
            ephemeral=True,
        )

    @app_commands.command(name="wipe_xp", description=app_commands.locale_str("cmd_wipe_xp_desc"))
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        member=app_commands.locale_str("cmd_member_desc")
    )
    @module_required("levels")
    async def wipe_xp(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ):
        await interaction.response.defer(ephemeral=True)
        key = (interaction.guild_id, member.id)
        _xp_cooldown.pop(key, None)

        data = await db_levels.wipe_xp(interaction.guild_id, member.id)
        lang = await get_lang(interaction.guild_id)

        await self._handle_level_up(
            interaction.guild, member, None, 
            {"old_level": data["old_level"], "new_level": data["new_level"]}, lang
        )

        await post_logging(
            self.bot,
            guild_id=interaction.guild_id,
            title=lang.get("log_wipe_xp", "{mod} reset XP of {target}.").format(
                mod=interaction.user,
                target=member
            ),
            moderator=interaction.user,
            lang=lang,
            target=member,
            color=discord.Color.red(),
        )

        await interaction.followup.send(
            lang.get("wipe_xp_success", "Wiped data for {user}.").format(user=member.mention),
            ephemeral=True,
        )

    @app_commands.command(name="level_roles", description=app_commands.locale_str("cmd_level_roles_desc"))
    @app_commands.default_permissions(moderate_members=True)
    @module_required("levels")
    async def level_roles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        roles_data = await db_levels.get_all_level_roles(interaction.guild_id)
        color = await db_settings.get_color(interaction.guild_id) or NEXUS_COLOR

        embed = self._build_level_roles_embed(roles_data, lang, color)
        view = LevelRolesView(interaction.guild_id, lang)
        view.remove_btn.disabled = not roles_data
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    def _build_level_roles_embed(self, roles_data: list[dict], lang: dict, color: int) -> discord.Embed:
        embed = discord.Embed(
            title=lang.get("level_roles_title", "📋 Configured Level Roles"),
            color=color,
        )
        if not roles_data:
            embed.description = lang.get("no_level_roles_found", "No level roles have been set up for this server yet.")
        else:
            text = ""
            for entry in roles_data:
                text += f"**Level {entry['level']}:** <@&{entry['role_id']}>\n"
            embed.description = text
        return embed

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        if isinstance(error, app_commands.CheckFailure) and not isinstance(error, app_commands.MissingPermissions):
            return

        lang = await get_lang(interaction.guild_id)

        if isinstance(error, app_commands.MissingPermissions):
            message = lang.get("no_permission", "No permission.")
        else:
            message = lang.get("error_occurred", "An error occurred: {error}").format(error=error)

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

# ─── Views & Modals ──────────────────────────────────────────────────────────

class LevelRolesView(discord.ui.View):
    def __init__(self, guild_id: int, lang: dict):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.lang = lang
        self.add_btn.label = lang.get("level_roles_btn_add", "➕ Add")
        self.remove_btn.label = lang.get("level_roles_btn_remove", "➖ Remove")

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddLevelRoleModal(self.guild_id, self.lang))

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        roles_data = await db_levels.get_all_level_roles(self.guild_id)
        if not roles_data:
            await interaction.response.edit_message(
                content=self.lang.get("no_level_roles_found", "No level roles have been set up for this server yet."),
                embed=None,
                view=None,
            )
            return
        view = RemoveLevelRoleView(self.guild_id, self.lang, roles_data)
        await interaction.response.edit_message(
            content=self.lang.get("level_roles_pick_remove", "Which level role should be removed?"),
            embed=None,
            view=view,
        )


class AddLevelRoleModal(discord.ui.Modal):
    def __init__(self, guild_id: int, lang: dict):
        super().__init__(title=lang.get("level_roles_add_modal_title", "Add Level Role"))
        self.guild_id = guild_id
        self.lang = lang
        self.level_input = discord.ui.TextInput(
            label=lang.get("level_roles_level_label", "Level"),
            placeholder=lang.get("level_roles_level_placeholder", "e.g. 10"),
            required=True,
            max_length=4,
        )
        self.add_item(self.level_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            level = int(self.level_input.value)
        except ValueError:
            await interaction.response.send_message(
                self.lang.get("level_roles_invalid_level", "❌ Please enter a valid number."), ephemeral=True
            )
            return
        if not (1 <= level <= 1000):
            await interaction.response.send_message(
                self.lang.get("level_roles_level_range", "❌ Level must be between 1 and 1000."), ephemeral=True
            )
            return

        select = discord.ui.RoleSelect(
            placeholder=self.lang.get("level_roles_pick_role_placeholder", "Select a role..."),
        )

        async def on_role_chosen(select_interaction: discord.Interaction):
            role = select.values[0]

            # Validierung: Managed-Status, @everyone & Rollenhierarchie
            if role.is_default():
                await select_interaction.response.send_message(
                    self.lang.get("level_roles_everyone_role_error", "❌ The `@everyone` role cannot be used as a level role."),
                    ephemeral=True
                )
                return
            if role.managed:
                await select_interaction.response.send_message(
                    self.lang.get("level_roles_managed_role_error", "❌ The role {role} is managed by an integration or bot and cannot be assigned manually.").format(role=role.mention),
                    ephemeral=True
                )
                return
            if role >= select_interaction.guild.me.top_role:
                await select_interaction.response.send_message(
                    self.lang.get("level_roles_hierarchy_nexus", "❌ The role {role} is higher than Nexus in the role hierarchy. Please move the Nexus role above it in your server settings.").format(role=role.mention),
                    ephemeral=True
                )
                return

            if (
                select_interaction.user.id != select_interaction.guild.owner_id
                and role >= select_interaction.user.top_role
            ):
                await select_interaction.response.send_message(
                    self.lang.get(
                        "level_roles_hierarchy_user",
                        "⚠️ This role is ranked equal to or higher than your own highest role. "
                        "You cannot use it for level roles. Please choose a different role.",
                    ),
                    ephemeral=True
                )
                return

            await db_levels.set_level_role(self.guild_id, level, role.id)
            await select_interaction.response.edit_message(
                content=self.lang.get(
                    "level_roles_add_success", "{check} Level {level} now grants {role}."
                ).format(check=NexusEmojis.CHECKMARK, level=level, role=role.mention),
                view=None,
            )

        select.callback = on_role_chosen
        view = discord.ui.View(timeout=60)
        view.add_item(select)

        await interaction.response.send_message(
            self.lang.get("level_roles_pick_role", "Which role should Level {level} grant?").format(level=level),
            view=view,
            ephemeral=True,
        )

class RemoveLevelRoleView(discord.ui.View):
    def __init__(self, guild_id: int, lang: dict, roles_data: list[dict]):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.lang = lang
        self.select_role.placeholder = lang.get("level_roles_remove_placeholder", "Select a level role to remove...")
        self.select_role.options = [
            discord.SelectOption(label=f"Level {entry['level']}", value=str(entry["level"]))
            for entry in roles_data
        ][:25]

    @discord.ui.select(placeholder="Select a level role to remove...")
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.Select):
        level = int(select.values[0])
        await db_levels.remove_level_role(self.guild_id, level)
        await interaction.response.edit_message(
            content=self.lang.get("remove_level_role_success", "{check} Removed role for Level {level}.").format(
                check=NexusEmojis.CHECKMARK, level=level
            ),
            view=None,
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(Levels(bot))