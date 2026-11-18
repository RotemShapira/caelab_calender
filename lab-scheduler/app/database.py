import logging
import os
import shutil
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger("lab_scheduler.database")

# Live production database lives in /app/data/lab_production.db (git-ignored)
# so future `git pull` updates never overwrite, conflict with, or delete your data.
_RAW_DB_URL = os.getenv("DATABASE_URL", "sqlite:////app/data/lab_production.db")

if _RAW_DB_URL == "sqlite:////app/data/lab_scheduler.db":
    DATABASE_URL = "sqlite:////app/data/lab_production.db"
else:
    DATABASE_URL = _RAW_DB_URL


def _ensure_production_db_migrated() -> None:
    prefix = "sqlite:///"
    if not DATABASE_URL.startswith(prefix):
        return
    prod_path = DATABASE_URL[len(prefix):]
    prod_dir = os.path.dirname(prod_path)
    if prod_dir:
        os.makedirs(prod_dir, exist_ok=True)
    legacy_path = os.path.join(prod_dir, "lab_scheduler.db")
    if not os.path.exists(prod_path) and os.path.exists(legacy_path):
        try:
            shutil.copy2(legacy_path, prod_path)
            logger.info("Migrated existing database %s -> %s", legacy_path, prod_path)
        except Exception as exc:
            logger.warning("Could not copy legacy database: %s", exc)


_ensure_production_db_migrated()

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a DB session and closes it afterward."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
