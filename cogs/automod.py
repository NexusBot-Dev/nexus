import logging
from dataclasses import dataclass
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from database import db_automod
from database import db_honeypot
from cogs.utils import get_lang, module_required
from systems import honeypot as hp_system
from systems import automod_escalation, automod_rules, automod_log, automod_links, shield_prompts, shield_whitelist
from config import DASHBOARD_URL, NEXUS_COLOR
from emojis import NexusEmojis
from systems.ui import NexusView, NexusModal, error_msg, with_emoji

log = logging.getLogger(__name__)

# ─── Zustand ─────────────────────────────────────────────────────────────────
# /automod ist eine Übersicht mit Schnellzugriff: alle Regeln mit Status und Kurzzeile, an/aus per
# Auswahlmenü, Anti-Nuke per Button, Wortfilter per Fenster. Alle Feineinstellungen (Hinweis, Ausnahmen,
# Timeout, Wortlisten, Links, Whitelist, Eskalation) im Dashboard — der Link-Button führt direkt hin.

@dataclass
class _State:
    kinds: tuple[str, ...]          # Testphase: nur die drei bisherigen Regeln
    rules: dict                     # Nexus-Regeln nach Art
    foreign: dict                   # fremde Regeln auf Einzelplätzen
    shield_enabled: bool
    whitelist: db_automod.ShieldWhitelist
    escalation: dict | None         # None: Testphase
    honeypot_channel_id: int | None
    honeypot_stats: dict
    link_settings: dict | None      # nur mit Link-Filter


async def _load_state(guild: discord.Guild, lang: dict) -> _State:
    overview = await automod_rules.get_overview(guild, lang)
    v2 = automod_rules.v2_available(guild.id)
    honeypot_channel = await hp_system.get_channel(guild)   # von Hand gelöscht → inaktiv
    return _State(
        kinds=automod_rules.RULE_KINDS if v2 else LEGACY_KINDS,
        rules=overview.nexus,
        foreign=overview.foreign,
        shield_enabled=bool(await db_automod.get_antinuke(guild.id)),
        whitelist=await db_automod.get_shield_whitelist(guild.id),
        escalation=await db_automod.get_escalation(guild.id) if v2 else None,
        honeypot_channel_id=honeypot_channel.id if honeypot_channel else None,
        honeypot_stats=await db_honeypot.get_honeypot_settings(guild.id),
        link_settings=await db_automod.get_link_settings(guild.id) if "links" in overview.nexus else None,
    )


# ─── Embed ───────────────────────────────────────────────────────────────────

LEGACY_KINDS = ("spam", "mentions", "keywords")
_RULE_EMOJIS = {"spam": "🛡️", "mentions": "📢", "keywords": "🔤", "presets": "📚", "links": "🔗", "names": "🪪"}


def _count(lang: dict, key: str, count: int, fallback: str, fallback_one: str) -> str:
    if count == 1:
        return lang.get(f"{key}_one", fallback_one)
    return lang.get(key, fallback).format(count=count)


def _rule_summary(kind: str, rule: discord.AutoModRule, lang: dict, link_settings: dict | None) -> str:
    """Kurzzeile wie im Dashboard, z. B. „12 Wörter · 2 Rollen ausgenommen · Timeout 10 Min.“."""
    settings = automod_rules.read_settings(kind, rule)
    content = automod_rules.read_content(kind, rule)
    parts: list[str] = []
    if kind == "mentions":
        parts.append(lang.get("automod_summary_limit", "max. {count} mentions").format(count=settings["mention_limit"]))
    if kind in ("keywords", "names") and content["keywords"]:
        parts.append(_count(lang, "automod_summary_words", len(content["keywords"]), "{count} words", "1 word"))
    if kind == "presets" and content["presets"]:
        parts.append(", ".join(lang.get(f"automod_preset_{p}", p) for p in content["presets"]))
    if kind == "links" and link_settings:
        if link_settings["block_invites"]:
            parts.append(lang.get("automod_summary_invites", "Invites blocked"))
        if domains := len(link_settings["blocked_domains"]):
            parts.append(_count(lang, "automod_summary_domains", domains, "{count} domains", "1 domain"))
    if roles := len(settings["exempt_role_ids"]):
        parts.append(_count(lang, "automod_summary_roles", roles, "{count} roles exempt", "1 role exempt"))
    if settings["timeout_minutes"]:
        parts.append(lang.get("automod_summary_timeout", "Timeout {duration}").format(
            duration=automod_log._duration(timedelta(minutes=settings["timeout_minutes"]), lang),
        ))
    return " · ".join(parts)


