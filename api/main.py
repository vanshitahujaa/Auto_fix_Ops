import os
import hashlib
import time
from typing import Optional
from fastapi import FastAPI, Depends, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from datetime import datetime, timedelta

from .database import init_relational_db, get_db, incident_contexts_collection, logger
from .models import Incident, IncidentStatus, RemediationAudit, ExecutionStatus, ProjectConfig
from .schemas import AlertmanagerPayload
from workers.tasks import build_incident_context, execute_remediation

app = FastAPI(title="AutoFixOps API Gateway")

# CORS — allow dashboard direct access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    logger.info("Initializing relational database schema...")
    init_relational_db()
    logger.info("AutoFixOps API Gateway started successfully.")


def generate_dedup_key(alert: dict, window_seconds: int = 120) -> str:
    """Generates a time-bucketed deduplication fingerprint."""
    labels = alert.get("labels", {})
    alertname = labels.get("alertname", "Unknown")
    namespace = labels.get("namespace", "default")
    pod = labels.get("pod", "unknown")
    container = labels.get("container", "unknown")
    severity = labels.get("severity", "unknown")

    bucket = int(time.time() / window_seconds)
    raw_key = f"{alertname}-{namespace}-{pod}-{container}-{severity}-{bucket}"
    return hashlib.md5(raw_key.encode()).hexdigest()


# ════════════════════════════════════════════════════════════
# ENDPOINT 1: Webhook Ingestion
# ════════════════════════════════════════════════════════════

@app.post("/api/v1/alerts")
async def receive_alert(
    payload: AlertmanagerPayload, request: Request, db: Session = Depends(get_db)
):
    logger.info("=== [INGEST] Received webhook from Alertmanager ===")
    processed_incidents = []

    for alert in payload.alerts:
        status = alert.get("status", "firing")
        dedup_key = generate_dedup_key(alert)
        alert_name = alert.get("labels", {}).get("alertname", "UnknownAlert")

        if status == "resolved":
            incident = (
                db.query(Incident)
                .filter(
                    Incident.alert_fingerprint == dedup_key,
                    Incident.status != IncidentStatus.RESOLVED,
                )
                .order_by(Incident.created_at.desc())
                .first()
            )
            if incident:
                incident.status = IncidentStatus.RESOLVED
                db.commit()
                logger.info(f"=== [DB WRITE] Incident {incident.id} RESOLVED. ===")
            else:
                logger.warning(f"[DB WRITE IGNORED] Resolved — no active incident for {dedup_key}.")
            continue

        severity = alert.get("labels", {}).get("severity", "unknown")
        incident = Incident(
            alert_fingerprint=dedup_key,
            alert_name=alert_name,
            severity=severity,
            status=IncidentStatus.INGESTED,
            raw_payload_cache=alert,
        )

        try:
            db.add(incident)
            db.commit()
            db.refresh(incident)
            logger.info(f"=== [DB WRITE] Created Incident: {incident.id} ===")
        except IntegrityError:
            db.rollback()
            logger.warning(f"[DB WRITE IGNORED] Deduplicated: {dedup_key}")
            continue

        logger.info(f"=== [QUEUE PUSH] Dispatching for Incident {incident.id} ===")
        task = build_incident_context.apply_async(args=[str(incident.id)])
        logger.info(f"[TRACE:{incident.id}] Task enqueued. Job ID: {task.id}")
        processed_incidents.append(str(incident.id))

    return {"status": "accepted", "processed_incidents": processed_incidents}


