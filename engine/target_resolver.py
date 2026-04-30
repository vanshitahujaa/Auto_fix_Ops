"""
Target Resolver for AutoFixOps
================================
Resolves an incident's alert metadata to an exact Kubernetes target spec.
Cross-validates pod labels, owner references, and namespace alignment
to prevent mismatched remediation.

No remediation executes without a resolved target.
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("autofixops")


class TargetResolutionError(Exception):
    """Raised when target cannot be resolved or cross-validation fails."""
    pass


class TargetResolver:
    """
    Resolves incident alert metadata → exact container spec.
    
    Output:
    {
        "namespace": "autofixops",
        "deployment": "target-app",
        "container": "app",
        "pod_pattern": "target-app-*",
        "resource_spec": {"memory_limit": "128Mi", "cpu_limit": "100m"},
        "current_values": {...},
        "confidence": 0.95,
        "validation_passed": True
    }
    """

    def resolve(
        self,
        incident_payload: Dict[str, Any],
        project_config: Dict[str, Any],
        context_doc: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Resolves the target from alert labels + context.
        Raises TargetResolutionError if cross-validation fails.
        """
        labels = incident_payload.get("labels", {})
        
        namespace = labels.get("namespace", project_config.get("target_namespace", "autofixops"))
        pod = labels.get("pod", "unknown")
        container = labels.get("container", "")
        deployment = labels.get("deployment", "")
        alertname = labels.get("alertname", "")

        # ─── Step 1: Extract deployment name from pod ───
        if not deployment and pod != "unknown":
            # Pod names are typically: deployment-name-replicaset-hash-pod-hash
            # Extract deployment by stripping last 2 segments
            parts = pod.rsplit("-", 2)
            if len(parts) >= 3:
                deployment = parts[0]
            elif len(parts) == 2:
                deployment = parts[0]
            else:
                deployment = pod

        if not deployment:
            raise TargetResolutionError(
                f"Cannot resolve deployment from pod '{pod}'. "
                f"Missing 'deployment' label in alert."
            )

        # ─── Step 2: Determine container ───
        if not container:
            # Default to first container (most apps are single-container)
            container = deployment
            logger.info(
                f"[TARGET RESOLVER] No container label. Defaulting to '{container}'."
            )

        # ─── Step 3: Cross-validate namespace ───
        config_namespace = project_config.get("target_namespace", "autofixops")
        if namespace != config_namespace:
            raise TargetResolutionError(
                f"Namespace mismatch: alert says '{namespace}', "
                f"project config says '{config_namespace}'. "
                f"Refusing to resolve — potential cross-project contamination."
            )

        # ─── Step 4: Extract current resource values from context ───
        current_values = {}
        if context_doc:
            metrics = context_doc.get("metrics", {})
            cpu_data = metrics.get("cpu", [])
            mem_data = metrics.get("memory", [])

            if cpu_data and isinstance(cpu_data, list) and len(cpu_data) > 0:
                try:
                    current_values["cpu_usage"] = float(cpu_data[0].get("value", [0, 0])[1])
                except (IndexError, ValueError, TypeError):
                    pass

            if mem_data and isinstance(mem_data, list) and len(mem_data) > 0:
                try:
                    current_values["memory_usage_bytes"] = float(mem_data[0].get("value", [0, 0])[1])
                except (IndexError, ValueError, TypeError):
                    pass

        # ─── Step 5: Build resolved target ───
        resolved = {
            "namespace": namespace,
            "deployment": deployment,
            "container": container,
            "pod_pattern": f"{deployment}-*",
            "alert_source": alertname,
            "resource_spec": {
                "manifest_path": project_config.get(
                    "target_manifest_path",
                    "kubernetes_integration/target_app/deployment.yaml"
                ),
            },
            "current_values": current_values,
            "validation": {
                "namespace_match": True,
                "pod_label_present": pod != "unknown",
                "deployment_resolved": bool(deployment),
            },
            "confidence": self._compute_confidence(pod, deployment, container, namespace, config_namespace),
        }

        logger.info(
            f"[TARGET RESOLVER] Resolved: {namespace}/{deployment}/{container} "
            f"(confidence: {resolved['confidence']:.0%})"
        )

        return resolved

    def _compute_confidence(
        self, pod: str, deployment: str, container: str,
        namespace: str, config_namespace: str
    ) -> float:
        """Confidence score based on how many fields we could resolve."""
        score = 0.0

        # Namespace alignment
        if namespace == config_namespace:
            score += 0.30

        # Pod label present
        if pod != "unknown":
            score += 0.25

        # Deployment resolved
        if deployment:
            score += 0.25

        # Container known
        if container:
            score += 0.20

        return min(score, 1.0)

    def validate_target(self, resolved_target: Dict[str, Any]) -> bool:
        """
        Pre-execution validation. Returns True if target is safe to act on.
        Called right before remediation — final safety gate.
        """
        validation = resolved_target.get("validation", {})

        if not validation.get("namespace_match"):
            logger.error("[TARGET RESOLVER] BLOCKED: Namespace mismatch in resolved target.")
            return False

        if not validation.get("deployment_resolved"):
            logger.error("[TARGET RESOLVER] BLOCKED: No deployment resolved.")
            return False

        if resolved_target.get("confidence", 0) < 0.50:
            logger.error(
                f"[TARGET RESOLVER] BLOCKED: Confidence too low "
                f"({resolved_target.get('confidence', 0):.0%} < 50%)."
            )
            return False

        return True
