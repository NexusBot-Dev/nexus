"""Premium-Anzeigen in Discord: die Übersicht von /premium und der Hinweis an gesperrten Funktionen.

Der Hinweis (send_upsell) ersetzt Sackgassen wie „Ist nur für Premium“: Er erklärt die Funktion,
bietet direkt den Kauf-Button an und über „Mehr Infos“ die volle /premium-Übersicht.
"""
import asyncio
import logging
import discord
from config import NEXUS_FOOTER, PREMIUM_SKU_ID
from database import db_limits, db_settings
from emojis import NexusEmojis
from systems.premium import PremiumInfo, get_premium_info
from systems.ui import with_emoji

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
    ("stream_schedule_streamers", "premium_perk_stream_schedule", "{emoji} Stream schedule sync", "📅"),
)
# Premium-Features ohne Zahl (Übersetzungs-Key, Fallback)
PERK_FEATURES = (
    ("premium_perk_schedule",    "⏰ Scheduled embed posting"),
    ("premium_perk_unique",      "🎭 Unique mode for reaction roles"),
    ("premium_perk_alert_roles", "🔔 Platform-specific ping roles for stream alerts"),
    ("premium_perk_dropdown",    "📋 Dropdown menus for reaction roles"),
    ("premium_perk_color",       "🎨 Custom embed color"),
)

def describe_status(info: PremiumInfo | None, lang: dict) -> tuple[str, bool, str | None]:
    """Status-Text für /premium.
    Rückgabe: (Text, Kauf-Button zeigen?, optionaler Hinweis)."""
    if info is None:
        return f"{NexusEmojis.OFF} {lang.get('premium_status_none', 'This server does not have Premium.')}", True, None

    def when() -> dict:
        return {
            "date": discord.utils.format_dt(info.expires_at, "D"),
            "relative": discord.utils.format_dt(info.expires_at, "R"),
        } if info.expires_at else {"date": "—", "relative": "—"}

    if not info.active:
        text = f"{NexusEmojis.OFF} " + lang.get("premium_status_expired", "Premium expired on {date}.").format(**when())
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
        text = with_emoji(NexusEmojis.LOADING, lang.get(
            "premium_status_sub_ending", "Subscription cancelled — Premium runs until {date} ({relative})."
        ).format(**when()))
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

async def limit_lines(guild_id: int, lang: dict) -> list[str]:
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

async def build_overview(guild_id: int, lang: dict) -> tuple[discord.Embed, bool]:
    """Das Embed von /premium. Rückgabe: (Embed, Kauf-Button zeigen?)."""
    info = await get_premium_info(guild_id)
    status_text, show_buy, hint = describe_status(info, lang)
    if hint:
        status_text += f"\n{hint}"

    embed = discord.Embed(title=with_emoji(NexusEmojis.PREMIUM, lang.get("premium_title", "Nexus Premium")), color=await db_settings.get_color(guild_id))
    embed.add_field(name=lang.get("premium_status_field", "Status"), value=status_text, inline=False)

    perk_lines = await limit_lines(guild_id, lang)
    perk_lines += [lang.get(key, fallback) for key, fallback in PERK_FEATURES]
    embed.add_field(
        name=lang.get("premium_perks_title", "What Premium gives you"),
        value="\n".join(perk_lines),
        inline=False,
    )
    embed.set_footer(text=NEXUS_FOOTER)
    return embed, show_buy


def buy_button() -> discord.ui.Button:
    """Premium-Button: Label und Icon bestimmt Discord, ein Klick öffnet den Kauf-Dialog."""
    return discord.ui.Button(sku_id=PREMIUM_SKU_ID)


def _sku_rejected(error: discord.HTTPException) -> bool:
    """SKU nicht veröffentlicht oder ID falsch: Discord lehnt die Nachricht mit dem Button ab."""
    if error.code == 50035:
        log.warning("Premium-Kauf-Button nicht möglich (SKU %s): %s", PREMIUM_SKU_ID, error)
        return True
    return False


async def send_overview(interaction: discord.Interaction, lang: dict) -> None:
    """Die /premium-Übersicht als neue ephemere Nachricht (Interaction muss schon deferred sein)."""
    embed, show_buy = await build_overview(interaction.guild_id, lang)
    view = discord.utils.MISSING
    if show_buy:
        view = discord.ui.View()
        view.add_item(buy_button())
    try:
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    except discord.HTTPException as e:
        if not show_buy or not _sku_rejected(e):
            raise
        await interaction.followup.send(embed=embed, ephemeral=True)


class UpsellView(discord.ui.View):
    """Kauf-Button + „Mehr Infos“ (zeigt an derselben Stelle die /premium-Übersicht)."""

    def __init__(self, lang: dict, *, with_buy: bool = True):
        super().__init__(timeout=600)
        self.lang = lang
        if with_buy:
            self.add_item(buy_button())
        self.more_info.label = lang.get("premium_upsell_more_info", "More info")

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="ℹ️")
    async def more_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        embed, show_buy = await build_overview(interaction.guild_id, self.lang)
        view = discord.ui.View()
        if show_buy:
            view.add_item(buy_button())
        try:
            await interaction.edit_original_response(embed=embed, view=view)
        except discord.HTTPException as e:
            if not show_buy or not _sku_rejected(e):
                raise
            await interaction.edit_original_response(embed=embed, view=None)


async def send_upsell(interaction: discord.Interaction, lang: dict, feature: str, *, extra: str = "", **params) -> None:
    """Hinweis an einer Premium-Funktion: Titel premium_upsell_<feature>_title, Text …_desc.
    In beiden stehen {embeds} (Embed-Limit mit Premium) und alle **params zur Verfügung (z. B. {limit}, {max}).
    extra: zusätzliche Zeile unter dem Text. Ist die Interaction noch unbeantwortet, wird sie hier deferred."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    values = {"embeds": await db_limits.get_limit(interaction.guild_id, "saved_embeds", True), **params}
    description = lang.get(f"premium_upsell_{feature}_desc", "").format(**values)
    if extra:
        description += f"\n\n{extra}"
    embed = discord.Embed(
        title=with_emoji(NexusEmojis.PREMIUM, lang.get(f"premium_upsell_{feature}_title", "This feature requires Nexus Premium").format(**values)),
        description=description,
        color=await db_settings.get_color(interaction.guild_id),
    )
    embed.set_footer(text=NEXUS_FOOTER)
    try:
        await interaction.followup.send(embed=embed, view=UpsellView(lang), ephemeral=True)
    except discord.HTTPException as e:
        if not _sku_rejected(e):
            raise
        await interaction.followup.send(embed=embed, view=UpsellView(lang, with_buy=False), ephemeral=True)
