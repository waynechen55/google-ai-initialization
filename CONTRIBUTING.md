# Contributing to Google AI Workspace & MCP Initializer

Thank you for your interest in contributing! This document details the architectural standards, Git workflows, and CI/CD quality gates enforced on all pull requests.

---

## 📋 Table of Contents
1. [Development Environment Setup](#development-environment-setup)
2. [Pull Request (PR) Quality Gates & Invariants](#pull-request-pr-quality-gates--invariants)
3. [Commit Message Standards (Enforced by CI)](#commit-message-standards-enforced-by-ci)
4. [Single-Commit Squash Rule (Enforced by CI)](#single-commit-squash-rule-enforced-by-ci)
5. [Local Testing & Verification](#local-testing--verification)
6. [Code Style & Conventions](#code-style--conventions)

---

## 🛠️ Development Environment Setup

1. Fork and clone the repository:
   ```bash
   git clone https://github.com/waynechen/google-ai-initialization.git
   cd google-ai-initialization
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements-dev.txt
   pip install -e .
   ```

3. Ensure Playwright browser binaries are available:
   ```bash
   python3 -m playwright install chromium
   ```

---

## 🛡️ Pull Request (PR) Quality Gates & Invariants

All contributions must pass automated GitHub Actions CI checks (`.github/workflows/ci.yml`). Pull requests that violate these invariants will be automatically rejected by CI:

### 1. 📝 Mandatory Commit Title and Body
* **Rule**: Every commit in a pull request **MUST** contain both a title (first line) and an explanatory body.
* **Empty bodies are strictly forbidden** and will trigger a CI/CD failure.
* **Example of a Valid Commit Message**:
  ```text
  feat(discovery): add native streamable http retry backoff

  Implement exponential backoff with jitter for remote HTTP MCP discovery
  endpoints when encountering transient 503 or network timeout errors.
  This prevents intermittent network drops from cascading to lower tiers.
  ```
* **Example of an Invalid Commit Message (CI Rejection)**:
  ```text
  fix: updated stuff
  (empty body - REJECTED BY CI)
  ```

### 2. 🔀 Single-Commit / Squash Enforcement
* **Rule**: All commits in a single pull request **MUST** be squashed into **exactly 1 commit**.
* Pull requests containing multiple intermediate commits (`WIP`, `fix typo`, `update test`) will be rejected by CI.
* **How to Squash Before Submitting**:
  ```bash
  # Squash all commits on your branch against the base branch (e.g. main)
  git rebase -i origin/main
  # Mark all commits after the first as 'squash' (or 's')
  # Save and force-push to your branch:
  git push --force-with-lease origin feature/your-branch-name
  ```

### 3. 🧪 Mandatory Test Execution
* All 128+ automated unit and integration tests must pass cleanly.
* Any new feature or bug fix must include automated test coverage in `tests/`.

---

## 🧪 Local Testing & Verification

Before submitting your pull request, run the complete verification suite locally:

```bash
# 1. Run the complete automated test suite
python3 run_tests.py

# 2. Run Python bytecode syntax validation
python3 -c "
import glob, os
for f in glob.glob('*.py') + glob.glob('bridges/*.py') + glob.glob('tests/*.py'):
    assert os.system(f'python3 -m py_compile {f}') == 0, f'Compilation failed for {f}'
print('All Python source files compiled successfully!')
"

# 3. Validate your PR commit locally with the CI validator script
python3 scripts/validate_pr_commits.py --base origin/main
```

---

## 🎨 Code Style & Conventions

- **Clean Settings Invariant**: Never add prompt instructions or arbitrary metadata to `.mcp.json` or Gemini CLI settings files.
- **Enterprise TLS Awareness**: Always use `resolve_gcloud_bin()` and respect `/etc/ssl/ca-bundle.pem` trust stores.
- **Zero Plain-Text Secrets**: Never commit real credentials or print API keys/tokens to console outputs.
