"""Nachgebautes Discord für die Tests: Server, Mitglieder, Rollen, Kanäle, Nachrichten.

Nur so viel, wie der Bot-Code tatsächlich benutzt. Alles, was der Bot bei Discord *tun* würde
(Kanal anlegen, Nachricht senden/bearbeiten/löschen), landet in `guild.calls` — die Tests prüfen das.
Neue Module brauchen evtl. weitere Methoden: dann hier ergänzen, nicht im Test zusammenbasteln.

FakeGuild erbt von discord.Guild, weil der Bot-Code an einigen Stellen isinstance(x, discord.Guild) prüft
(z. B. systems.hierarchy.missing_perms). Das echte __init__ wird nie aufgerufen; alles, was der Code liest,
ist hier überschrieben.
"""
import io
import itertools
import types
from datetime import timedelta

import discord
from PIL import Image

_ids = itertools.count(100_000_000_000_000_000)


def next_id() -> int:
    return next(_ids)


_message_seq = itertools.count()


def message_id(created_at=None) -> int:
    """Nachrichten-IDs wie bei Discord mit Zeitstempel — der Bot rechnet z. B. die Stream-Dauer daraus aus."""
    return discord.utils.time_snowflake(created_at or discord.utils.utcnow()) + next(_message_seq) % 4096


def _http_error(cls, status: int, code: int = 0, message: str = "fake"):
    response = types.SimpleNamespace(status=status, reason=message)
    return cls(response, {"code": code, "message": message})


def not_found(code: int = 10008):
    return _http_error(discord.NotFound, 404, code, "Unknown")


def forbidden(code: int = 50013):
    return _http_error(discord.Forbidden, 403, code, "Missing Permissions")


def bad_request(code: int = 0, message: str = "Bad Request"):
    return _http_error(discord.HTTPException, 400, code, message)


class FakeRole:
    def __init__(self, guild, name: str, position: int, permissions: discord.Permissions | None = None, id: int | None = None):
        self.id = id or next_id()
        self.guild = guild
        self.name = name
        self.position = position
        self.permissions = permissions or discord.Permissions.none()
        self.managed = False
        self.color = discord.Color.default()
        self.hoist = self.mentionable = False

    @property
    def mention(self):
        return f"<@&{self.id}>"

    def is_default(self):
        return self.id == self.guild.id

    @property
    def members(self):
        return [m for m in self.guild.members if self in m.roles]

    def __lt__(self, other):
        return self.position < other.position

    def __gt__(self, other):
        return self.position > other.position

    def __ge__(self, other):
        return self.position >= other.position

    def __le__(self, other):
        return self.position <= other.position


class FakeAsset:
    """Avatar wie bei discord.py: Format und Größe stehen in der URL, read() lädt das Bild.
    image: was read() liefert (Standard: ein kleines PNG); fail: read() scheitert wie ein CDN-Fehler."""

    def __init__(self, url: str, *, image: bytes | None = None, fail: bool = False):
        self.url, self.image, self.fail = url, image, fail
        self.reads: list[str] = []   # URLs, die gelesen wurden

    def _replace(self, url: str) -> "FakeAsset":
        clone = FakeAsset(url, image=self.image, fail=self.fail)
        clone.reads = self.reads
        return clone

    def with_format(self, fmt: str) -> "FakeAsset":
        base, _, query = self.url.partition("?")
        return self._replace(f"{base.rsplit('.', 1)[0]}.{fmt}" + (f"?{query}" if query else ""))

    def with_size(self, size: int) -> "FakeAsset":
        return self._replace(f"{self.url.partition('?')[0]}?size={size}")

    async def read(self) -> bytes:
        self.reads.append(self.url)
        if self.fail:
            raise not_found(0)
        if self.image is None:
            buffer = io.BytesIO()
            Image.new("RGB", (128, 128), (200, 60, 90)).save(buffer, "PNG")
            self.image = buffer.getvalue()
        return self.image


