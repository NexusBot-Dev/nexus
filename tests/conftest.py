"""Gemeinsamer Aufbau für die Tests der Dashboard-Schnittstelle.

Läuft gegen eine echte MariaDB. Pro Testlauf wird die Datenbank TEST_DB_NAME neu angelegt (aus
database/schema.sql), vor jedem Test werden alle Tabellen geleert. Aus Sicherheit muss der Name auf
"_test" enden — so kann ein falsch gesetztes TEST_DB_NAME nie die Produktion leeren.

Umgebungsvariablen (mit Standardwerten):
  TEST_DB_HOST=127.0.0.1  TEST_DB_PORT=3306  TEST_DB_USER=root  TEST_DB_PASSWORD=test  TEST_DB_NAME=nexus_test

Lokal z. B.:  TEST_DB_PORT=3307 TEST_DB_USER=test python -m pytest tests

Async-Tests brauchen kein Plugin: Jede `async def test_...` läuft in derselben Event-Loop wie der
DB-Pool (siehe pytest_pyfunc_call unten).
"""
import asyncio
import inspect
import os
import pathlib
import re
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_DB = {
    "host": os.environ.get("TEST_DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("TEST_DB_PORT", "3306")),
    "user": os.environ.get("TEST_DB_USER", "root"),
    "password": os.environ.get("TEST_DB_PASSWORD", "test"),
    "name": os.environ.get("TEST_DB_NAME", "nexus_test"),
}
if not TEST_DB["name"].endswith("_test"):
    raise SystemExit(f"TEST_DB_NAME muss auf _test enden (ist: {TEST_DB['name']!r}) — die Tests leeren die Datenbank.")

DASHBOARD_TOKEN = "t" * 40

# ── Konfiguration für config.py, bevor irgendein Bot-Modul importiert wird ────────────────────────
_secret_dir = pathlib.Path(tempfile.mkdtemp(prefix="nexus-tests-"))


def _secret(name: str, value: str) -> str:
    path = _secret_dir / name
    path.write_text(value)
    return str(path)


_config_source = (ROOT / "config.py").read_text(encoding="utf-8")
for _name in re.findall(r'_read_secret\(\s*"([A-Z0-9_]+)"', _config_source):
    os.environ[_name] = _secret(_name, "placeholder")
for _name in re.findall(r'os\.(?:environ\[|environ\.get\(|getenv\()\s*"([A-Z0-9_]+)"', _config_source):
    os.environ.setdefault(_name, "1")
def _kick_test_key() -> str:
    """Gültiger Public Key, damit WebhookServer beim Start keinen Fehler loggt (Kick wird hier nicht getestet)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


os.environ.update({
    "KICK_PUBLIC_KEY_FILE": _secret("kick_public_key", _kick_test_key()),
    "DB_HOST": TEST_DB["host"], "DB_PORT": str(TEST_DB["port"]),
    "DB_NAME": TEST_DB["name"], "DB_USER": TEST_DB["user"],
    "NEXUS_DB_PASSWORD_FILE": _secret("db_password", TEST_DB["password"]),
    "DASHBOARD_TOKEN_FILE": _secret("dashboard_token", DASHBOARD_TOKEN),
})

import aiomysql  # noqa: E402
import cachetools  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import dashboard  # noqa: E402,F401  registriert alle Dashboard-Aktionen
import database.database as db  # noqa: E402
from dashboard import dashboard_auth, registry  # noqa: E402
from database import db_limits  # noqa: E402
from integrations.webhook import WebhookServer  # noqa: E402
import systems.kick_api, systems.twitch_api, systems.youtube_api  # noqa: E402,F401,E401  registrieren die Plattformen (wie beim Bot-Start)
from tests.fakes import FakeBot, FakeGuild  # noqa: E402
from translations import TRANSLATIONS  # noqa: E402

# Grenzwerte wie in der Produktion (feature_limits ist die einzige Tabelle mit festen Daten)
FEATURE_LIMITS = {
    "boost_messages": (1, 5), "saved_embeds": (5, 100), "stream_alerts_kick": (1, 5),
    "stream_alerts_twitch": (1, 5), "stream_alerts_youtube": (1, 3), "ticket_categories": (2, 10),
    "welcome_messages": (1, 5),
}

# ── Eine Event-Loop für alles ─────────────────────────────────────────────────────────────────────
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def run(coro):
    return _loop.run_until_complete(coro)


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        run(pyfuncitem.obj(**kwargs))
        return True
    return None


# ── Datenbank ─────────────────────────────────────────────────────────────────────────────────────
def _schema_statements() -> list[str]:
    sql = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    sql = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in sql.split(";") if s.strip()]


async def _create_database():
    conn = await aiomysql.connect(host=TEST_DB["host"], port=TEST_DB["port"], user=TEST_DB["user"],
                                  password=TEST_DB["password"], autocommit=True, charset="utf8mb4")
    async with conn.cursor() as cur:
        await cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB['name']}`")
        await cur.execute(f"CREATE DATABASE `{TEST_DB['name']}` CHARACTER SET utf8mb4")
        await cur.execute(f"USE `{TEST_DB['name']}`")
        for statement in _schema_statements():
            await cur.execute(statement)
    conn.close()


@pytest.fixture(scope="session", autouse=True)
def database():
    run(_create_database())
    run(db.init_pool())
    yield
    run(db.close_pool())


async def _reset_database():
    async with db.pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (TEST_DB["name"],))
            tables = [row[0] for row in await cur.fetchall()]
            await cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            for table in tables:
                await cur.execute(f"TRUNCATE TABLE `{table}`")
            await cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            await cur.executemany(
                "INSERT INTO feature_limits (feature_key, free_limit, premium_limit) VALUES (%s, %s, %s)",
                [(key, free, premium) for key, (free, premium) in FEATURE_LIMITS.items()],
            )


def _clear_caches():
    """Alle Caches im Bot-Code leeren — sonst sähe ein Test die Daten des vorherigen."""
    for name, module in list(sys.modules.items()):
        if not name.startswith(("database", "systems", "cogs", "dashboard", "integrations")):
            continue
        for value in vars(module).values():
            if isinstance(value, cachetools.Cache):
                value.clear()
    db_limits._limits_cache.clear()
    db_limits._cache_loaded_at = 0
    dashboard_auth._request_log.clear()   # Rate-Limit: sonst bremst es nach 60 Requests die Tests aus


@pytest.fixture(autouse=True)
def clean_state():
    run(_reset_database())
    _clear_caches()
    yield


async def query(sql: str, *params) -> list[dict]:
    """Direkt in die Datenbank schauen (an den Caches des Bots vorbei)."""
    async with db.pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, params)
            return list(await cur.fetchall())


async def query_one(sql: str, *params) -> dict | None:
    rows = await query(sql, *params)
    return rows[0] if rows else None


# ── Discord & Dashboard-Schnittstelle ─────────────────────────────────────────────────────────────
@pytest.fixture
def guild():
    """Ein Server, auf dem der Bot ist und für den es (wie nach dem Beitritt) Einstellungen gibt."""
    g = FakeGuild()
    run(query(
        "INSERT INTO guild_settings (guild_id, locale, modules) VALUES (%s, %s, %s)",
        g.id, "de", '{"moderation": true, "automod": true, "stream_alerts": true, "tickets": true}',
    ))
    return g


@pytest.fixture
def bot(guild):
    b = FakeBot(guild)
    registry.set_bot(b)
    return b


class Dashboard:
    """Schickt Requests wie das echte Dashboard: Bearer-Token, guild_id, acting_user_id, lang."""

    def __init__(self, client: TestClient, guild: FakeGuild):
        self.client = client
        self.guild = guild

    async def raw(self, path: str, body=None, *, token: str | None = DASHBOARD_TOKEN, method: str = "POST"):
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        response = await self.client.request(method, path, json=body, headers=headers)
        return response.status, await response.json()

    async def action(self, module: str, action: str, payload: dict | None = None, *,
                     user=None, lang: str | None = "de", guild=None):
        guild = guild or self.guild
        user = user or guild.admin
        body = {"guild_id": str(guild.id), "acting_user_id": str(user.id), **(payload or {})}
        if lang:
            body["lang"] = lang
        return await self.raw(f"/api/dashboard/module/{module}/{action}", body)

    async def ok(self, module: str, action: str, payload: dict | None = None, **kwargs) -> dict:
        """Aktion, die klappen muss — gibt `data` zurück."""
        status, body = await self.action(module, action, payload, **kwargs)
        assert status == 200 and body.get("ok"), f"{module}/{action}: {status} {body}"
        return body["data"]

    async def error(self, module: str, action: str, payload: dict | None = None, *, status: int = 400, **kwargs) -> str:
        """Aktion, die abgelehnt werden muss — gibt die Fehlermeldung zurück."""
        got_status, body = await self.action(module, action, payload, **kwargs)
        assert got_status == status and not body.get("ok"), f"{module}/{action}: erwartet {status}, bekam {got_status} {body}"
        return body["error"]


@pytest.fixture
def dash(bot, guild):
    async def start():
        server = WebhookServer(bot)   # echte Routen und echte Auth-Middleware, ohne Port 8080 zu öffnen
        client = TestClient(TestServer(server.app))
        await client.start_server()
        return client
    client = run(start())
    yield Dashboard(client, guild)
    run(client.close())


class FakePlatforms:
    """Ersetzt die APIs von Twitch, Kick und YouTube (Nutzer suchen, Webhook-Abos anlegen/löschen).

    Die echte Eingabeprüfung (normalize_input, z. B. "https://twitch.tv/Name" → "name") bleibt.
    streamers[plattform][login] = (externe ID, Anzeigename); calls zeichnet die API-Aufrufe auf."""

    def __init__(self, monkeypatch):
        from systems.platform_registry import PLATFORM_HANDLERS
        self.streamers = {"twitch": {}, "kick": {}, "youtube": {}}
        self.calls: list[tuple] = []
        self.fail_subscriptions = False
        self.active: set[tuple[str, str]] = set()   # bestehende Webhook-Abos bei der Plattform
        for platform, handler in PLATFORM_HANDLERS.items():
            monkeypatch.setattr(handler, "resolve_user", self._resolver(platform))
            if handler.create_subscriptions:
                monkeypatch.setattr(handler, "create_subscriptions", self._creator(platform))
            if handler.delete_subscriptions:
                monkeypatch.setattr(handler, "delete_subscriptions", self._deleter(platform))

    def add(self, platform: str, login: str, external_id: str, display_name: str | None = None):
        self.streamers[platform][login] = (external_id, display_name or login)

    def _resolver(self, platform):
        async def resolve_user(login):
            self.calls.append(("resolve", platform, login))
            await asyncio.sleep(0.01)   # echte API-Anfrage: andere Anfragen laufen in der Zeit weiter
            if login not in self.streamers[platform]:
                return None
            external_id, name = self.streamers[platform][login]
            user = {"id": external_id, "login": login, "display_name": name}
            if platform == "youtube":
                user["uploads_playlist_id"] = "UU" + external_id[2:]
            return user
        return resolve_user

    def _creator(self, platform):
        async def create_subscriptions(external_id):
            self.calls.append(("subscribe", platform, external_id))
            await asyncio.sleep(0.01)
            if self.fail_subscriptions:
                raise RuntimeError("Plattform-API nicht erreichbar")
            if (platform, external_id) in self.active:   # wie Twitch: 409 "subscription already exists"
                raise RuntimeError("Twitch-API-Fehler (409): subscription already exists")
            self.active.add((platform, external_id))
            return {"online_id": f"{platform}-on-{external_id}", "offline_id": f"{platform}-off-{external_id}"}
        return create_subscriptions

    def _deleter(self, platform):
        async def delete_subscriptions(monitored):
            self.calls.append(("unsubscribe", platform, monitored["platform_channel_id"]))
            await asyncio.sleep(0.01)
            self.active.discard((platform, monitored["platform_channel_id"]))
        return delete_subscriptions


@pytest.fixture
def platforms(monkeypatch):
    return FakePlatforms(monkeypatch)


async def add_guild(bot, name: str = "Zweiter Server") -> FakeGuild:
    """Weiterer Server, auf dem der Bot ist (mit Einstellungen wie nach dem Beitritt)."""
    g = bot.add_guild(FakeGuild(name=name))
    await query("INSERT INTO guild_settings (guild_id, locale) VALUES (%s, 'de')", g.id)
    return g


async def set_premium(guild, active: bool = True):
    """Premium für den Server an-/ausschalten (wie über /grant-premium, ohne Ablaufdatum)."""
    from systems.premium import invalidate_cache
    if active:
        await query("INSERT INTO premium (guild_id, expires_at, source) VALUES (%s, NULL, 'manual') "
                    "ON DUPLICATE KEY UPDATE expires_at = NULL", guild.id)
    else:
        await query("DELETE FROM premium WHERE guild_id = %s", guild.id)
    invalidate_cache(guild.id)


def t(key: str, lang: str = "de", **params) -> str:
    """Übersetzter Text, wie ihn das Dashboard anzeigt."""
    return TRANSLATIONS[lang][key].format(**params)
