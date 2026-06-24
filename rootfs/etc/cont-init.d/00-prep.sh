#!/usr/bin/with-contenv bashio
# DaVinci Resolve Postgres addon — cont-init.
#
# Runs once per container start. Idempotent on every step (only does work
# if state is missing). On the very first boot it initialises PGDATA at
# /data/pgdata, writes a custom postgresql.conf + pg_hba.conf, generates a
# random superuser password, and stamps /data/davinci.yml so subsequent
# boots see the cluster is provisioned.
set -euo pipefail

bashio::log.info "davinci-resolve cont-init: bootstrap + perms"

PGDATA=/data/pgdata
CONFIG=/data/davinci.yml
LIBRARIES=/data/libraries.yml
PG_BIN=/usr/libexec/postgresql17

# Postgres uid/gid from the postgres user we'll exec as.
PG_UID=$(id -u postgres)
PG_GID=$(id -g postgres)

# 1. /data is owned by root by default (the supervisor mounts it).
#    initdb and the postgres longrun both need RW access to /data/pgdata,
#    so chown the dir once. Defensive chown on every boot covers image
#    rebuilds where UIDs may have drifted (parity with the traefik addon's
#    99-deploy-integration step).
mkdir -p "${PGDATA}"
chown -R "${PG_UID}:${PG_GID}" /data
chmod 700 "${PGDATA}"

# 1b. /run is a tmpfs and gets wiped on EVERY container start. Postgres
#     needs /run/postgresql/ to exist + be writable by the postgres user
#     so it can bind the unix socket .s.PGSQL.5432 there. Without this,
#     postgres starts TCP-only and the backend's asyncpg.connect() to the
#     unix socket fails with `[Errno 2] No such file or directory` — which
#     surfaces in the UI as "Couldn't create library" with that exact
#     wording. Alpha.5 fix; mkdir is idempotent.
mkdir -p /run/postgresql
chown "${PG_UID}:${PG_GID}" /run/postgresql
chmod 0775 /run/postgresql

# 2. First-boot initdb. The PGDATA/PG_VERSION sentinel file is written by
#    initdb itself and is the standard "cluster has been initialised"
#    signal; postgres' own startup checks for it too.
if [ ! -f "${PGDATA}/PG_VERSION" ]; then
    bashio::log.info "first boot — initdb scram-sha-256 + UTF-8"
    # --auth-host=scram-sha-256: modern auth; DR 18+ supports it.
    # --auth-local=trust: the backend connects via the local unix socket
    #   as the postgres superuser to create DBs/roles; trust on the local
    #   socket is fine because the socket only exists inside this
    #   container (no host bind mount).
    # --encoding=UTF8 + locale=C.UTF-8: stable across hosts; PG default
    #   on Alpine.
    su-exec postgres "${PG_BIN}/initdb" \
        --pgdata="${PGDATA}" \
        --auth-host=scram-sha-256 \
        --auth-local=trust \
        --encoding=UTF8 \
        --locale=C.UTF-8 \
        --username=postgres

    bashio::log.info "writing postgresql.conf + pg_hba.conf overrides"
    # Listen on all interfaces inside the container. The supervisor's
    # `ports:` mapping in config.yaml exposes 5432 to the LAN; PG itself
    # has to listen on 0.0.0.0 (not just 127.0.0.1) so the bridge network
    # forward works.
    cat > "${PGDATA}/postgresql.conf" <<'CONF'
# DaVinci Resolve addon — Postgres homelab defaults
# Comments mark non-defaults; everything else is the PG 17 stock default.

listen_addresses = '*'         # bind all NICs inside the container
port = 5432
# Pin the unix-socket dir to the path the backend connects on. /run is
# tmpfs in the container; cont-init recreates the dir on every boot.
# Without this explicit pin, PG might fall back to its compile-time
# default (sometimes /tmp) and the backend's asyncpg.connect would fail
# with [Errno 2] looking at /run/postgresql/.s.PGSQL.5432.
unix_socket_directories = '/run/postgresql'

