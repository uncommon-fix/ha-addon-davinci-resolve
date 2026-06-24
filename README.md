# DaVinci Resolve Postgres — Home Assistant add-on

Self-hosted PostgreSQL for DaVinci Resolve Studio project databases,
packaged as a Home Assistant supervisor add-on. One-click "Create
library" → a fresh database + user + password to paste into DaVinci's
*Connect to PostgreSQL* dialog. Media on your SMB share; project
database here, covered by Home Assistant's normal backup flow.

> [!WARNING]
> Early **public alpha** — expect breaking changes. Bug reports welcome
> in [Issues](https://github.com/uncommon-fix/ha-addon-davinci-resolve/issues).

## What it does

- Runs PostgreSQL 17 inside an HA add-on, with PGDATA on `/data` (so
  Home Assistant's full backups cover it automatically; `backup: cold`
  means the supervisor stops the add-on cleanly during the snapshot).
- Exposes a small web UI under HA ingress to create / list / delete
  libraries (one Postgres database + role per library) and rotate
  passwords.
- Listens for DaVinci Resolve clients on `your-HA-host:5432` using
  `scram-sha-256` auth (DaVinci Resolve 18 and 19 are supported; DR 17
  is not).

## Why

DaVinci Resolve Studio's project database + collaboration features
need a real Postgres somewhere on the LAN. If you already run Home
Assistant on a Pi, this add-on lets that same Pi be the database
host — and HA's existing backup story covers it for free, no separate
cron / pg_dump dance.

## Install

1. Add this addon repository to Home Assistant: **Settings → Add-ons →
   ⋮ → Repositories**, then paste
   `https://github.com/uncommon-fix/ha-addons` (the index repo).
2. The new add-on appears in the **Add-on store** as
   *DaVinci Resolve Postgres*. Install → Start.
3. Open **Web UI**. Click **Create library**.
4. Copy the connection block; paste into DaVinci Resolve's
   *Project Manager → Databases → New Database → PostgreSQL*.

See [DOCS.md](DOCS.md) for setup details + troubleshooting.

## License

[MIT](LICENSE) © 2026 uncommon-fix
