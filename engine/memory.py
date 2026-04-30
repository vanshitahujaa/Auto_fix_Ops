"""
Qdrant RAG memory layer.

Process-singleton client (one per Python process) to avoid the
"Storage folder is already accessed by another instance" error
from local Qdrant when multiple imports happen.

All retrieval methods degrade gracefully — if anything goes wrong,
we return an empty list rather than crashing the diagnosis pipeline.
"""

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance
from sentence_transformers import SentenceTransformer
import uuid
import logging
import threading

logger = logging.getLogger("autofixops")

SUCCESSES_COLLECTION = "incident_resolutions"
FAILURES_COLLECTION = "incident_failures"

# Process-level singletons. Lazy-initialized on first use.
_client_singleton = None
_encoder_singleton = None
_init_lock = threading.Lock()


def _get_client() -> QdrantClient:
    global _client_singleton
    if _client_singleton is None:
        with _init_lock:
            if _client_singleton is None:
                # Use in-memory store: avoids file-locking issues across
                # FastAPI + Celery worker processes on the same machine.
                _client_singleton = QdrantClient(location=":memory:")
                for collection in (SUCCESSES_COLLECTION, FAILURES_COLLECTION):
                    if not _client_singleton.collection_exists(collection_name=collection):
                        _client_singleton.create_collection(
                            collection_name=collection,
                            vectors_config=VectorParams(size=384, distance=Distance.COSINE),
                        )
                        logger.info(f"[RAG] Initialized collection: {collection}")
    return _client_singleton


def _get_encoder() -> SentenceTransformer:
    global _encoder_singleton
    if _encoder_singleton is None:
        with _init_lock:
            if _encoder_singleton is None:
                _encoder_singleton = SentenceTransformer("all-MiniLM-L6-v2")
    return _encoder_singleton


class QdrantMemoryStore:
    """
    Dual-collection RAG memory:
      - incident_resolutions: what WORKED (verified fixes)
      - incident_failures:    what FAILED (with reason classification)
    """

    def __init__(self, storage_path: str = "./qdrant_data"):
        # storage_path kept for backward compatibility but unused —
        # we always use in-memory mode now.
        try:
            self.client = _get_client()
            self.encoder = _get_encoder()
            self.available = True
        except Exception as e:
            logger.error(f"[RAG] Initialization failed, RAG disabled this run: {e}")
            self.client = None
            self.encoder = None
            self.available = False

    def store_resolution(self, summary_text: str, root_cause: str, successful_action: str):
        if not self.available:
            return
        try:
            vector = self.encoder.encode(summary_text).tolist()
            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "summary": summary_text,
                    "root_cause_classification": root_cause,
                    "action_taken": successful_action,
                    "outcome": "SUCCESS",
                },
            )
            self.client.upsert(collection_name=SUCCESSES_COLLECTION, points=[point])
            logger.info("[RAG] Successful resolution stored.")
        except Exception as e:
            logger.error(f"[RAG] Failed to store resolution: {e}")

    def store_failure(
        self, summary_text: str, root_cause: str, failed_action: str, failure_reason: str
    ):
        if not self.available:
            return
        if failure_reason not in ("verification_failed",):
            logger.debug(
                f"[RAG] Skipping failure storage — '{failure_reason}' is not a verified execution failure."
            )
            return

        try:
            vector = self.encoder.encode(summary_text).tolist()
            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "summary": summary_text,
                    "root_cause_classification": root_cause,
                    "action_taken": failed_action,
                    "failure_reason": failure_reason,
                    "outcome": "FAILED",
                },
            )
            self.client.upsert(collection_name=FAILURES_COLLECTION, points=[point])
            logger.info(f"[RAG] Failure pattern stored: {failure_reason}")
        except Exception as e:
            logger.error(f"[RAG] Failed to store failure: {e}")

    def _query(self, collection: str, query_text: str, top_k: int, score_threshold: float) -> list:
        """Common path for both retrieve_similar and retrieve_failures.
        Uses the modern qdrant-client query_points() API.
        """
        if not self.available:
            return []
        try:
            query_vector = self.encoder.encode(query_text).tolist()
            response = self.client.query_points(
                collection_name=collection,
                query=query_vector,
                limit=top_k,
                score_threshold=score_threshold,
                with_payload=True,
            )
            # query_points returns a QueryResponse with .points
            points = getattr(response, "points", None) or []
            return [p.payload for p in points if getattr(p, "payload", None)]
        except Exception as e:
            logger.warning(f"[RAG] Retrieval from '{collection}' failed (degrading gracefully): {e}")
            return []

    def retrieve_similar(self, query_text: str, top_k: int = 2) -> list:
        return self._query(SUCCESSES_COLLECTION, query_text, top_k, 0.8)

    def retrieve_failures(self, query_text: str, top_k: int = 2) -> list:
        return self._query(FAILURES_COLLECTION, query_text, top_k, 0.75)
