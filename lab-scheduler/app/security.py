"""
Password hashing, constant-time verification, password-bound signed session tokens,
brute-force login rate limiting, and booking spam throttling.

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


def password_fingerprint(password_hash: str) -> str:
    """Short non-reversible fingerprint embedded in session tokens to invalidate old sessions on password change."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Signed session cookie tokens (HMAC-SHA256, bound to password_hash)
# ---------------------------------------------------------------------------
_SESSION_SECRET: bytes = os.urandom(32)
SESSION_TTL_SECONDS = 14 * 24 * 60 * 60  # 14 days


def set_session_secret(secret_str: str) -> None:
    global _SESSION_SECRET
    _SESSION_SECRET = secret_str.encode("utf-8")


def create_session_token(username: str, role: str, password_hash: str) -> str:
    payload = {
        "u": username,
        "r": role,
        "p": password_fingerprint(password_hash),
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
# Brute-force login rate limiter (per IP and per username)
# ---------------------------------------------------------------------------
_MAX_FAILED_ATTEMPTS = 8
_WINDOW_SECONDS = 300  # 5 minutes
_failed_attempts: dict[str, list[float]] = {}


def is_rate_limited(key: str) -> bool:
    now = time.time()
    timestamps = _failed_attempts.get(key)
    if not timestamps:
        return False
    recent = [t for t in timestamps if now - t < _WINDOW_SECONDS]
    if recent:
        _failed_attempts[key] = recent
    else:
        _failed_attempts.pop(key, None)
    return len(recent) >= _MAX_FAILED_ATTEMPTS


def record_failed_attempt(key: str) -> None:
    now = time.time()
    recent = [t for t in _failed_attempts.get(key, []) if now - t < _WINDOW_SECONDS]
    recent.append(now)
    if len(_failed_attempts) > 5000 and key not in _failed_attempts:
        _failed_attempts.clear()
    _failed_attempts[key] = recent


def clear_failed_attempts(key: str) -> None:
    _failed_attempts.pop(key, None)


# ---------------------------------------------------------------------------
# Per-user booking submission rate limiter (anti-spam / email-flood guard)
# ---------------------------------------------------------------------------
_MAX_BOOKINGS_PER_WINDOW = 20
_BOOKING_WINDOW_SECONDS = 600  # 10 minutes
_booking_submissions: dict[str, list[float]] = {}


def check_and_record_booking_rate_limit(username: str) -> bool:
    """
    Returns True if the user is allowed to submit a booking, False if throttled.
    """
    key = username.strip().lower()
    now = time.time()
    recent = [t for t in _booking_submissions.get(key, []) if now - t < _BOOKING_WINDOW_SECONDS]
    if len(recent) >= _MAX_BOOKINGS_PER_WINDOW:
        _booking_submissions[key] = recent
        return False
    recent.append(now)
    if len(_booking_submissions) > 2000 and key not in _booking_submissions:
        _booking_submissions.clear()
    _booking_submissions[key] = recent
    return True
