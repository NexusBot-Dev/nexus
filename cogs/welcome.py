import random
import logging
import discord
from discord.ext import commands
from database import db_settings, db_welcome, db_limits
from systems.premium import is_premium
from cogs.utils import get_lang
from config import NEXUS_FOOTER

log = logging.getLogger(__name__)

def _display_name(member: discord.Member) -> str:
    return discord.utils.escape_markdown(member.display_name)

def render_welcome_template(template: str, member: discord.Member) -> str:
    """Mentions in Embeds löst Discord nur auf, wenn der Client des Betrachters den User
    schon kennt — bei frisch beigetretenen Membern fast nie, dann steht dort roh <@id>.
    Deshalb wird {user} im Embed als fetter Anzeigename gerendert."""
    return (
        template
        .replace("{user}", f"**{_display_name(member)}**")
        .replace("{username}", _display_name(member))
        .replace("{server}", member.guild.name)
        .replace("{membercount}", str(member.guild.member_count))
    )

class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            settings = await db_settings.get_guild(member.guild.id)
            if not settings or not settings["modules"].get("welcome", False):
                return
            if not settings["welcome_channel_id"]:
                return

            channel = member.guild.get_channel(settings["welcome_channel_id"])
            if channel is None:
                return

            lang = await get_lang(member.guild.id)
            color = await db_settings.get_color(member.guild.id)

            custom_messages = await db_welcome.get_welcome_messages(member.guild.id)
            limit = await db_limits.get_limit(member.guild.id, "welcome_messages", await is_premium(member.guild.id))
            active_messages = custom_messages[:limit]
            if active_messages:
                chosen = random.choice(active_messages)
                description = render_welcome_template(chosen["message"], member)
            else:
                description = lang.get(
                    "welcome_description", "Hey {user}, glad you're here!\nYou are member **#{count}**."
                ).format(user=f"**{_display_name(member)}**", count=member.guild.member_count)

            embed = discord.Embed(
                title=lang.get("welcome_title", "👋 Welcome!"),
                description=description,
                color=color,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=NEXUS_FOOTER)

            perms = channel.permissions_for(member.guild.me)
            if not perms.send_messages or not perms.embed_links:
                log.warning(
                    "Fehlende Rechte (send_messages/embed_links) in Welcome-Channel #%s auf %s.",
                    channel.name, member.guild.name,
                )
                return
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            log.info("%s ist %s beigetreten.", member, member.guild.name)

        except discord.Forbidden:
            log.warning(
                "Keine Berechtigung, Welcome-Nachricht in %s zu senden (Guild %s).",
                member.guild.name, member.guild.id,
            )
        except Exception:
            log.exception("Fehler in on_member_join für %s auf %s", member, member.guild.name)

async def setup(bot):
    await bot.add_cog(Welcome(bot))