import os
import logging
import yaml
from typing import Dict, Any, Optional
from github import Github, GithubException

from .patch_generator import PatchGenerator
from api.database import SessionLocal
from api.models import RemediationAudit, ExecutionStatus

logger = logging.getLogger("autofixops")

# ─── Configuration ───
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # e.g. "vanshitahuja/autofixops-infra"
TARGET_MANIFEST_PATH = os.getenv(
    "TARGET_MANIFEST_PATH",
    "kubernetes_integration/target_app/deployment.yaml"
)

# Shadow Mode: when True, PRs are labeled DO NOT MERGE and audits are tagged
SHADOW_MODE = os.getenv("SHADOW_MODE", "true").lower() == "true"


class RemediationEngine:
    """
    GitOps-based remediation: clones infra repo, patches manifest,
    opens a Pull Request with full evidence trail.
    Never touches the live cluster directly.
    """

    def __init__(self):
        if not GITHUB_TOKEN:
            logger.warning("[REMEDIATION] GITHUB_TOKEN not set. PR creation will be simulated.")
        self.github = Github(GITHUB_TOKEN) if GITHUB_TOKEN else None
        self.patch_generator = PatchGenerator()

    def execute(
        self,
        incident_id: str,
        verdict: Dict[str, Any],
        summary_text: str,
    ) -> Dict[str, Any]:
        """
        Full remediation pipeline:
        1. Load current manifest from GitHub
        2. Generate deterministic patch
        3. Create branch, commit, and open PR
        4. Record audit trail in Postgres

        Returns dict with pr_url, branch_name, and audit_id.
        """
        action = verdict.get("action", {})
        action_type = action.get("type", "UNKNOWN")

        logger.info(
            f"[TRACE:{incident_id}] [REMEDIATION START] "
            f"Action: {action_type}"
        )

        # ─── Step 1: Fetch current manifest ───
        if self.github and GITHUB_REPO:
            result = self._execute_real_pr(incident_id, action, verdict, summary_text)
        else:
            result = self._execute_simulated(incident_id, action, verdict, summary_text)

        # ─── Step 2: Write audit record ───
        db = SessionLocal()
        try:
            audit = RemediationAudit(
                incident_id=incident_id,
                proposed_action=action_type,
                patch_details=result.get("patch_diff"),
                policy_verdict="APPROVED",
                github_pr_url=result.get("pr_url"),
                github_branch=result.get("branch_name"),
                execution_status=ExecutionStatus.PR_CREATED,
                is_shadow_run="true" if SHADOW_MODE else "false",
            )
            db.add(audit)
            db.commit()
            db.refresh(audit)
            result["audit_id"] = str(audit.id)
            result["is_shadow"] = SHADOW_MODE
            logger.info(
                f"[TRACE:{incident_id}] [REMEDIATION AUDIT] "
                f"Recorded audit {audit.id} (shadow={SHADOW_MODE})"
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
        repo = self.github.get_repo(GITHUB_REPO)
        branch_name = f"autofixops/incident-{incident_id[:8]}"
        default_branch = repo.default_branch

        # Get current manifest content
        try:
            file_content = repo.get_contents(TARGET_MANIFEST_PATH, ref=default_branch)
            current_yaml = yaml.safe_load(file_content.decoded_content.decode("utf-8"))
        except GithubException as e:
            logger.error(f"[TRACE:{incident_id}] Failed to fetch manifest: {e}")
            raise

        # Generate the patch
        patched_yaml = self.patch_generator.generate(action, current_yaml)
        patched_content = yaml.dump(patched_yaml, default_flow_style=False)

        # Create branch from default
        source_branch = repo.get_branch(default_branch)
        try:
            repo.create_git_ref(
                ref=f"refs/heads/{branch_name}",
                sha=source_branch.commit.sha,
            )
        except GithubException:
            # Branch may already exist from a previous attempt
            logger.warning(f"[TRACE:{incident_id}] Branch {branch_name} already exists.")

        # Commit the patched file
        repo.update_file(
            path=TARGET_MANIFEST_PATH,
            message=f"fix(autofixops): {action.get('type')} for incident {incident_id[:8]}",
            content=patched_content,
            sha=file_content.sha,
            branch=branch_name,
        )

        # Build structured PR body
        pr_body = self._build_pr_body(incident_id, verdict, summary_text)

        # Shadow mode: prefix title and create as draft
        title_prefix = "[SHADOW] " if SHADOW_MODE else ""
        pr_title = f"{title_prefix}[AutoFixOps] {action.get('type')} — Incident {incident_id[:8]}"

        if SHADOW_MODE:
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
            draft=SHADOW_MODE,
        )

        logger.info(
            f"[TRACE:{incident_id}] [REMEDIATION DONE] PR #{pr.number} created: {pr.html_url}"
        )

        return {
            "pr_url": pr.html_url,
            "pr_number": pr.number,
            "branch_name": branch_name,
            "patch_diff": {"action": action, "file": TARGET_MANIFEST_PATH},
        }

    def _execute_simulated(
        self,
        incident_id: str,
        action: Dict,
        verdict: Dict,
        summary_text: str,
    ) -> Dict[str, Any]:
        """Simulates PR creation when GitHub credentials are not available."""
        # Load local manifest for patch validation
        local_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            TARGET_MANIFEST_PATH,
        )

        try:
            with open(local_path, "r") as f:
                current_yaml = yaml.safe_load(f)
            patched_yaml = self.patch_generator.generate(action, current_yaml)
            logger.info(
                f"[TRACE:{incident_id}] [REMEDIATION SIMULATED] "
                f"Patch validated successfully against local manifest."
            )
        except Exception as e:
            logger.warning(
                f"[TRACE:{incident_id}] [REMEDIATION SIMULATED] "
                f"Could not validate against local manifest: {e}"
            )
            patched_yaml = None

        simulated_url = f"https://github.com/{GITHUB_REPO or 'mock/repo'}/pull/simulated-{incident_id[:8]}"

        return {
            "pr_url": simulated_url,
            "pr_number": 0,
            "branch_name": f"autofixops/incident-{incident_id[:8]}",
            "patch_diff": {"action": action, "file": TARGET_MANIFEST_PATH, "simulated": True},
        }

    def _build_pr_body(
        self, incident_id: str, verdict: Dict, summary_text: str
    ) -> str:
        """Constructs a structured PR description with full evidence trail."""
        action = verdict.get("action", {})
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

---
> ⚠️ This PR was generated automatically by AutoFixOps.
> Please review the manifest changes carefully before merging.
> Post-merge, the system will verify recovery via telemetry re-check.
"""
