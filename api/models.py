from sqlalchemy import Column, String, DateTime, Enum, JSON, ForeignKey, Float, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import uuid
import datetime
import enum
from .database import Base


# ─── Valid State Transitions ───
# Each key maps to the set of states it is allowed to transition TO.
VALID_TRANSITIONS = {
    "INGESTED":         {"CONTEXT_BUILT", "FAILED"},
    "CONTEXT_BUILT":    {"DIAGNOSED", "FAILED"},
    "DIAGNOSED":        {"POLICY_APPROVED", "ESCALATED", "FAILED"},
    "POLICY_APPROVED":  {"REMEDIATING", "FAILED"},
    "REMEDIATING":      {"PENDING_PR_MERGE", "FAILED"},
    "PENDING_PR_MERGE": {"VERIFIED", "FAILED"},
    "VERIFIED":         {"RESOLVED", "FAILED"},
    "ESCALATED":        {"POLICY_APPROVED", "RESOLVED"},  # Human can approve or resolve
    "FAILED":           set(),          # Terminal state
    "RESOLVED":         set(),          # Terminal state
}


class IncidentStatus(str, enum.Enum):
    INGESTED = "INGESTED"
    CONTEXT_BUILT = "CONTEXT_BUILT"
    DIAGNOSED = "DIAGNOSED"
    POLICY_APPROVED = "POLICY_APPROVED"
    ESCALATED = "ESCALATED"
    REMEDIATING = "REMEDIATING"
    PENDING_PR_MERGE = "PENDING_PR_MERGE"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    RESOLVED = "RESOLVED"


class ExecutionStatus(str, enum.Enum):
    PR_CREATED = "PR_CREATED"
    PR_MERGED = "PR_MERGED"
    PR_REJECTED = "PR_REJECTED"
    VERIFICATION_PASSED = "VERIFICATION_PASSED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alert_fingerprint = Column(String, unique=True, index=True, nullable=False)
    status = Column(Enum(IncidentStatus, name="incident_status"), default=IncidentStatus.INGESTED, nullable=False)
    severity = Column(String, nullable=False)
    alert_name = Column(String, nullable=False)
    raw_payload_cache = Column(JSON, nullable=True)

    # Diagnosis fields (populated after engine runs)
    diagnosis_classification = Column(String, nullable=True)
    diagnosis_confidence = Column(Float, nullable=True)
    diagnosis_reasoning = Column(Text, nullable=True)
    diagnosed_by = Column(String, nullable=True)  # "RULE_ENGINE" or "AI_ENGINE"

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)

    # Relationship to audit trail
    remediation_audits = relationship("RemediationAudit", back_populates="incident")

    def transition_to(self, new_status: IncidentStatus):
        """Enforces valid state transitions. Raises ValueError on illegal jumps."""
        allowed = VALID_TRANSITIONS.get(self.status.value, set())
        if new_status.value not in allowed:
            raise ValueError(
                f"Illegal state transition: {self.status.value} → {new_status.value}. "
                f"Allowed: {allowed}"
            )
        self.status = new_status
        if new_status == IncidentStatus.RESOLVED:
            self.resolved_at = datetime.datetime.utcnow()


class RemediationAudit(Base):
    __tablename__ = "remediation_audits"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id = Column(UUID(as_uuid=True), ForeignKey("incidents.id"), nullable=False)
    proposed_action = Column(String, nullable=False)
    patch_details = Column(JSON, nullable=True)       # The exact YAML diff applied
    policy_verdict = Column(String, nullable=False)     # APPROVED / ESCALATED / REJECTED
    policy_reason = Column(Text, nullable=True)
    github_pr_url = Column(String, nullable=True)
    github_branch = Column(String, nullable=True)
    execution_status = Column(
        Enum(ExecutionStatus, name="execution_status"),
        default=ExecutionStatus.PR_CREATED,
        nullable=True
    )

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    # Trust calibration fields
    is_shadow_run = Column(String, default="false")  # "true" / "false"
    human_agreed = Column(String, nullable=True)      # "true" / "false" / None (pending)
    failure_reason = Column(String, nullable=True)     # verification_failed, policy_blocked, invalid_patch

    # Relationship back to parent incident
    incident = relationship("Incident", back_populates="remediation_audits")
