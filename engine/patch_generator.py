import copy
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("autofixops")


# ─── Patch Templates ───
# Each action type maps to a deterministic, controlled mutation.
# No AI-generated YAML. No freeform text. Just field-level patches.


class PatchGenerator:
    """
    Generates structured YAML-compatible patch dicts for Kubernetes manifests.
    Every patch is deterministic and testable — never LLM-generated.
    """

    def generate(self, action: Dict[str, Any], manifest: Dict[str, Any]) -> Dict[str, Any]:
        """
        Takes an action dict and the current manifest, returns the patched manifest.
        
        Args:
            action: {"type": "INCREASE_MEMORY_LIMIT", "patch_value": "256Mi", "target": "..."}
            manifest: Parsed YAML dict of the target deployment
            
        Returns:
            Patched manifest dict ready for serialization.
        """
        action_type = action.get("type")
        
        dispatch = {
            "INCREASE_MEMORY_LIMIT": self._patch_memory_limit,
            "INCREASE_CPU_LIMIT": self._patch_cpu_limit,
            "RESTART_POD": self._patch_restart_annotation,
            "ROLLBACK_DEPLOYMENT": self._patch_rollback,
        }

        handler = dispatch.get(action_type)
        if not handler:
            raise ValueError(f"No patch template for action type: {action_type}")

        # Deep copy to avoid mutating the original
        patched = copy.deepcopy(manifest)
        handler(patched, action)

        logger.info(f"[PATCH] Generated patch for action: {action_type}")
        return patched

    def _patch_memory_limit(self, manifest: Dict, action: Dict):
        """Patches spec.containers[0].resources.limits.memory"""
        new_value = action.get("patch_value", "256Mi")
        containers = (
            manifest.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        if not containers:
            raise ValueError("No containers found in manifest to patch.")

        for container in containers:
            resources = container.setdefault("resources", {})
            limits = resources.setdefault("limits", {})
            old_value = limits.get("memory", "unset")
            limits["memory"] = new_value
            logger.info(
                f"[PATCH] Container '{container.get('name')}': "
                f"memory limit {old_value} → {new_value}"
            )

    def _patch_cpu_limit(self, manifest: Dict, action: Dict):
        """Patches spec.containers[0].resources.limits.cpu"""
        new_value = action.get("patch_value", "1000m")
        containers = (
            manifest.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        if not containers:
            raise ValueError("No containers found in manifest to patch.")

        for container in containers:
            resources = container.setdefault("resources", {})
            limits = resources.setdefault("limits", {})
            old_value = limits.get("cpu", "unset")
            limits["cpu"] = new_value
            logger.info(
                f"[PATCH] Container '{container.get('name')}': "
                f"cpu limit {old_value} → {new_value}"
            )

    def _patch_restart_annotation(self, manifest: Dict, action: Dict):
        """Adds/updates kubectl.kubernetes.io/restartedAt annotation to force rollout."""
        import datetime

        template_metadata = (
            manifest.get("spec", {})
            .get("template", {})
            .setdefault("metadata", {})
        )
        annotations = template_metadata.setdefault("annotations", {})
        restart_time = datetime.datetime.utcnow().isoformat() + "Z"
        annotations["kubectl.kubernetes.io/restartedAt"] = restart_time
        logger.info(f"[PATCH] Added restart annotation: {restart_time}")

    def _patch_rollback(self, manifest: Dict, action: Dict):
        """
        Rollback strategy: revert the image tag to a known-good version.
        In production this would query the deployment history.
        For now we append ':previous' as a marker.
        """
        containers = (
            manifest.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        if not containers:
            raise ValueError("No containers found in manifest to patch.")

        for container in containers:
            current_image = container.get("image", "unknown:latest")
            # Strip current tag and apply rollback marker
            base_image = current_image.rsplit(":", 1)[0]
            container["image"] = f"{base_image}:rollback"
            logger.info(
                f"[PATCH] Container '{container.get('name')}': "
                f"image {current_image} → {container['image']}"
            )
