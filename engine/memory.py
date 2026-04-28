from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance
from sentence_transformers import SentenceTransformer
import uuid
import logging

logger = logging.getLogger("autofixops")

SUCCESSES_COLLECTION = "incident_resolutions"
FAILURES_COLLECTION = "incident_failures"


class QdrantMemoryStore:
    """
    Dual-collection RAG memory:
    - incident_resolutions: what WORKED (verified fixes)
    - incident_failures: what FAILED (with reason classification)

    This prevents the system from recommending proven-bad fixes.
    """

    def __init__(self, storage_path="./qdrant_data"):
        self.client = QdrantClient(path=storage_path)
        self.encoder = SentenceTransformer("all-MiniLM-L6-v2")

        # Ensure both collections exist
        for collection in [SUCCESSES_COLLECTION, FAILURES_COLLECTION]:
            if not self.client.collection_exists(collection_name=collection):
                self.client.create_collection(
                    collection_name=collection,
                    vectors_config=VectorParams(size=384, distance=Distance.COSINE),
                )
                logger.info(f"[RAG] Initialized collection: {collection}")

    def store_resolution(self, summary_text: str, root_cause: str, successful_action: str):
        """Stores a VERIFIED successful outcome as institutional memory."""
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

    def store_failure(
        self, summary_text: str, root_cause: str, failed_action: str, failure_reason: str
    ):
        """
        Stores a VERIFIED failure with reason classification.
        Only stores 'verification_failed' reasons — not noise failures
        like policy_blocked or invalid_patch.
        """
        # Filter: only store failures where the action was actually tried and failed
        if failure_reason not in ("verification_failed",):
            logger.debug(
                f"[RAG] Skipping failure storage — reason '{failure_reason}' "
                f"is not a verified execution failure."
            )
            return

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

    def retrieve_similar(self, query_text: str, top_k=2) -> list:
        """Retrieves top_k similar SUCCESSFUL resolutions."""
        query_vector = self.encoder.encode(query_text).tolist()

        results = self.client.search(
            collection_name=SUCCESSES_COLLECTION,
            query_vector=query_vector,
            limit=top_k,
            score_threshold=0.8,
        )
        return [hit.payload for hit in results]

    def retrieve_failures(self, query_text: str, top_k=2) -> list:
        """Retrieves top_k similar FAILED resolutions to warn the AI."""
        query_vector = self.encoder.encode(query_text).tolist()

        results = self.client.search(
            collection_name=FAILURES_COLLECTION,
            query_vector=query_vector,
            limit=top_k,
            score_threshold=0.75,
        )
        return [hit.payload for hit in results]
