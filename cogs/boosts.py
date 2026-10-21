import random
import logging
import discord
from discord.ext import commands
from database import db_settings, db_boosts, db_limits
from systems.premium import is_premium
from cogs.utils import get_lang

log = logging.getLogger(__name__)

def render_boost_template(template: str, member: discord.Member) -> str:
    return (
        template
        .replace("{user}", member.mention)
        .replace("{username}", member.display_name)
        .replace("{server}", member.guild.name)
        .replace("{count}", str(member.guild.premium_subscription_count))
    )

class Boosts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        try:
            if before.premium_since is None and after.premium_since is not None:
                await self._handle_boost(after)
        except Exception:
            log.exception("Fehler in on_member_update für %s auf %s", after, after.guild.name)

    async def _handle_boost(self, member: discord.Member):
        try:
            settings = await db_settings.get_guild(member.guild.id)
            if settings is None:
                return

            channel_id = settings.get("boost_channel_id") or settings.get("welcome_channel_id")
            if not channel_id:
                return

            channel = member.guild.get_channel(channel_id)
            if channel is None:
                return

            lang  = await get_lang(member.guild.id)
            color = await db_settings.get_color(member.guild.id)

            custom_messages = await db_boosts.get_boost_messages(member.guild.id)
            limit = await db_limits.get_limit(member.guild.id, "boost_messages", await is_premium(member.guild.id))
            active_messages = custom_messages[:limit]
            if active_messages:
                chosen = random.choice(active_messages)
                description = render_boost_template(chosen["message"], member)
            else:
                description = lang.get(
                    "boost_description", "{user} boosted the server! 🎉\nWe now have **{count}** boost(s)!"
                ).format(user=member.mention, count=member.guild.premium_subscription_count)

            embed = discord.Embed(
                title=lang.get("boost_title", "🚀 New Server Boost!"),
                description=description,
                color=color,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=member.guild.name)
            await channel.send(embed=embed)

            log.info("%s hat %s geboostet.", member, member.guild.name)

        except Exception:
            log.exception("Fehler in _handle_boost für %s auf %s", member, member.guild.name)

async def setup(bot):
    await bot.add_cog(Boosts(bot))
