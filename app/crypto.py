"""Encryption of stored credentials and hashing of login passwords.

Task Hub holds OAuth refresh tokens and app-specific passwords for up to ten
accounts across seven services. Those are long-lived credentials to somebody's
entire task and calendar history, so they are encrypted at rest with a key kept
in a separate file from the database. Copying ``taskhub.db`` off the volume
without also copying ``secret.key`` yields nothing useful.
"""

from __future__ import annotations

import json
from typing import Any

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_fernet_key

_fernet: Fernet | None = None


def _cipher() -> Fernet:
    """Lazily build the cipher so importing this module never touches disk."""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(get_fernet_key())
    return _fernet


# --- Credential blobs ---------------------------------------------------------


def encrypt_json(payload: dict[str, Any]) -> str:
    """Encrypt a credential dictionary for storage in a text column."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _cipher().encrypt(raw).decode("ascii")


def decrypt_json(token: str | None) -> dict[str, Any]:
    """Decrypt a credential blob.

    Returns an empty dict when the value is missing or undecryptable rather than
    raising. A lost or rotated key should surface in the UI as "this account
    needs reconnecting", not as a crash that takes down every other service.
    """
    if not token:
        return {}
    try:
        return json.loads(_cipher().decrypt(token.encode("ascii")).decode("utf-8"))
    except (InvalidToken, ValueError, UnicodeDecodeError):
        return {}


def is_decryptable(token: str | None) -> bool:
    """Whether a stored blob can still be read with the current key."""
    if not token:
        return False
    try:
        _cipher().decrypt(token.encode("ascii"))
        return True
    except (InvalidToken, ValueError):
        return False


# --- Password hashing ---------------------------------------------------------
#
# bcrypt is used for both the web login and the Radicale htpasswd file. Radicale
# reads that file on every request, so the same hash format serves both and the
# two stay in step without a second hashing scheme to reason about.

#: bcrypt silently truncates at 72 bytes; reject longer input instead of
#: accepting a password whose tail is ignored.
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password is too long ({len(encoded)} bytes); "
            f"the maximum is {MAX_PASSWORD_BYTES} bytes."
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:MAX_PASSWORD_BYTES],
                              hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False