class FakeMember:
    def __init__(self, guild, name: str, roles: list[FakeRole], *, bot: bool = False, id: int | None = None):
        self.id = id or next_id()
        self.guild = guild
        self.name = self.display_name = name
        self.bot = bot
        self.roles = [guild.default_role, *roles]
        self.voice = None   # VoiceState, solange das Mitglied in einem Voice-Kanal ist (FakeVoiceChannel.join)
        self.display_avatar = FakeAsset(f"https://cdn.discordapp.com/embed/avatars/{self.id % 6}.png")
        self.default_avatar = FakeAsset(f"https://cdn.discordapp.com/embed/avatars/{self.id % 6}.png")

    @property
    def mention(self):
        return f"<@{self.id}>"

    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position)

    @property
    def guild_permissions(self) -> discord.Permissions:
        if self.guild.owner_id == self.id:
            return discord.Permissions.all()
        value = 0
        for role in self.roles:
            value |= role.permissions.value
        perms = discord.Permissions(value)
        return discord.Permissions.all() if perms.administrator else perms

    async def timeout(self, until, *, reason=None):
        """Wie discord.Member.timeout: Discord lehnt ein Ende nach mehr als 28 Tagen ab."""
        if until is not None and until - discord.utils.utcnow() > timedelta(days=28):
            raise bad_request(50035, "Invalid communication_disabled_until")
        self.timed_out_until = until
        self.guild.calls.append(("member.timeout", self.id, until))

    async def send(self, content=None, *, embed=None, **kwargs):
        """DM — dms_closed: der User hat DMs vom Server abgeschaltet."""
        if getattr(self, "dms_closed", False):
            raise forbidden(50007)
        self.guild.calls.append(("member.dm", self.id, embed or content))

    def __str__(self):
        return self.name


class FakeMessage:
    def __init__(self, channel, author, *, content=None, embeds=None, id: int | None = None):
        self.id = id or message_id()
        self.channel = channel
        self.guild = channel.guild
        self.author = author
        self.content = content
        self.embeds = list(embeds or [])
        self.reactions: list[str] = []   # Emojis, die der Bot unter die Nachricht gesetzt hat
        self.view = None

    async def edit(self, **kwargs):
        if self.id not in self.channel.messages:
            raise not_found(10008)
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]] if kwargs["embed"] else []
        if "content" in kwargs:
            self.content = kwargs["content"]
        if "view" in kwargs:
            self.view = kwargs["view"]
        self.guild.calls.append(("message.edit", self.id, kwargs))
        return self

    async def add_reaction(self, emoji):
        emoji = str(emoji)
        if not self.guild.is_usable_emoji(emoji):
            raise bad_request(10014, "Unknown Emoji")
        if emoji not in self.reactions:
            self.reactions.append(emoji)
        self.guild.calls.append(("message.add_reaction", self.id, emoji))

    async def clear_reaction(self, emoji):
        emoji = str(emoji)
        if emoji in self.reactions:
            self.reactions.remove(emoji)
        self.guild.calls.append(("message.clear_reaction", self.id, emoji))

    async def clear_reactions(self):
        self.reactions.clear()
        self.guild.calls.append(("message.clear_reactions", self.id))

    async def delete(self, **kwargs):
        self.channel.messages.pop(self.id, None)
        self.guild.calls.append(("message.delete", self.id))


