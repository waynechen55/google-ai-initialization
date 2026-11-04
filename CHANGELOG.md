# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.0] - 2026-11-01

### Added
- **First-Class Keyless Vertex AI ADC Recognition (`is_agent_ready_to_run`)**:
  - Automatically identifies active Google Cloud Application Default Credentials (`~/.config/gcloud/application_default_credentials.json` and `GOOGLE_APPLICATION_CREDENTIALS`).
  - Launches Phase 4 in Autonomous AI Agent Mode on keyless setups without requiring an explicit `GEMINI_API_KEY`.
- **Dynamic GCloud SDK Executable Resolution (`resolve_gcloud_bin`)**:
  - Dynamically searches environment paths, system PATH, Homebrew, and standalone install directories (`/root/google-cloud-sdk/bin/gcloud`, `~/google-cloud-sdk/bin/gcloud`).
  - Completely eliminates `FileNotFoundError` when `gcloud` is outside default `/usr/bin/PATH`.
- **Continuous Integration (CI/CD) & Quality Gates**:
  - Added `.github/workflows/ci.yml` enforcing Pull Request title and mandatory non-empty body.
  - Added strict single-commit / squash verification gate on all Pull Requests.
  - Hermetic multi-version automated test execution across Python 3.10, 3.11, and 3.12.
- **Enterprise Release Engineering & Project Packaging**:
  - Modern PEP 517/518/621 packaging metadata via `pyproject.toml`.
  - Added CLI console script entry point `google-ai-init`.
  - Added `.env.example` and `.mcp.json.example` sanitized credential templates.
  - Added `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and GitHub issue/PR templates.

### Fixed
- Fixed silent import swallowing when loading `agent_orchestrator`, now reporting transparent diagnostic warnings.
- Fixed `check_gcloud_auth_status` to evaluate active accounts dynamically using resolved `gcloud` paths.
- Symmetrically synchronized all core modules and bridges across dual deployment workspaces.

---

## [1.1.0] - 2026-10-31

### Added
- **Enterprise TLS Certificate Auto-Injection (`NODE_EXTRA_CA_CERTS`)**:
  - Detects host operating system CA bundles (`/etc/ssl/ca-bundle.pem`, `/etc/pki/tls/certs/ca-bundle.crt`).
  - Automatically uncomments and exports `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, and `REQUESTS_CA_BUNDLE` across `~/.bashrc`, `~/.profile`, `/etc/profile.d/node_extra_ca_certs.sh`, `.env`, and `~/.gemini/.env`.
  - Enables Node.js runtimes and Gemini CLI to natively trust internal enterprise corporate certificates (`*.suse.de`).
- **Strict HTML Exclusion Rule**:
  - Rejects HTML 401/403 web portals during native MCP probing, cleanly eliminating false-positive traps for Jira (`jira.suse.com`) and Service Desk (`sd.suse.com`).
- **Clean Settings Invariant**:
  - Enforces strict JSON-RPC 2.0 compliance in `.mcp.json` and `/root/.gemini/settings.json`, completely preventing prompt or instruction metadata infiltration.
- **Disk Safety Pre-Flight Safeguards**:
  - Enforces a 1,024 MB free disk threshold before entering each phase, preventing filesystem exhaustion.
- **JIT Bridge Healing**:
  - Automatically purges corrupted/hallucinated LSP scripts and restores canonical enterprise wrappers.

---

## [1.0.0] - 2026-09-20

### Added
- **Initial Release**: Complete Google AI & MCP Workspace Initializer pipeline.
- Phase 1: Environment Setup (NPM, Node.js, Python, agy, gcloud, Playwright).
- Phase 2: Dual Google Cloud Authentication (Playwright automated browser & terminal fallbacks).
- Phase 3: Project Configuration & Vertex AI / Gemini API Service Enablement.
- Phase 4: Autonomous MCP Discovery & Multi-Tier Verification (Native HTTP, NPM/PyPI, Local Bridges).
