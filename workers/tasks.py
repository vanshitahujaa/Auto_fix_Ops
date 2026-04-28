"""
AutoFixOps Pipeline Orchestration
==================================
Each Celery task represents one discrete stage of the incident lifecycle.
Each task reads the current state, does its work, commits the new state,
then dispatches the next task. If any task fails, it sets FAILED and
stops the chain.

Pipeline:
  build_incident_context  →  CONTEXT_BUILT
  diagnose_incident       →  DIAGNOSED
  evaluate_policy         →  POLICY_APPROVED / ESCALATED
  execute_remediation     →  PENDING_PR_MERGE
  verify_resolution       →  RESOLVED / FAILED
"""

import requests
import time
from .celery_app import celery_app
from api.database import SessionLocal, incident_contexts_collection, logger
from api.models import Incident, IncidentStatus

# Engine imports
from engine.summarizer import ContextSummarizer
from engine.baseline import RuleBasedDiagnosisEngine
from engine.ai_diagnostics import AIDiagnosisEngine
from engine.memory import QdrantMemoryStore
from engine.policy import PolicyDecisionEngine
from engine.remediation import RemediationEngine
from engine.verification import VerificationEngine

PROMETHEUS_URL = "http://prometheus-operated.autofixops.svc.cluster.local:9090"


def fetch_prometheus_metric(query: str):
    """Executes a promQL query with strict timeouts."""
    try:
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=3,
        )
        response.raise_for_status()
        return response.json().get("data", {}).get("result", [])
    except Exception as e:
        logger.error(f"Prometheus query failed: {e}")
        return []


# ════════════════════════════════════════════════════════════
# TASK 1: Context Gathering
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=3)
def build_incident_context(self, incident_id: str):
    """Gathers telemetry from Prometheus, stores in MongoDB, transitions to CONTEXT_BUILT."""
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

        pod_name = incident.raw_payload_cache.get("labels", {}).get("pod", "unknown")

        # Gather evidence
        cpu_query = f'rate(container_cpu_usage_seconds_total{{namespace="autofixops", pod="{pod_name}"}}[1m])'
        mem_query = f'avg_over_time(container_memory_working_set_bytes{{namespace="autofixops", pod="{pod_name}"}}[5m])'
        cpu_data = fetch_prometheus_metric(cpu_query)
        mem_data = fetch_prometheus_metric(mem_query)

        context_doc = {
            "incident_id": incident_id,
            "metrics": {"cpu": cpu_data, "memory": mem_data},
            "timestamp": time.time(),
            "target_pod": pod_name,
        }

        incident_contexts_collection.update_one(
            {"incident_id": incident_id}, {"$set": context_doc}, upsert=True
        )

        # State transition
        incident.transition_to(IncidentStatus.CONTEXT_BUILT)
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
    logger.info(f"[TRACE:{incident_id}] === [TASK 2: DIAGNOSIS] START ===")
    db = SessionLocal()

    try:
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            return False

        if incident.status != IncidentStatus.CONTEXT_BUILT:
            logger.warning(f"[TRACE:{incident_id}] Not in CONTEXT_BUILT state. Skipping.")
            return True

        # Fetch context from Mongo
        context_doc = incident_contexts_collection.find_one({"incident_id": incident_id})
        if not context_doc:
            logger.error(f"[TRACE:{incident_id}] No context document found in MongoDB.")
            _mark_failed(db, incident_id)
            return False

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

        incident.transition_to(IncidentStatus.DIAGNOSED)
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
            db.commit()

            # Chain to remediation
            execute_remediation.apply_async(args=[incident_id, verdict])

        elif policy_result.decision in ("ESCALATED", "REJECTED"):
            incident.transition_to(IncidentStatus.ESCALATED)
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
# TASK 4: Remediation (GitOps PR)
# ════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=2)
def execute_remediation(self, incident_id: str, verdict: dict):
    """Creates the GitOps PR with the patched manifest."""
    logger.info(f"[TRACE:{incident_id}] === [TASK 4: REMEDIATION] START ===")
    db = SessionLocal()

    try:
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if not incident:
            return False

        if incident.status != IncidentStatus.POLICY_APPROVED:
            logger.warning(f"[TRACE:{incident_id}] Not in POLICY_APPROVED state. Skipping.")
            return True

        incident.transition_to(IncidentStatus.REMEDIATING)
        db.commit()

        # Build summary text for the PR body
        summary_text = (
            f"CPU Peak: {incident.diagnosis_confidence}, "
            f"Classification: {incident.diagnosis_classification}, "
            f"Reasoning: {incident.diagnosis_reasoning}"
        )

        remediation_engine = RemediationEngine()
        result = remediation_engine.execute(incident_id, verdict, summary_text)

        incident.transition_to(IncidentStatus.PENDING_PR_MERGE)
        db.commit()

        logger.info(
            f"[TRACE:{incident_id}] === [TASK 4: REMEDIATION] DONE → PENDING_PR_MERGE "
            f"(PR: {result.get('pr_url')}) ==="
        )

        # Chain to verification
        pod_name = incident.raw_payload_cache.get("labels", {}).get("pod", "unknown")
        verify_resolution.apply_async(
            args=[
                incident_id,
                result.get("audit_id"),
                summary_text,
                incident.diagnosis_classification,
                verdict.get("action", {}).get("type", "UNKNOWN"),
                pod_name,
            ],
            countdown=30,  # Small delay before starting verification
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
):
    """Polls PR merge, waits for sync, re-checks telemetry, stores learning."""
    logger.info(f"[TRACE:{incident_id}] === [TASK 5: VERIFICATION] START ===")

    try:
        engine = VerificationEngine()
        success = engine.verify(
            incident_id=incident_id,
            audit_id=audit_id,
            summary_text=summary_text,
            diagnosis=diagnosis,
            action_taken=action_taken,
            pod_name=pod_name,
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