class FakeTextChannel(discord.TextChannel):
    """Erbt von discord.TextChannel, weil der Code isinstance(channel, discord.TextChannel) prüft.

    bot_denied: Rechte, die der Bot in diesem Kanal NICHT hat (z. B. {"send_messages"}), unabhängig von
    seinen Server-Rechten — statt echter Overwrites, die hier nicht ausgewertet werden."""

    def __init__(self, guild, name: str, *, overwrites=None, position: int = 0, id: int | None = None,
                 bot_denied: set[str] | None = None):
        # discord.TextChannel.__init__ absichtlich nicht aufrufen (braucht Gateway-Daten)
        self.id = id or next_id()
        self.guild = guild
        self.name = name
        self.position = position
        self.category_id = None
        self._fake_overwrites = dict(overwrites or {})
        self.messages: dict[int, FakeMessage] = {}
        self.bot_denied = set(bot_denied or ())

    @property
    def overwrites(self):
        return self._fake_overwrites

    @property
    def type(self):
        return discord.ChannelType.text

    @property
    def mention(self):
        return f"<#{self.id}>"

    def permissions_for(self, member) -> discord.Permissions:
        perms = member.guild_permissions
        if member is self.guild.me and self.bot_denied:
            perms = discord.Permissions(perms.value)
            perms.update(**{name: False for name in self.bot_denied})
        return perms

    async def send(self, content=None, *, embed=None, embeds=None, **kwargs):
        if not self.permissions_for(self.guild.me).send_messages:
            raise forbidden(50013)
        message = FakeMessage(self, self.guild.me, content=content, embeds=[embed] if embed else embeds)
        message.view = kwargs.get("view")
        message.allowed_mentions = kwargs.get("allowed_mentions")
        self.messages[message.id] = message
        self.guild.calls.append(("channel.send", self.id, message))
        return message

    async def fetch_message(self, message_id: int):
        try:
            return self.messages[message_id]
        except KeyError:
            raise not_found(10008) from None

    def history(self, *, limit=100, oldest_first=False, **kwargs):
        ordered = sorted(self.messages.values(), key=lambda m: m.id, reverse=not oldest_first)[:limit]

        async def iterate():
            for message in ordered:
                yield message
        return iterate()

    async def edit(self, **kwargs):
        if "position" in kwargs:
            self.position = kwargs["position"]
        if "name" in kwargs:
            self.name = kwargs["name"]
        self.guild.calls.append(("channel.edit", self.id, kwargs))
        return self

    async def delete(self, reason=None):
        self.guild.channels_by_id.pop(self.id, None)
        self.guild.calls.append(("channel.delete", self.id))

    def add_message(self, *, author=None, embeds=None) -> FakeMessage:
        """Für den Testaufbau: eine Nachricht, die schon im Kanal steht (Standard: vom Bot)."""
        message = FakeMessage(self, author or self.guild.me, embeds=embeds)
        self.messages[message.id] = message
        return message

    def __str__(self):
        return f"#{self.name}"

    def __repr__(self):
        return f"<FakeTextChannel #{self.name} {self.id}>"


class FakeVoiceChannel(discord.VoiceChannel):
    """Voice-Kanal mit Text-Chat. Mitglieder kommen per join() hinein, mit Mute-/Deaf-Status."""

    def __init__(self, guild, name: str, *, position: int = 0, category_id: int | None = None):
        self.id = next_id()
        self.guild = guild
        self.name = name
        self.position = position
        self.category_id = category_id
        self.messages: dict[int, FakeMessage] = {}
        self.bot_denied: set[str] = set()
        self._members: list = []

    @property
    def members(self):
        return list(self._members)

    @property
    def type(self):
        return discord.ChannelType.voice

    @property
    def mention(self):
        return f"<#{self.id}>"

    def join(self, member, *, self_mute=False, mute=False, self_deaf=False, deaf=False):
        member.voice = types.SimpleNamespace(channel=self, self_mute=self_mute, mute=mute, self_deaf=self_deaf, deaf=deaf)
        self._members.append(member)
        return member

    permissions_for = FakeTextChannel.permissions_for
    send = FakeTextChannel.send

    def __repr__(self):
        return f"<FakeVoiceChannel {self.name} {self.id}>"


