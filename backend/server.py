"""DaVinci Resolve Postgres addon — aiohttp backend.

One process. Serves the SPA at `/`, static assets at `/static/`, and the
library-management JSON API at `/api/`. Listens on :8080; HA's ingress
proxy rewrites public URLs to /api/hassio_ingress/<token>/. The Postgres
daemon listens on :5432 (separate s6 service); DaVinci Resolve clients
on the LAN connect there directly with the user/password pair the UI
issues at library creation.

Endpoint surface:
    GET    /                            SPA shell
    GET    /static/<path>               vendored JS + (eventually) icons
    GET    /api/libraries               list (catalog cache, sorted by name)
    POST   /api/libraries               create -- returns one-time password
    DELETE /api/libraries/<name>        drop DB + role + remove from catalog
    POST   /api/libraries/<name>/reset-password   rotate password
    GET    /api/admin/state             pg up/version + library_count + host_hint
    POST   /api/session/claim           one-editor-at-a-time gate (parity with traefik alpha.12)
    POST   /api/session/takeover

Session model: same one-editor-at-a-time gating as the traefik addon
alpha.12, simplified -- DR doesn't have a "draft" surface, so claim +
takeover suffice. The intent is to avoid two browser tabs simultaneously
creating "library" entries with the same name and racing on
libraries.yml's append.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import os
import re
import secrets
import socket
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import yaml
from aiohttp import web

import pg_admin

# --------------------------- paths + constants ---------------------------

DATA = Path("/data")
LIBRARIES_YML = DATA / "libraries.yml"
DAVINCI_YML = DATA / "davinci.yml"
WEB_ROOT = Path("/usr/share/davinci-web")

# alpha.1: addon version, exported by Dockerfile (ENV ADDON_VERSION=$BUILD_VERSION).
# Used as the cache-buster on app.js + as the X-Addon-Version check value
# for stale-tab detection (parity with traefik addon alpha.20).
ADDON_VERSION = os.environ.get("ADDON_VERSION", "dev")

# HA ingress strips the path prefix and supplies it on every request via
# the X-Ingress-Path header; the SPA reads it from a <meta> tag to build
# absolute API URLs. Whitelist regex parity with the traefik addon: only
# the supervisor's known shape is accepted, anything else is empty
# (defence against a malicious upstream header).
INGRESS_RE = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_-]+/?$")

PG_PORT = 5432
SESSION_TTL = 60.0      # seconds; matches traefik addon's session model

# Allowed origins for the password-show endpoint (none -- there's no CORS;
# included for symmetry with the traefik backend's _strip_headers helper).

# --------------------------- catalog (libraries.yml) ---------------------

def _read_catalog() -> dict:
    """Returns the parsed /data/libraries.yml or a fresh skeleton."""
    if not LIBRARIES_YML.exists():
        return {"version": 1, "libraries": []}
    try:
        return yaml.safe_load(LIBRARIES_YML.read_text()) or {"version": 1, "libraries": []}
    except yaml.YAMLError as e:
        raise web.HTTPInternalServerError(
            text=f"/data/libraries.yml is unparseable; fix by hand: {e}"
        )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Crash-safe write: write to .tmp + fsync + atomic rename + parent fsync.
    Mirrors the traefik addon's _atomic_write_bytes."""
    tmp = path.parent / (path.name + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_catalog(doc: dict) -> None:
    text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=4096)
    _atomic_write_bytes(LIBRARIES_YML, text.encode("utf-8"))


