"""
Password hashing, constant-time verification, signed session tokens,
and brute-force rate limiting.

Uses PBKDF2-HMAC-SHA256 via the Python standard library (hashlib) so no
extra dependency (bcrypt/passlib) is required. Stored format:

    <iterations>$<salt_hex>$<hash_hex>
"""
import base64
import hashlib
import hmac
import json
import os
import time

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


# Precomputed dummy hash for constant-time verification when a username is not found
_DUMMY_HASH = hash_password("constant-time-dummy-password", b"0123456789abcdef")


def verify_password_constant_time(password: str, stored: str | None) -> bool:
    """
    Always runs full PBKDF2-HMAC-SHA256 even when `stored` is None,
    preventing timing-based username enumeration.
    """
    if stored is None:
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, stored)


# ---------------------------------------------------------------------------
# Signed session cookie tokens (HMAC-SHA256)
# ---------------------------------------------------------------------------
_SESSION_SECRET: bytes = os.urandom(32)
SESSION_TTL_SECONDS = 14 * 24 * 60 * 60  # 14 days


def set_session_secret(secret_str: str) -> None:
    global _SESSION_SECRET
    _SESSION_SECRET = secret_str.encode("utf-8")


def create_session_token(username: str, role: str) -> str:
    payload = {
        "u": username,
        "r": role,
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(_SESSION_SECRET, b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_session_token(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    try:
        b64, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(_SESSION_SECRET, b64.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        pad = "=" * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(b64 + pad).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Brute-force login rate limiter (in-memory per client IP)
# ---------------------------------------------------------------------------
_MAX_FAILED_ATTEMPTS = 8
_WINDOW_SECONDS = 300  # 5 minutes
_failed_attempts: dict[str, list[float]] = {}


def is_rate_limited(client_ip: str) -> bool:
    now = time.time()
    timestamps = _failed_attempts.get(client_ip)
    if not timestamps:
        return False
    recent = [t for t in timestamps if now - t < _WINDOW_SECONDS]
    if recent:
        _failed_attempts[client_ip] = recent
    else:
        _failed_attempts.pop(client_ip, None)
    return len(recent) >= _MAX_FAILED_ATTEMPTS


def record_failed_attempt(client_ip: str) -> None:
    now = time.time()
    recent = [t for t in _failed_attempts.get(client_ip, []) if now - t < _WINDOW_SECONDS]
    recent.append(now)
    # Prevent unbounded dict size under distributed spray
    if len(_failed_attempts) > 5000 and client_ip not in _failed_attempts:
        _failed_attempts.clear()
    _failed_attempts[client_ip] = recent


def clear_failed_attempts(client_ip: str) -> None:
    _failed_attempts.pop(client_ip, None)
