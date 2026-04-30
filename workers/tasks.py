"""
AutoFixOps Pipeline Orchestration
==================================
Each Celery task represents one discrete stage of the incident lifecycle.
Each task reads the current state, does its work, commits the new state,
then dispatches the next task. If any task fails, it sets FAILED and
stops the chain.

All tasks check SYSTEM_MODE at the top — DISABLED = immediate return.
All tasks thread project_id through the pipeline.

Pipeline:
  build_incident_context  →  CONTEXT_BUILT
  diagnose_incident       →  DIAGNOSED
  evaluate_policy         →  POLICY_APPROVED / ESCALATED
  execute_remediation     →  PENDING_PR_MERGE
  verify_resolution       →  RESOLVED / FAILED
"""

import os
import requests
import time
from .celery_app import celery_app
from api.database import SessionLocal, incident_contexts_collection, logger
from api.models import Incident, IncidentStatus
from api.config_helpers import get_project_config, get_system_mode, is_system_disabled
from api.events import emit_sync

# Engine imports
from engine.summarizer import ContextSummarizer
from engine.baseline import RuleBasedDiagnosisEngine
from engine.ai_diagnostics import AIDiagnosisEngine
from engine.memory import QdrantMemoryStore
from engine.policy import PolicyDecisionEngine
from engine.remediation import RemediationEngine
from engine.verification import VerificationEngine
from engine.target_resolver import TargetResolver, TargetResolutionError


def _check_kill_switch(incident_id: str) -> bool:
    """Returns True if system is disabled (task should abort)."""
    if is_system_disabled():
        logger.warning(
            f"[TRACE:{incident_id}] [KILL SWITCH] System is DISABLED. "
            f"Skipping task execution."
        )
        return True
    return False


_DEFAULT_PROM_URL = os.getenv("PROMETHEUS_URL", "").strip()


def _get_prometheus_url(project_id: str = None) -> str:
    """Gets Prometheus URL from project config, falling back to env, then to in-cluster DNS."""
    if project_id:
        config = get_project_config(project_id)
        if config and config.get("prometheus_url"):
            return config["prometheus_url"]
    if _DEFAULT_PROM_URL:
        return _DEFAULT_PROM_URL
    return "http://prometheus-operated.autofixops.svc.cluster.local:9090"


def fetch_prometheus_metric(query: str, prometheus_url: str = None):
    """Executes a promQL query with strict timeouts. Returns [] on any failure."""
    url = prometheus_url or _get_prometheus_url()
    try:
        response = requests.get(
            f"{url}/api/v1/query",
            params={"query": query},
            timeout=3,
        )
        response.raise_for_status()
        return response.json().get("data", {}).get("result", [])
    except requests.Timeout:
        logger.warning(f"Prometheus query timed out (3s) — degrading to empty context.")
        return []
    except requests.exceptions.ConnectionError:
        logger.warning(f"Prometheus unreachable at {url} — degrading to empty context.")
        return []
    except Exception as e:
        logger.warning(f"Prometheus query failed ({e.__class__.__name__}) — degrading to empty context.")
        return []


# ════════════════════════════════════════════════════════════
# TASK 1: Context Gathering
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=3, time_limit=120, soft_time_limit=90)
def build_incident_context(self, incident_id: str):
    """Gathers telemetry from Prometheus, stores in MongoDB, transitions to CONTEXT_BUILT."""
    if _check_kill_switch(incident_id):
        return False

    logger.info(f"[TRACE:{incident_id}] === [TASK 1: CONTEXT] START ===")
    db = SessionLocal()

    try:
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            logger.error(f"[TRACE:{incident_id}] Incident not found.")
            return False

        # Idempotency guard
        if incident.status != IncidentStatus.INGESTED:
            logger.warning(f"[TRACE:{incident_id}] Already past INGESTED. Skipping.")
            return True

        project_id = str(incident.project_id) if incident.project_id else None
        prometheus_url = _get_prometheus_url(project_id)
        pod_name = incident.raw_payload_cache.get("labels", {}).get("pod", "unknown")

        # Gather evidence
        cpu_query = f'rate(container_cpu_usage_seconds_total{{namespace="autofixops", pod="{pod_name}"}}[1m])'
        mem_query = f'avg_over_time(container_memory_working_set_bytes{{namespace="autofixops", pod="{pod_name}"}}[5m])'
        cpu_data = fetch_prometheus_metric(cpu_query, prometheus_url)
        mem_data = fetch_prometheus_metric(mem_query, prometheus_url)

        context_doc = {
            "incident_id": incident_id,
            "project_id": project_id,
            "metrics": {"cpu": cpu_data, "memory": mem_data},
            "timestamp": time.time(),
            "target_pod": pod_name,
        }

        try:
            incident_contexts_collection.update_one(
                {"incident_id": incident_id}, {"$set": context_doc}, upsert=True
            )
        except Exception as mongo_err:
            logger.warning(
                f"[TRACE:{incident_id}] MongoDB write failed ({mongo_err.__class__.__name__}); "
                f"continuing without persistent context."
            )

        # State transition
        incident.transition_to(IncidentStatus.CONTEXT_BUILT)
        emit_sync("incident.status_changed", {"new_status": "CONTEXT_BUILT"}, str(incident.id))
        db.commit()

        logger.info(f"[TRACE:{incident_id}] === [TASK 1: CONTEXT] DONE → CONTEXT_BUILT ===")

        # Chain to next task
        diagnose_incident.apply_async(args=[incident_id])
        return True

    except Exception as e:
        logger.error(f"[TRACE:{incident_id}] [TASK 1 FAILED] {e}")
        try:
            self.retry(exc=e, countdown=2 ** self.request.retries)
        except self.MaxRetriesExceededError:
            _mark_failed(db, incident_id)
            return False
    finally:
        db.close()