class FakeGuild(discord.Guild):
    """Ein Server mit Owner, einem Admin, einem normalen Mitglied und dem Bot.

    bot_permissions: Rechte der Bot-Rolle (Standard: alles, was Nexus beim Einladen anfragt, ohne Admin).
    """

    def __init__(self, *, name: str = "Testserver", locale: str = "de", bot_permissions: discord.Permissions | None = None):
        # discord.Guild.__init__ absichtlich nicht aufrufen (braucht Gateway-Daten)
        self.id = next_id()
        self.name = name
        self.owner_id = 0
        self.preferred_locale = discord.Locale.german if locale == "de" else discord.Locale.american_english
        self.calls: list[tuple] = []
        self.channels_by_id: dict[int, FakeTextChannel] = {}
        self.unavailable = False   # True: Discord-Störung, Server-Daten fehlen
        self.vanity_url_code: str | None = None
        self.invite_list: list = []   # Einladungen (siehe add_invite)
        self.automod_rules: list = []
        self.automod_fetches = 0
        self.emoji_ids: set[int] = set()   # eigene Server-Emojis (für add_reaction)

        self._default_role = FakeRole(self, "@everyone", 0, discord.Permissions.none(), id=self.id)
        self.admin_role = FakeRole(self, "Admin", 10, discord.Permissions(administrator=True))
        self.mod_role = FakeRole(self, "Mod", 5, discord.Permissions(manage_guild=True))
        self.bot_role = FakeRole(self, "Nexus", 8, bot_permissions or discord.Permissions(
            manage_channels=True, manage_roles=True, manage_messages=True, view_channel=True,
            send_messages=True, embed_links=True, read_message_history=True, kick_members=True,
            ban_members=True, moderate_members=True, manage_guild=True, add_reactions=True, create_events=True,
        ))
        self.role_list = [self._default_role, self.mod_role, self.bot_role, self.admin_role]

        self.owner_member = FakeMember(self, "owner", [])
        self.owner_id = self.owner_member.id
        self.admin = FakeMember(self, "admin", [self.admin_role])
        self.moderator = FakeMember(self, "moderator", [self.mod_role])
        self.member = FakeMember(self, "member", [])
        self._me = FakeMember(self, "Nexus", [self.bot_role], bot=True)
        self.members_by_id = {m.id: m for m in (self.owner_member, self.admin, self.moderator, self.member, self._me)}
        self.afk_channel_obj = None
        self.events_by_id: dict[int, FakeScheduledEvent] = {}
        self.max_events = 100   # Discord: höchstens 100 geplante oder laufende Events pro Server

    # Mitgliederliste vollständig geladen (discord.Guild.chunked rechnet mit Gateway-Daten)
    chunked = True

    async def chunk(self, *, cache: bool = True):
        self.calls.append(("chunk",))
        self.chunked = True
        return self.members

    # ── Lesen ────────────────────────────────────────────────────────────────
    @property
    def me(self):
        return self._me

    @property
    def owner(self):
        return self.owner_member

    @property
    def default_role(self):
        return self._default_role

    @property
    def roles(self):
        return sorted(self.role_list, key=lambda r: r.position)

    @property
    def channels(self):
        return list(self.channels_by_id.values())

    @property
    def text_channels(self):
        return sorted((c for c in self.channels_by_id.values() if isinstance(c, FakeTextChannel)), key=lambda c: c.position)

    @property
    def voice_channels(self):
        return sorted((c for c in self.channels_by_id.values() if isinstance(c, FakeVoiceChannel)), key=lambda c: c.position)

    @property
    def categories(self):
        return []

    @property
    def forums(self):
        return []

    @property
    def afk_channel(self):
        return self.afk_channel_obj

    @property
    def members(self):
        return list(self.members_by_id.values())

    @property
    def shard_id(self):
        return 0

    def get_member(self, user_id: int):
        return self.members_by_id.get(user_id)

    def get_channel(self, channel_id: int):
        return self.channels_by_id.get(channel_id)

    def get_role(self, role_id: int):
        return next((r for r in self.role_list if r.id == role_id), None)

    async def fetch_member(self, user_id: int):
        member = self.get_member(user_id)
        if member is None:
            raise not_found(10007)
        return member

    def add_invite(self, code: str, *, expires_in: timedelta | None = None, max_uses: int = 0, uses: int = 0):
        """Für den Testaufbau: Einladung des Servers. expires_in=None: läuft nie ab."""
        now = discord.utils.utcnow()
        invite = types.SimpleNamespace(
            code=code, guild=self, max_uses=max_uses, uses=uses, created_at=now,
            expires_at=now + expires_in if expires_in is not None else None,
            max_age=int(expires_in.total_seconds()) if expires_in is not None else 0,
        )
        self.invite_list.append(invite)
        return invite

    async def invites(self):
        if not self.me.guild_permissions.manage_guild:
            raise forbidden()
        return list(self.invite_list)

    async def fetch_automod_rules(self):
        # Wie Discord: ohne „Server verwalten“ gibt es 403 (viele davon können den Bot vorübergehend sperren)
        self.automod_fetches += 1
        if not self.me.guild_permissions.manage_guild:
            raise forbidden()
        return list(self.automod_rules)

    def get_scheduled_event(self, event_id: int):
        return self.events_by_id.get(event_id)

    async def fetch_scheduled_event(self, event_id: int, **kwargs):
        if (event := self.events_by_id.get(event_id)) is None:
            raise not_found(10070)
        return event

    # ── Schreiben ────────────────────────────────────────────────────────────
    def add_text_channel(self, name: str, **kwargs) -> FakeTextChannel:
        """Für den Testaufbau: Kanal anlegen, ohne dass es als Bot-Aktion zählt."""
        channel = FakeTextChannel(self, name, **kwargs)
        self.channels_by_id[channel.id] = channel
        return channel

    def add_voice_channel(self, name: str, **kwargs) -> FakeVoiceChannel:
        channel = FakeVoiceChannel(self, name, position=len(self.channels_by_id), **kwargs)
        self.channels_by_id[channel.id] = channel
        return channel

    def add_automod_rule(self, name: str, trigger_type, *, by_nexus: bool = True, enabled: bool = True, **trigger) -> "FakeAutoModRule":
        """Für den Testaufbau: Regel anlegen — von Nexus (by_nexus) oder von einem Admin."""
        rule = FakeAutoModRule(
            self, name=name, trigger=discord.AutoModTrigger(type=trigger_type, **trigger),
            creator_id=self.me.id if by_nexus else self.admin.id, enabled=enabled,
        )
        self.automod_rules.append(rule)
        return rule

    async def create_automod_rule(self, *, name, event_type, trigger, actions, enabled=True, reason=None, **kwargs):
        if not self.me.guild_permissions.manage_guild:
            raise forbidden()
        rule = FakeAutoModRule(self, name=name, trigger=trigger, creator_id=self.me.id, actions=actions, enabled=enabled)
        rule.event_type = event_type
        self.automod_rules.append(rule)
        self.calls.append(("guild.create_automod_rule", rule.id, name))
        return rule

    async def create_text_channel(self, name: str, *, overwrites=None, reason=None, **kwargs):
        if not self.me.guild_permissions.manage_channels:
            raise forbidden()
        channel = self.add_text_channel(name, overwrites=overwrites, position=len(self.channels_by_id))
        self.calls.append(("guild.create_text_channel", channel.id, name))
        return channel

    async def create_scheduled_event(self, *, name, start_time, end_time, location, entity_type, privacy_level,
                                     description=discord.utils.MISSING, image=discord.utils.MISSING, reason=None):
        if not self.me.guild_permissions.create_events:
            raise forbidden()
        if entity_type is not discord.EntityType.external or not location or end_time is None:
            raise bad_request(50035, "Events mit Link als Ort brauchen Ort und Endzeit")
        if len(name) > 100 or len(location) > 100 or (description and len(description) > 1000):
            raise bad_request(50035, "Text zu lang")
        if sum(e.status is not discord.EventStatus.completed for e in self.events_by_id.values()) >= self.max_events:
            raise bad_request(30038, "Maximum number of guild scheduled events reached")
        event = FakeScheduledEvent(self, name=name, description=description or None, start_time=start_time,
                                   end_time=end_time, location=location, privacy_level=privacy_level,
                                   image=None if image is discord.utils.MISSING else image)
        self.events_by_id[event.id] = event
        self.calls.append(("guild.create_scheduled_event", event.id, name))
        return event

    def add_scheduled_event(self, name: str, start_time, end_time, **kwargs) -> "FakeScheduledEvent":
        """Für den Testaufbau: ein Event, das jemand im Server angelegt hat."""
        event = FakeScheduledEvent(self, name=name, start_time=start_time, end_time=end_time, **kwargs)
        self.events_by_id[event.id] = event
        return event

    def add_role(self, name: str, position: int, **permissions) -> FakeRole:
        """Für den Testaufbau: eine Rolle anlegen (Position 1 = ganz unten, Bot steht auf 8, Admin auf 10)."""
        role = FakeRole(self, name, position, discord.Permissions(**permissions))
        self.role_list.append(role)
        return role

    def _can_moderate(self, user_id: int) -> bool:
        target = self.members_by_id.get(user_id)
        return target is None or (user_id != self.owner_id and target.top_role < self.me.top_role)

    async def kick(self, member, *, reason=None):
        if not self.me.guild_permissions.kick_members or not self._can_moderate(member.id):
            raise forbidden(50013)
        self.members_by_id.pop(member.id, None)
        self.calls.append(("guild.kick", member.id, reason))

    async def ban(self, user, *, reason=None, delete_message_seconds=86400):
        if not self.me.guild_permissions.ban_members or not self._can_moderate(user.id):
            raise forbidden(50013)
        self.members_by_id.pop(user.id, None)
        self.calls.append(("guild.ban", user.id, reason))

    def add_member(self, name: str, *roles: FakeRole) -> FakeMember:
        member = FakeMember(self, name, list(roles))
        self.members_by_id[member.id] = member
        return member

    def add_emoji(self, name: str) -> str:
        """Für den Testaufbau: ein Server-Emoji anlegen, gibt den Code <:name:id> zurück."""
        emoji_id = next_id()
        self.emoji_ids.add(emoji_id)
        return f"<:{name}:{emoji_id}>"

    def is_usable_emoji(self, emoji: str) -> bool:
        """Unicode-Emojis gehen immer, Server-Emojis nur von diesem Server, reiner Text nie."""
        if emoji.startswith("<") and emoji.endswith(">"):
            try:
                return int(emoji.rstrip(">").rsplit(":", 1)[1]) in self.emoji_ids
            except (IndexError, ValueError):
                return False
        return not emoji.isascii()

    def __repr__(self):
        return f"<FakeGuild {self.name} {self.id}>"


