import logging
import discord
from discord import app_commands
from discord.ext import commands
from database import db_settings
from database import db_automod
from database import db_honeypot
from cogs.utils import get_lang, module_required
from cogs.honeypot import _slugify_channel_name, cleanup_honeypot
from config import NEXUS_COLOR
from emojis import NexusEmojis
from systems.hierarchy import can_apply_overwrites, format_issue
from systems.ui import NexusView, NexusModal

log = logging.getLogger(__name__)

# ─── AutoMod Rule Konfig ─────────────────────────────────────────────────────

AUTOMOD_RULES = {
    "spam": {
        "name":    "Nexus — Anti-Spam",
        "trigger": discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.spam,
        ),
        "actions_log": True,
        "actions_block": True,
    },
    "mentions": {
        "name":    "Nexus — Mention-Spam",
        "trigger": discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.mention_spam,
            mention_limit=5,
        ),
        "actions_log": True,
        "actions_block": True,
    },
    "keywords": {
        "name":    "Nexus — Keyword-Filter",
        "trigger": discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword,
            keyword_filter=[],
        ),
        "actions_log": True,
        "actions_block": True,
    },
}

# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _get_nexus_rules(guild: discord.Guild) -> dict[str, discord.AutoModRule]:
    rules = await guild.fetch_automod_rules()
    name_to_key = {
        "Nexus — Anti-Spam":      "spam",
        "Nexus — Mention-Spam":   "mentions",
        "Nexus — Keyword-Filter": "keywords",
    }
    return {
        name_to_key[rule.name]: rule
        for rule in rules
        if rule.name in name_to_key
    }

async def _get_existing_rule_by_type(
    guild: discord.Guild,
    trigger_type: discord.AutoModRuleTriggerType
) -> discord.AutoModRule | None:
    rules = await guild.fetch_automod_rules()
    for rule in rules:
        if rule.trigger.type == trigger_type:
            return rule
    return None

async def _build_actions(guild: discord.Guild, log_channel_id: int | None) -> list[discord.AutoModRuleAction]:
    actions = [
        discord.AutoModRuleAction(
            type=discord.AutoModRuleActionType.block_message,
        )
    ]
    if log_channel_id:
        actions.append(
            discord.AutoModRuleAction(
                type=discord.AutoModRuleActionType.send_alert_message,
                channel_id=log_channel_id,
            )
        )
    return actions

# ─── Views ───────────────────────────────────────────────────────────────────