def _append_library(name: str, db: str, user: str) -> None:
    """Append a freshly-created library to the catalog. password is NEVER
    persisted -- PG holds the SCRAM verifier; the cleartext was shown
    once in the API response and the user copied it into DaVinci."""
    doc = _read_catalog()
    libs = doc.get("libraries") or []
    libs.append({
        "name": name,
        "db": db,
        "user": user,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    doc["libraries"] = libs
    _write_catalog(doc)


def _remove_library(name: str) -> None:
    doc = _read_catalog()
    libs = doc.get("libraries") or []
    doc["libraries"] = [x for x in libs if x.get("name") != name]
    _write_catalog(doc)


# ------------------------------- host hint -------------------------------

def _host_hint() -> str:
    """Best-effort 'what IP does DaVinci Resolve use to reach this Postgres'.

    The supervisor exposes the host's hostname via the SUPERVISOR_TOKEN
    API at /info, but we don't need the round trip -- our container has
    eth0 reachable on the LAN via the `ports:` forward. Pick a sensible
    default the user can verify by reading their HA Pi's local address.
    """
    try:
        # Trick: connect a UDP socket to a routable target to discover the
        # outbound interface IP. Doesn't actually send anything.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostname()


# --------------------------- session manager -----------------------------
# Trimmed copy of the traefik addon's alpha.12 SessionManager. The gate
# stops two browser tabs from racing on /data/libraries.yml; the DR addon
# doesn't have a draft surface so we don't need refresh-on-match logic.

class _Session:
    __slots__ = ("sid", "last_seen")
    def __init__(self, sid: str):
        self.sid = sid
        self.last_seen = time.time()


class SessionManager:
    def __init__(self) -> None:
        self._current: _Session | None = None

    def _expire(self, now: float) -> None:
        if self._current and now - self._current.last_seen > SESSION_TTL:
            self._current = None

    def claim(self) -> tuple[bool, str, float]:
        now = time.time()
        self._expire(now)
        if self._current is not None:
            return False, "", now - self._current.last_seen
        sid = secrets.token_urlsafe(16)
        self._current = _Session(sid)
        return True, sid, 0.0

    def takeover(self) -> str:
        sid = secrets.token_urlsafe(16)
        self._current = _Session(sid)
        return sid

    def heartbeat(self, sid: str) -> None:
        if self._current and self._current.sid == sid:
            self._current.last_seen = time.time()

    def is_current(self, sid: str) -> bool:
        now = time.time()
        self._expire(now)
        return self._current is not None and self._current.sid == sid


class _HTTPLocked(web.HTTPException):
    status_code = 423


GATED_MUTATIONS: set[tuple[str, str]] = {
    ("POST", "/api/libraries"),
    # DELETE /api/libraries/<name> and POST /api/libraries/<name>/reset-password
    # use prefix matching below.
}
GATED_PREFIXES: tuple[tuple[str, str], ...] = (
    ("DELETE", "/api/libraries/"),
    ("POST", "/api/libraries/"),     # covers reset-password and any future per-library POST
)


@web.middleware
async def session_gate_mw(request: web.Request, handler):
    mgr: SessionManager = request.app["session_mgr"]
    sid_header = request.headers.get("X-Session-Id", "")
    if sid_header:
        mgr.heartbeat(sid_header)
    if _is_gated(request.method, request.path):
        if not mgr.is_current(sid_header):
            raise _HTTPLocked(
                text="Another tab is editing this addon. Reload to claim a new "
                     "session or take over from the takeover prompt."
            )
    return await handler(request)


def _is_gated(method: str, path: str) -> bool:
    if (method, path) in GATED_MUTATIONS:
        return True
    for gated_method, prefix in GATED_PREFIXES:
        if method == gated_method and path.startswith(prefix):
            return True
    return False


# --------------------------- version-skew gate ---------------------------
# Same pattern as traefik addon alpha.20: clients send X-Addon-Version on
# mutating requests; mismatch -> 409 with code VERSION_MISMATCH so the UI
# can prompt the user to reload.

VERSION_UNGATED_PATHS = {"/api/session/claim", "/api/session/takeover"}


@web.middleware
async def version_gate_mw(request: web.Request, handler):
    if (request.method in {"POST", "PUT", "DELETE", "PATCH"}
            and request.path.startswith("/api/")
            and request.path not in VERSION_UNGATED_PATHS):
        client_version = request.headers.get("X-Addon-Version", "")
        if client_version and client_version != ADDON_VERSION:
            return web.json_response(
                {"error": f"Addon version mismatch: client={client_version} "
                          f"server={ADDON_VERSION}. Reload required.",
                 "code": "VERSION_MISMATCH"},
                status=409,
            )
    return await handler(request)


# --------------------------- json error wrap -----------------------------
# Same shape as traefik addon: every error response is `{"error": "..."}`
# JSON. Without this, HTTPException.text returns plain text the UI would
# display verbatim in a code block.

@web.middleware
async def json_error_mw(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException as ex:
        if ex.content_type == "application/json":
            raise
        body = {"error": ex.text or ex.reason or f"HTTP {ex.status}"}
        return web.json_response(body, status=ex.status)
    except Exception:
        sys.stderr.write(traceback.format_exc())
        return web.json_response({"error": "internal error"}, status=500)


# ------------------------------- handlers --------------------------------

async def serve_index(request: web.Request) -> web.Response:
    raw = request.headers.get("X-Ingress-Path", "")
    ingress_path = raw if INGRESS_RE.match(raw) else ""
    ingress_path = _html.escape(ingress_path.rstrip("/"), quote=True)
    html_text = (
        (WEB_ROOT / "index.html").read_text()
        .replace("{{INGRESS_PATH}}", ingress_path)
        .replace("{{APP_VERSION}}", ADDON_VERSION)
    )
    resp = web.Response(text=html_text, content_type="text/html")
    # Same-origin iframe lock; HA ingress is same-origin to our SPA.
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


async def get_libraries(request: web.Request) -> web.Response:
    doc = _read_catalog()
    return web.json_response({
        "libraries": doc.get("libraries") or [],
        "host_hint": _host_hint(),
        "port": PG_PORT,
    })


async def post_libraries(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise web.HTTPBadRequest(text=f"invalid JSON: {e}")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="payload: must be object")
    name = body.get("name", "")

    # Validate up front so we don't even hit PG on garbage input.
    try:
        name = pg_admin.validate_library_name(name)
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))

    # Catalog-level dedup (cheap; the underlying PG CREATE ROLE/DATABASE
    # would also fail, but we want a friendly 409 before the round trip).
    existing_names = {x["name"] for x in (_read_catalog().get("libraries") or [])}
    if name in existing_names:
        raise web.HTTPConflict(text=f"library {name!r} already exists")

    try:
        result = await pg_admin.create_library(name)
    except asyncpg.exceptions.DuplicateDatabaseError:
        raise web.HTTPConflict(
            text=f"database for {name!r} already exists in Postgres "
                 "(catalog may be out of sync; check Settings -> Add-ons -> Log)"
        )
    except asyncpg.exceptions.DuplicateObjectError:
        raise web.HTTPConflict(
            text=f"role for {name!r} already exists in Postgres "
                 "(catalog may be out of sync; check Settings -> Add-ons -> Log)"
        )
    except Exception as e:
        # Bubble up as 500 with the JSON wrapper.
        raise web.HTTPInternalServerError(
            text=f"failed to create library {name!r}: {e}"
        )

    _append_library(result["name"], result["db"], result["user"])
    # Return the cleartext password ONCE. Frontend immediately shows it
    # in the connection-details modal; user copies into DaVinci.
    return web.json_response({
        **result,
        "host_hint": _host_hint(),
        "port": PG_PORT,
    })


async def delete_library(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    try:
        name = pg_admin.validate_library_name(name)
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))

    # Catalog miss is non-fatal: PG might still have an orphan DB from a
    # prior crashed create. We attempt the drop regardless and quietly
    # accept "DB doesn't exist" because asyncpg's drop_library already
    # uses IF EXISTS.
    try:
        await pg_admin.drop_library(name)
    except Exception as e:
        raise web.HTTPInternalServerError(
            text=f"failed to drop library {name!r}: {e}"
        )

    _remove_library(name)
    return web.json_response({"deleted": name})


