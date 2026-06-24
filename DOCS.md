# DaVinci Resolve Postgres add-on — docs

## First-run

1. **Add-on store → DaVinci Resolve Postgres → Install → Start.**
   The supervisor builds (or pulls, for store-installed) the image,
   first boot runs `initdb` into `/data/pgdata`, writes a sane
   `postgresql.conf` + `pg_hba.conf`, and starts Postgres on
   `:5432` + the management UI on ingress.
2. **Open Web UI.** The header status strip should show
   `PG 17.x · 0 libraries · up · <your-Pi-IP>:5432`.

## Create a library (one per DaVinci Resolve project DB)

1. Click **Create library**. Pick a short name (`family-vlogs`,
   `work_2026`, etc.) — lowercase letter first, then alphanumerics +
   `_` / `-`, 3–32 chars.
2. The add-on creates a fresh database (`dr_<name>`) and a role
   (`<name>`) with a random 32-char password.
3. The modal flips to a **Connection details** card:

   ```
   Database type:  PostgreSQL
   Server name:    <your-Pi-IP>
   Port:           5432
   Database:       dr_family-vlogs
   Username:       family-vlogs
   Password:       ••••••••••••••••••••••••••••••••
   ```

   **The password is shown ONCE.** Copy it (the modal has a
   *Copy connection block* button) — the add-on stores only the SCRAM
   verifier from this point on; the cleartext can't be recovered.
4. In DaVinci Resolve: **Project Manager → Databases → New Database →
   PostgreSQL**, paste the values, **Save**.

If you lose the password later, click **Reset password** on the
library card. The previous password stops working immediately; copy
the new one into any DaVinci client that connects to this library.

## Delete a library

Click **Delete** on a library card → type the library name to confirm
→ **Delete library**.

This drops the database AND the role. DaVinci Resolve projects stored
in this library are gone; **media files on your SMB share are NOT
affected**.

## Backup + restore

`backup: cold` in `config.yaml`. Home Assistant's full backup workflow
already covers this add-on:

- **Settings → System → Backups → Create backup** (or any scheduled
  HA backup).
- The supervisor stops the add-on, snapshots `/data` (which contains
  `pgdata/` + `libraries.yml` + `davinci.yml`), restarts the add-on.
- Restoring from a backup brings every library — schema, data,
  credentials — back exactly as it was at snapshot time.

There is no per-library export in alpha.1 — the HA full backup is the
only backup path. If you want one-shot `.sql` dumps for off-site
archive, file an issue and it'll move up the roadmap.

## Networking

- Postgres listens on `0.0.0.0:5432` inside the container.
  The `ports:` mapping in `config.yaml` forwards 5432/tcp to the host;
  DaVinci Resolve clients on the same LAN connect to
  `<your-HA-Pi-IP>:5432` directly.
- The Web UI is only available through HA ingress (admin users only,
  per `panel_admin: true`).
- Per-DB GRANTs gate access — each library's role can only see its own
  database. Even though Postgres accepts TCP from `0.0.0.0/0` (a LAN
  thing), the user needs a valid `<library>` / `<password>` pair
  scoped to one DB.

If you want to lock things tighter, use your router/firewall to
restrict access to port 5432.

## Troubleshooting

### "Library exists in catalog but role missing in Postgres"

`/data/libraries.yml` is a fast cache of catalog state; PG is the
truth. If the two drift (e.g. you restored a partial backup, or PG
crashed mid-`CREATE`), reset-password will surface a clear
"role missing" error and the cleanest fix is **Delete library** +
**Create library** again.

### "Couldn't connect from DaVinci Resolve"

- Confirm the addon is **Started** and the header status strip says
  `up`.
- Confirm port 5432 is exposed on the LAN — Home Assistant's add-on
  **Network** tab shows the mapping; the default `5432/tcp → 5432` is
  open by default.
- From any LAN host: `pg_isready -h <your-Pi-IP> -p 5432` should
  return `accepting connections`.
- Re-copy the password from a fresh **Reset password** if you suspect
  a copy-paste error mangled it.

### "Backup window seems long"

`backup: cold` stops PG during the snapshot. With many large
libraries on slow storage the addon can be down for tens of seconds.
This is intentional — quiesced snapshots are the cleanest path for a
database; "hot" backup with `pg_dumpall` is a possible future option
(file an issue if you want it).

## Versions

- PostgreSQL **17** (from Alpine 3.23's main repo).
- DaVinci Resolve **18** + **19** clients supported (both use
  `scram-sha-256`); **DR 17** is not (requires PG 9.5 + `md5`).
- HA add-on base image: `ghcr.io/home-assistant/base:3.23`.
- Multi-arch images on GHCR: `aarch64` (RPi HAOS), `amd64` (dev VM).
