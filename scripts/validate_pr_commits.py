#!/usr/bin/env python3
"""
PR Commit Validator & Squash Enforcement Utility
=================================================
Validates repository invariants for GitHub Pull Requests:
  1. Title and Non-Empty Body Enforcement:
     Every commit MUST have a non-empty subject title and a non-empty explanatory body.
  2. Single Squashed Commit Enforcement:
     Every PR must contain exactly 1 commit against the target base branch.
"""

import argparse
import subprocess
import sys
from typing import List, Tuple


def get_commit_messages(base_ref: str, head_ref: str = "HEAD") -> List[Tuple[str, str, str]]:
    """
    Retrieves commit SHAs, subjects (titles), and bodies between base_ref and head_ref.
    Returns list of (sha, subject, body).
    """
    cmd = ["git", "log", f"{base_ref}..{head_ref}", "--format=%H%x1f%s%x1f%b%x1e"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as err:
        print(f"❌ [GIT ERROR] Failed to inspect git log between '{base_ref}' and '{head_ref}': {err.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    raw = res.stdout.strip()
    if not raw:
        return []

    commits = []
    for entry in raw.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("\x1f")
        sha = parts[0].strip() if len(parts) > 0 else ""
        subject = parts[1].strip() if len(parts) > 1 else ""
        body = parts[2].strip() if len(parts) > 2 else ""
        commits.append((sha, subject, body))

    return commits


def validate_commit_content(sha: str, subject: str, body: str) -> Tuple[bool, str]:
    """
    Validates that a commit has both a non-empty title and a non-empty body.
    """
    if not subject:
        return False, f"Commit {sha[:8]} has an empty title (subject line)."

    # Remove comment lines or blank lines
    clean_body = "\n".join(
        line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith("#")
    ).strip()

    if not clean_body:
        return (
            False,
            f"Commit {sha[:8]} ('{subject}') has an EMPTY BODY!\n"
            f"  Every commit in a pull request must provide an explanatory body describing\n"
            f"  the technical rationale, architecture, and verification strategy."
        )

    return True, "OK"


def validate_pull_request(base_ref: str, head_ref: str = "HEAD", max_commits: int = 1) -> bool:
    """
    Performs full PR validation: squash rule + commit message title & body check.
    """
    print("=" * 70)
    print("🔍 [CI QUALITY GATE] Validating Pull Request Commits & Message Rules")
    print("=" * 70)
    print(f"• Base Reference : {base_ref}")
    print(f"• Head Reference : {head_ref}")
    print(f"• Expected Count : Exactly {max_commits} commit(s)")
    print("-" * 70)

    commits = get_commit_messages(base_ref, head_ref)
    commit_count = len(commits)

    if commit_count == 0:
        print(f"⚠️ [WARNING] No commits detected between '{base_ref}' and '{head_ref}'. Nothing to validate.")
        return True

    # Rule 1: Single Squashed Commit Enforcement
    if commit_count > max_commits:
        print(
            f"❌ [CI ERROR] Pull request contains {commit_count} commits (Maximum allowed: {max_commits})!\n\n"
            f"In accordance with repository standards, all commits in a single pull request\n"
            f"MUST be squashed or merged into exactly ONE clean commit before merging.\n\n"
            f"To squash your commits locally, run:\n"
            f"  git fetch origin\n"
            f"  git rebase -i {base_ref}\n"
            f"  # Mark commits 2..N as 'squash' (or 's'), provide a full title & body, then:\n"
            f"  git push --force-with-lease\n"
        )
        return False

    print(f"✅ [SQUASH CHECK PASSED] Single squashed commit confirmed ({commit_count}/{max_commits}).")

    # Rule 2: Non-Empty Title and Body Enforcement
    has_errors = False
    for sha, subject, body in commits:
        valid, msg = validate_commit_content(sha, subject, body)
        if not valid:
            print(f"❌ [COMMIT FORMAT ERROR]\n{msg}\n")
            has_errors = True
        else:
            print(f"✅ [COMMIT MESSAGE PASSED] {sha[:8]}: {subject}")
            print(f"   Body snippet: {body.splitlines()[0][:60]}...")

    if has_errors:
        print("=" * 70)
        print("❌ [CI FAILED] One or more commits violated commit message quality standards.")
        print("=" * 70)
        return False

    print("=" * 70)
    print("🎉 [CI PASSED] All pull request commits conform to repository quality gates!")
    print("=" * 70)
    return True


def main():
    parser = argparse.ArgumentParser(description="Validate PR commit count and message format.")
    parser.add_argument("--base", default="HEAD~1", help="Base git reference (default: HEAD~1)")
    parser.add_argument("--head", default="HEAD", help="Head git reference (default: HEAD)")
    parser.add_argument("--max-commits", type=int, default=1, help="Max commits allowed (default: 1)")

    args = parser.parse_args()
    success = validate_pull_request(args.base, args.head, args.max_commits)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
