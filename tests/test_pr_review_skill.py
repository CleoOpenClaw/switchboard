"""Tests for PRReviewSkill — checkpoint jack creation and bot-reviewer skip."""

import subprocess
from unittest import mock

import pytest

from switchboard.skills.pr_review import (
    PRReviewSkill,
    _is_bot_reviewer,
    _sw_create_checkpoint,
    _wake_openclaw,
)
from switchboard.skills.base import Confidence


# ── _is_bot_reviewer ──────────────────────────────────────────────────────────

def test_is_bot_reviewer_copilot():
    assert _is_bot_reviewer("copilot-pull-request-reviewer") is True


def test_is_bot_reviewer_bot_suffix():
    assert _is_bot_reviewer("dependabot") is True


def test_is_bot_reviewer_human():
    assert _is_bot_reviewer("kale") is False


def test_is_bot_reviewer_mixed_case():
    assert _is_bot_reviewer("CoPilotReviewer") is True


# ── _sw_create_checkpoint ─────────────────────────────────────────────────────

def _mock_sw_result(returncode=0, stdout="Created jack butiq-abc123\n", stderr=""):
    result = mock.Mock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_sw_create_checkpoint_returns_jack_id():
    with mock.patch("subprocess.run", return_value=_mock_sw_result()) as mock_run:
        jack_id = _sw_create_checkpoint("review: CHANGES_REQUESTED on butiq PR \"Fix thing\"",
                                        "Reviewer: kale\nPR: http://example.com\nBranch: fix/thing\n\nReview body:\nPlease fix.",
                                        "butiq")
    assert jack_id == "butiq-abc123"
    call_args = mock_run.call_args[0][0]
    assert "sw" in call_args
    assert "create" in call_args
    assert "-t" in call_args
    assert "checkpoint" in call_args
    assert "--requires" in call_args
    assert "kale" in call_args


def test_sw_create_checkpoint_failure_returns_none():
    with mock.patch("subprocess.run", return_value=_mock_sw_result(returncode=1, stdout="", stderr="error")):
        jack_id = _sw_create_checkpoint("title", "desc", "butiq")
    assert jack_id is None


def test_sw_create_checkpoint_no_id_in_output_returns_none():
    with mock.patch("subprocess.run", return_value=_mock_sw_result(stdout="Success but no id here!")):
        jack_id = _sw_create_checkpoint("title", "desc", "butiq")
    assert jack_id is None


def test_sw_create_checkpoint_exception_returns_none():
    with mock.patch("subprocess.run", side_effect=FileNotFoundError("sw not found")):
        jack_id = _sw_create_checkpoint("title", "desc", "butiq")
    assert jack_id is None


# ── PRReviewSkill.should_run ──────────────────────────────────────────────────

def test_should_run_on_pr_reviewed():
    skill = PRReviewSkill()
    assert skill.should_run({"type": "pr.reviewed"}) is True


def test_should_run_ignores_other_events():
    skill = PRReviewSkill()
    assert skill.should_run({"type": "pr.merged"}) is False
    assert skill.should_run({"type": "push"}) is False


# ── PRReviewSkill.run — APPROVED ──────────────────────────────────────────────

def test_approved_is_noop():
    skill = PRReviewSkill()
    results = skill.run({"type": "pr.reviewed", "review_state": "APPROVED"}, config=None)
    assert results == []


# ── PRReviewSkill.run — bot reviewer ─────────────────────────────────────────

def test_bot_reviewer_skipped():
    skill = PRReviewSkill()
    event = {
        "type": "pr.reviewed",
        "review_state": "CHANGES_REQUESTED",
        "reviewer": "copilot-pull-request-reviewer",
        "repo": "butiq",
        "ref": "fix/thing",
        "title": "Fix thing",
        "pr_url": "https://github.com/example/butiq/pull/42",
        "review_body": "Please fix the indentation.",
    }
    with mock.patch("switchboard.skills.pr_review._sw_create_checkpoint") as mock_create, \
         mock.patch("switchboard.skills.pr_review._wake_openclaw") as mock_wake:
        results = skill.run(event, config=None)

    assert results == []
    mock_create.assert_not_called()
    mock_wake.assert_not_called()


# ── PRReviewSkill.run — CHANGES_REQUESTED ────────────────────────────────────