# Modest memory footprint suitable for an HA Pi (1-2 GB available RAM).
# Tune in-place by editing this file directly + restarting the addon if
# the workload grows; the addon UI does not (yet) surface a tuning panel.
max_connections = 20
shared_buffers = 128MB
work_mem = 4MB

# Auth: scram-sha-256 for TCP, password storage matches the initdb
# encryption above.
password_encryption = scram-sha-256

# Logging: send everything to stderr so s6-overlay picks it up into the
# addon log (visible under Settings -> Add-ons -> DaVinci Resolve Postgres
# -> Log).
log_destination = 'stderr'
logging_collector = off
log_min_duration_statement = 250ms
log_line_prefix = '%t [%p] '

# WAL + checkpoints — small DBs, default-ish settings are fine.
wal_level = replica
max_wal_size = 256MB
min_wal_size = 80MB
CONF

    cat > "${PGDATA}/pg_hba.conf" <<'CONF'
# DaVinci Resolve addon — pg_hba
#
# Local socket (inside the container only): trust. The backend connects
# here as `postgres` to manage libraries; no auth surface exposed.
#
# Network: scram-sha-256 from anywhere. Per-DB GRANTs gate access -- each
# library has its own role that can only see its own database. The LAN
# user runs DaVinci Resolve and presents the username/password the addon
# generated when they clicked "Create library".
local   all             all                                     trust
host    all             all             0.0.0.0/0               scram-sha-256
host    all             all             ::/0                    scram-sha-256
CONF

    chown -R "${PG_UID}:${PG_GID}" "${PGDATA}"
    chmod 600 "${PGDATA}/postgresql.conf" "${PGDATA}/pg_hba.conf"
fi

# 2b. Alpha.5 upgrade migration: ensure unix_socket_directories is pinned
#     in postgresql.conf for installs that originally came up on alpha.4
#     (which omitted the line and let postgres pick its compile-time
#     default, which on Alpine 3.23's postgresql17 evidently isn't
#     /run/postgresql/). Idempotent: only appends if the line isn't
#     already there. Safe to run on every boot.
if [ -f "${PGDATA}/postgresql.conf" ] \
   && ! grep -q "^unix_socket_directories" "${PGDATA}/postgresql.conf"; then
    bashio::log.info "alpha.5 migration: pinning unix_socket_directories in existing postgresql.conf"
    printf "\n# alpha.5 migration: pinned to match backend's asyncpg socket path\nunix_socket_directories = '/run/postgresql'\n" \
        >> "${PGDATA}/postgresql.conf"
    chown "${PG_UID}:${PG_GID}" "${PGDATA}/postgresql.conf"
fi

# 3. Stamp the addon config file with a superuser password (used internally
#    by /api/admin endpoints in the future; the backend's day-to-day path
#    is unix-socket trust, no password needed). Idempotent: only writes
#    if the file doesn't exist.
if [ ! -f "${CONFIG}" ]; then
    SUPERUSER_PW=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
    cat > "${CONFIG}" <<EOF
# DaVinci Resolve Postgres addon — persistent state
# Written once on first boot. Do not edit unless you know what you're doing.
version: "${ADDON_VERSION:-unknown}"
superuser_password: "${SUPERUSER_PW}"
EOF
    chown "${PG_UID}:${PG_GID}" "${CONFIG}"
    chmod 600 "${CONFIG}"
    bashio::log.info "wrote /data/davinci.yml (superuser password stored)"
fi

# 4. Bootstrap an empty libraries catalog. The backend appends to this on
#    every Create; reads it on every GET /api/libraries. PG is the source
#    of truth for the actual DB+role existence — libraries.yml is a fast
#    cache so the UI doesn't have to hit PG just to list names.
if [ ! -f "${LIBRARIES}" ]; then
    cat > "${LIBRARIES}" <<'EOF'
version: 1
libraries: []
EOF
    chown "${PG_UID}:${PG_GID}" "${LIBRARIES}"
    chmod 600 "${LIBRARIES}"
fi

bashio::log.info "cont-init done — handing off to s6 services (postgres + backend)"