def _status(lang: dict, active: bool) -> str:
    return f"{NexusEmojis.CHECKMARK} {lang.get('module_active', 'Active')}" if active else f"{NexusEmojis.OFF} {lang.get('module_inactive', 'Disabled')}"


def _automod_embed(state: _State, lang: dict) -> discord.Embed:
    embed = discord.Embed(
        title=lang.get("automod_title", "🛡️ AutoMod Configuration"),
        description=lang.get(
            "automod_desc_overview",
            "Turn rules on and off here. All settings (notice, exemptions, timeout, word lists, escalation) "
            "are in the dashboard.",
        ),
        color=NEXUS_COLOR,
    )

    lines = []
    for kind in state.kinds:
        name = f"{_RULE_EMOJIS[kind]} **{automod_rules.rule_name(kind, lang)}**"
        rule = state.rules.get(kind)
        if rule is None and kind in state.foreign:
            line = f"{NexusEmojis.OFF} {name} — " + lang.get(
                "automod_rule_foreign", "taken by the server's own rule “{name}”",
            ).format(name=discord.utils.escape_markdown(state.foreign[kind].name))
        else:
            active = rule is not None and rule.enabled
            line = f"{NexusEmojis.CHECKMARK if active else NexusEmojis.OFF} {name}"
            if rule is not None and (summary := _rule_summary(kind, rule, lang, state.link_settings)):
                line += f" — {summary}"
        lines.append(line)
    embed.add_field(name=lang.get("automod_rules_title", "Rules"), value="\n".join(lines)[:1024], inline=False)

    shield_value = _status(lang, state.shield_enabled)
    if state.whitelist.bots or state.whitelist.roles:
        shield_value += "\n" + lang.get("automod_shield_whitelist", "Whitelist: {bots} bots, {roles} roles").format(
            bots=len(state.whitelist.bots), roles=len(state.whitelist.roles),
        )
    shield_value += "\n" + lang.get("automod_shield_desc", "Monitors audit logs with risk-scoring against rapid server nukes.")
    embed.add_field(name="🚨 Anti-Nuke Shield", value=shield_value, inline=False)

    if state.escalation is not None:
        steps = len(state.escalation["steps"])
        active = state.escalation["enabled"] and steps > 0
        value = _status(lang, active)
        if active:
            value += " — " + _count(lang, "automod_escalation_levels", steps, "{count} levels", "1 level")
        embed.add_field(name=f"⚖️ {lang.get('automod_escalation_title', 'Escalation')}", value=value, inline=False)

    # Honeypot: nur Überblick — eingerichtet und bearbeitet wird er mit /honeypot
    stats = state.honeypot_stats
    days = stats.get("timeout_days", db_honeypot.DEFAULT_TIMEOUT_DAYS)
    honeypot_value = _status(lang, state.honeypot_channel_id is not None)
    if state.honeypot_channel_id is not None:
        honeypot_value += f" — <#{state.honeypot_channel_id}>\n" + lang.get("automod_honeypot_punish", "👤 {members} · 🤖 {bots}").format(
            members=hp_system.punishment_label(lang, stats.get("punish_members", db_honeypot.DEFAULT_PUNISH_MEMBERS), days),
            bots=hp_system.punishment_label(lang, stats.get("punish_bots", db_honeypot.DEFAULT_PUNISH_BOTS), days),
        )
        if stats.get("catches"):
            honeypot_value += "\n" + lang.get("automod_honeypot_caught", "🍯 {count} caught so far").format(count=f"{stats['catches']:,}")
    honeypot_value += "\n" + lang.get("automod_honeypot_manage", "Set up and edit with `/honeypot`.")
    embed.add_field(name="🍯 Honeypot", value=honeypot_value, inline=False)

    embed.set_footer(text="Nexus • AutoMod")
    return embed


