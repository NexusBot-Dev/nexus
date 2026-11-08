import logging
import discord
import random
from typing import Optional, Union
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone
from database import db_settings, db_levels, db_activity
from systems.exp_system import XP_COOLDOWN, CHAT_XP_MAX, xp_range_error
from cogs.utils import post_logging, get_lang, module_required
from config import NEXUS_FOOTER
from emojis import NexusEmojis
from systems.ui import NexusView, NexusModal, send_ephemeral
from systems.hierarchy import can_use_channel, can_grant_role, format_issue
from systems import voice_xp

log = logging.getLogger(__name__)

# Cooldown-Cache: Key=(guild_id, user_id), Value=Timestamp
_xp_cooldown: dict[tuple[int, int], datetime] = {}

class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.voice_xp_loop.start()

    async def cog_unload(self):
        self.voice_xp_loop.cancel()

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
        if new_level > old_level:
            levels_cfg = await db_levels.get_levels_settings(guild.id)
            target, persistent = channel, False
            if levelup_id := levels_cfg.get("levelup_channel_id"):
                configured = guild.get_channel(levelup_id)
                if configured is not None and can_use_channel(configured, "send_messages", "embed_links"):
                    target, persistent = configured, True
                else:
                    log.warning("Level-Up-Channel %s auf %s nicht nutzbar — Fallback auf Nachrichten-Channel.", levelup_id, guild.name)

            if target is not None and hasattr(target, "send"):
                try:
                    color = await db_settings.get_color(guild.id)
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
                    await target.send(embed=embed, delete_after=None if persistent else 30)
                except discord.Forbidden:
                    log.warning("Keine Berechtigung, Level-Up-Embed in %s auf %s zu senden.", target, guild.name)
                except discord.HTTPException as e:
                    log.error("HTTP-Fehler beim Senden des Level-Up-Embeds: %s", e)

    @staticmethod
    def _is_excluded(channel, excluded: frozenset[int]) -> bool:
        """Der Channel selbst, seine Kategorie oder (bei Threads) der Eltern-Channel."""
        ids = {channel.id, getattr(channel, "category_id", None), getattr(channel, "parent_id", None)}
        return bool(ids & excluded)

    # ─── Events ──────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            if message.author.bot or not message.guild:
                return
            if not await db_settings.is_module_enabled(message.guild.id, "levels"):
                return

            excluded = await db_levels.get_excluded_channels(message.guild.id)
            if excluded and self._is_excluded(message.channel, excluded):
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

            settings = await db_levels.get_levels_settings(message.guild.id)
            xp_min, xp_max = settings["xp_min"], settings["xp_max"]
            amount = random.randint(min(xp_min, xp_max), max(xp_min, xp_max))

            result = await db_levels.add_xp(
                guild_id=message.guild.id,
                user_id=message.author.id,
                amount=amount,
            )
            if result is None:
                return

            if settings["shop_enabled"]:
                points_min, points_max = settings["points_min"], settings["points_max"]
                points_amount = random.randint(min(points_min, points_max), max(points_min, points_max))
                await db_activity.add_points(message.guild.id, message.author.id, points_amount)

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

    # ─── Voice-XP ────────────────────────────────────────────────────────────────

    @tasks.loop(seconds=voice_xp.TICK_SECONDS)
    async def voice_xp_loop(self):
        for guild in list(self.bot.guilds):
            try:
                await self.award_voice_xp(guild)
            except Exception:
                log.exception("Voice-XP auf %s fehlgeschlagen.", guild.id)

    @voice_xp_loop.before_loop
    async def _before_voice_xp(self):
        await self.bot.wait_until_ready()

    async def award_voice_xp(self, guild: discord.Guild):
        """Eine Minute Voice-XP für diesen Server (siehe systems/voice_xp.py)."""
        # Erst der billige Blick in den Cache: auf den meisten Servern sitzt gerade niemand zu zweit im Voice
        channels = [c for c in guild.voice_channels if len(c.members) >= voice_xp.MIN_ACTIVE]
        if not channels:
            return
        if not await db_settings.is_module_enabled(guild.id, "levels"):
            return
        settings = await db_levels.get_levels_settings(guild.id)
        if not settings.get("voice_xp_enabled"):
            return

        excluded = await db_levels.get_excluded_channels(guild.id)
        afk_channel_id = guild.afk_channel.id if guild.afk_channel else None
        cap = voice_xp.MAX_XP_PER_MINUTE   # auch für Werte, die vor Einführung des Höchstwerts gespeichert wurden
        low, high = sorted((min(settings["voice_xp_min"], cap), min(settings["voice_xp_max"], cap)))
        lang = None
        for channel in channels:
            for member in voice_xp.eligible_members(channel, afk_channel_id=afk_channel_id, excluded=excluded):
                result = await db_levels.add_xp(guild.id, member.id, random.randint(low, high))
                if result and result["new_level"] > result["old_level"]:
                    lang = lang or await get_lang(guild.id)
                    # Ohne Level-Up-Kanal landet die Nachricht im Text-Chat des Voice-Kanals
                    await self._handle_level_up(guild, member, channel, result, lang)

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

        color = await db_settings.get_color(interaction.guild_id)
        embed = discord.Embed(
            title=lang.get("leaderboard_title", "🏆 Leaderboard"),
            color=color,
        )
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, entry in enumerate(entries, start=1):
            m = interaction.guild.get_member(entry["user_id"])
            if m is None:
                try:
                    m = await interaction.guild.fetch_member(entry["user_id"])
                except discord.NotFound:
                    m = None
            name = m.display_name if m else f"Unknown User ({entry['user_id']})"
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
        color = await db_settings.get_color(interaction.guild_id)

        embed = self._build_level_roles_embed(roles_data, lang, color)
        view = LevelRolesView(interaction.guild_id, lang)
        view.remove_btn.disabled = not roles_data
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.bind(interaction)

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

    @app_commands.command(name="levelup_channel", description=app_commands.locale_str("cmd_levelup_channel_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("levels")
    async def levelup_channel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        cfg = await db_levels.get_levels_settings(interaction.guild_id)
        view = LevelUpChannelView(lang, has_channel=bool(cfg.get("levelup_channel_id")))
        await interaction.followup.send(
            embed=await _levelup_embed(interaction.guild_id, lang), view=view, ephemeral=True,
        )
        view.bind(interaction)

    @app_commands.command(name="xp_channels", description=app_commands.locale_str("cmd_xp_channels_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("levels")
    async def xp_channels(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        excluded = await db_levels.get_excluded_channels(interaction.guild_id)
        valid = sorted(cid for cid in excluded if interaction.guild.get_channel(cid))
        if len(valid) != len(excluded):   # gelöschte Channels aufräumen
            await db_levels.set_excluded_channels(interaction.guild_id, valid)
        view = LevelExcludeView(lang, valid)
        await interaction.followup.send(
            embed=await _exclude_embed(interaction.guild, lang), view=view, ephemeral=True,
        )
        view.bind(interaction)

    @app_commands.command(name="xp_settings", description=app_commands.locale_str("cmd_xp_settings_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @module_required("levels")
    async def xp_settings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction.guild_id)
        settings = await db_levels.get_levels_settings(interaction.guild_id)
        view = XpSettingsView(lang, settings)
        await interaction.followup.send(embed=await _xp_settings_embed(interaction.guild_id, lang, settings), view=view, ephemeral=True)
        view.bind(interaction)

# ─── Views & Modals ──────────────────────────────────────────────────────────

class LevelRolesView(NexusView):
    def __init__(self, guild_id: int, lang: dict):
        super().__init__(lang, owner_id=None, require=None, timeout=180)
        self.guild_id = guild_id
        self.add_btn.label = lang.get("level_roles_btn_add", "➕ Add")
        self.remove_btn.label = lang.get("level_roles_btn_remove", "➖ Remove")

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddLevelRoleModal(self.guild_id, self.lang))

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        roles_data = await db_levels.get_all_level_roles(self.guild_id)
        if not roles_data:
            self.stop()
            await interaction.edit_original_response(
                content=self.lang.get("no_level_roles_found", "No level roles have been set up for this server yet."),
                embed=None,
                view=None,
            )
            return
        await self.show(
            interaction,
            RemoveLevelRoleView(self.guild_id, self.lang, roles_data),
            content=self.lang.get("level_roles_pick_remove", "Which level role should be removed?"),
            embed=None,
        )


class AddLevelRoleModal(NexusModal):
    def __init__(self, guild_id: int, lang: dict):
        super().__init__(lang, title=lang.get("level_roles_add_modal_title", "Add Level Role"))
        self.guild_id = guild_id
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

            if not (check := can_grant_role(select_interaction.guild, role, select_interaction.user)):
                await select_interaction.response.send_message(format_issue(check, self.lang), ephemeral=True)
                return

            await db_levels.set_level_role(self.guild_id, level, role.id)
            await select_interaction.response.edit_message(
                content=self.lang.get(
                    "level_roles_add_success", "{check} Level {level} now grants {role}."
                ).format(check=NexusEmojis.CHECKMARK, level=level, role=role.mention),
                view=None,
            )

        select.callback = on_role_chosen
        view = NexusView(self.lang, owner_id=None, require=None, timeout=60)
        view.add_item(select)

        await interaction.response.send_message(
            self.lang.get("level_roles_pick_role", "Which role should Level {level} grant?").format(level=level),
            view=view,
            ephemeral=True,
        )
        view.bind(interaction)

class RemoveLevelRoleView(NexusView):
    def __init__(self, guild_id: int, lang: dict, roles_data: list[dict]):
        super().__init__(lang, owner_id=None, require=None, timeout=60)
        self.guild_id = guild_id
        self.select_role.placeholder = lang.get("level_roles_remove_placeholder", "Select a level role to remove...")
        self.select_role.options = [
            discord.SelectOption(label=f"Level {entry['level']}", value=str(entry["level"]))
            for entry in roles_data
        ][:25]

    @discord.ui.select(placeholder="Select a level role to remove...")
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()
        level = int(select.values[0])
        await db_levels.remove_level_role(self.guild_id, level)
        self.stop()
        await interaction.edit_original_response(
            content=self.lang.get("remove_level_role_success", "{check} Removed role for Level {level}.").format(
                check=NexusEmojis.CHECKMARK, level=level
            ),
            view=None,
        )

async def _levelup_embed(guild_id: int, lang: dict) -> discord.Embed:
    cfg = await db_levels.get_levels_settings(guild_id)
    color = await db_settings.get_color(guild_id)
    channel_id = cfg.get("levelup_channel_id")
    embed = discord.Embed(title=lang.get("levelup_title", "📈 Level-up channel"), color=color)
    if channel_id:
        embed.description = lang.get(
            "levelup_desc_set", "Level-up messages are posted in {channel} and stay there."
        ).format(channel=f"<#{channel_id}>")
    else:
        embed.description = lang.get(
            "levelup_desc_default",
            "No channel set: Level-up messages appear where the user wrote and disappear after 30 seconds.",
        )
    embed.set_footer(text=NEXUS_FOOTER)
    return embed


class LevelUpChannelView(NexusView):
    def __init__(self, lang: dict, has_channel: bool):
        super().__init__(lang, timeout=180)
        self.select_channel.placeholder = lang.get("levelup_select_placeholder", "📋 Select channel...")
        self.reset_btn.label = lang.get("levelup_btn_reset", "Reset to default")
        self.reset_btn.disabled = not has_channel

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        row=0,
    )
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = interaction.guild.get_channel(select.values[0].id)
        if channel is None:
            await send_ephemeral(interaction, self.lang.get("giveaway_channel_unreachable", "❌ I can't access that channel."))
            return
        if not (check := can_use_channel(channel, "send_messages", "embed_links")):
            await send_ephemeral(interaction, format_issue(check, self.lang))
            return

        await db_levels.update_levels_settings(interaction.guild_id, levelup_channel_id=channel.id)
        self.reset_btn.disabled = False
        await interaction.response.edit_message(
            embed=await _levelup_embed(interaction.guild_id, self.lang), view=self,
        )

    @discord.ui.button(style=discord.ButtonStyle.secondary, row=1)
    async def reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await db_levels.update_levels_settings(interaction.guild_id, levelup_channel_id=None)
        button.disabled = True
        await interaction.edit_original_response(
            embed=await _levelup_embed(interaction.guild_id, self.lang), view=self,
        )

async def _exclude_embed(guild: discord.Guild, lang: dict) -> discord.Embed:
    excluded = await db_levels.get_excluded_channels(guild.id)
    embed = discord.Embed(
        title=lang.get("xpx_title", "🚫 Channels without XP"),
        color=await db_settings.get_color(guild.id),
    )
    if excluded:
        embed.description = lang.get(
            "xpx_desc_set",
            "No XP is earned in these channels. A category covers all channels and threads inside it:\n{channels}",
        ).format(channels="\n".join(f"<#{cid}>" for cid in sorted(excluded)))
    else:
        embed.description = lang.get(
            "xpx_desc_none", "XP is earned everywhere. Select channels or categories below to exclude them.",
        )
    embed.set_footer(text=NEXUS_FOOTER)
    return embed


def _channel_defaults(ids: list[int]) -> list[discord.SelectDefaultValue]:
    return [discord.SelectDefaultValue(id=cid, type=discord.SelectDefaultValueType.channel) for cid in ids]


class LevelExcludeView(NexusView):
    def __init__(self, lang: dict, current_ids: list[int]):
        super().__init__(lang, timeout=180)
        self.select_channels.placeholder = lang.get("xpx_select_placeholder", "📋 Select channels or categories...")
        self.select_channels.default_values = _channel_defaults(current_ids)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news,
                       discord.ChannelType.forum, discord.ChannelType.voice, discord.ChannelType.category],
        min_values=0,
        max_values=25,
        row=0,
    )
    async def select_channels(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await interaction.response.defer()
        ids = sorted({c.id for c in select.values})
        await db_levels.set_excluded_channels(interaction.guild_id, ids)
        select.default_values = _channel_defaults(ids)
        await interaction.edit_original_response(
            embed=await _exclude_embed(interaction.guild, self.lang), view=self,
        )

# ─── /xp_settings ────────────────────────────────────────────────────────────

async def _xp_settings_embed(guild_id: int, lang: dict, settings: dict) -> discord.Embed:
    embed = discord.Embed(
        title=lang.get("xps_title", "⚙️ XP settings"),
        description=lang.get(
            "xps_desc",
            "Voice XP is earned every minute while at least two members are unmuted in the same voice channel.",
        ),
        color=await db_settings.get_color(guild_id),
    )
    embed.add_field(
        name=lang.get("xps_chat_field", "💬 Chat XP"),
        value=lang.get("xps_chat_value", "{min}–{max} XP per message · cooldown {cooldown}s").format(
            min=settings["xp_min"], max=settings["xp_max"], cooldown=XP_COOLDOWN,
        ),
        inline=False,
    )
    status = (lang.get("xps_voice_on", "✅ On") if settings["voice_xp_enabled"]
              else lang.get("xps_voice_off", "❌ Off"))
    embed.add_field(
        name=lang.get("xps_voice_field", "🎙️ Voice XP"),
        value=lang.get("xps_voice_value", "{status} · {min}–{max} XP per minute").format(
            status=status, min=settings["voice_xp_min"], max=settings["voice_xp_max"],
        ),
        inline=False,
    )
    embed.set_footer(text=NEXUS_FOOTER)
    return embed


class XpSettingsView(NexusView):
    def __init__(self, lang: dict, settings: dict):
        super().__init__(lang, timeout=300)
        self.settings = settings
        self.btn_chat.label = lang.get("xps_btn_chat", "Chat XP")
        self.btn_chat.emoji = "💬"
        self.btn_voice.label = lang.get("xps_btn_voice", "Voice XP")
        self.btn_voice.emoji = "🎙️"
        enabled = bool(settings["voice_xp_enabled"])
        self.btn_toggle.label = (lang.get("xps_btn_voice_disable", "Disable voice XP") if enabled
                                 else lang.get("xps_btn_voice_enable", "Enable voice XP"))
        self.btn_toggle.style = discord.ButtonStyle.secondary if enabled else discord.ButtonStyle.success

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Neue Werte laden und die Übersicht an derselben Stelle neu zeigen."""
        settings = await db_levels.get_levels_settings(interaction.guild_id)
        embed = await _xp_settings_embed(interaction.guild_id, self.lang, settings)
        await self.show(interaction, XpSettingsView(self.lang, settings), embed=embed)

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def btn_chat(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(XpRangeModal(self, "chat"))

    @discord.ui.button(style=discord.ButtonStyle.primary)
    async def btn_voice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(XpRangeModal(self, "voice"))

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def btn_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await db_levels.update_levels_settings(
            interaction.guild_id, voice_xp_enabled=not self.settings["voice_xp_enabled"],
        )
        await self.refresh(interaction)


class XpRangeModal(NexusModal):
    """Min./Max. XP — für Chat (pro Nachricht, bis 1000) oder Voice (pro Minute, bis 150)."""

    def __init__(self, parent: XpSettingsView, kind: str):
        lang = parent.lang
        self.parent, self.kind = parent, kind
        self.max_xp = CHAT_XP_MAX if kind == "chat" else voice_xp.MAX_XP_PER_MINUTE
        prefix = "" if kind == "chat" else "voice_"
        self.columns = (f"{prefix}xp_min", f"{prefix}xp_max")
        super().__init__(lang, title=lang.get(f"xps_modal_{kind}_title", "Chat XP per message" if kind == "chat" else "Voice XP per minute"))
        self.min_input = discord.ui.TextInput(
            label=lang.get("xps_modal_min_label", "Minimum (1–{max})").format(max=self.max_xp),
            default=str(parent.settings[self.columns[0]]), max_length=4,
        )
        self.max_input = discord.ui.TextInput(
            label=lang.get("xps_modal_max_label", "Maximum (1–{max})").format(max=self.max_xp),
            default=str(parent.settings[self.columns[1]]), max_length=4,
        )
        self.add_item(self.min_input)
        self.add_item(self.max_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            xp_min, xp_max = int(self.min_input.value.strip()), int(self.max_input.value.strip())
        except ValueError:
            xp_min = xp_max = None
        problem = xp_range_error(xp_min, xp_max, self.max_xp)
        if problem:
            key = ("dash_err_lvl_xp_min_gt_max" if problem == "min_gt_max"
                   else "dash_err_lvl_xp_range" if self.kind == "chat" else "dash_err_lvl_voice_xp_range")
            await interaction.response.send_message(
                "❌ " + self.lang.get(key, "Invalid values.").format(max=self.max_xp), ephemeral=True,
            )
            return
        await db_levels.update_levels_settings(interaction.guild_id, **dict(zip(self.columns, (xp_min, xp_max))))
        await self.parent.refresh(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(Levels(bot))