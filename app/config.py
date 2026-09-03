"""Filesystem layout, process-level configuration and secret bootstrapping.

Everything Task Hub persists lives under a single data directory (``/data`` in
the container, mounted from a Docker volume). Keeping every mutable file in one
place is what makes backup, restore and "delete everything and start over" a
single-directory operation rather than a scavenger hunt.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _data_dir() -> Path:
    """Resolve the data directory, defaulting to ./data outside a container."""
    raw = os.environ.get("TASKHUB_DATA_DIR")
    if raw:
        return Path(raw)
    # Running from a source checkout (development): keep data beside the code.
    return Path(__file__).resolve().parent.parent / "data"


DATA_DIR: Path = _data_dir()

# --- Individual paths within the data directory -------------------------------

DB_PATH: Path = DATA_DIR / "taskhub.db"

#: Fernet key used to encrypt service credentials (OAuth tokens, app-specific
#: passwords) before they are written to the database.
SECRET_KEY_PATH: Path = DATA_DIR / "secret.key"

#: Key for signing browser session cookies. Separate from SECRET_KEY_PATH so
#: that rotating one does not invalidate the other.
SESSION_KEY_PATH: Path = DATA_DIR / "session.key"

RADICALE_DIR: Path = DATA_DIR / "radicale"
RADICALE_COLLECTIONS: Path = RADICALE_DIR / "collections"
RADICALE_USERS_FILE: Path = RADICALE_DIR / "users"  # htpasswd format

#: Where Radicale is mounted inside this application. The CalDAV client talks to
#: the embedded server through this prefix, and so can any external CalDAV app.
RADICALE_MOUNT_PATH = "/radicale"

# --- Obsidian -----------------------------------------------------------------

#: Everything the Obsidian headless client owns. It lives in the data volume
#: rather than in the container's home directory so that a rebuild does not
#: throw away the login and force the whole vault to be downloaded again.
OBSIDIAN_DIR: Path = DATA_DIR / "obsidian"

#: The headless client's own state: the saved login and its sync database. The
#: client looks for this under XDG_CONFIG_HOME, which the Dockerfile points here.
OBSIDIAN_CONFIG_DIR: Path = OBSIDIAN_DIR / "config"

#: Where synced vaults are written, one directory per vault.
OBSIDIAN_VAULTS_DIR: Path = OBSIDIAN_DIR / "vaults"

# --- Source tree paths --------------------------------------------------------

APP_DIR: Path = Path(__file__).resolve().parent
TEMPLATES_DIR: Path = APP_DIR / "templates"
STATIC_DIR: Path = APP_DIR / "static"
DOCS_DIR: Path = APP_DIR.parent / "docs"


@dataclass(frozen=True)
class RuntimeConfig:
    """Configuration that comes from the environment rather than the database.

    Anything a user can change at runtime belongs in the ``app_settings`` table
    instead; this holds only what must be known before the database exists.
    """

    #: Normally empty. Task Hub works out its own public address from each
    #: request, so that one image serves a LAN address, a tunnel, a Tailscale
    #: name or a reverse proxy without being configured for any of them. This
    #: is only a manual override for the case where the address to hand out is
    #: not the address being used -- and Settings offers the same override in
    #: the interface, which takes precedence over this one.
    base_url_override: str

    #: Bind address of the embedded server, used for loopback self-calls.
    internal_url: str

    #: Set by docker-compose; only a fallback until onboarding sets a timezone.
    default_timezone: str

    @property
    def radicale_url(self) -> str:
        """Loopback URL of the embedded Radicale server.

        The CalDAV client connects here rather than through ``base_url`` so that
        syncing keeps working even when the public hostname is unreachable from
        inside the container (which is the normal case behind a reverse proxy).
        """
        return f"{self.internal_url.rstrip('/')}{RADICALE_MOUNT_PATH}"


def load_runtime_config() -> RuntimeConfig:
    override = (os.environ.get("TASKHUB_BASE_URL") or "").strip().rstrip("/")
    port = os.environ.get("TASKHUB_PORT", "8080")
    internal_url = os.environ.get("TASKHUB_INTERNAL_URL", f"http://127.0.0.1:{port}")
    tz = os.environ.get("TZ") or "UTC"
    return RuntimeConfig(
        base_url_override=override,
        internal_url=internal_url.rstrip("/"),
        default_timezone=tz,
    )


RUNTIME: RuntimeConfig = load_runtime_config()


# --- Bootstrap ----------------------------------------------------------------


def ensure_directories() -> None:
    """Create the data directory tree. Safe to call on every startup."""
    for path in (DATA_DIR, RADICALE_DIR, RADICALE_COLLECTIONS,
                 OBSIDIAN_DIR, OBSIDIAN_CONFIG_DIR, OBSIDIAN_VAULTS_DIR):
        path.mkdir(parents=True, exist_ok=True)
    # Credentials live here, so keep the tree private to the running user.
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        # Bind mounts on Windows/macOS may not support chmod; not fatal.
        pass


def _read_or_create_key(path: Path, generator) -> bytes:
    """Return the key stored at ``path``, creating it on first run.

    Written with ``0o600`` and an exclusive create so two workers starting at
    once cannot race and produce two different keys.
    """
    if path.exists():
        return path.read_bytes().strip()

    key = generator()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Another worker won the race; use whatever it wrote.
        return path.read_bytes().strip()
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def get_fernet_key() -> bytes:
    """Encryption key for stored service credentials."""
    from cryptography.fernet import Fernet

    return _read_or_create_key(SECRET_KEY_PATH, Fernet.generate_key)


def get_session_secret() -> str:
    """Signing key for session cookies."""
    key = _read_or_create_key(
        SESSION_KEY_PATH, lambda: secrets.token_urlsafe(48).encode("ascii")
    )
    return key.decode("ascii")

def _asset_version() -> str:
    """A token that changes whenever the CSS or JS on disk changes.

    Static files are served with an ETag but browsers apply heuristic caching to
    anything without a Cache-Control header, so a stylesheet could stay stale
    for hours after an upgrade -- long enough for a user to report a fixed bug
    as still broken. Stamping the URL makes a changed file a different URL,
    which no cache can get wrong.
    """
    newest = 0.0
    for path in (STATIC_DIR / "css", STATIC_DIR / "js"):
        if not path.exists():
            continue
        for item in path.rglob("*"):
            if item.is_file():
                newest = max(newest, item.stat().st_mtime)
    return str(int(newest))


ASSET_VERSION: str = _asset_version()