# ─── View ────────────────────────────────────────────────────────────────────

class AutoModView(NexusView):
    def __init__(self, guild: discord.Guild, owner_id: int, lang: dict, state: _State):
        super().__init__(lang, owner_id=owner_id, require=None, timeout=300)
        self.guild = guild
        self.state = state
        self._build()

    def _build(self) -> None:
        """Komponenten passend zum Zustand neu aufbauen (Auswahl zeigt den aktuellen Status jeder Regel)."""
        self.clear_items()
        lang, state = self.lang, self.state

        options = []
        for kind in state.kinds:
            rule = state.rules.get(kind)
            if rule is None and kind in state.foreign:
                description = lang.get("automod_select_foreign", "Taken by a rule of the server — adopt it in the dashboard")
                emoji = NexusEmojis.OFF
            elif rule is not None and rule.enabled:
                description, emoji = lang.get("automod_select_on", "Active — select to turn off"), NexusEmojis.CHECKMARK
            else:
                description, emoji = lang.get("automod_select_off", "Off — select to turn on"), NexusEmojis.OFF
            options.append(discord.SelectOption(
                label=automod_rules.rule_name(kind, lang), value=kind, description=description[:100], emoji=emoji,
            ))
        rule_select = discord.ui.Select(
            custom_id="nexus_am_rule_select", row=0, options=options,
            placeholder=lang.get("automod_select_placeholder", "Turn a rule on or off"),
        )
        rule_select.callback = self._rule_chosen
        self.add_item(rule_select)

        shield = discord.ui.Button(
            custom_id="nexus_am_toggle_shield", row=1, emoji="🚨",
            label=f"Anti-Nuke: {lang.get('setup_on', 'ON') if state.shield_enabled else lang.get('setup_off', 'OFF')}",
            style=discord.ButtonStyle.success if state.shield_enabled else discord.ButtonStyle.secondary,
        )
        shield.callback = self._toggle_shield
        self.add_item(shield)

        if "keywords" in state.rules:
            keywords = discord.ui.Button(
                custom_id="nexus_am_manage_kw", row=1, emoji="🔤", style=discord.ButtonStyle.secondary,
                label=lang.get("automod_manage_keywords", "Manage keywords"),
            )
            keywords.callback = self._manage_keywords
            self.add_item(keywords)

        self.add_item(discord.ui.Button(
            row=1, style=discord.ButtonStyle.link, url=f"{DASHBOARD_URL}/server/{self.guild.id}/automod",
            label=lang.get("automod_open_dashboard", "Edit in dashboard"),
        ))

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Zustand neu laden und Nachricht aktualisieren (Interaction ist schon beantwortet)."""
        self.state = await _load_state(self.guild, self.lang)
        self._build()
        await interaction.edit_original_response(embed=_automod_embed(self.state, self.lang), view=self)

    async def _rule_chosen(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        kind = interaction.data["values"][0]
        try:
            rule = (await automod_rules.get_nexus_rules(self.guild, self.lang)).get(kind)
            if rule and rule.enabled:
                await rule.edit(enabled=False, reason=self.lang.get("automod_rule_disabled", "Nexus AutoMod — disabled"))
                log.info("AutoMod Regel '%s' für %s deaktiviert.", kind, self.guild.name)
            else:
                await automod_rules.enable_rule(
                    self.guild, kind, self.lang, reason=self.lang.get("automod_rule_enabled", "Nexus AutoMod — enabled"),
                )
                log.info("AutoMod Regel '%s' für %s aktiviert.", kind, self.guild.name)
        except automod_rules.ForeignRuleError:
            message = self.lang.get("automod_rule_exists", "An external AutoMod rule already exists for this type.")
        except automod_rules.SettingsError as e:
            message = self.lang.get(e.key, e.key).format(**e.params)
        except discord.Forbidden:
            message = self.lang.get("automod_no_permission", "I don't have permission to manage AutoMod.")
        else:
            await self.refresh(interaction)
            return
        await interaction.followup.send(error_msg(message), ephemeral=True)

    async def _toggle_shield(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        current = bool(await db_automod.get_antinuke(self.guild.id))   # kann sich im Dashboard geändert haben
        # Ausschalten nur durch den Besitzer: sonst hebelt ein gekaperter Admin das Shield vor dem Angriff aus
        if current and not shield_whitelist.is_owner(self.guild, interaction.user):
            await interaction.followup.send(
                error_msg(self.lang.get("shield_owner_only", "Only the server owner can turn off the Anti-Nuke Shield.")),
                ephemeral=True,
            )
            return
        await db_automod.set_antinuke(self.guild.id, not current)
        log.info("Anti-Nuke Shield für %s auf %s gesetzt.", self.guild.name, not current)
        await self.refresh(interaction)
        if current:
            return
        # Ohne Admin: Erkennen/Bestrafen geht, Wiederherstellen nur teilweise
        if not self.guild.me.guild_permissions.administrator:
            await interaction.followup.send(
                with_emoji(NexusEmojis.WARNING, self.lang.get(
                    "shield_admin_hint",
                    "Nexus Shield is active and will detect attacks and stop the attackers. "
                    "To fully **restore** deleted roles and channels with their permissions, "
                    "Nexus needs **Administrator**. Without it, restores may be incomplete.",
                )),
                ephemeral=True,
            )
        # Bots mit Mod-Rechten gleich zur Whitelist anbieten (systems/shield_prompts.py)
        await shield_prompts.offer_after_enable(interaction, self.lang)

    async def _manage_keywords(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(KeywordModal(self.lang, self.guild, self.state.rules.get("keywords"), self))


# ─── Modal ───────────────────────────────────────────────────────────────────

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
            await self.parent_view.refresh(interaction)
            await interaction.followup.send(
                self.lang.get("automod_keywords_saved", "{nexus_checkmark} {count} keywords saved.").format(
                    nexus_checkmark=NexusEmojis.CHECKMARK,
                    count=len(keywords)
                ),
                ephemeral=True,
            )

        except discord.Forbidden:
            await interaction.followup.send(
                error_msg(self.lang.get("automod_no_permission", "I don't have permission to manage AutoMod.")),
                ephemeral=True,
            )

# ─── Cog ─────────────────────────────────────────────────────────────────────

class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.link_sync_loop.start()
        self.strike_cleanup_loop.start()

    async def cog_unload(self):
        self.link_sync_loop.cancel()
        self.strike_cleanup_loop.cancel()

    # Eskalation: Strikes verfallen nach 30 Tagen (systems/automod_escalation.py)
    @tasks.loop(hours=24)
    async def strike_cleanup_loop(self):
        try:
            if removed := await automod_escalation.cleanup():
                log.info("AutoMod: %s abgelaufene Strikes gelöscht.", removed)
        except Exception:
            log.exception("AutoMod: Strikes nicht aufgeräumt.")

    @strike_cleanup_loop.before_loop
    async def _before_strike_cleanup(self):
        await self.bot.wait_until_ready()

    # Link-Filter: eigene Einladungen erlauben (systems/automod_links.py). Discord meldet neue und gelöschte
    # Einladungen, aber keine abgelaufenen — die räumt der regelmäßige Abgleich auf.
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if isinstance(invite.guild, discord.Guild):
            automod_links.schedule_sync(invite.guild)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if isinstance(invite.guild, discord.Guild):
            automod_links.schedule_sync(invite.guild)

    @tasks.loop(hours=6)
    async def link_sync_loop(self):
        await automod_links.sync_all(self.bot)

    @link_sync_loop.before_loop
    async def _before_link_sync(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_automod_action(self, execution: discord.AutoModAction):
        # Discord meldet jeden Eingriff einer AutoMod-Regel — Nexus schreibt daraus sein eigenes Log
        await automod_log.handle_action(execution)

    @app_commands.command(name="automod", description=app_commands.locale_str("cmd_automod_desc"))
    @app_commands.default_permissions(administrator=True)
    @module_required("automod")
    async def automod(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        lang  = await get_lang(interaction.guild_id)
        state = await _load_state(interaction.guild, lang)
        view  = AutoModView(interaction.guild, interaction.user.id, lang, state)
        await interaction.followup.send(embed=_automod_embed(state, lang), view=view)
        view.bind(interaction)

async def setup(bot):
    await bot.add_cog(AutoMod(bot))