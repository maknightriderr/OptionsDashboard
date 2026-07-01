"""
Credential encryption at rest.

We never want plaintext API keys / PINs sitting in a .env on a VPS in the clear
if it can be avoided. This module wraps Fernet (AES-128-CBC + HMAC) so the four
SmartAPI credentials can be stored as ciphertext and decrypted at startup using
a single key held in OT_ENCRYPTION_KEY (which itself can live in a secrets
manager / systemd credential rather than on disk).

Generate a key once:
    python -m utils.crypto keygen

Encrypt a value:
    python -m utils.crypto encrypt "my-secret-pin"   (prompts for the key)
"""

from __future__ import annotations

import getpass
import sys

from cryptography.fernet import Fernet, InvalidToken


def generate_key() -> str:
    """Return a new URL-safe base64 Fernet key as a str."""
    return Fernet.generate_key().decode("utf-8")


def encrypt(plaintext: str, key: str) -> str:
    """Encrypt ``plaintext`` with ``key`` and return ciphertext as str."""
    token = Fernet(key.encode("utf-8")).encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt(ciphertext: str, key: str) -> str:
    """Decrypt ``ciphertext`` with ``key``. Raises ValueError on bad key/data."""
    try:
        plain = Fernet(key.encode("utf-8")).decrypt(ciphertext.encode("utf-8"))
    except InvalidToken as exc:  # wrong key or corrupted value
        raise ValueError("Failed to decrypt credential: invalid key or ciphertext.") from exc
    return plain.decode("utf-8")


def _cli() -> None:
    """Tiny CLI so operators can manage keys without writing code."""
    if len(sys.argv) < 2 or sys.argv[1] not in {"keygen", "encrypt", "decrypt"}:
        print("usage: python -m utils.crypto [keygen|encrypt <value>|decrypt <value>]")
        raise SystemExit(2)

    command = sys.argv[1]
    if command == "keygen":
        print(generate_key())
        return

    if len(sys.argv) < 3:
        print(f"'{command}' needs a value argument.")
        raise SystemExit(2)

    value = sys.argv[2]
    key = getpass.getpass("Encryption key: ")
    print(encrypt(value, key) if command == "encrypt" else decrypt(value, key))


if __name__ == "__main__":
    _cli()
