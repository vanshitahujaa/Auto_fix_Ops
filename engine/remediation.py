"""
Remediation Engine for AutoFixOps
====================================
GitOps-based remediation: patches manifest, opens PR with evidence trail.
Never touches the live cluster directly.

Now uses:
  - Project config (not env vars)
  - Patch generator with safety bounds
  - Remediation dedup (no duplicate PRs)
  - Previous value tracking for rollback
"""

import os
import logging
import yaml
from typing import Dict, Any, Optional
from github import Github, GithubException

from .patch_generator import PatchGenerator
from api.database import SessionLocal
from api.models import RemediationAudit, ExecutionStatus

logger = logging.getLogger("autofixops")


class RemediationEngine:
    """
    GitOps-based remediation: clones infra repo, patches manifest,
    opens a Pull Request with full evidence trail.
    """

    def __init__(self, project_config: Dict[str, Any] = None):
        self.config = project_config or {}

        # Use project config first, fall back to env vars
        github_token = self.config.get("github_token") or os.getenv("GITHUB_TOKEN", "")
        self.github_repo = self.config.get("github_repo") or os.getenv("GITHUB_REPO", "")
        self.manifest_path = (
            self.config.get("target_manifest_path")
            or os.getenv("TARGET_MANIFEST_PATH", "kubernetes_integration/target_app/deployment.yaml")
        )
        self.shadow_mode = (
            self.config.get("shadow_mode", "true") == "true"
            if self.config.get("shadow_mode") is not None
            else os.getenv("SHADOW_MODE", "true").lower() == "true"
        )
        self.max_scale_factor = self.config.get("max_resource_scale_factor", 2.0)

        if not github_token:
            logger.warning("[REMEDIATION] No GitHub token. PR creation will be simulated.")
        self.github = Github(github_token) if github_token else None
        self.patch_generator = PatchGenerator(max_scale_factor=self.max_scale_factor)

    def execute(
        self,
        incident_id: str,
        verdict: Dict[str, Any],
        summary_text: str,
    ) -> Dict[str, Any]:
        """
        Full remediation pipeline:
        1. Check for duplicate remediations
        2. Load current manifest from GitHub
        3. Generate bounded patch (with previous values)
        4. Create branch, commit, and open PR
        5. Record audit trail with previous values

        Returns dict with pr_url, branch_name, audit_id, and previous_values.
        """
        action = verdict.get("action", {})
        action_type = action.get("type", "UNKNOWN")

        logger.info(
            f"[TRACE:{incident_id}] [REMEDIATION START] "
            f"Action: {action_type}"
        )

        # ─── Dedup check ───
        db = SessionLocal()
        try:
            existing = (
                db.query(RemediationAudit)
                .filter(
                    RemediationAudit.incident_id == incident_id,
                    RemediationAudit.execution_status.notin_([
                        ExecutionStatus.VERIFICATION_FAILED,
                        ExecutionStatus.PR_REJECTED,
                    ])
                )
                .first()
            )
            if existing:
                logger.warning(
                    f"[TRACE:{incident_id}] [REMEDIATION DEDUP] "
                    f"Active remediation already exists (audit: {existing.id}). Skipping."
                )
                return {
                    "pr_url": existing.github_pr_url,
                    "branch_name": existing.github_branch,
                    "audit_id": str(existing.id),
                    "deduplicated": True,
                }
        finally:
            db.close()

        # ─── Execute PR creation ───
        if self.github and self.github_repo:
            result = self._execute_real_pr(incident_id, action, verdict, summary_text)
        else:
            result = self._execute_simulated(incident_id, action, verdict, summary_text)

        # ─── Write audit record with previous values ───
        db = SessionLocal()
        try:
            # Get project_id from incident
            from api.models import Incident
            incident = db.query(Incident).filter(Incident.id == incident_id).first()
            project_id = incident.project_id if incident else None

            audit = RemediationAudit(
                incident_id=incident_id,
                project_id=project_id,
                proposed_action=action_type,
                patch_details=result.get("patch_diff"),
                previous_values=result.get("previous_values"),
                policy_verdict="APPROVED",
                github_pr_url=result.get("pr_url"),
                github_branch=result.get("branch_name"),
                execution_status=ExecutionStatus.PR_CREATED,
                is_shadow_run="true" if self.shadow_mode else "false",
            )
            db.add(audit)
            db.commit()
            db.refresh(audit)
            result["audit_id"] = str(audit.id)
            result["is_shadow"] = self.shadow_mode
            logger.info(
                f"[TRACE:{incident_id}] [REMEDIATION AUDIT] "
                f"Recorded audit {audit.id} (shadow={self.shadow_mode})"
            )
        finally:
            db.close()

        return result

    def _execute_real_pr(
        self,
        incident_id: str,
        action: Dict,
        verdict: Dict,
        summary_text: str,
    ) -> Dict[str, Any]:
        """Creates a real GitHub PR with the patched manifest."""
        repo = self.github.get_repo(self.github_repo)
        branch_name = f"autofixops/incident-{incident_id[:8]}"
        default_branch = repo.default_branch

        # Get current manifest content
        try:
            file_content = repo.get_contents(self.manifest_path, ref=default_branch)
            current_yaml = yaml.safe_load(file_content.decoded_content.decode("utf-8"))
        except GithubException as e:
            logger.error(f"[TRACE:{incident_id}] Failed to fetch manifest: {e}")
            raise

        # Generate the bounded patch (returns tuple now)
        patched_yaml, previous_values = self.patch_generator.generate(action, current_yaml)
        patched_content = yaml.dump(patched_yaml, default_flow_style=False)

        # Create branch from default
        source_branch = repo.get_branch(default_branch)
        try:
            repo.create_git_ref(
                ref=f"refs/heads/{branch_name}",
                sha=source_branch.commit.sha,
            )
        except GithubException:
            logger.warning(f"[TRACE:{incident_id}] Branch {branch_name} already exists.")

        # Commit the patched file
        repo.update_file(
            path=self.manifest_path,
            message=f"fix(autofixops): {action.get('type')} for incident {incident_id[:8]}",
            content=patched_content,
            sha=file_content.sha,
            branch=branch_name,
        )

        # Build structured PR body
        pr_body = self._build_pr_body(incident_id, verdict, summary_text, previous_values)

        # Shadow mode: prefix title and create as draft
        title_prefix = "[SHADOW] " if self.shadow_mode else ""
        pr_title = f"{title_prefix}[AutoFixOps] {action.get('type')} — Incident {incident_id[:8]}"

        if self.shadow_mode:
            pr_body = (
                "## ⚠️ SHADOW MODE — DO NOT MERGE\n"
                "This PR was generated in shadow mode for evaluation purposes only.\n"
                "It should be reviewed but **NOT merged**.\n\n---\n\n"
                + pr_body
            )

        # Open the PR (as draft if shadow mode)
        pr = repo.create_pull(
            title=pr_title,
            body=pr_body,
            head=branch_name,
            base=default_branch,
            draft=self.shadow_mode,
        )

        logger.info(
            f"[TRACE:{incident_id}] [REMEDIATION DONE] PR #{pr.number} created: {pr.html_url}"
        )

        return {
            "pr_url": pr.html_url,
            "pr_number": pr.number,
            "branch_name": branch_name,
            "patch_diff": {"action": action, "file": self.manifest_path},
            "previous_values": previous_values,
        }

    def execute_rollback(self, incident_id: str, previous_values: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a rollback PR reverting the manifest to the stored previous_values."""
        if not self.github:
            logger.warning(f"[TRACE:{incident_id}] [ROLLBACK] No GitHub token. Simulating rollback.")
            return {"pr_url": f"https://github.com/mock/pull/rollback-{incident_id[:8]}"}

        repo = self.github.get_repo(self.github_repo)
        branch_name = f"autofixops/rollback-{incident_id[:8]}"
        default_branch = repo.default_branch

        try:
            file_content = repo.get_contents(self.manifest_path, ref=default_branch)
            current_yaml = yaml.safe_load(file_content.decoded_content.decode("utf-8"))
        except GithubException as e:
            logger.error(f"[TRACE:{incident_id}] Failed to fetch manifest for rollback: {e}")
            raise

        reverted_yaml = self.patch_generator.apply_rollback(current_yaml, previous_values)
        reverted_content = yaml.dump(reverted_yaml, default_flow_style=False)

        source_branch = repo.get_branch(default_branch)
        try:
            repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=source_branch.commit.sha)
        except GithubException:
            logger.warning(f"[TRACE:{incident_id}] Rollback branch {branch_name} already exists.")

        repo.update_file(
            path=self.manifest_path,
            message=f"revert(autofixops): rollback for incident {incident_id[:8]}",
            content=reverted_content,
            sha=file_content.sha,
            branch=branch_name,
        )

        pr_title = f"[ROLLBACK] AutoFixOps — Incident {incident_id[:8]}"
        pr_body = (
            f"## ⏪ AutoFixOps Rollback\n\n"
            f"Reverting manifest changes made during Incident `{incident_id}`.\n\n"
            f"### Restored Values:\n"
            + "".join(f"- `{k}`: `{v}`\n" for k, v in previous_values.items())
        )

        pr = repo.create_pull(
            title=pr_title,
            body=pr_body,
            head=branch_name,
            base=default_branch,
            draft=False,  # Rollbacks shouldn't be drafts
        )

        logger.info(f"[TRACE:{incident_id}] [ROLLBACK DONE] PR #{pr.number} created: {pr.html_url}")
        return {"pr_url": pr.html_url, "pr_number": pr.number, "branch_name": branch_name}

    def _execute_simulated(
        self,
        incident_id: str,
        action: Dict,
        verdict: Dict,
        summary_text: str,
    ) -> Dict[str, Any]:
        """Simulates PR creation when GitHub credentials are not available."""
        local_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            self.manifest_path,
        )

        previous_values = {}
        try:
            with open(local_path, "r") as f:
                current_yaml = yaml.safe_load(f)
            patched_yaml, previous_values = self.patch_generator.generate(action, current_yaml)
            logger.info(
                f"[TRACE:{incident_id}] [REMEDIATION SIMULATED] "
                f"Patch validated successfully against local manifest."
            )
        except Exception as e:
            logger.warning(
                f"[TRACE:{incident_id}] [REMEDIATION SIMULATED] "
                f"Could not validate against local manifest: {e}"
            )

        simulated_url = f"https://github.com/{self.github_repo or 'mock/repo'}/pull/simulated-{incident_id[:8]}"

        return {
            "pr_url": simulated_url,
            "pr_number": 0,
            "branch_name": f"autofixops/incident-{incident_id[:8]}",
            "patch_diff": {"action": action, "file": self.manifest_path, "simulated": True},
            "previous_values": previous_values,
        }

    def _build_pr_body(
        self, incident_id: str, verdict: Dict, summary_text: str,
        previous_values: Dict = None,
    ) -> str:
        """Constructs a structured PR description with full evidence trail."""
        action = verdict.get("action", {})
        rollback_section = ""
        if previous_values:
            rollback_section = "\n### Rollback Values\n"
            for k, v in previous_values.items():
                rollback_section += f"- `{k}`: `{v}`\n"
            rollback_section += "\n> To rollback, use `POST /api/v1/incidents/{incident_id}/rollback`\n"

        return f"""## 🤖 AutoFixOps Automated Remediation

### Incident
- **ID:** `{incident_id}`
- **Classification:** `{verdict.get('root_cause_classification', 'N/A')}`
- **Confidence:** `{verdict.get('confidence', 0):.0%}`
- **Diagnosed By:** `{verdict.get('diagnosed_by', 'UNKNOWN')}`

### Evidence Summary
{summary_text}

### Reasoning
{verdict.get('reasoning', 'No reasoning provided.')}

### Applied Action
- **Type:** `{action.get('type')}`
- **Target:** `{action.get('target', 'N/A')}`
- **Patch Value:** `{action.get('patch_value', 'N/A')}`
{rollback_section}
---
> ⚠️ This PR was generated automatically by AutoFixOps.
> Please review the manifest changes carefully before merging.
> Post-merge, the system will verify recovery via telemetry re-check.
"""
