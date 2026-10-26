import logging
import discord
from datetime import timezone
from discord import app_commands
from discord.ext import commands
from cogs.utils import get_lang, module_required, post_logging
from config import NEXUS_FOOTER
from database import db_levels, db_settings
from emojis import NexusEmojis
from systems.ui import NexusView
from systems.hierarchy import can_grant_role, format_issue
from systems.mee6_import import (
    MAX_PLAYERS,
    Mee6NotFound,
    Mee6Private,
    Mee6Unavailable,
    fetch_leaderboard,
)

log = logging.getLogger(__name__)

class Mee6Import(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._busy: set[int] = set()   # Guilds, deren Import gerade vorbereitet wird

    @app_commands.command(name="import_mee6", description=app_commands.locale_str("cmd_import_mee6_desc"))
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(roles=app_commands.locale_str("cmd_import_mee6_roles_desc"))
    @module_required("levels")
    async def import_mee6(self, interaction: discord.Interaction, roles: bool = True):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        lang = await get_lang(guild.id)

        if guild.id in self._busy:
            await interaction.followup.send(
                lang.get("mee6_busy", "⏳ An import is already being prepared on this server."), ephemeral=True,
            )
            return

        cfg = await db_levels.get_levels_settings(guild.id)
        if imported_at := cfg.get("mee6_imported_at"):
            ts = int(imported_at.replace(tzinfo=timezone.utc).timestamp())
            await interaction.followup.send(
                lang.get(
                    "mee6_already",
                    "ℹ️ The MEE6 import was already done on this server (<t:{ts}:F>). It can only be run once.",
                ).format(ts=ts),
                ephemeral=True,
            )
            return

        self._busy.add(guild.id)
        try:
            await interaction.followup.send(
                lang.get("mee6_loading", "⏳ Loading data from MEE6 …"), ephemeral=True,
            )
            await self._prepare(interaction, lang, roles)
        finally:
            self._busy.discard(guild.id)

    # ─── Vorbereitung: laden, abgleichen, Vorschau ───────────────────────────

    async def _prepare(self, interaction: discord.Interaction, lang: dict, import_roles: bool):
        guild = interaction.guild

        async def progress(count: int):
            try:
                await interaction.edit_original_response(
                    content=lang.get("mee6_progress", "⏳ Loading from MEE6 … {count} players").format(count=f"{count:,}"),
                )
            except discord.HTTPException:
                pass

        try:
            data = await fetch_leaderboard(guild.id, on_progress=progress)
        except Mee6Private:
            await self._fail(interaction, lang.get(
                "mee6_private",
                "❌ The MEE6 leaderboard of this server is private. Open the MEE6 dashboard → Leaderboard, "
                "enable *Make my server's leaderboard public* and run the command again. "
                "You can switch it back to private afterwards.",
            ))
            return
        except Mee6NotFound:
            await self._fail(interaction, lang.get(
                "mee6_not_found",
                "❌ No MEE6 leaderboard was found for this server. Is MEE6 on this server, and was Levels ever used?",
            ))
            return
        except Mee6Unavailable:
            await self._fail(interaction, lang.get(
                "mee6_unavailable", "❌ MEE6 isn't responding right now. Please try again later.",
            ))
            return

        if not data.players:
            await self._fail(interaction, lang.get(
                "mee6_empty", "ℹ️ The MEE6 leaderboard is public but empty — there is nothing to import.",
            ))
            return

        # Nur aktuelle Mitglieder (REST statt Cache: chunk_guilds_at_startup=False)
        await interaction.edit_original_response(
            content=lang.get("mee6_matching", "⏳ Matching with server members …"),
        )
        names = {m.id: m.display_name async for m in guild.fetch_members(limit=None)}
        entries = [(uid, xp) for uid, xp in data.players if uid in names]
        if not entries:
            await self._fail(interaction, lang.get(
                "mee6_no_members", "ℹ️ None of the players in the MEE6 leaderboard are currently on this server.",
            ))
            return

        valid_rewards: list[tuple[int, int]] = []
        if not import_roles:
            roles_text = lang.get("mee6_roles_off", "Not imported (`roles: no`).")
        elif not data.rewards:
            roles_text = lang.get("mee6_roles_none", "MEE6 has no role rewards set up.")
        else:
            existing = {r["level"]: r["role_id"] for r in await db_levels.get_all_level_roles(guild.id)}
            lines = []
            for rank, role_id in sorted(data.rewards):
                level = rank + 1   # MEE6 zählt ab Level 0, Nexus ab Level 1
                issue = self._role_issue(guild, interaction.user, role_id, level, existing, lang)
                if issue is None:
                    valid_rewards.append((level, role_id))
                    lines.append(f"✅ Level {level}: <@&{role_id}>")
                else:
                    lines.append(f"⚠️ Level {level}: <@&{role_id}> — {issue}")
            shown = lines[:10]
            if len(lines) > 10:
                shown.append(lang.get("mee6_more", "… and {count} more").format(count=len(lines) - 10))
            roles_text = "\n".join(shown)

        total_xp = sum(xp for _, xp in entries)
        found = f"{len(data.players):,}"
        if data.truncated:
            found += " " + lang.get("mee6_truncated", "(capped at {max})").format(max=f"{MAX_PLAYERS:,}")

        embed = discord.Embed(
            title=lang.get("mee6_preview_title", "📥 MEE6 import — preview"),
            description=lang.get(
                "mee6_preview_desc",
                "Nothing has been changed yet. The XP is **added** to existing XP and this can only be done once "
                "per server. Nexus levels start at 1, so MEE6 levels appear one level higher.",
            ),
            color=await db_settings.get_color(guild.id),
        )
        embed.add_field(name=lang.get("mee6_f_found", "Players in MEE6"), value=found)
        embed.add_field(name=lang.get("mee6_f_members", "Current members"), value=f"{len(entries):,}")
        embed.add_field(name=lang.get("mee6_f_xp", "XP to add"), value=f"{total_xp:,}")
        embed.add_field(
            name=lang.get("mee6_f_top", "Top 3"),
            value="\n".join(f"**{i}.** {names[uid]} — {xp:,} XP" for i, (uid, xp) in enumerate(entries[:3], 1)),
            inline=False,
        )
        embed.add_field(name=lang.get("mee6_f_roles", "Level roles"), value=roles_text, inline=False)
        embed.set_footer(text=NEXUS_FOOTER)

        view = Mee6ImportView(
            lang, owner_id=interaction.user.id, bot=self.bot, entries=entries, rewards=valid_rewards,
        )
        await interaction.edit_original_response(content=None, embed=embed, view=view)
        view.bind(interaction)

    @staticmethod
    def _role_issue(
        guild: discord.Guild, invoker: discord.Member, role_id: int, level: int, existing: dict, lang: dict,
    ) -> str | None:
        """Gleiche Prüfungen wie beim AddLevelRoleModal. None = Rolle kann übernommen werden."""
        if not 1 <= level <= 1000:
            return lang.get("mee6_issue_level", "level out of range")
        role = guild.get_role(role_id)
        if role is None:
            return lang.get("mee6_issue_missing", "role no longer exists")
        if role.is_default() or role.managed:
            return lang.get("mee6_issue_managed", "role can't be assigned by Nexus")
        if role >= guild.me.top_role:
            return lang.get("mee6_issue_nexus", "role is above Nexus")
        if invoker.id != guild.owner_id and role >= invoker.top_role:
            return lang.get("mee6_issue_user", "role is above your own highest role")
        if not (check := can_grant_role(guild, role, invoker)):
            return format_issue(check, lang).removeprefix("❌ ")
        if level in existing:
            return lang.get("mee6_issue_exists", "this level already has a role")
        return None

    @staticmethod
    async def _fail(interaction: discord.Interaction, text: str):
        await interaction.edit_original_response(content=text, embed=None, view=None)

# ─── Bestätigung ─────────────────────────────────────────────────────────────

class Mee6ImportView(NexusView):
    def __init__(self, lang: dict, *, owner_id: int, bot: commands.Bot, entries: list[tuple[int, int]],
                 rewards: list[tuple[int, int]]):
        super().__init__(lang, owner_id=owner_id, timeout=300)
        self.bot = bot
        self.entries = entries
        self.rewards = rewards
        self.confirm_btn.label = lang.get("mee6_btn_import", "Import")
        self.confirm_btn.emoji = NexusEmojis.CHECKMARK
        self.cancel_btn.label = lang.get("mee6_btn_cancel", "Cancel")

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(
            content=self.lang.get("mee6_importing", "⏳ Importing …"), view=self,
        )

        guild = interaction.guild
        imported = await db_levels.import_xp(guild.id, self.entries)
        if imported is None:
            await interaction.edit_original_response(
                content=self.lang.get("mee6_already_short", "ℹ️ The MEE6 import was already done on this server."),
                embed=None, view=None,
            )
            return

        roles_set, roles_failed = 0, False
        for level, role_id in self.rewards:
            try:
                await db_levels.set_level_role(guild.id, level, role_id)
                roles_set += 1
            except Exception:
                log.exception("MEE6-Import: Level-Rolle %s -> %s auf %s nicht gespeichert.", level, role_id, guild.id)
                roles_failed = True
                break

        await post_logging(
            self.bot,
            guild_id=guild.id,
            title=self.lang.get("log_mee6_import", "{mod} imported MEE6 levels.").format(mod=interaction.user),
            moderator=interaction.user,
            target=interaction.user,
            lang=self.lang,
            reason=f"{imported} members, {roles_set} level roles",
            color=discord.Color.blue(),
        )
        log.info("MEE6-Import auf %s: %s Mitglieder, %s Rollen.", guild.id, imported, roles_set)

        text = self.lang.get(
            "mee6_done", "{check} MEE6 import finished: **{members}** members, **{roles}** level roles.",
        ).format(check=NexusEmojis.CHECKMARK, members=f"{imported:,}", roles=roles_set)
        if roles_failed:
            text += "\n" + self.lang.get(
                "mee6_roles_partial", "⚠️ Not all level roles could be saved. Check `/level_roles`.",
            )
        await interaction.edit_original_response(content=text, embed=None, view=None)

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            content=self.lang.get("mee6_cancelled", "Import cancelled. Nothing was changed."),
            embed=None, view=None,
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(Mee6Import(bot))