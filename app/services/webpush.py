"""Web push notifications, without an account anywhere and without a library.

Task Hub is self-hosted and says so, which makes "we will send your alerts
through somebody's cloud" an awkward thing to add. Web push turns out not to
need one. The browser vendor runs a relay -- Google's for Chrome, Apple's for
Safari -- but the message reaching it is already encrypted with a key only the
subscribing browser holds, and the server identifies itself with a keypair it
generates for itself. No account, no registration, nothing that can read the
contents on the way.

That is worth doing properly rather than reaching for a library, because the
two standards involved are small and ``cryptography`` is already in the image
for the credential encryption:

* **RFC 8291** encrypts the payload. A one-off key is agreed with the browser's
  public key, mixed with the subscription's auth secret, and used once.
* **RFC 8292 (VAPID)** signs a short-lived token proving the message came from
  this server, so the relay will accept it.

The alternative was pywebpush, which would pull in its own crypto stack and an
HTTP client for perhaps two hundred lines of arithmetic.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

#: How long a push may sit at the relay waiting for a device that is switched
#: off. A day is right for these messages: a sync that failed this morning is
#: still worth knowing about this evening, and worth nothing next week.
TTL_SECONDS = 86400

#: VAPID tokens are short-lived by design. Twelve hours is inside the maximum
#: every push service accepts (twenty-four) with room for clock drift.
TOKEN_LIFETIME = 12 * 3600


def b64(data: bytes) -> str:
    """URL-safe base64 with no padding, which is what both standards use."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


# --- The server's own identity ------------------------------------------------

def generate_keypair() -> tuple[str, str]:
    """A new VAPID keypair: (private, public), both base64url.

    Made once and kept. Replacing it invalidates every existing subscription,
    because a browser records which key it agreed to accept messages signed by.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    private = b64(key.private_numbers().private_value.to_bytes(32, "big"))
    public = b64(
        key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )
    return private, public


def _load_private(private_b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(
        int.from_bytes(unb64(private_b64), "big"), ec.SECP256R1()
    )


def _vapid_header(endpoint: str, private_b64: str, public_b64: str, subject: str) -> str:
    """The Authorization header proving this server sent the message.

    A JWT signed with the server's key, naming the push service it is being
    presented to. ``cryptography`` signs in DER; the JOSE format wants the two
    halves raw and fixed-width, which is the one fiddly step here.
    """
    parts = urlparse(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(time.time()) + TOKEN_LIFETIME,
        "sub": subject,
    }
    header = b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode()

    der = _load_private(private_b64).sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    signature = b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    return f"vapid t={header}.{body}.{signature},k={public_b64}"


# --- Encrypting one message ---------------------------------------------------

def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """Encrypt for one subscription, in the aes128gcm form browsers expect.

    The result carries everything the browser needs to undo it except the two
    secrets it already has: a random salt, the record size, and the one-off
    public key this message was encrypted with.
    """
    ua_public_bytes = unb64(p256dh)
    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), ua_public_bytes
    )
    auth_secret = unb64(auth)

    # A fresh keypair per message: reusing one would let two messages to the
    # same device share key material.
    as_private = ec.generate_private_key(ec.SECP256R1())
    as_public_bytes = as_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = as_private.exchange(ec.ECDH(), ua_public)

    # The subscription's auth secret is the salt for this first step, which is
    # what ties the key to this subscriber rather than to anyone who knows the
    # public key.
    ikm = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=auth_secret,
        info=b"WebPush: info\x00" + ua_public_bytes + as_public_bytes,
    ).derive(shared)

    salt = os.urandom(16)
    content_key = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=salt,
        info=b"Content-Encoding: aes128gcm\x00",
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=salt,
        info=b"Content-Encoding: nonce\x00",
    ).derive(ikm)

    # 0x02 marks the final record. Task Hub's messages are far smaller than one
    # record, so there is only ever the one.
    ciphertext = AESGCM(content_key).encrypt(nonce, payload + b"\x02", None)

    return (
        salt
        + struct.pack(">I", 4096)          # record size
        + struct.pack(">B", len(as_public_bytes))
        + as_public_bytes
        + ciphertext
    )


# --- Sending ------------------------------------------------------------------

class PushGone(Exception):
    """The subscription is dead and should be forgotten.

    Distinct from a failure to send because the remedy is different: retrying
    will never work, and keeping the row means trying again on every future
    notification for a browser that no longer exists.
    """


def send(
    endpoint: str,
    p256dh: str,
    auth: str,
    message: dict,
    private_b64: str,
    public_b64: str,
    subject: str = "mailto:admin@example.invalid",
    timeout: float = 20.0,
) -> None:
    """Deliver one notification, raising PushGone if the subscription is dead."""
    body = encrypt(json.dumps(message).encode("utf-8"), p256dh, auth)
    headers = {
        "Authorization": _vapid_header(endpoint, private_b64, public_b64, subject),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(TTL_SECONDS),
        # Chrome drops normal-priority pushes to a dozing phone until it next
        # wakes, which for "your sync is failing" is too late to be useful.
        "Urgency": "normal",
    }
    try:
        response = httpx.post(endpoint, content=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach the push service: {exc}") from exc

    if response.status_code in (404, 410):
        raise PushGone(f"The push service says this subscription is gone ({response.status_code}).")
    if response.status_code >= 400:
        raise RuntimeError(
            f"The push service refused the message ({response.status_code}): "
            f"{response.text[:200]}"
        )
