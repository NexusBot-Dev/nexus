import asyncio
import logging
import discord
from discord import app_commands
from discord.ext import commands
from database import db_settings, db_limits
from systems.premium import get_premium_info, PremiumInfo
from cogs.utils import get_lang
from emojis import NexusEmojis
from config import PREMIUM_SKU_ID, NEXUS_FOOTER

log = logging.getLogger(__name__)

PERK_LIMITS = (
    # (Limit-Key,             Text-Key,                      Fallback,                    Emoji)
    ("saved_embeds",          "premium_perk_embeds",         "{emoji} Saved embeds",      "📋"),
    ("ticket_categories",     "premium_perk_tickets",        "{emoji} Ticket categories", NexusEmojis.Modules.TICKET),
    ("welcome_messages",      "premium_perk_welcome",        "{emoji} Welcome messages",  "👋"),
    ("boost_messages",        "premium_perk_boosts",         "{emoji} Boost messages",    NexusEmojis.Modules.BOOST),
    ("stream_alerts_twitch",  "premium_perk_alerts_twitch",  "{emoji} Twitch alerts",     NexusEmojis.Integrations.TWITCH),
    ("stream_alerts_youtube", "premium_perk_alerts_youtube", "{emoji} YouTube alerts",    NexusEmojis.Integrations.YOUTUBE),
    ("stream_alerts_kick",    "premium_perk_alerts_kick",    "{emoji} Kick alerts",       NexusEmojis.Integrations.KICK),
)
# Premium-Features ohne Zahl (Übersetzungs-Key, Fallback)
PERK_FEATURES = (
    ("premium_perk_schedule",    "⏰ Scheduled embed posting"),
    ("premium_perk_unique",      "🎭 Unique mode for reaction roles"),
    ("premium_perk_alert_roles", "🔔 Platform-specific ping roles for stream alerts"),
)

def describe_status(info: PremiumInfo | None, lang: dict) -> tuple[str, bool, str | None]:
    """Status-Text für /premium.
    Rückgabe: (Text, Kauf-Button zeigen?, optionaler Hinweis)."""
    if info is None:
        return lang.get("premium_status_none", "❌ This server doesn't have Premium."), True, None

    def when() -> dict:
        return {
            "date": discord.utils.format_dt(info.expires_at, "D"),
            "relative": discord.utils.format_dt(info.expires_at, "R"),
        } if info.expires_at else {"date": "—", "relative": "—"}

    if not info.active:
        text = lang.get("premium_status_expired", "❌ Premium expired on {date}.").format(**when())
        return text, True, None

    if info.expires_at is None:
        if info.source == "discord":
            text = lang.get(
                "premium_status_sub_active", "{nexus_checkmark} Premium subscription active — renews automatically."
            )
            return text.format(nexus_checkmark=NexusEmojis.CHECKMARK), False, None
        text = lang.get("premium_status_unlimited", "{nexus_checkmark} Premium active — unlimited.")
        return text.format(nexus_checkmark=NexusEmojis.CHECKMARK), False, None

    if info.source == "discord":
        text = lang.get(
            "premium_status_sub_ending", "⏳ Subscription cancelled — Premium runs until {date} ({relative})."
        ).format(**when())
        hint = lang.get(
            "premium_hint_resume", "You can resume it in your Discord settings under **Subscriptions**."
        )
        return text, False, hint

    if info.source == "manual":
        text = lang.get(
            "premium_status_manual", "🎁 Gift from the Nexus team — Premium until {date} ({relative})."
        ).format(**when())
    else:
        text = lang.get(
            "premium_status_gift", "🎁 Early Bird gift — Premium until {date} ({relative})."
        ).format(**when())
    return text, True, None

async def _limit_lines(guild_id: int, lang: dict) -> list[str]:
    async def fetch(key: str):
        try:
            free = await db_limits.get_limit(guild_id, key, False)
            premium = await db_limits.get_limit(guild_id, key, True)
            return key, free, premium
        except Exception as e:
            log.warning("Limit '%s' für /premium nicht lesbar (fehlt in feature_limits?): %r", key, e)
            return key, None, None

    results = await asyncio.gather(*(fetch(key) for key, *_ in PERK_LIMITS))
    lines = []
    for (key, free, premium), (_, lang_key, fallback, emoji) in zip(results, PERK_LIMITS):
        if free is None or premium is None or premium <= free:
            continue
        lines.append(f"{lang.get(lang_key, fallback).format(emoji=emoji)}: **{free}** → **{premium}**")
    return lines

class PremiumCommand(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="premium", description=app_commands.locale_str("cmd_premium_desc"))
    @app_commands.default_permissions(manage_guild=True)
    async def premium(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        lang  = await get_lang(guild_id)
        color = await db_settings.get_color(guild_id)
        info  = await get_premium_info(guild_id)

        status_text, show_buy, hint = describe_status(info, lang)
        if hint:
            status_text += f"\n{hint}"

        embed = discord.Embed(title=lang.get("premium_title", "⭐ Nexus Premium"), color=color)
        embed.add_field(name=lang.get("premium_status_field", "Status"), value=status_text, inline=False)

        perk_lines = await _limit_lines(guild_id, lang)
        perk_lines += [lang.get(key, fallback) for key, fallback in PERK_FEATURES]
        embed.add_field(
            name=lang.get("premium_perks_title", "What Premium gives you"),
            value="\n".join(perk_lines),
            inline=False,
        )
        embed.set_footer(text=NEXUS_FOOTER)

        view = discord.utils.MISSING
        if show_buy:
            # Premium-Button: Label/Icon bestimmt Discord, ein Klick öffnet den Kauf-Dialog.
            view = discord.ui.View()
            view.add_item(discord.ui.Button(sku_id=PREMIUM_SKU_ID))

        try:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except discord.HTTPException as e:
            if not show_buy or e.code != 50035:
                raise
            # SKU nicht veröffentlicht oder ID falsch: Status trotzdem zeigen, nur ohne Kauf-Button
            log.warning("/premium: Kauf-Button nicht möglich (SKU %s): %s", PREMIUM_SKU_ID, e)
            await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(PremiumCommand(bot))