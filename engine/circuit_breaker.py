"""
Circuit Breaker Registry for AutoFixOps
=========================================
Per-action, per-project circuit breakers.
One project's failures don't block another project's actions.

States:
  CLOSED   → normal operation, remediation allowed
  OPEN     → too many failures, all actions forced to ESCALATE
  HALF_OPEN → testing recovery, allows one action through

Global emergency breaker still exists as a kill switch.
"""

import time
import logging
from typing import Optional, Dict, Tuple
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
    Single circuit breaker instance for one (project_id, action_type) pair.
    """

    def __init__(self, project_id: str = "global", action_type: str = "global"):
        self.project_id = project_id
        self.action_type = action_type
        self._state = CircuitState.CLOSED
        self._opened_at: Optional[float] = None
        self._half_open_test_passed: Optional[bool] = None

    @property
    def key(self) -> str:
        return f"{self.project_id}:{self.action_type}"

    @property
    def state(self) -> str:
        if self._state == CircuitState.OPEN and self._opened_at:
            elapsed = time.time() - self._opened_at
            if elapsed >= OPEN_DURATION_SECONDS:
                logger.info(
                    f"[CIRCUIT BREAKER:{self.key}] Open duration expired. "
                    f"Transitioning to HALF_OPEN."
                )
                self._state = CircuitState.HALF_OPEN
                self._half_open_test_passed = None
        return self._state

    def should_allow_action(self) -> bool:
        """Returns True if remediation is allowed."""
        current_state = self.state

        if current_state == CircuitState.CLOSED:
            self._evaluate()
            return self._state == CircuitState.CLOSED

        if current_state == CircuitState.HALF_OPEN:
            logger.info(f"[CIRCUIT BREAKER:{self.key}] HALF_OPEN: Allowing one probe.")
            return True

        return False

    def record_outcome(self, success: bool):
        """Called after verification completes."""
        if self._state == CircuitState.HALF_OPEN:
            if success:
                logger.info(f"[CIRCUIT BREAKER:{self.key}] Probe succeeded. Closing.")
                self._state = CircuitState.CLOSED
                self._opened_at = None
            else:
                logger.warning(f"[CIRCUIT BREAKER:{self.key}] Probe failed. Re-opening.")
                self._state = CircuitState.OPEN
                self._opened_at = time.time()

    def _evaluate(self):
        """Queries recent remediation outcomes for this project+action scope."""
        try:
            db = SessionLocal()
        except Exception:
            return

        try:
            query = (
                db.query(RemediationAudit)
                .filter(RemediationAudit.execution_status.isnot(None))
            )

            # Scope to project if not global
            if self.project_id != "global":
                query = query.filter(RemediationAudit.project_id == self.project_id)

            # Scope to action type if not global
            if self.action_type != "global":
                query = query.filter(RemediationAudit.proposed_action == self.action_type)

            recent_audits = (
                query
                .order_by(RemediationAudit.created_at.desc())
                .limit(ROLLING_WINDOW_SIZE)
                .all()
            )

            if len(recent_audits) < MINIMUM_SAMPLE_SIZE:
                return

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
                f"[CIRCUIT BREAKER:{self.key}] Failure rate: {weighted_failure_rate:.2%} "
                f"(threshold: {FAILURE_THRESHOLD:.0%}, samples: {len(recent_audits)})"
            )

            if weighted_failure_rate > FAILURE_THRESHOLD:
                logger.warning(
                    f"[CIRCUIT BREAKER:{self.key}] OPENING — "
                    f"{weighted_failure_rate:.2%} exceeds {FAILURE_THRESHOLD:.0%}"
                )
                self._state = CircuitState.OPEN
                self._opened_at = time.time()

        except Exception as e:
            logger.warning(f"[CIRCUIT BREAKER:{self.key}] Evaluation error: {e}")
        finally:
            db.close()

    def force_open(self, reason: str = ""):
        """Manual emergency open."""
        logger.critical(f"[CIRCUIT BREAKER:{self.key}] FORCE OPENED: {reason}")
        self._state = CircuitState.OPEN
        self._opened_at = time.time()

    def force_close(self):
        """Manual reset."""
        logger.info(f"[CIRCUIT BREAKER:{self.key}] FORCE CLOSED (manual reset).")
        self._state = CircuitState.CLOSED
        self._opened_at = None


class CircuitBreakerRegistry:
    """
    Manages circuit breakers keyed by (project_id, action_type).
    Also maintains a global emergency breaker.
    """

    def __init__(self):
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._global = CircuitBreaker(project_id="global", action_type="global")

    def get(self, project_id: str, action_type: str) -> CircuitBreaker:
        """Gets or creates a breaker for the given scope."""
        key = f"{project_id}:{action_type}"
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker(project_id=project_id, action_type=action_type)
        return self._breakers[key]

    def should_allow(self, project_id: str, action_type: str) -> bool:
        """
        Returns True only if BOTH the scoped breaker AND global breaker allow.
        Global breaker = emergency kill switch.
        """
        if not self._global.should_allow_action():
            logger.warning(
                f"[CIRCUIT BREAKER] GLOBAL breaker is OPEN. "
                f"Blocking {project_id}:{action_type}."
            )
            return False

        scoped = self.get(project_id, action_type)
        return scoped.should_allow_action()

    def record_outcome(self, project_id: str, action_type: str, success: bool):
        """Records outcome on both the scoped and global breakers."""
        scoped = self.get(project_id, action_type)
        scoped.record_outcome(success)
        self._global.record_outcome(success)

    @property
    def global_breaker(self) -> CircuitBreaker:
        return self._global

    def get_all_states(self) -> Dict[str, str]:
        """Returns all breaker states for dashboard display."""
        states = {"global": self._global.state}
        for key, breaker in self._breakers.items():
            states[key] = breaker.state
        return states


# ─── Singleton Registry ───
_registry_instance: Optional[CircuitBreakerRegistry] = None


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = CircuitBreakerRegistry()
    return _registry_instance


def get_circuit_breaker() -> CircuitBreaker:
    """Backward-compatible: returns the global breaker."""
    return get_circuit_breaker_registry().global_breaker
