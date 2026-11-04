"""
Unit Tests for CI/CD Pull Request Validation & Commit Hygiene Rules
===================================================================
Tests repository invariants:
  1. Commit title and non-empty body enforcement.
  2. Single-commit squash gate.
"""

import unittest
from unittest.mock import patch

from scripts.validate_pr_commits import (
    validate_commit_content,
    validate_pull_request,
)


class TestCICommitValidation(unittest.TestCase):

    def test_valid_commit_with_title_and_body_passes(self):
        """A commit with a title and non-empty body must pass validation."""
        sha = "a1b2c3d4e5f6"
        subject = "feat(ci): implement squash and commit message quality gates"
        body = (
            "Enforce that all pull requests have an explanatory commit body\n"
            "and are squashed into a single commit prior to merging."
        )
        valid, msg = validate_commit_content(sha, subject, body)
        self.assertTrue(valid)
        self.assertEqual(msg, "OK")

    def test_commit_with_empty_title_fails(self):
        """A commit with an empty title must be rejected."""
        sha = "a1b2c3d4e5f6"
        subject = ""
        body = "Some body content here."
        valid, msg = validate_commit_content(sha, subject, body)
        self.assertFalse(valid)
        self.assertIn("empty title", msg)

    def test_commit_with_empty_body_fails(self):
        """A commit with an empty body must be rejected."""
        sha = "a1b2c3d4e5f6"
        subject = "fix: update dependency"
        body = ""
        valid, msg = validate_commit_content(sha, subject, body)
        self.assertFalse(valid)
        self.assertIn("EMPTY BODY", msg)

    def test_commit_with_whitespace_only_body_fails(self):
        """A commit with only whitespace or newlines in the body must be rejected."""
        sha = "a1b2c3d4e5f6"
        subject = "fix: minor tweak"
        body = "   \n\n\t  \n  "
        valid, msg = validate_commit_content(sha, subject, body)
        self.assertFalse(valid)
        self.assertIn("EMPTY BODY", msg)

    def test_commit_with_only_git_comments_in_body_fails(self):
        """A commit with only git comments (# lines) in the body must be rejected."""
        sha = "a1b2c3d4e5f6"
        subject = "chore: cleanup"
        body = "# Please enter the commit message for your changes.\n# Lines starting with '#' will be ignored."
        valid, msg = validate_commit_content(sha, subject, body)
        self.assertFalse(valid)
        self.assertIn("EMPTY BODY", msg)

    @patch("scripts.validate_pr_commits.get_commit_messages")
    def test_pr_with_single_valid_commit_passes(self, mock_commits):
        """A pull request with exactly 1 valid commit passes."""
        mock_commits.return_value = [
            ("sha12345", "feat: title", "This is a comprehensive body describing the feature.")
        ]
        result = validate_pull_request("origin/main", "HEAD", max_commits=1)
        self.assertTrue(result)

    @patch("scripts.validate_pr_commits.get_commit_messages")
    def test_pr_with_multiple_commits_rejected_by_squash_rule(self, mock_commits):
        """A pull request with multiple commits must be rejected by the squash rule."""
        mock_commits.return_value = [
            ("sha1", "feat: first commit", "Body for first commit."),
            ("sha2", "fix: second commit", "Body for second commit."),
            ("sha3", "docs: third commit", "Body for third commit."),
        ]
        result = validate_pull_request("origin/main", "HEAD", max_commits=1)
        self.assertFalse(result)

    @patch("scripts.validate_pr_commits.get_commit_messages")
    def test_pr_with_single_commit_missing_body_rejected(self, mock_commits):
        """A pull request with 1 commit but an empty body must be rejected."""
        mock_commits.return_value = [
            ("sha12345", "feat: title only", "")
        ]
        result = validate_pull_request("origin/main", "HEAD", max_commits=1)
        self.assertFalse(result)

    @patch("scripts.validate_pr_commits.get_commit_messages")
    def test_pr_with_zero_commits_passes(self, mock_commits):
        """A pull request range with 0 commits (e.g. identical refs) passes without error."""
        mock_commits.return_value = []
        result = validate_pull_request("origin/main", "HEAD", max_commits=1)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
