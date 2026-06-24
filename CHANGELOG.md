# Changelog

## 0.1.0-alpha.2

- **CI fix — no functional changes.** The alpha.1 GHCR build failed
  during the multi-arch image step because the `description:` field
  in `config.yaml` contained an apostrophe (`DaVinci's`) plus literal
  double quotes (`Each "library"`); the `home-assistant/builder`
  composite action splices the description into a single-quoted bash
  label, and the apostrophe closed the quote and broke the script
  with `unexpected EOF while looking for matching backtick-quote`.
  Rewrote the description without `'` or `"` so the build runs clean.
  No image at `ghcr.io/uncommon-fix/ha-addon-davinci-resolve:alpha.1`
  was ever published; alpha.2 is the first usable release.

## 0.1.0-alpha.1

- **First public alpha.** Self-hosted PostgreSQL for DaVinci Resolve
  Studio project databases, packaged as a Home Assistant supervisor
  add-on.
- **Per-library Postgres provisioning.** Click **Create library** in
  the add-on's web UI; the add-on creates a fresh database + user
  with a random password and shows the connection block ready to paste
  into DaVinci Resolve's *Project Manager → Databases → New Database*
  dialog. PostgreSQL 15, `scram-sha-256` auth. DaVinci Resolve 18 + 19
  are the supported clients; DR 17 is not (it requires `md5` and an
  older PG).
- **Library lifecycle.** List existing libraries, **Reset password**
  to rotate credentials, **Delete** to drop the database + user (the
  on-SMB media files are not touched). Deletes are gated by a
  type-the-name confirmation.
- **Backups handled by Home Assistant.** The add-on uses `backup: cold`:
  the supervisor stops the add-on during HA full backups, snapshots
  `/data/pgdata` cleanly, and restarts. No separate backup wiring
  needed.
- **Postgres on port 5432.** DaVinci Resolve clients on the LAN connect
  to `<your-HA-host>:5432`. The connection is `scram-sha-256` over
  plain TCP — sufficient for a LAN; TLS is on the roadmap if a use
  case comes up.
- **Single-editor lock.** Same one-tab-at-a-time pattern as the
  Traefik add-on: a second browser tab gets a take-over prompt so two
  editors can't race on the libraries catalog.
