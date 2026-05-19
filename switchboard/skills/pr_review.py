"""PRReviewSkill — respond to PR review feedback through Switchboard.

On a pr.reviewed event:
- CHANGES_REQUESTED / COMMENTED: creates a checkpoint jack for operator review,
  then wakes the OpenClaw main session with a structured summary including the
  jack ID and ack command.
- APPROVED: no-op (merge is handled by PRMergeSkill).
- Bot reviewers (name contains 'copilot' or 'bot'): skipped silently.

Requires env:
  OPENCLAW_WEBHOOK_URL   — e.g. http://127.0.0.1:18789/hooks/wake
  OPENCLAW_WEBHOOK_TOKEN — bearer token for the wake endpoint
"""

import logging
import os
import re
import json
import subprocess
import urllib.request
from pathlib import Path

from .base import BaseSkill, Confidence, SkillResult

logger = logging.getLogger(__name__)

# Repo prefix → local directory (extend as needed)
_REPO_DIRS: dict[str, Path] = {
    "butiq": Path.home() / "projects" / "nonsuch" / "butiq",
    "componentlibrary": Path.home() / "projects" / "nonsuch" / "componentLibrary",
    "componentLibrary": Path.home() / "projects" / "nonsuch" / "componentLibrary",
}


def _find_repo_dir(repo: str) -> Path | None:
    """Return the best-guess local directory for a repo name."""
    # Try exact match first, then prefix match from known repos
    key = repo.lower().replace("-", "").replace("_", "")
    for k, v in _REPO_DIRS.items():
        if key == k.lower().replace("-", "").replace("_", ""):
            return v
    # Fallback: ~/projects/<repo>
    candidate = Path.home() / "projects" / repo
    if candidate.exists():
        return candidate
    return None


def _sw_create_checkpoint(title: str, description: str, repo: str) -> str | None:
    """Create a checkpoint jack via `sw create`. Returns the jack ID or None on failure."""
    cwd = _find_repo_dir(repo)
    try:
        result = subprocess.run(
            [
                "sw", "create", title,
                "-t", "checkpoint",
                "--requires", "kale",
                "-d", description,
                "-p", "2",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            logger.warning("sw create checkpoint failed (rc=%d): %s", result.returncode, output)
            return None
        # Extract jack ID — looks like butiq-abc or switchboard-xyz
        match = re.search(r'[a-z][a-z0-9]*-[a-z0-9]+', output)
        if match:
            return match.group(0)
        logger.warning("sw create succeeded but no jack ID found in output: %s", output)
        return None
    except Exception as e:
        logger.warning("sw create checkpoint exception: %s", e)
        return None


def _wake_openclaw(text: str) -> None:
    """Send a wake event to the OpenClaw main session."""
    url = os.environ.get("OPENCLAW_WEBHOOK_URL", "http://127.0.0.1:18789/hooks/wake")
    token = os.environ.get("OPENCLAW_WEBHOOK_TOKEN", "")
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            logger.info("OpenClaw wake sent: %s", resp.status)
    except Exception as e:
        logger.warning("Failed to wake OpenClaw: %s", e)


def _is_bot_reviewer(reviewer: str) -> bool:
    """Return True if the reviewer looks like an automated bot."""
    name = reviewer.lower()
    return "copilot" in name or "bot" in name


class PRReviewSkill(BaseSkill):
    name = "pr-review"
    description = "Handle PR review feedback — create checkpoint jacks, notify Cleo."

    def should_run(self, event: dict) -> bool:
        return event.get("type") == "pr.reviewed"

    def run(self, event: dict, config) -> list[SkillResult]:
        state = event.get("review_state", "").upper()
        pr_title = event.get("title", "")
        pr_url = event.get("pr_url", "")
        repo = event.get("repo", "")
        ref = event.get("ref", "")
        reviewer = event.get("reviewer", "")
        review_body = event.get("review_body", "") or ""

        if state == "APPROVED":
            return []  # PRMergeSkill handles post-merge; nothing to do on approve

        if _is_bot_reviewer(reviewer):
            logger.debug("Skipping bot reviewer: %s", reviewer)
            return []

        if state not in ("CHANGES_REQUESTED", "COMMENTED"):
            return []

        # Truncate PR title for checkpoint title
        short_title = pr_title[:60] + ("…" if len(pr_title) > 60 else "")
        checkpoint_title = f"review: {state} on {repo} PR \"{short_title}\""

        # Truncate review body for description
        body_for_desc = review_body.strip() or "(see inline comments on PR)"

        checkpoint_description = (
            f"Reviewer: {reviewer}\n"
            f"PR: {pr_url}\n"
            f"Branch: {ref}\n"
            f"\n"
            f"Review body:\n"
            f"{body_for_desc}"
        )

        jack_id = _sw_create_checkpoint(checkpoint_title, checkpoint_description, repo)

        # Truncate review body for wake message (max 300 chars)
        feedback_preview = review_body.strip()[:300] + ("…" if len(review_body.strip()) > 300 else "")
        feedback_preview = feedback_preview or "(see inline comments on PR)"

        if jack_id:
            wake_text = (
                f"PR review checkpoint created.\n\n"
                f"PR: \"{pr_title}\" ({pr_url})\n"
                f"Reviewer: {reviewer} ({state})\n"
                f"Feedback: {feedback_preview}\n\n"
                f"Checkpoint jack: {jack_id}\n"
                f"Ack to queue the work: sw checkpoint ack {jack_id} \"approved\"\n"
                f"Or dismiss: sw checkpoint ack {jack_id} \"dismissed — not actioning\""
            )
        else:
            # Fallback: no checkpoint jack was created, still notify
            wake_text = (
                f"PR review ({state}) on {repo} PR \"{pr_title}\" ({pr_url})\n"
                f"Reviewer: {reviewer}\n"
                f"Feedback: {feedback_preview}\n\n"
                f"(Note: checkpoint jack creation failed — handle manually.)"
            )

        _wake_openclaw(wake_text)

        return [
            SkillResult(
                skill=self.name,
                confidence=Confidence.HIGH,
                action=(
                    f"{state.lower()} on {repo}/{ref} — checkpoint {jack_id} created, Cleo notified"
                    if jack_id
                    else f"{state.lower()} on {repo}/{ref} — checkpoint creation failed, Cleo notified"
                ),
                jack_ids=[jack_id] if jack_id else [],
                auto_applied=True,
            )
        ]