# ════════════════════════════════════════════════════════════
# TASK 2: Diagnosis (Rule Engine → AI Fallback)
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=2)
def diagnose_incident(self, incident_id: str):
    """Runs Rule Engine first. Falls through to AI on UNKNOWN. Stores verdict on Incident."""
    if _check_kill_switch(incident_id):
        return False

    logger.info(f"[TRACE:{incident_id}] === [TASK 2: DIAGNOSIS] START ===")
    db = SessionLocal()

    try:
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            return False

        if incident.status != IncidentStatus.CONTEXT_BUILT:
            logger.warning(f"[TRACE:{incident_id}] Not in CONTEXT_BUILT state. Skipping.")
            return True

        # Fetch context from Mongo (degrade gracefully if Atlas is down)
        try:
            context_doc = incident_contexts_collection.find_one({"incident_id": incident_id})
        except Exception as mongo_err:
            logger.warning(
                f"[TRACE:{incident_id}] MongoDB read failed ({mongo_err.__class__.__name__}); "
                f"using empty context."
            )
            context_doc = None

        if not context_doc:
            logger.warning(
                f"[TRACE:{incident_id}] No context document available — "
                f"diagnosing on alert metadata only."
            )
            context_doc = {"incident_id": incident_id, "metrics": {"cpu": [], "memory": []}}

        alert_name = incident.alert_name

        # Summarize
        summary = ContextSummarizer.summarize(context_doc)
        logger.info(f"[TRACE:{incident_id}] Summary: {summary.model_dump_json()}")

        # Rule Engine first
        rule_engine = RuleBasedDiagnosisEngine()
        verdict = rule_engine.analyze(alert_name, context_doc)
        diagnosed_by = "RULE_ENGINE"

        # AI fallback if unknown
        if verdict.get("root_cause_classification") == "UNKNOWN_ANOMALY":
            logger.info(f"[TRACE:{incident_id}] Rule Engine → UNKNOWN. Invoking AI...")
            diagnosed_by = "AI_ENGINE"

            memory_store = QdrantMemoryStore()
            rag_hits = memory_store.retrieve_similar(summary.model_dump_json(), top_k=2)
            failed_hits = memory_store.retrieve_failures(summary.model_dump_json(), top_k=2)

            ai_engine = AIDiagnosisEngine()
            ai_verdict = ai_engine.analyze(summary, rag_hits, failed_hits=failed_hits)

            verdict = {
                "root_cause_classification": ai_verdict.get("diagnosis"),
                "confidence": ai_verdict.get("confidence", 0.0),
                "reasoning": " ".join(
                    ai_verdict.get("recommended_action", {}).get("reasoning", [])
                ),
                "action": ai_verdict.get("recommended_action"),
            }

        # Persist diagnosis on the incident record
        incident.diagnosis_classification = verdict.get("root_cause_classification")
        incident.diagnosis_confidence = verdict.get("confidence", 1.0)
        incident.diagnosis_reasoning = verdict.get("reasoning", "")
        incident.diagnosed_by = diagnosed_by
        emit_sync("incident.diagnosed", {"classification": incident.diagnosis_classification, "confidence": incident.diagnosis_confidence, "engine": incident.diagnosed_by}, str(incident.id))

        incident.transition_to(IncidentStatus.DIAGNOSED)
        emit_sync("incident.status_changed", {"new_status": "DIAGNOSED"}, str(incident.id))
        db.commit()

        logger.info(
            f"[TRACE:{incident_id}] === [TASK 2: DIAGNOSIS] DONE → DIAGNOSED "
            f"({diagnosed_by}: {verdict.get('root_cause_classification')}) ==="
        )

        # Chain: pass verdict along
        evaluate_policy.apply_async(args=[incident_id, verdict])
        return True

    except Exception as e:
        logger.error(f"[TRACE:{incident_id}] [TASK 2 FAILED] {e}")
        try:
            self.retry(exc=e, countdown=2 ** self.request.retries)
        except self.MaxRetriesExceededError:
            _mark_failed(db, incident_id)
            return False
    finally:
        db.close()


