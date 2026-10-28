# Nexus

A modular, multi-language Discord bot with a generous free tier — leveling, tickets, stream alerts, moderation, AutoMod & anti-nuke, giveaways, reaction roles and more.

👉 [Add Nexus to Discord](https://discord.com/oauth2/authorize?client_id=1510643364104765460) · [Website](https://trynexus.de) · [Dashboard](https://dashboard.trynexus.de) · [Support server](https://discord.gg/YFCrvBb6t3)

## About this repository
This is the source code of the official Nexus bot, published for transparency and community contributions. It is licensed under the **AGPL-3.0**.

- The hosted bot at trynexus.de is the recommended way to use Nexus.
- Self-hosting is possible but **not officially supported**: you need your own MariaDB, configuration (`config.example.py`) and Discord application. No secrets or production data are part of this repository.
- The web dashboard frontend lives in a separate project.

## Tech stack
Python · discord.py · MariaDB (aiomysql) · Docker

## Project structure
| Folder | Content |
|---|---|
| `cogs/` | Slash commands and event listeners per module |
| `systems/` | Shared logic (UI base classes, premium, permissions, …) |
| `database/` | Database access layer |
| `dashboard/` | Internal API used by the web dashboard |
| `integrations/` | Twitch, YouTube and Kick webhooks |
| `translations/` | German, English, Spanish, Portuguese |

## Contributing
Bug reports and pull requests are welcome — see [Contributing.md](Contributing.md).

## Security
Please **do not** report security issues publicly. Contact us privately via the support server instead.

## License
[AGPL-3.0](LICENSE) — if you run a modified version as a service, you must make your source code available under the same license.
