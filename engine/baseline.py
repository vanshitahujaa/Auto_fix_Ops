import logging
from typing import Dict, Any

logger = logging.getLogger("autofixops")

class RuleBasedDiagnosisEngine:
    """
    Phase 3 Baseline: Maps operational evidence directly to remediation actions
    without requiring LLM generation. Deterministic and safe map.
    """
    
    def __init__(self):
        # Bounded taxonomy definition maps directly to Phase 1 alerts
        self.rules = {
            "HighCPUUsage": self._handle_cpu_spike,
            "TargetAppMemoryLeak": self._handle_memory_leak,
            "PodCrashLooping": self._handle_crash_loop
        }

    def analyze(self, incident_name: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Entry point for the Engine. Returns a precise Action Output."""
        logger.info(f"Executing Rule-Based Diagnostics for {incident_name}")
        
        handler = self.rules.get(incident_name, self._handle_unknown)
        return handler(context)
        
    def _handle_cpu_spike(self, context: Dict):
        return {
            "root_cause_classification": "CPU_RESOURCE_STARVATION",
            "confidence": 1.0, # Rule based is 100%
            "reasoning": "A known HighCPUUsage alert was caught. Evidence metric exceeds limits.",
            "action": {
                "type": "RESTART_POD",
                "target": context.get("target_pod")
            }
        }
        
    def _handle_memory_leak(self, context: Dict):
        return {
            "root_cause_classification": "MEMORY_LEAK_OOM_RISK",
            "confidence": 1.0,
            "reasoning": "Memory leak detected exceeding limits. Recommend vertical scaling via GitOps.",
            "action": {
                "type": "INCREASE_MEMORY_LIMIT",
                "target": context.get("target_pod"),
                "patch_value": "200Mi" # Hardcoded safe baseline
            }
        }
        
    def _handle_crash_loop(self, context: Dict):
        return {
            "root_cause_classification": "CRASH_LOOP_BACKOFF",
            "confidence": 1.0,
            "reasoning": "Liveness failures triggered backoff.",
            "action": {
                "type": "ROLLBACK_DEPLOYMENT",
                "target": context.get("target_pod")
            }
        }
        
    def _handle_unknown(self, context: Dict):
        return {
            "root_cause_classification": "UNKNOWN_ANOMALY",
            "confidence": 0.0,
            "reasoning": "No deterministic rule exists for this incident type.",
            "action": {
                "type": "ESCALATE",
                "target": "human"
            }
        }
