import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from pymongo import MongoClient
from pymongo.errors import PyMongoError, ServerSelectionTimeoutError

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("autofixops")

# ─────────────────────────────────────────────────────────────
# PostgreSQL
# ─────────────────────────────────────────────────────────────
PG_URL = os.getenv("POSTGRES_URL", "")
if not PG_URL:
    logger.warning("[DB INIT] POSTGRES_URL not set. Database operations will fail.")

engine = create_engine(PG_URL, echo=False, pool_pre_ping=False, pool_recycle=300)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_relational_db():
    Base.metadata.create_all(bind=engine)
    logger.info("[DB INIT] PostgreSQL tables created/verified.")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────
# MongoDB — with graceful degradation
#
# If the Atlas TLS handshake fails (common on macOS Python 3.9 +
# LibreSSL 2.8.3), we fall back to an in-memory mock collection so
# the pipeline can still complete. Context loss is logged loudly.
# ─────────────────────────────────────────────────────────────
MONGO_URL = os.getenv("MONGO_URL", "")


class _InMemoryCollection:
    """Minimal stand-in for a pymongo collection used when Atlas is unreachable."""

    def __init__(self):
        self._docs = {}

    def update_one(self, filter_, update, upsert=False):
        key = filter_.get("incident_id")
        if key is None:
            return
        existing = self._docs.get(key, {})
        new_fields = update.get("$set", {})
        existing.update(new_fields)
        self._docs[key] = existing

    def find_one(self, filter_, *args, **kwargs):
        key = filter_.get("incident_id")
        return self._docs.get(key)

    def __repr__(self):
        return f"<_InMemoryCollection docs={len(self._docs)}>"


def _build_mongo_collection():
    """Returns a usable collection object — real or in-memory fallback."""
    if not MONGO_URL:
        logger.warning("[DB INIT] MONGO_URL not set — using in-memory context store.")
        return _InMemoryCollection()

    try:
        import certifi
        client = MongoClient(
            MONGO_URL,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
    except ImportError:
        client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)

    # Probe the connection. If it fails, fall back to in-memory.
    try:
        client.admin.command("ping")
        logger.info("[DB INIT] MongoDB Atlas reachable.")
        return client["autofixops"]["incident_contexts"]
    except (PyMongoError, ServerSelectionTimeoutError, OSError) as e:
        logger.error(
            f"[DB INIT] MongoDB unreachable ({e.__class__.__name__}: {e}). "
            f"Falling back to in-memory context store. "
            f"Pipeline will run, but telemetry context will not persist."
        )
        return _InMemoryCollection()


incident_contexts_collection = _build_mongo_collection()

logger.info("[DB INIT] Database connection strings loaded successfully.")