def _make_review_event(state="CHANGES_REQUESTED", reviewer="kale",
                        body="Please rename this variable.", repo="butiq"):
    return {
        "type": "pr.reviewed",
        "review_state": state,
        "reviewer": reviewer,
        "repo": repo,
        "ref": "fix/widget",
        "title": "Fix the widget parser",
        "pr_url": "https://github.com/example/butiq/pull/7",
        "review_body": body,
    }


def test_changes_requested_creates_checkpoint_and_wakes():
    skill = PRReviewSkill()
    event = _make_review_event()

    with mock.patch("switchboard.skills.pr_review._sw_create_checkpoint",
                    return_value="butiq-cp001") as mock_create, \
         mock.patch("switchboard.skills.pr_review._wake_openclaw") as mock_wake:
        results = skill.run(event, config=None)

    assert len(results) == 1
    r = results[0]
    assert r.skill == "pr-review"
    assert "butiq-cp001" in r.jack_ids
    assert r.auto_applied is True

    mock_create.assert_called_once()
    title_arg = mock_create.call_args[0][0]
    assert "CHANGES_REQUESTED" in title_arg
    assert "butiq" in title_arg

    mock_wake.assert_called_once()
    wake_text = mock_wake.call_args[0][0]
    assert "butiq-cp001" in wake_text
    assert "sw checkpoint ack butiq-cp001" in wake_text
    assert "Please rename this variable." in wake_text


def test_commented_also_creates_checkpoint():
    skill = PRReviewSkill()
    event = _make_review_event(state="COMMENTED")

    with mock.patch("switchboard.skills.pr_review._sw_create_checkpoint",
                    return_value="butiq-cp002"), \
         mock.patch("switchboard.skills.pr_review._wake_openclaw") as mock_wake:
        results = skill.run(event, config=None)

    assert len(results) == 1
    wake_text = mock_wake.call_args[0][0]
    assert "butiq-cp002" in wake_text


def test_checkpoint_creation_failure_still_wakes():
    """Even if sw create fails, Cleo should still be notified."""
    skill = PRReviewSkill()
    event = _make_review_event()

    with mock.patch("switchboard.skills.pr_review._sw_create_checkpoint",
                    return_value=None), \
         mock.patch("switchboard.skills.pr_review._wake_openclaw") as mock_wake:
        results = skill.run(event, config=None)

    assert len(results) == 1
    assert results[0].jack_ids == []
    mock_wake.assert_called_once()
    wake_text = mock_wake.call_args[0][0]
    assert "failed" in wake_text.lower()


def test_long_pr_title_truncated_in_checkpoint_title():
    skill = PRReviewSkill()
    long_title = "A" * 80
    event = _make_review_event()
    event["title"] = long_title

    with mock.patch("switchboard.skills.pr_review._sw_create_checkpoint",
                    return_value="butiq-cp003") as mock_create, \
         mock.patch("switchboard.skills.pr_review._wake_openclaw"):
        skill.run(event, config=None)

    title_arg = mock_create.call_args[0][0]
    # The PR title portion in the checkpoint title should be truncated (≤60 chars of original + ellipsis)
    assert "A" * 61 not in title_arg


def test_review_body_truncated_in_wake_text():
    skill = PRReviewSkill()
    long_body = "X" * 500
    event = _make_review_event(body=long_body)

    with mock.patch("switchboard.skills.pr_review._sw_create_checkpoint",
                    return_value="butiq-cp004"), \
         mock.patch("switchboard.skills.pr_review._wake_openclaw") as mock_wake:
        skill.run(event, config=None)

    wake_text = mock_wake.call_args[0][0]
    # 300 chars of body + ellipsis — should not contain 400 Xs in a row
    assert "X" * 400 not in wake_text


def test_empty_review_body_uses_placeholder():
    skill = PRReviewSkill()
    event = _make_review_event(body="")

    with mock.patch("switchboard.skills.pr_review._sw_create_checkpoint",
                    return_value="butiq-cp005"), \
         mock.patch("switchboard.skills.pr_review._wake_openclaw") as mock_wake:
        skill.run(event, config=None)

    wake_text = mock_wake.call_args[0][0]
    assert "inline comments" in wake_text
