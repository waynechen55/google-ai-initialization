## 📋 Description
<!-- Provide a detailed summary of the changes and the architectural rationale. -->

## 🔗 Related Issues
<!-- Closes #123 -->

## 🛡️ Pull Request Quality Gates (Enforced by CI)
Please verify that your pull request complies with all repository invariants:

- [ ] **Commit Title & Non-Empty Body**: All commits contain both a descriptive title and a non-empty body explaining *why* the change was made (CI will fail if body is empty).
- [ ] **Single Squashed Commit**: All branch commits have been rebased and squashed into **exactly 1 commit** (`git rebase -i origin/main`).
- [ ] **Zero Hardcoded Secrets**: Verified that no API keys, PATs, tokens, or credentials were added to code, tests, or documentation.
- [ ] **Automated Tests**: Added new tests in `tests/` covering this change, and all tests pass cleanly (`python3 run_tests.py`).
- [ ] **Clean Settings Invariant**: Confirmed that `.mcp.json` remains strictly standard JSON-RPC 2.0 without instructional metadata.

## 🧪 Testing Checklist
- [ ] Executed `python3 run_tests.py` locally (100% pass rate).
- [ ] Ran bytecode compilation check (`python3 -m py_compile ...`).
- [ ] Verified locally via `python3 scripts/validate_pr_commits.py`.
