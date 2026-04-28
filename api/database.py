import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from pymongo import MongoClient

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configure base logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("autofixops")

# PostgreSQL Configuration
PG_URL = os.getenv("POSTGRES_URL", "")
if not PG_URL:
    logger.warning("[DB INIT] POSTGRES_URL not set. Database operations will fail.")

# SQLAlchemy Engines and Sessions
engine = create_engine(PG_URL, echo=False)
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

# MongoDB Configuration
MONGO_URL = os.getenv("MONGO_URL", "")

# Fix for macOS LibreSSL + MongoDB Atlas TLS
try:
    import certifi
    mongo_client = MongoClient(MONGO_URL, tlsCAFile=certifi.where())
except ImportError:
    mongo_client = MongoClient(MONGO_URL)

mongo_db = mongo_client["autofixops"]
incident_contexts_collection = mongo_db["incident_contexts"]

logger.info("[DB INIT] Database connection strings loaded successfully.")
