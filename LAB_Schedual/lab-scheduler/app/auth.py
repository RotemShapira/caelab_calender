from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .database import SessionLocal
from .models import AdminCredential
from .security import verify_password

security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """
    FastAPI dependency that protects admin routes with HTTP Basic Auth,
    checked against the `admin_credentials` table (managed via the GUI
    settings page at /admin/settings) rather than static env vars.
    """
    db = SessionLocal()
    try:
        record = (
            db.query(AdminCredential)
            .filter(AdminCredential.username == credentials.username)
            .first()
        )
        if record is None or not verify_password(credentials.password, record.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid admin credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username
    finally:
        db.close()
