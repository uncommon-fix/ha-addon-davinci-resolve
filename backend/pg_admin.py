"""DaVinci Resolve Postgres addon — asyncpg helpers.

The backend's `server.py` calls these to manage libraries (= one Postgres
database + one role per library). All connections are via the local unix
socket as the `postgres` superuser; pg_hba.conf trusts that path so no
password is needed.

Identifiers used in SQL strings are validated by `_assert_valid_ident` and
double-quoted via psycopg-style escaping. asyncpg's `$N` parameter
substitution is NOT used for identifiers (it can't be — only literals
take parameters in PG SQL), so we lean on the regex validator + quoting.

Conventions:
    library name  -> validated string, 3-32 chars, ^[a-z][a-z0-9_-]*$
    db name       -> `dr_<library>` (prefixed to avoid collisions with
                     PG's own template1 etc.)
    role name     -> `<library>` (no prefix — what the user pastes into
                     DaVinci Resolve's username field)
"""
from __future__ import annotations

import re
import secrets
import string

import asyncpg

# --------------------------- naming + validation ---------------------------

# A library name is what the user types in the Create modal. Lowercase
# alnum + `_` + `-`, starting with a letter, 3-32 chars. Mirrors PG's own
# regex for unquoted identifiers (minus the case-folding ambiguity that
# comes from accepting uppercase).
LIBRARY_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")

# Reserved against the LIBRARY name (= role name AND part of the DB
# name). The role would clash with PG built-ins; the DB-name collision
# is theoretical (we prefix with `dr_`) but it's cleaner to reject
# obviously-reserved tokens up front. The supervisor's `postgres` role
# is the addon's superuser and must never be touched by user actions.
RESERVED_LIBRARY_NAMES = {
    "postgres", "pg_signal_backend", "pg_read_all_data", "pg_write_all_data",
    "pg_read_server_files", "pg_write_server_files", "pg_execute_server_program",
    "pg_monitor", "pg_read_all_settings", "pg_read_all_stats",
    "pg_stat_scan_tables", "admin", "root", "template", "template0", "template1",
}

# DB name = `dr_<library>`. Role name = `<library>`.
DB_PREFIX = "dr_"


def db_name_for(library: str) -> str:
    return DB_PREFIX + library


def role_name_for(library: str) -> str:
    return library


def validate_library_name(name: str) -> str:
    """Returns the validated name or raises ValueError.

    Callers (server.py) translate the raise into an HTTPBadRequest. Done
    here so the validator stays a pure function on a string -- no aiohttp
    dependency in the SQL helper module.
    """
    if not isinstance(name, str):
        raise ValueError("library name must be a string")
    name = name.strip()
    if not LIBRARY_NAME_RE.match(name):
        raise ValueError(
            f"library name {name!r} invalid -- must match "
            f"{LIBRARY_NAME_RE.pattern} (lowercase letter first, "
            "alphanumerics + _ - only, 3-32 chars)"
        )
    if name in RESERVED_LIBRARY_NAMES:
        raise ValueError(f"library name {name!r} is reserved")
    return name


def _assert_valid_ident(s: str) -> None:
    """Belt-and-braces guard before we splice an identifier into SQL.

    validate_library_name above is the user-facing check; this is the
    final line of defence right before the SQL splice. If a caller ever
    constructs a db/role name programmatically (e.g. via `db_name_for`),
    this still applies because the prefix is constant and the suffix
    came through validate_library_name.
    """
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$", s):
        raise RuntimeError(f"refusing to use identifier {s!r} in SQL")


def _quote_ident(s: str) -> str:
    """Standard double-quoted PG identifier. No `"` characters are allowed
    by the regex above, so we don't need to escape inner double quotes."""
    _assert_valid_ident(s)
    return '"' + s + '"'


def _quote_literal(s: str) -> str:
    """Escape a string LITERAL for PG SQL. Used ONLY for the random
    password in the ALTER ROLE statement -- asyncpg.execute can't
    parameterise role passwords (CREATE/ALTER ROLE doesn't take $N for
    its PASSWORD clause). The password alphabet (alnum + a small set of
    safe symbols) keeps the SQL escape trivial: we double any embedded
    single quotes. The password is also vetted by `random_password`."""
    return "'" + s.replace("'", "''") + "'"


# ----------------------------- random password ----------------------------

# 32 chars of alnum + ! @ - _ . The alphabet is intentionally narrow:
# - Excludes characters that would need escaping in the YAML the user
#   pastes into a DaVinci connection block.
# - Excludes `'` and `"` which would otherwise need to be escaped in
#   _quote_literal above.
# - 32 chars * log2(64) bits = 192 bits of entropy. More than enough.
_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@-_."


def random_password(length: int = 32) -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


# ------------------------------ connect ----------------------------------

async def _connect_superuser() -> asyncpg.Connection:
    """Connect via the local unix socket as `postgres`. pg_hba.conf trusts
    this path; no password needed. The socket dir is the PG 15 default on
    Alpine: /run/postgresql/."""
    return await asyncpg.connect(
        host="/run/postgresql",
        user="postgres",
        database="postgres",
    )