async def post_reset_password(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    try:
        name = pg_admin.validate_library_name(name)
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))

    catalog_names = {x["name"] for x in (_read_catalog().get("libraries") or [])}
    if name not in catalog_names:
        raise web.HTTPNotFound(text=f"library {name!r} not found")

    try:
        result = await pg_admin.reset_password(name)
    except asyncpg.exceptions.UndefinedObjectError:
        # Role missing despite catalog entry. Surface a clear error rather
        # than silently fixing the drift.
        raise web.HTTPInternalServerError(
            text=f"role for {name!r} missing in Postgres -- catalog drift; "
                 "delete the library and recreate."
        )
    except Exception as e:
        raise web.HTTPInternalServerError(
            text=f"failed to reset password for {name!r}: {e}"
        )

    return web.json_response({
        **result,
        "host_hint": _host_hint(),
        "port": PG_PORT,
    })


async def get_admin_state(request: web.Request) -> web.Response:
    """Status strip in the UI header: PG version, library count, up/down."""
    libs = _read_catalog().get("libraries") or []
    pg_up = False
    pg_ver = ""
    try:
        pg_ver = await pg_admin.pg_version()
        pg_up = True
    except Exception:
        pass
    return web.json_response({
        "pg_up": pg_up,
        "pg_version": pg_ver,
        "library_count": len(libs),
        "host_hint": _host_hint(),
        "port": PG_PORT,
        "addon_version": ADDON_VERSION,
    })


# ----------------------------- session API -------------------------------

async def post_session_claim(request: web.Request) -> web.Response:
    mgr: SessionManager = request.app["session_mgr"]
    ok, sid, age = mgr.claim()
    if ok:
        return web.json_response({"sid": sid})
    return web.json_response({"current_age_s": age}, status=409)


async def post_session_takeover(request: web.Request) -> web.Response:
    mgr: SessionManager = request.app["session_mgr"]
    sid = mgr.takeover()
    return web.json_response({"sid": sid})


# --------------------------- lifecycle / app -----------------------------

async def session_ctx(app: web.Application):
    app["session_mgr"] = SessionManager()
    yield


def make_app() -> web.Application:
    # Middleware order: outermost first.
    # - json_error_mw wraps every response (including HTTPException + the
    #   _HTTPLocked from session_gate_mw) as JSON.
    # - version_gate_mw rejects stale clients early so neither session_gate
    #   nor the handler see a mismatched-version request.
    # - session_gate_mw runs innermost.
    app = web.Application(middlewares=[json_error_mw, version_gate_mw, session_gate_mw])
    app.cleanup_ctx.append(session_ctx)
    app.router.add_get("/", serve_index)
    app.router.add_static("/static", str(WEB_ROOT / "static"))
    app.router.add_get("/api/libraries", get_libraries)
    app.router.add_post("/api/libraries", post_libraries)
    app.router.add_delete("/api/libraries/{name}", delete_library)
    app.router.add_post("/api/libraries/{name}/reset-password", post_reset_password)
    app.router.add_get("/api/admin/state", get_admin_state)
    app.router.add_post("/api/session/claim", post_session_claim)
    app.router.add_post("/api/session/takeover", post_session_takeover)
    return app


if __name__ == "__main__":
    # shutdown_timeout matches s6-overlay's default S6_SERVICES_GRACETIME
    # so in-flight requests drain before s6 sends SIGKILL.
    web.run_app(
        make_app(),
        host="0.0.0.0",
        port=8080,
        shutdown_timeout=2.0,
    )
