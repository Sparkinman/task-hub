"""Web push encryption, checked by decrypting it the way a browser would.

This is the one part of Task Hub where being subtly wrong produces no error at
all. A mistake in the key derivation gives a perfectly well-formed message that
the push service accepts, delivers, and the browser silently discards -- so the
only honest test is to undo the encryption independently, following RFC 8291
from the other end, and see the original bytes come back.

Everything here uses `cryptography` directly rather than the module under test,
so the two implementations have to agree rather than sharing a mistake.
"""

from __future__ import annotations

import json
import os
import struct
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.services import webpush

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _failures.append(name)


def fake_browser() -> tuple[ec.EllipticCurvePrivateKey, str, bytes, str]:
    """A subscription's keys, generated the way a browser generates them."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = os.urandom(16)
    return key, webpush.b64(public), auth, webpush.b64(auth)


def decrypt_as_browser(blob: bytes, ua_private, ua_public_b64: str, auth: bytes) -> bytes:
    """Undo the encryption, following RFC 8291 from the receiving end."""
    salt, rest = blob[:16], blob[16:]
    _record_size = struct.unpack(">I", rest[:4])[0]
    key_length = rest[4]
    as_public_bytes = rest[5:5 + key_length]
    ciphertext = rest[5 + key_length:]

    as_public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), as_public_bytes
    )
    shared = ua_private.exchange(ec.ECDH(), as_public)

    ikm = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=auth,
        info=b"WebPush: info\x00" + webpush.unb64(ua_public_b64) + as_public_bytes,
    ).derive(shared)
    content_key = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=salt,
        info=b"Content-Encoding: aes128gcm\x00",
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=salt,
        info=b"Content-Encoding: nonce\x00",
    ).derive(ikm)

    plaintext = AESGCM(content_key).decrypt(nonce, ciphertext, None)
    return plaintext.rstrip(b"\x02")  # strip the final-record delimiter


print("\nA message encrypts to something a browser can actually read")
ua_key, ua_public, auth_bytes, auth_b64 = fake_browser()
payload = json.dumps({
    "title": "Task Hub sync is failing",
    "body": "A sync did not complete.",
    "url": "/sync",
}).encode()

blob = webpush.encrypt(payload, ua_public, auth_b64)
check("it produces a body at all", len(blob) > 86, str(len(blob)))
recovered = decrypt_as_browser(blob, ua_key, ua_public, auth_bytes)
check("and it decrypts back to exactly what was sent",
      recovered == payload, recovered[:80].decode("utf-8", "replace"))

print("\nThe header is shaped the way the standard describes")
salt, rest = blob[:16], blob[16:]
check("a 16-byte salt", len(salt) == 16)
check("a record size of 4096", struct.unpack(">I", rest[:4])[0] == 4096)
check("a 65-byte uncompressed key", rest[4] == 65)
check("and the key is a real point on P-256",
      ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), rest[5:70]) is not None)

print("\nEvery message uses its own key and its own salt")
# Reusing either across messages to the same device would be a real weakness,
# and is the sort of thing an optimisation could quietly introduce.
first = webpush.encrypt(b"one", ua_public, auth_b64)
second = webpush.encrypt(b"one", ua_public, auth_b64)
check("the salt differs", first[:16] != second[:16])
check("the one-off public key differs", first[21:86] != second[21:86])
check("so identical messages encrypt differently", first != second)

print("\nThe wrong subscription cannot read it")
other_key, other_public, other_auth_bytes, _ = fake_browser()
try:
    decrypt_as_browser(blob, other_key, other_public, other_auth_bytes)
    check("a different device's key fails", False, "it decrypted, which is wrong")
except Exception:
    check("a different device's key fails", True)

print("\nThe VAPID token is a real ES256 JWT")
private_b64, public_b64 = webpush.generate_keypair()
check("the private half is 32 bytes", len(webpush.unb64(private_b64)) == 32)
check("the public half is an uncompressed point", len(webpush.unb64(public_b64)) == 65)

header = webpush._vapid_header(
    "https://fcm.googleapis.com/fcm/send/abc123", private_b64, public_b64,
    "mailto:someone@example.com",
)
check("it is a vapid header", header.startswith("vapid t="))
token = header[len("vapid t="):].split(",k=")[0]
parts = token.split(".")
check("with three JWT parts", len(parts) == 3, str(len(parts)))

claims = json.loads(webpush.unb64(parts[1]))
# The audience must be the push service's origin and nothing more -- including
# the path would have the service reject it.
check("the audience is the push service's origin",
      claims["aud"] == "https://fcm.googleapis.com", claims.get("aud", ""))
check("it expires, and within a day",
      0 < claims["exp"] - int(__import__("time").time()) <= 86400,
      str(claims.get("exp")))
check("and it names a subject", claims["sub"].startswith("mailto:"))

# The signature has to verify against the public key the header advertises,
# or the push service rejects the message.
public_key = ec.EllipticCurvePublicKey.from_encoded_point(
    ec.SECP256R1(), webpush.unb64(header.split(",k=")[1])
)
signature = webpush.unb64(parts[2])
check("the signature is 64 raw bytes, not DER", len(signature) == 64, str(len(signature)))
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402

try:
    public_key.verify(
        encode_dss_signature(
            int.from_bytes(signature[:32], "big"),
            int.from_bytes(signature[32:], "big"),
        ),
        f"{parts[0]}.{parts[1]}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )
    check("and it verifies against the advertised key", True)
except Exception as exc:  # noqa: BLE001
    check("and it verifies against the advertised key", False, repr(exc))

print("\nBase64 round-trips without padding, which is what both RFCs use")
for raw in (b"", b"a", b"ab", b"abc", os.urandom(65)):
    check(f"{len(raw)} bytes survive", webpush.unb64(webpush.b64(raw)) == raw)
check("and no padding is emitted", "=" not in webpush.b64(os.urandom(65)))

if _failures:
    print(f"\n{len(_failures)} check(s) failed.")
    sys.exit(1)
print("\nAll web push tests passed.")