# ----------------------------- public API --------------------------------

async def pg_version() -> str:
    """Returns the running server's version string, e.g. '15.5'.

    Used by /api/admin/state for the status strip in the UI. Raises if PG
    is unreachable -- the caller (the state endpoint) catches and reports
    `pg_up: false`.
    """
    conn = await _connect_superuser()
    try:
        row = await conn.fetchrow("SHOW server_version")
        return row[0]
    finally:
        await conn.close()


async def list_databases() -> list[dict]:
    """Returns a list of `{database, owner}` rows for every database that
    starts with our DB_PREFIX. Used for an integrity check between
    /data/libraries.yml (the fast cache) and PG (the truth)."""
    conn = await _connect_superuser()
    try:
        rows = await conn.fetch(
            "SELECT d.datname, r.rolname "
            "FROM pg_database d JOIN pg_roles r ON d.datdba = r.oid "
            "WHERE d.datname LIKE $1 "
            "ORDER BY d.datname",
            DB_PREFIX + "%",
        )
        return [{"database": r["datname"], "owner": r["rolname"]} for r in rows]
    finally:
        await conn.close()


async def create_library(name: str) -> dict:
    """Create a new library: role + DB + GRANT ALL.

    Returns {name, db, user, password} -- the password is shown ONCE to
    the user via the UI; PG holds the SCRAM hash afterwards, so we
    intentionally do NOT store the cleartext.

    Raises:
        ValueError on invalid name (caller maps to HTTPBadRequest).
        asyncpg.exceptions.DuplicateDatabaseError / DuplicateObjectError
        if the role or DB already exists -- caller maps to HTTPConflict.
    """
    name = validate_library_name(name)
    db = db_name_for(name)
    role = role_name_for(name)
    password = random_password()

    db_ident = _quote_ident(db)
    role_ident = _quote_ident(role)
    pw_literal = _quote_literal(password)

    conn = await _connect_superuser()
    try:
        # CREATE ROLE first. LOGIN + ENCRYPTED PASSWORD; the cluster's
        # password_encryption=scram-sha-256 ensures the stored verifier is
        # SCRAM, not legacy md5.
        await conn.execute(
            f"CREATE ROLE {role_ident} WITH LOGIN ENCRYPTED PASSWORD {pw_literal}"
        )
        # CREATE DATABASE owned by the new role. The role-owns-db gives
        # ALL privileges implicitly; no separate GRANT needed.
        # CREATE DATABASE can't run inside an explicit txn; asyncpg runs
        # each .execute() in its own auto-commit, which is fine here.
        try:
            await conn.execute(
                f"CREATE DATABASE {db_ident} OWNER {role_ident} "
                f"ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8' "
                f"TEMPLATE template0"
            )
        except Exception:
            # CREATE DATABASE failed -- roll back the ROLE we just created
            # so a retry doesn't trip over "role already exists".
            await conn.execute(f"DROP ROLE IF EXISTS {role_ident}")
            raise
    finally:
        await conn.close()

    return {"name": name, "db": db, "user": role, "password": password}


async def drop_library(name: str) -> None:
    """DROP DATABASE + DROP ROLE. The DB drop disconnects any live
    connections via FORCE (PG 13+ feature). After this call, the role's
    name + DB name are free for re-use."""
    name = validate_library_name(name)
    db = db_name_for(name)
    role = role_name_for(name)
    db_ident = _quote_ident(db)
    role_ident = _quote_ident(role)

    conn = await _connect_superuser()
    try:
        # WITH (FORCE) terminates other backends connected to this DB.
        # IF EXISTS makes the drop idempotent against partially-created
        # libraries (e.g. if a prior create crashed between ROLE and DB).
        await conn.execute(f"DROP DATABASE IF EXISTS {db_ident} WITH (FORCE)")
        await conn.execute(f"DROP ROLE IF EXISTS {role_ident}")
    finally:
        await conn.close()


async def reset_password(name: str) -> dict:
    """Generate a fresh random password for an existing library's role.

    Returns {name, db, user, password}. Like create_library, the
    cleartext password is shown ONCE; PG stores the SCRAM hash.

    Raises ValueError if the name is invalid; the underlying ALTER ROLE
    fails with `role does not exist` if the library wasn't actually
    present -- caller maps to HTTPNotFound.
    """
    name = validate_library_name(name)
    role = role_name_for(name)
    password = random_password()
    role_ident = _quote_ident(role)
    pw_literal = _quote_literal(password)

    conn = await _connect_superuser()
    try:
        # ALTER ROLE ... PASSWORD respects the cluster's
        # password_encryption setting -- stores a SCRAM verifier given our
        # cluster config.
        await conn.execute(
            f"ALTER ROLE {role_ident} WITH ENCRYPTED PASSWORD {pw_literal}"
        )
    finally:
        await conn.close()

    return {
        "name": name,
        "db": db_name_for(name),
        "user": role,
        "password": password,
    }
