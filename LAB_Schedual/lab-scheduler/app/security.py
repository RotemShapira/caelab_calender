"""
Password hashing helpers for admin credentials.

Uses PBKDF2-HMAC-SHA256 via the Python standard library (hashlib) so no
extra dependency (bcrypt/passlib) is required. Stored format:

    <iterations>$<salt_hex>$<hash_hex>

This lets us verify old hashes even if we ever raise the iteration count
in the future.
"""
import hashlib
import hmac
import os

PBKDF2_ITERATIONS = 260_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        iterations_str, salt_hex, hash_hex = stored.split("$")
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)
