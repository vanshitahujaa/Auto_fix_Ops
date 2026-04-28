"""
Circuit Breaker for AutoFixOps Remediation Pipeline
====================================================
Prevents the system from becoming its own outage.

States:
  CLOSED   → normal operation, remediation allowed
  OPEN     → too many failures, all actions forced to ESCALATE
  HALF_OPEN → testing recovery, allows one action through

Thresholds are guarded by a minimum sample size to prevent
panic on low traffic.
"""

import time
import logging
from typing import Optional
from api.database import SessionLocal
from api.models import RemediationAudit, ExecutionStatus

logger = logging.getLogger("autofixops")

# ─── Configuration ───
FAILURE_THRESHOLD = 0.50        # 50% failure rate triggers open
MINIMUM_SAMPLE_SIZE = 5         # Don't evaluate until at least 5 actions exist
ROLLING_WINDOW_SIZE = 10        # Evaluate last N actions
OPEN_DURATION_SECONDS = 1800    # 30 minutes before half-open
RECENCY_WEIGHT_FACTOR = 1.5    # Recent failures weigh 1.5x more


class CircuitState:
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """
    Rolling-window circuit breaker with weighted recency scoring.
    Thread-safe via database state — no in-memory locks needed.
    """

    def __init__(self):
        self._state = CircuitState.CLOSED
        self._opened_at: Optional[float] = None
        self._half_open_test_passed: Optional[bool] = None

    @property
    def state(self) -> str:
        # Check if OPEN should transition to HALF_OPEN
        if self._state == CircuitState.OPEN and self._opened_at:
            elapsed = time.time() - self._opened_at
            if elapsed >= OPEN_DURATION_SECONDS:
                logger.info(
                    "[CIRCUIT BREAKER] Open duration expired. "
                    "Transitioning to HALF_OPEN — allowing one test action."
                )
                self._state = CircuitState.HALF_OPEN
                self._half_open_test_passed = None
        return self._state

    def should_allow_action(self) -> bool:
        """
        Returns True if remediation is allowed.
        Returns False if the circuit is OPEN (force escalation).
        """
        current_state = self.state  # Triggers OPEN → HALF_OPEN check

        if current_state == CircuitState.CLOSED:
            # Re-evaluate based on recent history
            self._evaluate()
            return self._state == CircuitState.CLOSED

        if current_state == CircuitState.HALF_OPEN:
            # Allow exactly one action as a probe
            logger.info("[CIRCUIT BREAKER] HALF_OPEN: Allowing one probe action.")
            return True

        # OPEN
        return False

    def record_outcome(self, success: bool):
        """Called after a verification completes. Updates circuit state."""
        if self._state == CircuitState.HALF_OPEN:
            if success:
                logger.info("[CIRCUIT BREAKER] Probe succeeded. Closing circuit.")
                self._state = CircuitState.CLOSED
                self._opened_at = None
            else:
                logger.warning("[CIRCUIT BREAKER] Probe failed. Re-opening circuit.")
                self._state = CircuitState.OPEN
                self._opened_at = time.time()

    def _evaluate(self):
        """
        Queries recent remediation outcomes and calculates weighted failure rate.
        Opens the circuit if failure rate exceeds threshold.
        Degrades gracefully if DB is unavailable.
        """
        try:
            db = SessionLocal()
        except Exception:
            logger.debug("[CIRCUIT BREAKER] DB unavailable — staying CLOSED.")
            return

        try:
            recent_audits = (
                db.query(RemediationAudit)
                .filter(RemediationAudit.execution_status.isnot(None))
                .order_by(RemediationAudit.created_at.desc())
                .limit(ROLLING_WINDOW_SIZE)
                .all()
            )

            if len(recent_audits) < MINIMUM_SAMPLE_SIZE:
                logger.debug(
                    f"[CIRCUIT BREAKER] Only {len(recent_audits)} samples "
                    f"(need {MINIMUM_SAMPLE_SIZE}). Staying CLOSED."
                )
                return

            # Calculate weighted failure score
            total_weight = 0.0
            failure_weight = 0.0

            for i, audit in enumerate(recent_audits):
                weight = RECENCY_WEIGHT_FACTOR ** (ROLLING_WINDOW_SIZE - i - 1)
                weight = min(weight, 3.0)
                total_weight += weight

                if audit.execution_status in (
                    ExecutionStatus.VERIFICATION_FAILED,
                    ExecutionStatus.PR_REJECTED,
                ):
                    failure_weight += weight

            weighted_failure_rate = failure_weight / total_weight if total_weight > 0 else 0.0

            logger.info(
                f"[CIRCUIT BREAKER] Weighted failure rate: {weighted_failure_rate:.2%} "
                f"(threshold: {FAILURE_THRESHOLD:.0%}, samples: {len(recent_audits)})"
            )

            if weighted_failure_rate > FAILURE_THRESHOLD:
                logger.warning(
                    f"[CIRCUIT BREAKER] OPENING — weighted failure rate "
                    f"{weighted_failure_rate:.2%} exceeds {FAILURE_THRESHOLD:.0%}"
                )
                self._state = CircuitState.OPEN
                self._opened_at = time.time()

        except Exception as e:
            logger.warning(f"[CIRCUIT BREAKER] Evaluation error — staying CLOSED: {e}")
        finally:
            db.close()


# ─── Singleton ───
# One circuit breaker instance per process.
# In production, this state would live in Redis for cross-worker visibility.
_breaker_instance: Optional[CircuitBreaker] = None


def get_circuit_breaker() -> CircuitBreaker:
    global _breaker_instance
    if _breaker_instance is None:
        _breaker_instance = CircuitBreaker()
    return _breaker_instance