class FakeAutoModRule:
    """Nachgebaute AutoMod-Regel. creator_id entscheidet, ob Nexus sie als eigene erkennt."""

    def __init__(self, guild, *, name, trigger, creator_id, actions=(), enabled=True, id: int | None = None):
        self.id = id or next_id()
        self.guild = guild
        self.name = name
        self.trigger = trigger
        self.creator_id = creator_id
        self.actions = list(actions)
        self.enabled = enabled
        self.exempt_role_ids: set[int] = set()
        self.exempt_channel_ids: set[int] = set()

    async def edit(self, *, reason=None, **kwargs):
        if not self.guild.me.guild_permissions.manage_guild:
            raise forbidden()
        for key, value in kwargs.items():
            if key in ("exempt_roles", "exempt_channels"):   # wie discord.py: Objekte rein, IDs in der Regel
                setattr(self, f"{key[:-1]}_ids", {o.id for o in value})
            else:
                setattr(self, key, value)
        self.guild.calls.append(("automod.edit", self.id, tuple(sorted(kwargs))))
        return self

    async def delete(self, *, reason=None):
        self.guild.automod_rules.remove(self)
        self.guild.calls.append(("automod.delete", self.id))


class FakeScheduledEvent:
    """Ein Server-Event. Discord startet und beendet Events mit Link als Ort selbst — im Test setzt man status."""

    def __init__(self, guild, *, name, start_time, end_time, location="https://example.com", description=None,
                 privacy_level=discord.PrivacyLevel.guild_only, image=None):
        self.guild = guild
        self.id = next_id()
        self.name, self.description, self.location = name, description, location
        self.start_time, self.end_time = start_time, end_time
        self.privacy_level, self.image = privacy_level, image
        self.status = discord.EventStatus.scheduled

    async def edit(self, **kwargs):
        if self.status is not discord.EventStatus.scheduled:
            raise bad_request(180000, "Cannot modify a finished event")
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.guild.calls.append(("event.edit", self.id, tuple(sorted(kwargs))))
        return self

    async def delete(self, *, reason=None):
        if self.guild.events_by_id.pop(self.id, None) is None:
            raise not_found(10070)
        self.guild.calls.append(("event.delete", self.id))

    def __repr__(self):
        return f"<FakeScheduledEvent {self.name!r} {self.start_time:%d.%m. %H:%M}>"