# ════════════════════════════════════════════════════════════
# TASK 3: Policy Evaluation
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=1)
def evaluate_policy(self, incident_id: str, verdict: dict):
    """Evaluates the diagnosis against policy gates. Routes to remediation or escalation."""
    if _check_kill_switch(incident_id):
        return False

    logger.info(f"[TRACE:{incident_id}] === [TASK 3: POLICY] START ===")
    db = SessionLocal()

    try:
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            return False

        if incident.status != IncidentStatus.DIAGNOSED:
            logger.warning(f"[TRACE:{incident_id}] Not in DIAGNOSED state. Skipping.")
            return True

        namespace = incident.raw_payload_cache.get("labels", {}).get(
            "namespace", "autofixops"
        )

        policy_engine = PolicyDecisionEngine()
        policy_result = policy_engine.evaluate(incident, verdict, namespace)

        logger.info(
            f"[TRACE:{incident_id}] [POLICY RESULT] "
            f"{policy_result.decision}: {policy_result.reason}"
        )

        if policy_result.decision == "APPROVED":
            incident.transition_to(IncidentStatus.POLICY_APPROVED)
            emit_sync("incident.status_changed", {"new_status": "POLICY_APPROVED"}, str(incident.id))
            db.commit()

            # Chain to remediation
            execute_remediation.apply_async(args=[incident_id, verdict])

        elif policy_result.decision in ("ESCALATED", "REJECTED"):
            incident.transition_to(IncidentStatus.ESCALATED)
            emit_sync("incident.status_changed", {"new_status": "ESCALATED"}, str(incident.id))
            db.commit()
            logger.info(
                f"[TRACE:{incident_id}] === [TASK 3: POLICY] DONE → ESCALATED ==="
            )
            # Pipeline stops here — human must intervene

        logger.info(f"[TRACE:{incident_id}] === [TASK 3: POLICY] DONE ===")
        return True

    except Exception as e:
        logger.error(f"[TRACE:{incident_id}] [TASK 3 FAILED] {e}")
        _mark_failed(db, incident_id)
        return False
    finally:
        db.close()


