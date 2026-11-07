"""Nachgebautes Discord für die Tests: Server, Mitglieder, Rollen, Kanäle, Nachrichten.

Nur so viel, wie der Bot-Code tatsächlich benutzt. Alles, was der Bot bei Discord *tun* würde
(Kanal anlegen, Nachricht senden/bearbeiten/löschen), landet in `guild.calls` — die Tests prüfen das.
Neue Module brauchen evtl. weitere Methoden: dann hier ergänzen, nicht im Test zusammenbasteln.

FakeGuild erbt von discord.Guild, weil der Bot-Code an einigen Stellen isinstance(x, discord.Guild) prüft
(z. B. systems.hierarchy.missing_perms). Das echte __init__ wird nie aufgerufen; alles, was der Code liest,
ist hier überschrieben.
"""
import itertools
import types

import discord

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

    @property
    def mention(self):
        return f"<@&{self.id}>"

    def is_default(self):
        return self.id == self.guild.id

    def __lt__(self, other):
        return self.position < other.position

    def __gt__(self, other):
        return self.position > other.position

    def __ge__(self, other):
        return self.position >= other.position

    def __le__(self, other):
        return self.position <= other.position


class FakeMember:
    def __init__(self, guild, name: str, roles: list[FakeRole], *, bot: bool = False, id: int | None = None):
        self.id = id or next_id()
        self.guild = guild
        self.name = self.display_name = name
        self.bot = bot
        self.roles = [guild.default_role, *roles]

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
        self.automod_rules: list = []
        self.emoji_ids: set[int] = set()   # eigene Server-Emojis (für add_reaction)

        self._default_role = FakeRole(self, "@everyone", 0, discord.Permissions.none(), id=self.id)
        self.admin_role = FakeRole(self, "Admin", 10, discord.Permissions(administrator=True))
        self.mod_role = FakeRole(self, "Mod", 5, discord.Permissions(manage_guild=True))
        self.bot_role = FakeRole(self, "Nexus", 8, bot_permissions or discord.Permissions(
            manage_channels=True, manage_roles=True, manage_messages=True, view_channel=True,
            send_messages=True, embed_links=True, read_message_history=True, kick_members=True,
            ban_members=True, moderate_members=True, manage_guild=True, add_reactions=True,
        ))
        self.role_list = [self._default_role, self.mod_role, self.bot_role, self.admin_role]

        self.owner_member = FakeMember(self, "owner", [])
        self.owner_id = self.owner_member.id
        self.admin = FakeMember(self, "admin", [self.admin_role])
        self.moderator = FakeMember(self, "moderator", [self.mod_role])
        self.member = FakeMember(self, "member", [])
        self._me = FakeMember(self, "Nexus", [self.bot_role], bot=True)
        self.members_by_id = {m.id: m for m in (self.owner_member, self.admin, self.moderator, self.member, self._me)}

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
        return sorted(self.channels_by_id.values(), key=lambda c: c.position)

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

    async def fetch_automod_rules(self):
        return list(self.automod_rules)

    # ── Schreiben ────────────────────────────────────────────────────────────
    def add_text_channel(self, name: str, **kwargs) -> FakeTextChannel:
        """Für den Testaufbau: Kanal anlegen, ohne dass es als Bot-Aktion zählt."""
        channel = FakeTextChannel(self, name, **kwargs)
        self.channels_by_id[channel.id] = channel
        return channel

    async def create_text_channel(self, name: str, *, overwrites=None, reason=None, **kwargs):
        if not self.me.guild_permissions.manage_channels:
            raise forbidden()
        channel = self.add_text_channel(name, overwrites=overwrites, position=len(self.channels_by_id))
        self.calls.append(("guild.create_text_channel", channel.id, name))
        return channel

    def add_role(self, name: str, position: int, **permissions) -> FakeRole:
        """Für den Testaufbau: eine Rolle anlegen (Position 1 = ganz unten, Bot steht auf 8, Admin auf 10)."""
        role = FakeRole(self, name, position, discord.Permissions(**permissions))
        self.role_list.append(role)
        return role

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
