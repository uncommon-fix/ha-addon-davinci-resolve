ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# DaVinci Resolve Postgres addon — alpha.4
#
# Apk layout:
# - postgresql17 + postgresql17-contrib: the server. HA base 3.23 == Alpine
#   3.23, which ships PG 16 in community + 17/18 in main; we use 17 to stay
#   in main (no --repository flag needed) and pick the LTS-class current
#   release. Alpha.1 + alpha.2 + alpha.3 used postgresql15 which Alpine 3.23
#   dropped from its package index entirely (the migration of `image:` =
#   alpine:3.23 vs the earlier 3.21 happened upstream between drafts).
# - py3-aiohttp: backend HTTP framework (same as traefik addon, ingress UI).
# - py3-asyncpg: async PG client used by the backend to manage libraries.
# - py3-yaml: parse + write /data/libraries.yml + /data/davinci.yml.
# - su-exec: drop from root to postgres UID for the daemon longrun.
# - ca-certificates: belt-and-braces; not strictly needed by PG itself.
#
# One RUN to keep layer count down. The smoke test at the end (`postgres
# --version` + python3 import asyncpg) fails the build loud if any apk pull
# regressed.
RUN apk add --no-cache \
        postgresql17 postgresql17-contrib \
        python3 py3-aiohttp py3-asyncpg py3-yaml \
        su-exec ca-certificates \
 && postgres --version \
 && python3 -c "import asyncpg, aiohttp, yaml; print('backend deps ok')"

# Alpine.js vendored at build time with SHA256 pinning (network at BUILD,
# not at user runtime). Matches the traefik addon's pattern. Tailwind is
# committed to web/static/ (see ALPHA.1 NOTE below) so this RUN doesn't
# need to fetch it.
ARG ALPINEJS_VERSION=3.15.12
ARG ALPINEJS_SHA256=57b37d7cae9a27d965fdae4adcc844245dfdc407e655aee85dcfff3a08036a3f

RUN mkdir -p /usr/share/davinci-web/static \
 && wget -q "https://cdn.jsdelivr.net/npm/alpinejs@${ALPINEJS_VERSION}/dist/cdn.min.js" \
        -O "/usr/share/davinci-web/static/alpinejs-${ALPINEJS_VERSION}.min.js" \
 && echo "${ALPINEJS_SHA256}  /usr/share/davinci-web/static/alpinejs-${ALPINEJS_VERSION}.min.js" \
        | sha256sum -c -

# rootfs (cont-init + service longruns). HA base 3.23 ships s6-overlay v3
# which supports the legacy /etc/cont-init.d/ + /etc/services.d/ paths via
# its compat layer. After COPY, explicitly chmod +x the run scripts --
# Windows checkouts have no POSIX exec bits, and COPY --chmod doesn't
# compose with directory-tree COPY (lesson from the traefik addon).
COPY rootfs/ /
RUN chmod +x /etc/services.d/backend/run \
             /etc/services.d/postgres/run \
             /etc/cont-init.d/00-prep.sh

# Backend Python sources -- flat layout under /usr/local/bin/backend/,
# invoked as `python3 /usr/local/bin/backend/server.py` (no -m, no
# PYTHONPATH gotchas).
COPY backend/ /usr/local/bin/backend/

# Static web assets the backend serves from
# /usr/share/davinci-web/{index.html,static/}. Re-COPY merges with the
# vendored Alpine.js from the earlier RUN (same target dir).
COPY web/ /usr/share/davinci-web/

# ALPHA.1 NOTE — alpine + tailwind vendoring:
# Tailwind is committed to web/static/tailwindcss-3.4.17.min.js (~400 KB)
# matching the traefik addon's alpha.15 vendor-tailwind move. Alpine.js is
# fetched at build time and SHA-pinned above. Both end up under
# /usr/share/davinci-web/static/ in the final image — no runtime CDN.

# BUILD_VERSION is auto-injected by the home-assistant/builder CI action
# from config.yaml's `version:` field. Default below covers local builds —
# keep it in sync with config.yaml's `version:` when bumping. The backend
# reads ADDON_VERSION as a runtime env var (cache-busts app.js via a
# `?v=<version>` query string on the <script src>, parity with the traefik
# addon's alpha.14 pattern).
ARG BUILD_VERSION=0.1.0-alpha.4
ENV ADDON_VERSION=${BUILD_VERSION}

# No CMD: s6-overlay's `legacy-services` service runs CMD if present; with
# no CMD it becomes a silent no-op longrun. Our two real services are under
# /etc/services.d/{postgres,backend}/.
