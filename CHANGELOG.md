# Changelog

## 0.1.0-alpha.6

- **Fixed: Server name showed the addon's container IP instead of the
  HA host's LAN IP.** The Create-library + Reset-password modals were
  displaying values like `172.30.33.4` (the addon's address on the
  supervisor bridge), which is unreachable from DaVinci Resolve
  clients on the LAN. The detection now queries the supervisor's
  `/network/info` endpoint for the host's primary IPv4 (e.g.
  `10.0.0.169`) and caches it. Falls back to the host's
  `<hostname>.local` mDNS name, and finally to a clear
  `<your-HA-host-IP>` placeholder so the wrong-but-plausible
  container IP can't sneak through.
- **New: Traefik add-on integration banner.** If the sibling Traefik
  add-on is installed (detected by polling the supervisor's
  `/addons` endpoint), the DR dashboard shows a dismissible info
  banner explaining how to expose the Postgres server on a subdomain
  via a Traefik **TCP route**. The banner links to the Traefik
  add-on's UI for manual setup. Auto-route-creation isn't in this
  release (Traefik's data model is HTTP-only today; TCP-route
  support needs a UI extension in the Traefik addon). The banner is
  dismissed per-browser via localStorage.
- **Note on Traefik detection scope.** Auto-detection uses the
  supervisor's API with the addon's default `hassio_role`. If the
  role isn't elevated enough to list `/addons`, the banner just
  doesn't render — no error surface. If you have Traefik installed
  and the banner doesn't appear, file an issue and we'll bump the
  role.

## 0.1.0-alpha.5

- **Fixed: `Couldn't create library: failed to create library 'X':
  [Errno 2] No such file or directory`.** Root cause was that
  `/run/` is a tmpfs in the addon container and gets wiped on every
  start, so the `/run/postgresql/` directory the backend connects
  through never existed. Postgres started TCP-only and the backend's
  `asyncpg.connect(host='/run/postgresql', ...)` failed with
  `FileNotFoundError`, surfacing as the UI error above. Three fixes:
  - cont-init now `mkdir -p /run/postgresql` + chowns it to the
    postgres user on every boot, before either service starts.
  - `postgresql.conf` pins `unix_socket_directories = '/run/postgresql'`
    explicitly so PG and the backend agree on the path regardless of
    whatever the Alpine build chose at compile time.
  - alpha.5 includes an idempotent in-place migration step that
    appends the `unix_socket_directories` line to existing
    installs' `postgresql.conf` if it's missing (so alpha.4 →
    alpha.5 upgrades pick up the fix without needing a manual
    edit).
  - The backend's wait-for-socket loop in
    `rootfs/etc/services.d/backend/run` now logs LOUD errors on
    timeout (instead of silently proceeding) so the next class of
    socket issue surfaces in `ha apps logs` directly.

## 0.1.0-alpha.4

- **CI fix round three — now we actually build.** alpha.3's tag pushed
  the first time a real Docker build was attempted (the shell-quoting
  saga in alpha.1+alpha.2 never got far enough to run apk). That build
  failed with `ERROR: unable to select packages: postgresql15 (no such
  package)`. Investigation: the HA base image
  `ghcr.io/home-assistant/base:3.23` is built on **Alpine 3.23**
  (per its `io.hass.base.image` label), and Alpine 3.23's repos only
  ship PostgreSQL 16 (community), 17 (main), and 18 (main) — PG 15
  was dropped from the index entirely. Switched the addon to
  **PostgreSQL 17** (main repo, no extra `--repository` flag) and
  updated the binary paths from `/usr/libexec/postgresql15` to
  `/usr/libexec/postgresql17` in cont-init + the postgres service
  longrun.
- **DaVinci Resolve compatibility note.** Blackmagic's official docs
  call out PG 13 for DR 18/19; PG 17 is newer and works fine in
  practice for the project-DB use case (DR uses basic SQL + the
  stable wire protocol). If you hit a compat issue with a specific
  DR build, file an issue; we can always pin to PG 16 (community
  repo) instead.
- **First publish that actually lands an image.** alpha.1, alpha.2,
  and alpha.3 produced no `ghcr.io/uncommon-fix/ha-addon-davinci-resolve`
  image at all (apostrophe → semicolon → missing package, in that
  order). alpha.4 is the first usable release; install / upgrade
  starts here.

## 0.1.0-alpha.3

- **CI fix round two — still no functional changes.** The alpha.2
  build also failed because the `description:` field still contained
  a SEMICOLON (`SMB share; only the project database lives here.`).
  The `home-assistant/builder` composite action splices the
  description into a bash context where `;` is a statement
  separator, so bash tried to execute `only` as a command and bailed
  with `line 9: only: command not found`. Rewrote the sentence
  without `;` (split into two sentences). No image at
  `ghcr.io/uncommon-fix/ha-addon-davinci-resolve:alpha.2` was
  published; alpha.3 is the first usable release.

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
