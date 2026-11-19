from urllib.parse import urlparse
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .database import SessionLocal
from .models import AdminCredential, User
from .security import (
    clear_failed_attempts,
    is_rate_limited,
    record_failed_attempt,
    verify_password_constant_time,
    verify_session_token,
)

SESSION_COOKIE_NAME = "lab_session"
security_basic = HTTPBasic(auto_error=False)


class LoginRequiredException(Exception):
    """Raised when a browser route requires user login; handled in main.py by redirecting to /login."""


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def verify_same_origin(request: Request) -> None:
    """
    CSRF protection: ensures state-changing POST requests originate from the
    same host as the server.
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return
    expected_host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or ""
    ).split(",")[0].strip().lower()
    if not expected_host:
        return

    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    source_url = origin or referer
    if not source_url:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing Origin/Referer header (CSRF protection).",
        )
    parsed = urlparse(source_url)
    source_host = (parsed.netloc or "").lower()
    if source_host != expected_host:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-origin request blocked (CSRF protection).",
        )


def get_current_session(request: Request) -> dict | None:
    """
    Returns {"username": str, "role": "user" | "manager" | "admin"} if the
    request carries a valid session cookie and the account still exists in DB.
    Checks `User.is_manager` live so promotions/revocations apply immediately.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    payload = verify_session_token(token)
    if not payload:
        return None

    username = payload.get("u")
    token_role = payload.get("r")
    if not username or token_role not in ("user", "manager", "admin"):
        return None

    db = SessionLocal()
    try:
        if token_role == "admin":
            exists = db.query(AdminCredential).filter(AdminCredential.username == username).first() is not None
            if not exists:
                return None
            return {"username": username, "role": "admin"}
        else:
            user = db.query(User).filter(User.username == username).first()
            if user is None:
                return None
            live_role = "manager" if user.is_manager else "user"
            return {"username": user.username, "role": live_role}
    finally:
        db.close()


def require_user(request: Request) -> dict:
    """
    Dependency that requires any signed-in Lab User, Manager, or Admin.
    Redirects unauthenticated visitors to /login.
    """
    verify_same_origin(request)
    session = get_current_session(request)
    if session is None:
        raise LoginRequiredException()
    return session


def _verify_basic_admin(
    request: Request,
    credentials: HTTPBasicCredentials | None,
) -> str:
    client_ip = get_client_ip(request)
    if is_rate_limited(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please wait 5 minutes before trying again.",
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    db = SessionLocal()
    try:
        record = (
            db.query(AdminCredential)
            .filter(AdminCredential.username == credentials.username)
            .first()
        )
        stored_hash = record.password_hash if record is not None else None
        if not verify_password_constant_time(credentials.password, stored_hash):
            record_failed_attempt(client_ip)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid admin credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        clear_failed_attempts(client_ip)
        return credentials.username
    finally:
        db.close()


def require_manager_or_admin(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security_basic),
) -> dict:
    """
    Allows either a full Admin OR a Lab Manager (user with `is_manager=True`).
    Returns {"username": str, "role": "admin" | "manager"}.
    """
    verify_same_origin(request)

    session = get_current_session(request)
    if session and session["role"] in ("admin", "manager"):
        return session

    admin_user = _verify_basic_admin(request, credentials)
    return {"username": admin_user, "role": "admin"}


def require_admin(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security_basic),
) -> str:
    """
    Strictly allows ONLY the full Admin (via Admin session cookie or HTTP Basic Auth).
    """
    verify_same_origin(request)

    session = get_current_session(request)
    if session and session["role"] == "admin":
        return session["username"]

    if session and session["role"] == "manager":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action is restricted to the main Lab Admin.",
        )

    return _verify_basic_admin(request, credentials)