class FakeBot:
    def __init__(self, *guilds: FakeGuild):
        self.guilds_by_id = {g.id: g for g in guilds}
        self.user = types.SimpleNamespace(id=next_id(), name="Nexus")
        self.latency = 0.05

    def add_guild(self, guild: FakeGuild) -> FakeGuild:
        self.guilds_by_id[guild.id] = guild
        return guild

    def get_guild(self, guild_id: int):
        return self.guilds_by_id.get(guild_id)

    def get_channel(self, channel_id: int):
        return next((c for g in self.guilds_by_id.values() if (c := g.get_channel(channel_id))), None)

    async def fetch_channel(self, channel_id: int):
        if (channel := self.get_channel(channel_id)) is None:
            raise not_found(10003)
        return channel

    def get_partial_messageable(self, channel_id: int):
        """Wie bei discord.py: Nachricht nur per ID bearbeiten, ohne sie vorher zu holen."""
        bot = self

        class PartialMessage:
            def __init__(self, message_id):
                self.id = message_id

            async def edit(self, **kwargs):
                channel = bot.get_channel(channel_id)
                message = channel.messages.get(self.id) if channel else None
                if message is None:
                    raise not_found(10008)
                return await message.edit(**kwargs)

        return types.SimpleNamespace(id=channel_id, get_partial_message=PartialMessage)

    async def wait_until_ready(self):
        return None

    @property
    def guilds(self):
        return list(self.guilds_by_id.values())