# ════════════════════════════════════════════════════════════
# TASK 4: Remediation (GitOps PR) — with target resolution
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=2)
def execute_remediation(self, incident_id: str, verdict: dict):
    """Resolves target, validates, then creates the GitOps PR."""
    if _check_kill_switch(incident_id):
        return False

    logger.info(f"[TRACE:{incident_id}] === [TASK 4: REMEDIATION] START ===")
    db = SessionLocal()

    try:
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            return False

        if incident.status != IncidentStatus.POLICY_APPROVED:
            logger.warning(f"[TRACE:{incident_id}] Not in POLICY_APPROVED state. Skipping.")
            return True

        project_id = str(incident.project_id) if incident.project_id else None
        project_config = get_project_config(project_id) if project_id else {}

        # ─── Circuit breaker check ───
        from engine.circuit_breaker import get_circuit_breaker_registry
        action_type = verdict.get("action", {}).get("type", "UNKNOWN")

        if not get_circuit_breaker_registry().should_allow(
            project_id or "global", action_type
        ):
            logger.warning(
                f"[TRACE:{incident_id}] [CIRCUIT BREAKER] Blocked action {action_type}. "
                f"Escalating instead."
            )
            incident.transition_to(IncidentStatus.ESCALATED)
            emit_sync("incident.status_changed", {"new_status": "ESCALATED"}, str(incident.id))
            db.commit()
            return True

        # ─── Target resolution ───
        resolver = TargetResolver()
        try:
            context_doc = incident_contexts_collection.find_one({"incident_id": incident_id})
        except Exception as mongo_err:
            logger.warning(
                f"[TRACE:{incident_id}] MongoDB read failed in remediation "
                f"({mongo_err.__class__.__name__}); resolving without context."
            )
            context_doc = None

        try:
            resolved_target = resolver.resolve(
                incident.raw_payload_cache or {},
                project_config or {},
                context_doc,
            )
        except TargetResolutionError as e:
            logger.error(f"[TRACE:{incident_id}] [TARGET RESOLUTION FAILED] {e}")
            _mark_failed(db, incident_id)
            return False

        # Pre-execution validation
        if not resolver.validate_target(resolved_target):
            logger.error(f"[TRACE:{incident_id}] [TARGET VALIDATION FAILED]")
            _mark_failed(db, incident_id)
            return False

        # Store resolved target on incident
        incident.resolved_target = resolved_target
        incident.transition_to(IncidentStatus.REMEDIATING)
        emit_sync("incident.status_changed", {"new_status": "REMEDIATING"}, str(incident.id))
        db.commit()

        # Build summary text for the PR body
        summary_text = (
            f"CPU Peak: {incident.diagnosis_confidence}, "
            f"Classification: {incident.diagnosis_classification}, "
            f"Reasoning: {incident.diagnosis_reasoning}"
        )

        remediation_engine = RemediationEngine(project_config=project_config)
        result = remediation_engine.execute(incident_id, verdict, summary_text)

        incident.transition_to(IncidentStatus.PENDING_PR_MERGE)
        emit_sync("incident.status_changed", {"new_status": "PENDING_PR_MERGE"}, str(incident.id))
        db.commit()

        logger.info(
            f"[TRACE:{incident_id}] === [TASK 4: REMEDIATION] DONE → PENDING_PR_MERGE "
            f"(PR: {result.get('pr_url')}) ==="
        )

        # Capture baseline metrics for negative signal detection
        baseline_metrics = resolved_target.get("current_values", {})

        # Chain to verification
        pod_name = incident.raw_payload_cache.get("labels", {}).get("pod", "unknown")
        verify_resolution.apply_async(
            args=[
                incident_id,
                result.get("audit_id"),
                summary_text,
                incident.diagnosis_classification,
                action_type,
                pod_name,
                baseline_metrics,
            ],
            countdown=30,
        )
        return True

    except Exception as e:
        logger.error(f"[TRACE:{incident_id}] [TASK 4 FAILED] {e}")
        try:
            self.retry(exc=e, countdown=2 ** self.request.retries)
        except self.MaxRetriesExceededError:
            _mark_failed(db, incident_id)
            return False
    finally:
        db.close()


# ════════════════════════════════════════════════════════════
# TASK 5: Verification & Learning
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=1)
def verify_resolution(
    self,
    incident_id: str,
    audit_id: str,
    summary_text: str,
    diagnosis: str,
    action_taken: str,
    pod_name: str,
    baseline_metrics: dict = None,
):
    """Polls PR merge, stability window check, stores learning."""
    if _check_kill_switch(incident_id):
        return False

    logger.info(f"[TRACE:{incident_id}] === [TASK 5: VERIFICATION] START ===")

    try:
        # Get project config for this incident
        db = SessionLocal()
        try:
            incident = db.query(Incident).filter(Incident.id == incident_id).first()
            project_id = str(incident.project_id) if incident and incident.project_id else None
        finally:
            db.close()

        project_config = get_project_config(project_id) if project_id else {}

        engine = VerificationEngine(project_config=project_config)
        success = engine.verify(
            incident_id=incident_id,
            audit_id=audit_id,
            summary_text=summary_text,
            diagnosis=diagnosis,
            action_taken=action_taken,
            pod_name=pod_name,
            baseline_metrics=baseline_metrics,
        )

        if success:
            logger.info(
                f"[TRACE:{incident_id}] === [TASK 5: VERIFICATION] DONE → RESOLVED ==="
            )
        else:
            logger.warning(
                f"[TRACE:{incident_id}] === [TASK 5: VERIFICATION] DONE → FAILED ==="
            )

        return success

    except Exception as e:
        logger.error(f"[TRACE:{incident_id}] [TASK 5 FAILED] {e}")
        return False


# ════════════════════════════════════════════════════════════
# Utility
# ════════════════════════════════════════════════════════════

def _mark_failed(db, incident_id: str):
    """Safely marks an incident as FAILED after max retries."""
    try:
        db.rollback()
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if incident and incident.status not in (
            IncidentStatus.FAILED,
            IncidentStatus.RESOLVED,
        ):
            incident.status = IncidentStatus.FAILED
            db.commit()
            logger.critical(
                f"[TRACE:{incident_id}] [FATAL] Incident marked FAILED after max retries."
            )
    except Exception as e:
        logger.error(f"[TRACE:{incident_id}] Failed to mark FAILED: {e}")