class AutoModView(NexusView):
    def __init__(self, bot, guild: discord.Guild, owner_id: int, lang: dict, active_rules: dict, shield_enabled: bool, honeypot_channel_id: int | None = None, honeypot_stats: dict | None = None):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        self.bot            = bot
        self.guild          = guild
        self.active_rules   = active_rules
        self.shield_enabled = shield_enabled
        self.honeypot_channel_id = honeypot_channel_id
        self.honeypot_stats = honeypot_stats or {"timeout_days": db_honeypot.DEFAULT_TIMEOUT_DAYS, "catches": 0}
        self._sync_button_styles()

    def _sync_button_styles(self):
        spam_active = "spam" in self.active_rules and self.active_rules["spam"].enabled
        self.toggle_spam.emoji = NexusEmojis.SHIELD
        self.toggle_spam.label = f"Anti-Spam: {self.lang.get('setup_on', 'ON') if spam_active else self.lang.get('setup_off', 'OFF')}"
        self.toggle_spam.style = discord.ButtonStyle.success if spam_active else discord.ButtonStyle.secondary

        mentions_active = "mentions" in self.active_rules and self.active_rules["mentions"].enabled
        self.toggle_mentions.label = f"Mention-Spam: {self.lang.get('setup_on', 'ON') if mentions_active else self.lang.get('setup_off', 'OFF')}"
        self.toggle_mentions.emoji = NexusEmojis.Modules.BROADCAST
        self.toggle_mentions.style = discord.ButtonStyle.success if mentions_active else discord.ButtonStyle.secondary

        kw_rule = self.active_rules.get("keywords")
        is_on = kw_rule is not None and kw_rule.enabled
        self.toggle_keywords.label = f"Keyword-Filter: {self.lang.get('setup_on', 'ON') if is_on else self.lang.get('setup_off', 'OFF')}"
        self.toggle_keywords.emoji = "🔤"
        self.toggle_keywords.style = discord.ButtonStyle.success if is_on else discord.ButtonStyle.secondary

        self.toggle_shield.label = f"Anti-Nuke: {self.lang.get('setup_on', 'ON') if self.shield_enabled else self.lang.get('setup_off', 'OFF')}"
        self.toggle_shield.emoji = "🚨"
        self.toggle_shield.style = discord.ButtonStyle.success if self.shield_enabled else discord.ButtonStyle.secondary

        honeypot_active = self.honeypot_channel_id is not None
        self.toggle_honeypot.label = f"Honeypot: {self.lang.get('setup_on', 'ON') if honeypot_active else self.lang.get('setup_off', 'OFF')}"
        self.toggle_honeypot.emoji = "🍯"
        self.toggle_honeypot.style = discord.ButtonStyle.success if honeypot_active else discord.ButtonStyle.secondary

        self.remove_item(self.manage_keywords_btn)
        if kw_rule is not None:
            self.add_item(self.manage_keywords_btn)

    async def _handle_toggle(self, interaction: discord.Interaction, rule_key: str):
        await interaction.response.defer(ephemeral=True)

        if rule_key == "shield":
            new_status = not self.shield_enabled
            await db_automod.set_antinuke(self.guild.id, new_status)
            self.shield_enabled = new_status
            log.info("Anti-Nuke Shield für %s auf %s gesetzt.", self.guild.name, new_status)

            # Ohne Admin: Erkennen/Bestrafen geht, Wiederherstellen nur teilweise
            if new_status and not self.guild.me.guild_permissions.administrator:
                await interaction.followup.send(
                    self.lang.get(
                        "shield_admin_hint",
                        "⚠️ Nexus Shield is active and will detect attacks and stop the attackers. "
                        "To fully **restore** deleted roles and channels with their permissions, "
                        "Nexus needs **Administrator**. Without it, restores may be incomplete.",
                    ),
                    ephemeral=True,
                )

        else:
            self.active_rules = await _get_nexus_rules(self.guild)
            rule = self.active_rules.get(rule_key)

            if rule and rule.enabled:
                try:
                    await rule.edit(enabled=False, reason=self.lang.get("automod_rule_disabled", "Nexus AutoMod — disabled"))
                    log.info("AutoMod Regel '%s' für %s deaktiviert.", rule_key, self.guild.name)
                except discord.Forbidden:
                    await interaction.followup.send(
                        self.lang.get("automod_no_permission", "❌ I don't have permission to manage AutoMod."),
                        ephemeral=True,
                    )
                    return

            else:
                rule_conf = AUTOMOD_RULES[rule_key]
                existing  = await _get_existing_rule_by_type(self.guild, rule_conf["trigger"].type)

                if existing and not existing.name.startswith("Nexus —"):
                    await interaction.followup.send(
                        self.lang.get("automod_rule_exists", "❌ An external AutoMod rule already exists for this type."),
                        ephemeral=True
                    )
                    return

                if existing and existing.name.startswith("Nexus —"):
                    try:
                        await existing.edit(enabled=True, reason=self.lang.get("automod_rule_enabled", "Nexus AutoMod — enabled"))
                        log.info("AutoMod Regel '%s' für %s reaktiviert.", rule_key, self.guild.name)
                    except discord.Forbidden:
                        await interaction.followup.send(
                            self.lang.get("automod_no_permission", "❌ I don't have permission to manage AutoMod."),
                            ephemeral=True,
                        )
                        return

                else:
                    settings  = await db_settings.get_guild(self.guild.id)
                    log_ch_id = settings.get("log_channel_id") if settings else None
                    actions   = await _build_actions(self.guild, log_ch_id)

                    if rule_key == "keywords":
                        all_rules = await self.guild.fetch_automod_rules()
                        for r in all_rules:
                            if r.name == "Nexus — Keyword-Filter":
                                await r.delete(reason=self.lang.get("automod_rule_duplicate", "Nexus AutoMod — Altes Duplikat entfernt"))

                    try:
                        await self.guild.create_automod_rule(
                            name       = rule_conf["name"],
                            event_type = discord.AutoModRuleEventType.message_send,
                            trigger    = rule_conf["trigger"],
                            actions    = actions,
                            enabled    = True,
                            reason     = self.lang.get("automod_rule_enabled", "Nexus AutoMod — enabled"),
                        )
                        log.info("AutoMod Regel '%s' für %s neu erstellt.", rule_key, self.guild.name)
                    except discord.Forbidden:
                        await interaction.followup.send(
                            self.lang.get("automod_no_permission", "❌ I don't have permission to manage AutoMod."),
                            ephemeral=True,
                        )
                        return

        self.active_rules = await _get_nexus_rules(self.guild)
        self._sync_button_styles()
        await interaction.edit_original_response(
            embed=_automod_embed(self.active_rules, self.shield_enabled, self.lang, self.honeypot_channel_id, self.honeypot_stats),
            view=self
        )

    @discord.ui.button(custom_id="nexus_am_toggle_spam", row=0)
    async def toggle_spam(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_toggle(interaction, "spam")

    @discord.ui.button(custom_id="nexus_am_toggle_mentions", row=0)
    async def toggle_mentions(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_toggle(interaction, "mentions")

    @discord.ui.button(custom_id="nexus_am_toggle_keywords", row=0)
    async def toggle_keywords(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_toggle(interaction, "keywords")

    @discord.ui.button(custom_id="nexus_am_toggle_shield", row=1)
    async def toggle_shield(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_toggle(interaction, "shield")

    @discord.ui.button(label="🔤 Keywords verwalten", style=discord.ButtonStyle.secondary, custom_id="nexus_am_manage_kw", row=1)
    async def manage_keywords_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            KeywordModal(self.lang, self.guild, self.active_rules.get("keywords"), self)
        )

    @discord.ui.button(custom_id="nexus_am_toggle_honeypot", row=1)
    async def toggle_honeypot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.honeypot_channel_id is None:
            await interaction.response.send_modal(HoneypotModal(self.guild, self.lang, self))
            return

        await interaction.response.defer(ephemeral=True)
        await cleanup_honeypot(self.guild, self.honeypot_channel_id)
        self.honeypot_channel_id = None
        self.honeypot_stats = await db_honeypot.get_honeypot_settings(self.guild.id)
        self._sync_button_styles()
        await interaction.edit_original_response(
            embed=_automod_embed(self.active_rules, self.shield_enabled, self.lang, self.honeypot_channel_id, self.honeypot_stats),
            view=self,
        )

# ─── Modal ───────────────────────────────────────────────────────────────────

class HoneypotModal(NexusModal):
    def __init__(self, guild: discord.Guild, lang: dict, parent_view: AutoModView):
        super().__init__(lang, title=lang.get("honeypot_modal_title", "Create Honeypot"))
        self.guild       = guild
        self.parent_view = parent_view

        self.name_input = discord.ui.TextInput(
            label=lang.get("honeypot_modal_name_label", "Channel Name"),
            placeholder=lang.get("honeypot_modal_name_placeholder", "e.g. dont-write-here"),
            max_length=90,
            required=True,
        )
        self.warntext_input = discord.ui.TextInput(
            label=lang.get("honeypot_modal_warntext_label", "Warning Text in Channel"),
            style=discord.TextStyle.paragraph,
            placeholder=lang.get(
                "honeypot_modal_warntext_placeholder",
                "⚠️ Raid bot trap: Anyone posting here will be automatically timed out.",
            ),
            max_length=1000,
            required=True,
        )
        self.add_item(self.name_input)
        self.add_item(self.warntext_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        overwrites = {
            self.guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            self.guild.me:           discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True,
                embed_links=True, read_message_history=True,
            ),
        }

        check = can_apply_overwrites(self.guild, overwrites)
        if not check:
            await interaction.followup.send(format_issue(check, self.lang), ephemeral=True)
            return

        try:
            channel = await self.guild.create_text_channel(
                name=_slugify_channel_name(self.name_input.value),
                overwrites=overwrites,
                reason="[Nexus Honeypot] Erstellt",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                self.lang.get("setup_right_error", "I am missing the 'Manage Channels' permission."),
                ephemeral=True,
            )
            return

        try:
            await channel.edit(position=0)
        except discord.HTTPException:
            pass

        color = await db_settings.get_color(self.guild.id)
        embed = discord.Embed(
            title=self.lang.get("honeypot_channel_title", "⚠️ Warning"),
            description=self.warntext_input.value,
            color=color,
        )
        embed.set_footer(text="Nexus • Honeypot")
        message_id = None
        try:
            sent = await channel.send(embed=embed)
            message_id = sent.id
        except discord.HTTPException:
            log.warning("[HONEYPOT] Konnte Warn-Embed nicht in %s posten.", channel)

        await db_settings.update_guild(self.guild.id, honeypot_channel_id=channel.id)
        await db_honeypot.update_honeypot_settings(self.guild.id, message_id=message_id)

        self.parent_view.honeypot_channel_id = channel.id
        self.parent_view.honeypot_stats = await db_honeypot.get_honeypot_settings(self.guild.id)
        self.parent_view._sync_button_styles()

        await interaction.followup.edit_message(
            message_id="@original",
            embed=_automod_embed(
                self.parent_view.active_rules, self.parent_view.shield_enabled, self.lang,
                channel.id, self.parent_view.honeypot_stats,
            ),
            view=self.parent_view,
        )
        await interaction.followup.send(
            self.lang.get("honeypot_created", "{nexus_checkmark} Honeypot set up in {channel}.").format(
                nexus_checkmark=NexusEmojis.CHECKMARK,
                channel=channel.mention,
            ),
            ephemeral=True,
        )

class KeywordModal(NexusModal):
    def __init__(self, lang: dict, guild: discord.Guild, rule: discord.AutoModRule | None, parent_view: AutoModView):
        super().__init__(lang, title=lang.get("automod_keywords_title", "Manage Keywords"))
        self.guild       = guild
        self.rule        = rule
        self.parent_view = parent_view

        current_keywords = ""
        if rule and rule.trigger.keyword_filter:
            current_keywords = "\n".join(rule.trigger.keyword_filter)

        self.keywords_input = discord.ui.TextInput(
            label=lang.get("automod_keywords_label", "Blocked words (one per line)"),
            style=discord.TextStyle.paragraph,
            placeholder=lang.get("automod_placeholder", "spam\nwadvertisement\nbad_word"),
            default=current_keywords,
            max_length=1000,
            required=False,
        )
        self.add_item(self.keywords_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        keywords = [
            k.strip().lower()
            for k in self.keywords_input.value.split("\n")
            if k.strip()
        ]

        try:
            if self.rule:
                await self.rule.edit(
                    trigger=discord.AutoModTrigger(
                        type=discord.AutoModRuleTriggerType.keyword,
                        keyword_filter=keywords,
                    ),
                    reason=self.lang.get("automod_rule_updated", "Nexus AutoMod — Keywords updated"),
                )

            self.parent_view.active_rules = await _get_nexus_rules(self.guild)
            self.parent_view._sync_button_styles()

            await interaction.followup.edit_message(
                message_id="@original",
                embed=_automod_embed(
                    self.parent_view.active_rules, self.parent_view.shield_enabled, self.lang,
                    self.parent_view.honeypot_channel_id, self.parent_view.honeypot_stats,
                ),
                view=self.parent_view
            )

            await interaction.followup.send(
                self.lang.get("automod_keywords_saved", "{nexus_checkmark} {count} keywords saved.").format(
                    nexus_checkmark=NexusEmojis.CHECKMARK,
                    count=len(keywords)
                ),
                ephemeral=True,
            )

        except discord.Forbidden:
            await interaction.followup.send(
                self.lang.get("automod_no_permission", "❌ I don't have permission to manage AutoMod."),
                ephemeral=True,
            )

# ─── Embed ───────────────────────────────────────────────────────────────────

def _automod_embed(active_rules: dict, shield_enabled: bool, lang: dict, honeypot_channel_id: int | None = None, honeypot_stats: dict | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=lang.get("automod_title", "🛡️ AutoMod Configuration"),
        description=lang.get("automod_desc", "Manage the automatic moderation protection for this server."),
        color=NEXUS_COLOR,
    )

    rules_info = {
        "spam":     ("🛡️ Anti-Spam",      lang.get("automod_spam_desc",    "Blocks automatic spam.")),
        "mentions": ("📢 Mention-Spam",   lang.get("automod_mention_desc", "Blocks too many mentions (max. 5).")),
        "keywords": ("🔤 Keyword-Filter", lang.get("automod_keyword_desc", "Blocks specific words and phrases.")),
    }

    for key, (name, desc) in rules_info.items():
        rule = active_rules.get(key)
        is_active = rule is not None and rule.enabled

        status = f"{NexusEmojis.CHECKMARK} {lang.get('module_active', 'Active')}" if is_active else f"{lang.get('module_inactive', '❌ Disabled')}"

        if key == "keywords" and is_active:
            count  = len(rule.trigger.keyword_filter or [])
            status += f" ({count} Keywords)"

        embed.add_field(name=f"{name}", value=f"{status}\n{desc}", inline=False)

    shield_status = f"{NexusEmojis.CHECKMARK} {lang.get('module_active', 'Active')}" if shield_enabled else f"{lang.get('module_inactive', '❌ Disabled')}"
    shield_desc = lang.get("automod_shield_desc", "Monitors audit logs with risk-scoring against rapid server nukes.")
    embed.add_field(name="🚨 Anti-Nuke Shield", value=f"{shield_status}\n{shield_desc}", inline=False)

    stats = honeypot_stats or {}
    days = stats.get("timeout_days", db_honeypot.DEFAULT_TIMEOUT_DAYS)
    honeypot_active = honeypot_channel_id is not None
    honeypot_status = f"{NexusEmojis.CHECKMARK} {lang.get('module_active', 'Active')} — <#{honeypot_channel_id}>" if honeypot_active else f"{lang.get('module_inactive', '❌ Disabled')}"
    honeypot_desc = lang.get("automod_honeypot_desc", "Anyone posting here will be timed out for {days} days.").format(days=days)
    honeypot_value = f"{honeypot_status}\n{honeypot_desc}"
    if honeypot_active and stats.get("catches"):
        honeypot_value += "\n" + lang.get("automod_honeypot_caught", "🍯 {count} caught so far").format(count=f"{stats['catches']:,}")
    embed.add_field(name="🍯 Honeypot", value=honeypot_value, inline=False)

    embed.set_footer(text="Nexus • AutoMod")
    return embed

# ─── Cog ─────────────────────────────────────────────────────────────────────

class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="automod", description=app_commands.locale_str("cmd_automod_desc"))
    @app_commands.default_permissions(administrator=True)
    @module_required("automod")
    async def automod(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        lang         = await get_lang(interaction.guild_id)
        active_rules = await _get_nexus_rules(interaction.guild)

        settings       = await db_settings.get_guild(interaction.guild_id)
        shield_enabled = bool(await db_automod.get_antinuke(interaction.guild_id))
        honeypot_channel_id = settings.get("honeypot_channel_id") if settings else None
        honeypot_stats = await db_honeypot.get_honeypot_settings(interaction.guild_id)

        view = AutoModView(
            self.bot,
            interaction.guild,
            interaction.user.id,
            lang,
            active_rules,
            shield_enabled,
            honeypot_channel_id,
            honeypot_stats,
        )
        await interaction.followup.send(
            embed=_automod_embed(active_rules, shield_enabled, lang, honeypot_channel_id, honeypot_stats),
            view=view,
        )
        view.bind(interaction)

async def setup(bot):
    await bot.add_cog(AutoMod(bot))