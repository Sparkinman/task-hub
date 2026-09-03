"""Embeds the Radicale CalDAV server inside the Task Hub web application.

Radicale ships a standard WSGI application, so rather than running it as a
second container with its own port and its own authentication handshake, Task
Hub mounts it directly at ``/radicale``. That gives one image, one port and one
process to supervise, which matters because the only command you should ever
have to type is ``docker compose up``.

External CalDAV clients (Apple Calendar, DAVx5, Thunderbird) point at the same
mount, so the embedded server is a real, fully addressable CalDAV endpoint --
not a private implementation detail.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from radicale import config as radicale_config
from radicale.app import Application as RadicaleApplication

from app.config import (
    RADICALE_COLLECTIONS,
    RADICALE_USERS_FILE,
    ensure_directories,
)

logger = logging.getLogger(__name__)


def _write_default_users_file() -> None:
    """Ensure an htpasswd file exists so Radicale can start.

    Radicale refuses to start when ``htpasswd_filename`` points at a missing
    file, but on a first run the onboarding wizard has not chosen a username
    yet. An empty file satisfies Radicale and authenticates nobody, which is the
    correct behaviour for a server with no accounts.
    """
    if RADICALE_USERS_FILE.exists():
        return
    RADICALE_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(RADICALE_USERS_FILE, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)


def build_configuration() -> radicale_config.Configuration:
    """Construct Radicale's configuration entirely in memory.

    Deliberately not read from a config file: the settings below are decided by
    Task Hub, and a stray edited file on the volume silently changing storage
    paths or authentication is a failure mode worth designing out.
    """
    ensure_directories()
    _write_default_users_file()

    configuration = radicale_config.load()
    configuration.update(
        {
            "auth": {
                "type": "htpasswd",
                "htpasswd_filename": str(RADICALE_USERS_FILE),
                # We always write bcrypt hashes, so pin the scheme rather than
                # paying autodetect's cost on every single request.
                "htpasswd_encryption": "bcrypt",
                # Without this, every CalDAV request re-runs a 12-round bcrypt
                # verification. A sync pass makes hundreds of requests, so the
                # login cache is the difference between a snappy sync and one
                # that spends most of its time hashing the same password.
                "cache_logins": "True",
                "cache_successful_logins_expiry": "60",
                "cache_failed_logins_expiry": "20",
                # Radicale sleeps this long after a failed login. The default of
                # one second turns a stale saved password into an apparent hang.
                "delay": "0",
                "realm": "Task Hub CalDAV",
            },
            # owner_only: a user may read and write only their own collections,
            # which is exactly the isolation we want between Radicale accounts.
            "rights": {"type": "owner_only"},
            "storage": {
                "type": "multifilesystem",
                "filesystem_folder": str(RADICALE_COLLECTIONS),
            },
            # Radicale's own web interface, reachable at /radicale/.web/. Every
            # management action has a Task Hub equivalent, so nobody needs it --
            # but it is the only way to look at the raw CalDAV view of a
            # collection, which is genuinely useful when a sync looks wrong. It
            # is linked from the Radicale page rather than the main navigation,
            # so the Task Hub UI stays the obvious path.
            "web": {"type": "internal"},
            "logging": {"level": "warning", "mask_passwords": "True"},
            # Leave server.script_name empty so Radicale derives its base prefix
            # from the WSGI SCRIPT_NAME that the ASGI mount provides. That keeps
            # the mount point defined in exactly one place.
            "server": {"script_name": ""},
        },
        "Task Hub embedded configuration",
    )
    return configuration


_application: RadicaleApplication | None = None


def get_radicale_app() -> RadicaleApplication:
    """Return the shared Radicale WSGI application, building it on first use."""
    global _application
    if _application is None:
        # Radicale dumps its entire resolved configuration at INFO on startup --
        # roughly a hundred lines. Embedded, that buries Task Hub's own startup
        # output and makes `docker compose logs` useless for diagnosing a real
        # problem, so its logger is pinned to warnings and above.
        logging.getLogger("radicale").setLevel(logging.WARNING)
        _application = RadicaleApplication(build_configuration())
        logger.info("Radicale storage: %s", RADICALE_COLLECTIONS)
    return _application


def reload_radicale() -> None:
    """Drop the cached application so the next request rebuilds it.

    Only needed when storage configuration changes. Adding or removing users
    does not require this: Radicale re-reads the htpasswd file as needed, so the
    onboarding wizard's new account works on the very next request.
    """
    global _application
    _application = None


# --- htpasswd management ------------------------------------------------------
#
# The wizard creates the Radicale account, so writing htpasswd entries has to be
# something the web UI can do. These helpers keep that file consistent.


def _read_users() -> dict[str, str]:
    """Parse the htpasswd file into {username: hash}."""
    if not RADICALE_USERS_FILE.exists():
        return {}
    users: dict[str, str] = {}
    for line in RADICALE_USERS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        username, _, hashed = line.partition(":")
        users[username] = hashed
    return users


def _write_users(users: dict[str, str]) -> None:
    """Rewrite the htpasswd file atomically.

    Written to a temporary file and renamed so that a crash mid-write cannot
    leave a truncated file that locks every account out of Radicale.
    """
    RADICALE_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RADICALE_USERS_FILE.with_suffix(".tmp")
    body = "".join(f"{name}:{hashed}\n" for name, hashed in sorted(users.items()))
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, RADICALE_USERS_FILE)


def set_radicale_user(username: str, password_hash: str) -> None:
    """Create or update a Radicale account with an already-bcrypted password."""
    users = _read_users()
    users[username] = password_hash
    _write_users(users)
    logger.info("Radicale account written: %s", username)


def delete_radicale_user(username: str) -> None:
    users = _read_users()
    if users.pop(username, None) is not None:
        _write_users(users)


def list_radicale_users() -> list[str]:
    return sorted(_read_users())


def radicale_user_exists(username: str) -> bool:
    return username in _read_users()


def user_storage_path(username: str) -> Path:
    """Filesystem location of a user's collections, for size reporting."""
    return RADICALE_COLLECTIONS / "collection-root" / username
