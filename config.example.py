import os

def _read_secret(env_var: str, default_path: str) -> str:
    path = os.environ.get(env_var, default_path)
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        raise RuntimeError(f"Secret-Datei nicht gefunden: {path}")

APPLICATION_ID = 1510643364104765460

# Support Server-ID
SUPPORT_GUILD_ID: int = 1516821048421781674
OWNER_ID: int = 0 # Replace with your actual owner ID
DEV_GUILD_ID: int = 0  # Replace with your dev/test server ID

# Dashboard Token
DASHBOARD_TOKEN: str = _read_secret(
    "DASHBOARD_TOKEN_FILE", "/run/secrets/dashboard_token"
)

# Nexus Branding
NEXUS_COLOR: int = 0x00F5D4
NEXUS_FOOTER: str = "Nexus • trynexus.de"

# Bot invite URL
INVITE_URL = f"https://discord.com/oauth2/authorize?client_id={APPLICATION_ID}"
DASHBOARD_URL = "https://dashboard.trynexus.de"

# Twitch
TWITCH_WEBHOOK_SECRET: str = _read_secret(
    "TWITCH_WEBHOOK_SECRET_FILE", "/run/secrets/twitch_webhook_secret"
)
TWITCH_CLIENT_ID: str = _read_secret(
    "TWITCH_CLIENT_ID_FILE", "/run/secrets/twitch_client_id"
)
TWITCH_CLIENT_SECRET: str = _read_secret(
    "TWITCH_CLIENT_SECRET_FILE", "/run/secrets/twitch_client_secret"
)

# Kick
KICK_CLIENT_ID: str = _read_secret(
    "KICK_CLIENT_ID_FILE", "/run/secrets/kick_client_id"
)
KICK_CLIENT_SECRET: str = _read_secret(
    "KICK_CLIENT_SECRET_FILE", "/run/secrets/kick_client_secret"
)
KICK_PUBLIC_KEY: str = _read_secret(
    "KICK_PUBLIC_KEY_FILE", "/run/secrets/kick_public_key"
)

# YouTube
YOUTUBE_API_KEY: str = _read_secret(
    "YOUTUBE_API_KEY_FILE", "/run/secrets/youtube_api_key"
)

# Datenbank
DB_HOST:     str = "<YOUR_DB_HOST>" # Replace with your actual database host
DB_PORT:     int = 3306
DB_NAME:     str = "<YOUR_DB_NAME>" # Replace with your actual database name
DB_USER:     str = "<YOUR_DB_USER>" # Replace with your actual database user
DB_PASSWORD: str = _read_secret("DB_PASSWORD_FILE", "/run/secrets/db_password")