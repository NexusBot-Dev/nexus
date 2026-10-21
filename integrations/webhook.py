import logging
import hmac
import hashlib
import json
import aiohttp
import base64
import asyncio
from aiohttp import web
from discord.ext import commands
from config import TWITCH_WEBHOOK_SECRET, DASHBOARD_TOKEN, KICK_PUBLIC_KEY
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature
from dashboard.dashboard_auth import dashboard_auth_middleware
from dashboard.registry import get_handler
from dashboard import settings as dashboard_settings
from collections import OrderedDict

MAX_MESSAGE_AGE = timedelta(minutes=10)
MAX_PROCESSED_MESSAGES = 1000
MAX_CONCURRENT_NOTIFICATIONS = 8   # gleichzeitig verarbeitete Twitch-/Kick-Notifications

TWITCH_MESSAGE_TYPE_HEADER      = "Twitch-Eventsub-Message-Type"
TWITCH_MESSAGE_ID_HEADER        = "Twitch-Eventsub-Message-Id"
TWITCH_MESSAGE_TIMESTAMP_HEADER = "Twitch-Eventsub-Message-Timestamp"
TWITCH_MESSAGE_SIGNATURE_HEADER = "Twitch-Eventsub-Message-Signature"

KICK_MESSAGE_ID_HEADER        = "Kick-Event-Message-Id"
KICK_MESSAGE_TIMESTAMP_HEADER = "Kick-Event-Message-Timestamp"
KICK_MESSAGE_SIGNATURE_HEADER = "Kick-Event-Signature"

KICK_PUBLIC_KEY_URL = "https://api.kick.com/public/v1/public-key"

log = logging.getLogger(__name__)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

def _timestamp_valid(timestamp: str) -> bool:
    try:
        msg_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if msg_time.tzinfo is None:
        return False
    diff = (datetime.now(timezone.utc) - msg_time).total_seconds()
    return -60 <= diff <= MAX_MESSAGE_AGE.total_seconds()