class FakeInteraction:
    """Nachgebaute Component-Interaction: zeichnet ephemere Antworten und Bearbeitungen auf.
    reject_sku: Discord lehnt Nachrichten mit Kauf-Button ab (SKU nicht veröffentlicht)."""

    def __init__(self, guild, *, reject_sku=False):
        self.guild, self.guild_id, self.user = guild, guild.id, guild.admin
        self.message = None   # Slash-Command: keine Nachricht mit Komponenten
        self.reject_sku = reject_sku
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.modals: list = []
        interaction = self

        class Response:
            done = False

            def is_done(self):
                return self.done

            async def defer(self, **kwargs):
                self.done = True

            async def send_message(self, content=None, **kwargs):
                self.done = True
                interaction.sent.append({"content": content, **kwargs})

            async def edit_message(self, **kwargs):
                self.done = True
                interaction._check(kwargs.get("view"))
                interaction.edits.append(kwargs)

            async def send_modal(self, modal):
                self.done = True
                interaction.modals.append(modal)

        class Followup:
            async def send(self, content=None, **kwargs):
                interaction._check(kwargs.get("view"))
                interaction.sent.append({"content": content, **kwargs})
                return types.SimpleNamespace(id=len(interaction.sent))

        self.response, self.followup = Response(), Followup()

    def _check(self, view):
        if self.reject_sku and view and any(getattr(i, "sku_id", None) for i in view.children):
            raise bad_request(50035, "Invalid Form Body")

    async def edit_original_response(self, **kwargs):
        self._check(kwargs.get("view"))
        self.edits.append(kwargs)
