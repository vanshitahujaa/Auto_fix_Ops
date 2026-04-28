"""
Safety Drill: Bad Diagnosis
============================
Forces a wrong classification through the pipeline.
Verifies that the policy engine blocks it OR verification catches the error.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from engine.baseline import RuleBasedDiagnosisEngine
from engine.policy import PolicyDecisionEngine, PolicyVerdict
from api.models import Incident, IncidentStatus


def test_unknown_alert_escalates():
    """An unknown alert should NEVER be auto-approved — it must escalate."""
    rule_engine = RuleBasedDiagnosisEngine()
    
    # Force a completely unknown alert through the rule engine
    verdict = rule_engine.analyze("TotallyFakeAlert", {"target_pod": "test-pod"})
    
    assert verdict["root_cause_classification"] == "UNKNOWN_ANOMALY", \
        f"Expected UNKNOWN_ANOMALY, got {verdict['root_cause_classification']}"
    assert verdict["confidence"] == 0.0, \
        f"Expected 0.0 confidence, got {verdict['confidence']}"
    assert verdict["action"]["type"] == "ESCALATE", \
        f"Expected ESCALATE action, got {verdict['action']['type']}"
    
    print("✅ PASS: Unknown alert correctly classified as UNKNOWN_ANOMALY with ESCALATE")


def test_low_confidence_blocks_remediation():
    """A verdict with confidence < 0.80 must be escalated by policy, never approved."""
    policy_engine = PolicyDecisionEngine()
    
    # Create a mock incident
    class MockIncident:
        id = "drill-bad-diag-001"
        status = IncidentStatus.DIAGNOSED
    
    low_confidence_verdict = {
        "root_cause_classification": "CPU_SPIKE",
        "confidence": 0.55,  # Below 0.80 threshold
        "action": {"type": "RESTART_POD", "target": "test-pod"},
    }
    
    result = policy_engine.evaluate(MockIncident(), low_confidence_verdict, "autofixops")
    
    assert result.decision == "ESCALATED", \
        f"Expected ESCALATED, got {result.decision}"
    assert "0.80" in result.reason or "threshold" in result.reason.lower(), \
        f"Expected confidence threshold in reason, got: {result.reason}"
    
    print("✅ PASS: Low confidence (0.55) correctly ESCALATED by policy engine")


def test_invalid_action_rejected():
    """An action NOT in the allowlist must be REJECTED outright."""
    policy_engine = PolicyDecisionEngine()
    
    class MockIncident:
        id = "drill-bad-diag-002"
        status = IncidentStatus.DIAGNOSED
    
    bad_verdict = {
        "confidence": 0.99,
        "action": {"type": "DELETE_NAMESPACE", "target": "prod"},
    }
    
    result = policy_engine.evaluate(MockIncident(), bad_verdict, "autofixops")
    
    assert result.decision == "REJECTED", \
        f"Expected REJECTED, got {result.decision}"
    
    print("✅ PASS: Invalid action 'DELETE_NAMESPACE' correctly REJECTED")


if __name__ == "__main__":
    test_unknown_alert_escalates()
    test_low_confidence_blocks_remediation()
    test_invalid_action_rejected()
    print("\n🛡️  All bad diagnosis drills passed.")