class WebhookServer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        self.app = web.Application(middlewares=[dashboard_auth_middleware])
        self.app["dashboard_token"] = DASHBOARD_TOKEN

        self.app.router.add_post("/webhooks/twitch", self.handle_twitch)
        self.twitch_secret = TWITCH_WEBHOOK_SECRET

        self.app.router.add_post("/webhooks/kick", self.handle_kick)
        try:
            self._kick_public_key = serialization.load_pem_public_key(KICK_PUBLIC_KEY.encode("utf-8"))
        except Exception:
            log.exception("KICK_PUBLIC_KEY ungültig, wird beim ersten Webhook nachgeladen.")
            self._kick_public_key = None

        self.app.router.add_post("/api/dashboard/module/{module}/{action}", self.handle_module_action)
        self.app.router.add_post("/api/dashboard/settings", self.handle_settings_update)
        self.app.router.add_get("/api/dashboard/health", self.handle_dashboard_health)

        self.runner = None
        self._processed_messages = OrderedDict()
        self._last_kick_key_fetch = None
        self._tasks = set()
        self._notification_slots = asyncio.Semaphore(MAX_CONCURRENT_NOTIFICATIONS)

        if not DASHBOARD_TOKEN or len(DASHBOARD_TOKEN) < 32:
            log.warning("DASHBOARD_TOKEN ist leer oder kürzer als 32 Zeichen — Dashboard-API ist gesperrt bzw. schwach geschützt.")
        if not TWITCH_WEBHOOK_SECRET:
            log.warning("TWITCH_WEBHOOK_SECRET ist leer — Twitch-Webhooks werden abgelehnt.")

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def cog_load(self):
        from dashboard.registry import set_bot
        set_bot(self.bot)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", 8080)
        await site.start()
        log.info("Zentraler Webhook-Server lauscht auf Port 8080.")

    async def cog_unload(self):
        if self.runner:
            await self.runner.cleanup()
        for task in list(self._tasks):
            task.cancel()

    def _seen_before(self, msg_id: str) -> bool:
        if msg_id in self._processed_messages:
            return True
        self._processed_messages[msg_id] = datetime.now(timezone.utc)
        if len(self._processed_messages) > MAX_PROCESSED_MESSAGES:
            self._processed_messages.popitem(last=False)
        return False

    async def handle_module_action(self, request: web.Request) -> web.Response:
        module = request.match_info["module"]
        action = request.match_info["action"]

        handler = get_handler(module, action)
        if handler is None:
            return web.json_response({"ok": False, "error": "unknown_action"}, status=404)

        try:
            body = request.get("_cached_json") or await request.json()
            guild_id = int(body["guild_id"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"ok": False, "error": "invalid_request"}, status=400)

        try:
            result = await handler(guild_id, body)
            return web.json_response({"ok": True, "data": result})
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception:
            log.exception("Dashboard-Aktion fehlgeschlagen: %s/%s", module, action)
            return web.json_response({"ok": False, "error": "internal_error"}, status=500)

    async def _get_kick_public_key(self, force_refresh: bool = False):
        now = datetime.now(timezone.utc)

        # Gecachten Key direkt liefern, außer Refresh ist explizit angefordert
        if self._kick_public_key is not None and not force_refresh:
            return self._kick_public_key

        # Throttle: egal ob Refresh oder fehlender Key, max. 1 Abruf pro 15 Min
        if self._last_kick_key_fetch and now - self._last_kick_key_fetch < timedelta(minutes=15):
            return self._kick_public_key

        self._last_kick_key_fetch = now  # direkt setzen, deckt alle Fehlerpfade ab

        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(KICK_PUBLIC_KEY_URL) as resp:
                    if resp.status != 200:
                        log.error("Kick-Public-Key konnte nicht geladen werden (%s).", resp.status)
                        return self._kick_public_key
                    data = await resp.json()
        except Exception:
            log.exception("Fehler beim Abruf des Kick-Public-Keys.")
            return self._kick_public_key

        if not isinstance(data, dict):
            log.error("Kick-Public-Key-Response hatte unerwartetes Format.")
            return self._kick_public_key

        pem = data.get("data", {}).get("public_key")
        if not pem:
            log.error("Kick-Public-Key-Response hatte kein 'public_key'-Feld: %s", data)
            return self._kick_public_key

        try:
            self._kick_public_key = serialization.load_pem_public_key(pem.encode("utf-8"))
        except ValueError:
            log.exception("Kick-Public-Key-Response enthielt ungültiges PEM.")
        return self._kick_public_key

    @staticmethod
    def _try_verify(key, sig_bytes: bytes, signed_payload: bytes) -> bool:
        try:
            key.verify(sig_bytes, signed_payload, padding.PKCS1v15(), hashes.SHA256())
            return True
        except InvalidSignature:
            return False

    async def _verify_kick_signature(self, request: web.Request, body: bytes) -> bool:
        message_id = request.headers.get(KICK_MESSAGE_ID_HEADER, "")
        timestamp  = request.headers.get(KICK_MESSAGE_TIMESTAMP_HEADER, "")
        signature  = request.headers.get(KICK_MESSAGE_SIGNATURE_HEADER, "")

        if not (message_id and timestamp and signature):
            return False

        if not _timestamp_valid(timestamp):
            log.warning("Kick-Webhook: Nachricht zu alt (%r), abgelehnt.", timestamp)
            return False

        signed_payload = f"{message_id}.{timestamp}.".encode("utf-8") + body

        try:
            sig_bytes = base64.b64decode(signature, validate=True)
        except Exception:
            return False

        key = await self._get_kick_public_key()
        if key is not None and self._try_verify(key, sig_bytes, signed_payload):
            return True

        key = await self._get_kick_public_key(force_refresh=True)
        if key is not None and self._try_verify(key, sig_bytes, signed_payload):
            log.info("Kick-Webhook: Signatur erst nach Key-Refresh gültig — Key war veraltet.")
            return True

        return False

    def _verify_twitch_signature(self, request: web.Request, body: bytes) -> bool:
        if not self.twitch_secret:
            return False   # leeres Secret = HMAC mit leerem Key = fälschbar
        message_id = request.headers.get(TWITCH_MESSAGE_ID_HEADER, "")
        timestamp  = request.headers.get(TWITCH_MESSAGE_TIMESTAMP_HEADER, "")
        signature  = request.headers.get(TWITCH_MESSAGE_SIGNATURE_HEADER, "")

        if not (message_id and timestamp and signature):
            return False

        if not _timestamp_valid(timestamp):
            log.warning("Twitch-Webhook: Nachricht zu alt (%r), abgelehnt.", timestamp)
            return False

        message_bytes = message_id.encode('utf-8') + timestamp.encode('utf-8') + body
        computed = "sha256=" + hmac.new(
            self.twitch_secret.encode('utf-8'),
            message_bytes,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(computed.encode(), signature.encode())

    async def handle_settings_update(self, request: web.Request) -> web.Response:
        try:
            body = request.get("_cached_json") or await request.json()
            guild_id = int(body["guild_id"])
            fields = body.get("fields", {})
            if not isinstance(fields, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return web.json_response({"ok": False, "error": "invalid_request"}, status=400)

        try:
            result = await dashboard_settings.update_settings(self.bot, guild_id, fields)
            return web.json_response({"ok": True, "data": result})
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception:
            log.exception("Settings-Update fehlgeschlagen für Guild %s", guild_id)
            return web.json_response({"ok": False, "error": "internal_error"}, status=500)

    async def handle_dashboard_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "online",
            "bot_ready": self.bot.is_ready(),
            "guilds_count": len(self.bot.guilds),
            "latency_ms": round(self.bot.latency * 1000, 2)
        })

    async def handle_twitch(self, request: web.Request) -> web.Response:
        body = await request.read()

        if not self._verify_twitch_signature(request, body):
            log.warning("Twitch-Webhook: ungültige Signatur, Request abgelehnt.")
            return web.Response(status=403)

        msg_id = request.headers.get(TWITCH_MESSAGE_ID_HEADER)
        if not msg_id or self._seen_before(msg_id):
            return web.Response(status=200)

        msg_type = request.headers.get(TWITCH_MESSAGE_TYPE_HEADER)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        if msg_type == "webhook_callback_verification":
            challenge = data.get("challenge", "")
            log.info("Twitch-EventSub-Verification empfangen und bestätigt.")
            return web.Response(text=challenge, content_type="text/plain")

        if msg_type == "revocation":
            sub = data.get("subscription", {})
            log.warning(
                "Twitch-EventSub-Abo widerrufen: %s (Grund: %s)",
                sub.get("id"), sub.get("status"),
            )
            return web.Response(status=200)

        if msg_type == "notification":
            self._spawn(self._execute_twitch(data))
            return web.Response(status=200)

        log.warning("Twitch-Webhook: unbekannter Message-Type '%s'.", msg_type)
        return web.Response(status=400)

    async def _execute_twitch(self, data: dict):
        async with self._notification_slots:
            try:
                twitch_cog = self.bot.get_cog("Twitch")
                if twitch_cog:
                    await twitch_cog.twitch_process_notification(data)
                else:
                    log.error("Twitch-Cog ist nicht geladen! Notification abgebrochen.")
            except Exception:
                log.exception("Fehler bei der Verarbeitung im Twitch-Modul")

    async def handle_kick(self, request: web.Request) -> web.Response:
        body = await request.read()

        if not await self._verify_kick_signature(request, body):
            log.warning("Kick-Webhook: ungültige Signatur, Request abgelehnt.")
            return web.Response(status=403)

        msg_id = request.headers.get(KICK_MESSAGE_ID_HEADER)
        if not msg_id or self._seen_before(msg_id):
            return web.Response(status=200)

        msg_type = request.headers.get("Kick-Event-Type")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        if msg_type == "livestream.status.updated":
            self._spawn(self._execute_kick(data))
            return web.Response(status=200)

        log.warning("Kick-Webhook: unbekannter Event-Type '%s'.", msg_type)
        return web.Response(status=200)

    async def _execute_kick(self, data: dict):
        async with self._notification_slots:
            try:
                kick_cog = self.bot.get_cog("Kick")
                if kick_cog:
                    await kick_cog.kick_process_notification(data)
                else:
                    log.error("Kick-Cog ist nicht geladen! Notification abgebrochen.")
            except Exception:
                log.exception("Fehler bei der Verarbeitung im Kick-Modul")

async def setup(bot):
    await bot.add_cog(WebhookServer(bot))