# ════════════════════════════════════════════════════════════
# ENDPOINT 2: List Incidents (filterable)
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/incidents")
async def list_incidents(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(Incident).order_by(Incident.created_at.desc())

    if status:
        try:
            status_enum = IncidentStatus(status.upper())
            query = query.filter(Incident.status == status_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    incidents = query.limit(limit).all()

    return {
        "count": len(incidents),
        "incidents": [
            {
                "id": str(i.id),
                "status": i.status.value,
                "alert_name": i.alert_name,
                "severity": i.severity,
                "diagnosis": i.diagnosis_classification,
                "confidence": i.diagnosis_confidence,
                "diagnosed_by": i.diagnosed_by,
                "created_at": i.created_at.isoformat() if i.created_at else None,
                "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
            }
            for i in incidents
        ],
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 3: Incident Detail + Context
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/incidents/{incident_id}/context")
async def get_incident_context(incident_id: str, db: Session = Depends(get_db)):
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")

    # Fetch MongoDB context
    mongo_ctx = incident_contexts_collection.find_one(
        {"incident_id": incident_id}, {"_id": 0}
    )

    # Fetch remediation audits
    audits = (
        db.query(RemediationAudit)
        .filter(RemediationAudit.incident_id == incident_id)
        .all()
    )

    return {
        "incident": {
            "id": str(incident.id),
            "status": incident.status.value,
            "alert_name": incident.alert_name,
            "severity": incident.severity,
            "diagnosis": incident.diagnosis_classification,
            "confidence": incident.diagnosis_confidence,
            "reasoning": incident.diagnosis_reasoning,
            "diagnosed_by": incident.diagnosed_by,
            "created_at": incident.created_at.isoformat() if incident.created_at else None,
        },
        "telemetry_context": mongo_ctx,
        "remediation_audits": [
            {
                "id": str(a.id),
                "action": a.proposed_action,
                "policy_verdict": a.policy_verdict,
                "pr_url": a.github_pr_url,
                "execution_status": a.execution_status.value if a.execution_status else None,
                "is_shadow": a.is_shadow_run,
                "human_agreed": a.human_agreed,
            }
            for a in audits
        ],
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 4: Human Approval (Escalation Override)
# ════════════════════════════════════════════════════════════

@app.post("/api/v1/escalations/{incident_id}/approve")
async def approve_escalation(incident_id: str, db: Session = Depends(get_db)):
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")

    if incident.status != IncidentStatus.ESCALATED:
        raise HTTPException(
            status_code=409,
            detail=f"Incident is in state {incident.status.value}, not ESCALATED.",
        )

    # Build verdict from stored diagnosis
    verdict = {
        "root_cause_classification": incident.diagnosis_classification,
        "confidence": incident.diagnosis_confidence or 1.0,
        "reasoning": incident.diagnosis_reasoning or "",
        "action": {
            "type": (
                "INCREASE_MEMORY_LIMIT"
                if "MEMORY" in (incident.diagnosis_classification or "")
                else "RESTART_POD"
            ),
            "target": incident.raw_payload_cache.get("labels", {}).get("pod", "unknown"),
            "patch_value": "256Mi",
        },
    }

    incident.transition_to(IncidentStatus.POLICY_APPROVED)
    db.commit()

    logger.info(f"[TRACE:{incident.id}] Human approved escalated incident.")
    execute_remediation.apply_async(args=[str(incident.id), verdict])

    return {
        "status": "approved",
        "incident_id": str(incident.id),
        "message": "Incident approved by human. Remediation dispatched.",
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 5: Pipeline Metrics (with temporal windows)
# ════════════════════════════════════════════════════════════

def _compute_metrics(db: Session, since: Optional[datetime] = None) -> dict:
    """Computes aggregate pipeline health metrics, optionally filtered by time."""
    base_q = db.query(Incident)
    audit_q = db.query(RemediationAudit)

    if since:
        base_q = base_q.filter(Incident.created_at >= since)
        audit_q = audit_q.filter(RemediationAudit.created_at >= since)

    total = base_q.count()
    if total == 0:
        return {"total_incidents": 0, "message": "No incidents in this window."}

    resolved = base_q.filter(Incident.status == IncidentStatus.RESOLVED).count()
    failed = base_q.filter(Incident.status == IncidentStatus.FAILED).count()
    escalated = base_q.filter(Incident.status == IncidentStatus.ESCALATED).count()
    by_rule = base_q.filter(Incident.diagnosed_by == "RULE_ENGINE").count()
    by_ai = base_q.filter(Incident.diagnosed_by == "AI_ENGINE").count()

    total_audits = audit_q.count()
    approved = audit_q.filter(RemediationAudit.policy_verdict == "APPROVED").count()
    shadow = audit_q.filter(RemediationAudit.is_shadow_run == "true").count()
    v_pass = audit_q.filter(
        RemediationAudit.execution_status == ExecutionStatus.VERIFICATION_PASSED
    ).count()
    v_fail = audit_q.filter(
        RemediationAudit.execution_status == ExecutionStatus.VERIFICATION_FAILED
    ).count()

    return {
        "total_incidents": total,
        "resolved": resolved,
        "failed": failed,
        "escalated": escalated,
        "rule_engine_hit_rate": round(by_rule / total, 3) if total else 0,
        "ai_fallback_rate": round(by_ai / total, 3) if total else 0,
        "policy_approval_rate": round(approved / total_audits, 3) if total_audits else 0,
        "escalation_rate": round(escalated / total, 3) if total else 0,
        "verification_success_rate": (
            round(v_pass / (v_pass + v_fail), 3)
            if (v_pass + v_fail) > 0
            else 0
        ),
        "shadow_runs": shadow,
    }


@app.get("/api/v1/metrics")
async def get_metrics(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    return {
        "all_time": _compute_metrics(db),
        "last_24h": _compute_metrics(db, since=now - timedelta(hours=24)),
        "last_7d": _compute_metrics(db, since=now - timedelta(days=7)),
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 6: System Status (top bar indicator)
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/status")
async def get_system_status(db: Session = Depends(get_db)):
    from engine.circuit_breaker import get_circuit_breaker
    config = db.query(ProjectConfig).filter(ProjectConfig.id == "singleton").first()
    breaker = get_circuit_breaker()
    return {
        "shadow_mode": config.shadow_mode if config else "true",
        "circuit_breaker": breaker.state,
        "github_connected": bool(config and config.github_token_encrypted),
        "prometheus_url": config.prometheus_url if config else None,
        "target_namespace": config.target_namespace if config else "autofixops",
    }


# ════════════════════════════════════════════════════════════
# ENDPOINT 7: Project Configuration (encrypted token)
# ════════════════════════════════════════════════════════════

@app.get("/api/v1/config")
async def get_config(db: Session = Depends(get_db)):
    config = db.query(ProjectConfig).filter(ProjectConfig.id == "singleton").first()
    if not config:
        return {"configured": False}
    return {
        "configured": True,
        "github_repo": config.github_repo,
        "github_token": config.get_masked_token(),
        "prometheus_url": config.prometheus_url,
        "target_namespace": config.target_namespace,
        "target_manifest_path": config.target_manifest_path,
        "shadow_mode": config.shadow_mode,
        "confidence_threshold": config.confidence_threshold,
    }


@app.post("/api/v1/config")
async def save_config(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    config = db.query(ProjectConfig).filter(ProjectConfig.id == "singleton").first()

    if not config:
        config = ProjectConfig(id="singleton")
        db.add(config)

    config.github_repo = body.get("github_repo", config.github_repo)
    config.prometheus_url = body.get("prometheus_url", config.prometheus_url)
    config.target_namespace = body.get("target_namespace", config.target_namespace)
    config.target_manifest_path = body.get("target_manifest_path", config.target_manifest_path)
    config.shadow_mode = body.get("shadow_mode", config.shadow_mode)
    config.confidence_threshold = body.get("confidence_threshold", config.confidence_threshold)

    # Token: only update if provided (never return raw)
    if body.get("github_token"):
        config.set_github_token(body["github_token"])

    db.commit()
    logger.info("[CONFIG] Project configuration saved.")
    return {"status": "saved"}


# ════════════════════════════════════════════════════════════
# ENDPOINT 8: Chaos Injection (with safeguards)
# ════════════════════════════════════════════════════════════

import httpx

ALLOWED_CHAOS_NAMESPACES = {"autofixops", "staging", "test", "dev", "default"}
CHAOS_RATE_LIMIT: dict = {}  # Simple in-memory rate limiter

@app.post("/api/v1/chaos/inject")
async def inject_chaos(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    fault_type = body.get("fault_type")  # "memory_leak", "cpu_spike", "crash_loop"
    target_url = body.get("target_url")  # e.g. "http://target-app:8000"
    confirmation = body.get("confirmation")  # Must be "CONFIRM"

    if confirmation != "CONFIRM":
        raise HTTPException(status_code=400, detail="Must type CONFIRM to inject chaos.")

    # Namespace check
    config = db.query(ProjectConfig).filter(ProjectConfig.id == "singleton").first()
    namespace = config.target_namespace if config else "autofixops"
    if namespace not in ALLOWED_CHAOS_NAMESPACES:
        raise HTTPException(
            status_code=403,
            detail=f"Chaos injection blocked: namespace '{namespace}' is not in allowed list.",
        )

    # Rate limit: max 1 injection per 30 seconds
    now = time.time()
    last = CHAOS_RATE_LIMIT.get(fault_type, 0)
    if now - last < 30:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Wait {30 - int(now - last)}s before next injection.",
        )
    CHAOS_RATE_LIMIT[fault_type] = now

    # Route to target app endpoint
    endpoint_map = {
        "memory_leak": "/leak",
        "cpu_spike": "/cpu",
        "crash_loop": "/crash",
    }
    endpoint = endpoint_map.get(fault_type)
    if not endpoint:
        raise HTTPException(status_code=400, detail=f"Unknown fault type: {fault_type}")

    if not target_url:
        raise HTTPException(status_code=400, detail="target_url is required.")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{target_url}{endpoint}")
        logger.info(f"[CHAOS] Injected {fault_type} via {target_url}{endpoint} → {resp.status_code}")
        return {
            "status": "injected",
            "fault_type": fault_type,
            "target": f"{target_url}{endpoint}",
            "response_code": resp.status_code,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to reach target: {str(e)}")
