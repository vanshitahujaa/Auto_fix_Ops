import os
import time
import logging
import requests
from typing import Optional
from github import Github, GithubException

from api.database import SessionLocal
from api.models import Incident, IncidentStatus, RemediationAudit, ExecutionStatus
from engine.memory import QdrantMemoryStore

logger = logging.getLogger("autofixops")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
PROMETHEUS_URL = os.getenv(
    "PROMETHEUS_URL",
    "http://prometheus-operated.autofixops.svc.cluster.local:9090",
)

# Verification constants
PR_POLL_INTERVAL_SECONDS = 60
PR_POLL_MAX_ATTEMPTS = 30       # 30 minutes max wait
POST_MERGE_COOLDOWN_SECONDS = 300  # 5 minutes for GitOps sync
HEALTH_CHECK_THRESHOLD = 0.80  # Metric must be below 80% to pass


class VerificationEngine:
    """
    Post-remediation verification worker.
    1. Polls GitHub for PR merge status.
    2. Waits for GitOps sync cooldown.
    3. Re-checks Prometheus telemetry.
    4. Marks incident RESOLVED or FAILED.
    5. Stores successful resolutions into Qdrant for the learning loop.
    """

    def __init__(self):
        self.github = Github(GITHUB_TOKEN) if GITHUB_TOKEN else None

    def verify(
        self,
        incident_id: str,
        audit_id: str,
        summary_text: str,
        diagnosis: str,
        action_taken: str,
        pod_name: str,
    ) -> bool:
        """
        Full verification pipeline. Returns True if incident is resolved.
        """
        logger.info(f"[TRACE:{incident_id}] [VERIFICATION START]")

        db = SessionLocal()
        try:
            audit = db.query(RemediationAudit).filter(
                RemediationAudit.id == audit_id
            ).first()

            if not audit:
                logger.error(f"[TRACE:{incident_id}] Audit record {audit_id} not found.")
                return False

            # ─── Step 1: Poll PR Merge Status ───
            pr_merged = self._wait_for_pr_merge(incident_id, audit)

            if not pr_merged:
                logger.warning(
                    f"[TRACE:{incident_id}] [VERIFICATION TIMEOUT] "
                    f"PR was not merged within {PR_POLL_MAX_ATTEMPTS} polls."
                )
                audit.execution_status = ExecutionStatus.PR_REJECTED
                db.commit()
                return False

            audit.execution_status = ExecutionStatus.PR_MERGED
            db.commit()
            logger.info(f"[TRACE:{incident_id}] [VERIFICATION] PR merged confirmed.")

            # ─── Step 2: Post-Merge Cooldown ───
            logger.info(
                f"[TRACE:{incident_id}] [VERIFICATION] Waiting "
                f"{POST_MERGE_COOLDOWN_SECONDS}s for GitOps sync..."
            )
            time.sleep(POST_MERGE_COOLDOWN_SECONDS)

            # ─── Step 3: Telemetry Re-check ───
            is_healthy = self._check_post_fix_health(incident_id, pod_name)

            incident = db.query(Incident).filter(Incident.id == incident_id).first()

            if is_healthy:
                audit.execution_status = ExecutionStatus.VERIFICATION_PASSED
                incident.transition_to(IncidentStatus.VERIFIED)
                db.commit()
                incident.transition_to(IncidentStatus.RESOLVED)
                db.commit()

                logger.info(
                    f"[TRACE:{incident_id}] [VERIFICATION PASSED] "
                    f"Incident marked RESOLVED."
                )

                # ─── Step 4: Experience Learning Loop ───
                self._store_learning(summary_text, diagnosis, action_taken)

                # Record success in circuit breaker
                from engine.circuit_breaker import get_circuit_breaker
                get_circuit_breaker().record_outcome(success=True)

                return True
            else:
                audit.execution_status = ExecutionStatus.VERIFICATION_FAILED
                audit.failure_reason = "verification_failed"
                incident.transition_to(IncidentStatus.FAILED)
                db.commit()

                logger.warning(
                    f"[TRACE:{incident_id}] [VERIFICATION FAILED] "
                    f"Telemetry still degraded after fix."
                )

                # Store failure pattern in memory
                self._store_failure(summary_text, diagnosis, action_taken)

                # Record failure in circuit breaker
                from engine.circuit_breaker import get_circuit_breaker
                get_circuit_breaker().record_outcome(success=False)

                return False

        except Exception as e:
            logger.error(f"[TRACE:{incident_id}] [VERIFICATION ERROR] {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def _wait_for_pr_merge(
        self, incident_id: str, audit: RemediationAudit
    ) -> bool:
        """Polls GitHub API for PR merge status."""
        if not self.github or not GITHUB_REPO:
            # Simulate merge for local development
            logger.info(
                f"[TRACE:{incident_id}] [VERIFICATION SIMULATED] "
                f"Assuming PR merged (no GitHub credentials)."
            )
            return True

        try:
            repo = self.github.get_repo(GITHUB_REPO)
        except GithubException as e:
            logger.error(f"[TRACE:{incident_id}] Failed to access repo: {e}")
            return False

        pr_url = audit.github_pr_url or ""
        # Extract PR number from URL (last segment)
        try:
            pr_number = int(pr_url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            logger.error(
                f"[TRACE:{incident_id}] Could not extract PR number from: {pr_url}"
            )
            return False

        for attempt in range(1, PR_POLL_MAX_ATTEMPTS + 1):
            try:
                pr = repo.get_pull(pr_number)
                if pr.merged:
                    return True
                if pr.state == "closed" and not pr.merged:
                    logger.warning(
                        f"[TRACE:{incident_id}] PR #{pr_number} was closed without merge."
                    )
                    return False
            except GithubException as e:
                logger.error(
                    f"[TRACE:{incident_id}] GitHub API error on poll {attempt}: {e}"
                )

            logger.info(
                f"[TRACE:{incident_id}] [VERIFICATION POLL {attempt}/{PR_POLL_MAX_ATTEMPTS}] "
                f"PR not yet merged. Waiting {PR_POLL_INTERVAL_SECONDS}s..."
            )
            time.sleep(PR_POLL_INTERVAL_SECONDS)

        return False

    def _check_post_fix_health(self, incident_id: str, pod_name: str) -> bool:
        """
        Queries Prometheus for current resource usage.
        Returns True if both CPU and memory are below the health threshold.
        """
        queries = {
            "cpu": f'rate(container_cpu_usage_seconds_total{{namespace="autofixops", pod=~"{pod_name}.*"}}[5m])',
            "memory": f'container_memory_working_set_bytes{{namespace="autofixops", pod=~"{pod_name}.*"}}',
        }

        for metric_name, query in queries.items():
            try:
                response = requests.get(
                    f"{PROMETHEUS_URL}/api/v1/query",
                    params={"query": query},
                    timeout=5,
                )
                response.raise_for_status()
                results = response.json().get("data", {}).get("result", [])

                if results:
                    value = float(results[0]["value"][1])
                    logger.info(
                        f"[TRACE:{incident_id}] [HEALTH CHECK] "
                        f"{metric_name} = {value:.4f}"
                    )
                    if metric_name == "cpu" and value > HEALTH_CHECK_THRESHOLD:
                        return False
                    # Memory: compare against container limit (100Mi = 104857600 bytes)
                    if metric_name == "memory" and value > 104857600 * HEALTH_CHECK_THRESHOLD:
                        return False
                else:
                    logger.warning(
                        f"[TRACE:{incident_id}] [HEALTH CHECK] "
                        f"No data for {metric_name}. Assuming healthy (pod may have restarted)."
                    )
            except Exception as e:
                logger.error(
                    f"[TRACE:{incident_id}] [HEALTH CHECK FAILED] "
                    f"{metric_name} query error: {e}"
                )
                # If we can't check, we don't assume healthy
                return False

        return True

    def _store_learning(self, summary_text: str, diagnosis: str, action: str):
        """Closes the experience loop — stores verified resolution in Qdrant."""
        try:
            memory = QdrantMemoryStore()
            memory.store_resolution(summary_text, diagnosis, action)
            logger.info("[VERIFICATION] Successfully stored resolution in Qdrant memory.")
        except Exception as e:
            # Learning failure should never block the pipeline
            logger.error(f"[VERIFICATION] Failed to store learning: {e}")

    def _store_failure(self, summary_text: str, diagnosis: str, action: str):
        """Stores a verified failure pattern to prevent future repeat mistakes."""
        try:
            memory = QdrantMemoryStore()
            memory.store_failure(summary_text, diagnosis, action, "verification_failed")
            logger.info("[VERIFICATION] Failure pattern stored in Qdrant memory.")
        except Exception as e:
            logger.error(f"[VERIFICATION] Failed to store failure pattern: {e}")

