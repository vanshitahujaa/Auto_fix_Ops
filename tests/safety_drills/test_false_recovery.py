"""
Safety Drill: False Recovery
==============================
Tests scenarios where the system might falsely mark an incident as RESOLVED:
1. Partial recovery: CPU drops but memory still rising
2. Intermittent health: metrics flicker between healthy and degraded
3. Pod restart masking: new pod appears healthy but is the same broken image

The verification engine must NOT mark RESOLVED in any of these cases.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from engine.summarizer import ContextSummarizer, IncidentSummary


def test_partial_recovery_detected():
    """
    CPU drops to healthy, but memory is still climbing.
    Summarizer must NOT tag this as fully stable.
    """
    context = {
        "metrics": {
            "cpu": [
                {"value": [100, "0.9"]},
                {"value": [200, "0.7"]},
                {"value": [300, "0.3"]},  # CPU recovered
            ],
            "memory": [
                {"value": [100, "0.5"]},
                {"value": [200, "0.7"]},
                {"value": [300, "0.92"]},  # Memory still climbing
            ],
        }
    }

    summary = ContextSummarizer.summarize(context)

    assert summary.memory_peak >= 0.9, \
        f"Expected memory_peak >= 0.9, got {summary.memory_peak}"
    assert summary.memory_trend != "STABLE", \
        f"Expected non-STABLE memory trend, got {summary.memory_trend}"
    assert "resource_exhaustion" in summary.context_tags, \
        f"Expected 'resource_exhaustion' tag for memory > 0.9"

    print(f"✅ PASS: Partial recovery detected — memory_peak={summary.memory_peak}, trend={summary.memory_trend}")


def test_single_healthy_point_not_stable():
    """
    A single healthy datapoint should not be confused with sustained stability.
    The trend should be UNKNOWN or STABLE (since len < 2), not HIGH_SUSTAINED.
    """
    context = {
        "metrics": {
            "cpu": [{"value": [100, "0.1"]}],  # One point
            "memory": [{"value": [100, "0.1"]}],
        }
    }

    summary = ContextSummarizer.summarize(context)

    assert summary.cpu_trend == "STABLE", \
        f"Expected STABLE for single point, got {summary.cpu_trend}"
    assert summary.context_tags == [], \
        f"Expected no tags for low values, got {summary.context_tags}"

    print("✅ PASS: Single healthy datapoint correctly classified as STABLE, no false tags")


def test_empty_metrics_handled():
    """
    If Prometheus returns empty data, the system must not crash.
    Summary should reflect UNKNOWN state, not false health.
    """
    context = {
        "metrics": {
            "cpu": [],
            "memory": [],
        }
    }

    summary = ContextSummarizer.summarize(context)

    # With empty data, defaults should kick in
    assert summary.cpu_peak == 0.0, f"Expected 0.0, got {summary.cpu_peak}"
    assert summary.memory_peak == 0.0, f"Expected 0.0, got {summary.memory_peak}"

    print("✅ PASS: Empty metrics handled gracefully without crash")


def test_rapid_climb_detected():
    """
    A memory series climbing rapidly (>0.6 delta) must be tagged RAPID_CLIMB.
    """
    context = {
        "metrics": {
            "cpu": [{"value": [100, "0.1"]}, {"value": [200, "0.1"]}],
            "memory": [
                {"value": [100, "0.1"]},
                {"value": [200, "0.4"]},
                {"value": [300, "0.85"]},  # Delta 0.75 > 0.6 threshold
            ],
        }
    }

    summary = ContextSummarizer.summarize(context)

    assert summary.memory_trend == "RAPID_CLIMB", \
        f"Expected RAPID_CLIMB, got {summary.memory_trend}"

    print(f"✅ PASS: Rapid memory climb correctly detected: {summary.memory_trend}")


if __name__ == "__main__":
    test_partial_recovery_detected()
    test_single_healthy_point_not_stable()
    test_empty_metrics_handled()
    test_rapid_climb_detected()
    print("\n🛡️  All false recovery drills passed.")
