# 🚀 Google AI & MCP Workspace Initializer

[![CI](https://github.com/waynechen/google-ai-initialization/actions/workflows/ci.yml/badge.svg)](https://github.com/waynechen/google-ai-initialization/actions)
[![Python 3.10 | 3.11 | 3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-2.0.0-green.svg)](CHANGELOG.md)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Model Context Protocol](https://img.shields.io/badge/MCP-Standard%20JSON--RPC%202.0-orange.svg)](https://modelcontextprotocol.io/)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

An **autonomous, enterprise-grade bootstrapping and self-healing workspace orchestrator** designed to prepare local development environments with Google Cloud SDK, Vertex AI, Gemini, Antigravity CLI, and automatically discover, write, and verify **Model Context Protocol (MCP) servers** over standard input/output (`stdio`) and native Streamable HTTP (`http`).

---

## 1. Project Overview & Distinguishing Characteristics

Onboarding local workspaces to Google Cloud APIs, Vertex AI models, and custom Model Context Protocol (MCP) servers has historically been a fragmented, error-prone manual process. Expired CLI OAuth tokens, mismatched quota project configs, headless display issues in remote SSH VMs, and malformed third-party JSON schemas often stall development pipelines.

This project delivers a **unified, multi-phase master orchestrator (`initialize_google_ai.py`)** that automates the entire workspace configuration. It bridges standard procedural scripting with cutting-edge **Autonomous AI ReAct loops** to automatically search, code custom bridges, and verify standard-compliant local workspace registries.

### 🌟 Key Innovations & Distinguishing Characteristics

* **Default-Permissive Architecture with Strict Inversion (`--no-allow-service-disabled`)**:
  * *Permissive by Default (No Flag)*: Corporate network developers with restricted GCloud IAM rights can bypass standard GCloud `services enable` failures warningly, continuing the setup seamlessly as long as the APIs are pre-enabled on the GCP Console.
  * *Strict on Demand*: Passing `--no-allow-service-disabled` toggles Strict Mode, forcing hard failures and stopping executions if any GCloud enablement or list commands fail (perfect for production release pipelines).
* **Zero-Display Headless/SSH Bypass (`--no-automatic-browser`)**:
  * Skips importing or spawning Playwright headed browsers entirely inside headless Docker containers, remote VM servers, or SSH shells, saving 15 seconds of displays timing out and outputting clean manual URL copy-paste OAuth login prompts instantly.
* **Dual-Mode Cognitive Setup Engine**:
  * *Autonomous ReAct Mode (Default)*: Dynamically promotes Phase 4 to an AI Setup Agent. The agent autonomously audits files, runs web-grounded Google searches, and utilizes local tools to write custom Python/Node.js bridges on the fly.
  * *Offline Procedural Fallback*: If tokens are missing or you are offline, appending `--no-agent` bypasses LLMs entirely, utilizing a highly optimized local database mapping common websites to NPM packages and auditing pre-existing local scripts.
* **20-Turn Conversational Headroom & Loop-Trap Protection**:
  * Configures the GenAI `GenerateContentConfig` with `types.AutomaticFunctionCallingConfig(maximum_remote_calls=20)` to **double the conversational turn budget from 10 to 20 remote calls**. This provides massive headroom for the Agent to probe non-traditional HTTP paths, check community libraries, write custom scripts, and execute handshakes on highly proprietary systems without ever hitting the turn-exhaustion wall.
* **Strict, File-Driven Target-Key Assertion**:
  * Eradicates false-positive reports by implementing a strict, file-driven success validator. The orchestrator loads `.mcp.json` and asserts that **a unique key matching that specific target actually exists inside the file**. If the Agent fails to write the configuration to disk (either due to turn exhaustion or logic errors), the execution summary will **unconditionally mark it as `❌ FAILED`**, ensuring 100% visual and logical consistency.
* **Strict Domain-Derived Unique Key Guardrails**:
  * Commands the Agent via `CRITICAL 4` system instructions to **always generate unique, domain-derived registry key names** (such as `"bugzilla-suse-com"` and `"bugzilla-opensuse-org"`), completely preventing sequential batch targets from silently overwriting each other in `.mcp.json`!
* **Strict Double-Handshake Schema Enforcement & Dependency Transparency**:
  * Prevents malformed community packages (such as versions of `@modelcontextprotocol/server-gitlab` omitting `type: "object"`) from being mistakenly approved. Both Agent and Procedural validators execute live `initialize` and `tools/list` handshakes, strictly rejecting any tool schema lacking `type: "object"`. In addition, custom Python bridge scripts are automatically inspected via `extract_script_dependencies()` to document external pip module requirements in `instructions`.
* **Bidirectional Target Reconciliation Audit**:
  * Eliminates reporting blind spots by performing an exhaustive end-of-run reconciliation audit between every requested `--mcp-server` target and the resulting `.mcp.json` registry. If any target was omitted or failed to write to disk, the Consolidated Report explicitly flags it as `❌ MISSING FROM .mcp.json REGISTRY!`, ensuring 100% visibility into which servers are active and which were dropped.
* **Two-Level Verification Standard (Process Health + Live Upstream Auth)**:
  * Eliminates false-positive "Connected" states where a local MCP process starts cleanly but remote API calls fail due to bad credentials. The validator conducts a strict Level 1 JSON-RPC stdio handshake (`initialize` + `tools/list`), followed by an active Level 2 non-blocking probe against the remote service endpoint (`verify_upstream_credentials()`). If the remote service returns HTTP 401/403, the tool immediately flags the credential rejection.
* **Open Build Service (OBS / IBS) Authentication Architecture**:
  * Open Build Service (`build.opensuse.org`, `build.suse.de`, `api.opensuse.org`, `api.suse.de`) strictly enforces **HTTP Basic Authentication** (`OBS_USER` and `OBS_PASSWORD`) or auto-discovery from `~/.config/osc/oscrc`. OBS does NOT support Bearer or Personal Access Tokens on its REST API; tokens issued in the OBS web UI under `/token` are strictly for automated webhook/service triggers.
* **Unbuffered Stdio Pipeline & Timeout Protection (`sanitize_stdio_script_content()`)**:
  * Completely cures the Python stdio block-buffering deadlock (`MCP error 32001: request timed out`). Automatically intercepts and sanitizes any Python bridge script to enforce real-time line-by-line streaming (`sys.stdin.readline()`) and strict socket timeouts on outbound HTTP requests, preventing client connection hangs.
* **Enterprise TLS & Corporate CA Resilience Architecture (`detect_system_ca_bundle()` & `probe_target_tls()`)**:
  * Eliminates runtime `[SSL: CERTIFICATE_VERIFY_FAILED]` errors when MCP tools interact with internal corporate backends (such as `*.suse.de`, internal enterprise PKIs, or self-signed servers). Automatically discovers the host OS root CA bundle (`/etc/ssl/ca-bundle.pem`, `/etc/ssl/certs/ca-certificates.crt`) and injects concrete `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, and `NODE_EXTRA_CA_CERTS` paths without shell syntax traps. In addition, pre-flight upstream TLS probing actively diagnoses internal corporate certificates and auto-provisions `PYTHONHTTPSVERIFY=0` and `NODE_TLS_REJECT_UNAUTHORIZED=0`, while automatically synchronizing verified servers between workspace `.mcp.json` and user global `~/.gemini/settings.json`.
* **Zero-Blocking Credentials & Exploration-Driven URL Derivation**:
  * Eliminates interactive terminal stalling and repetitive prompts. Automatically derives URL, Base URL, Endpoint, and Host settings directly from target domains during procedural sourcing. If optional secrets or tokens are absent, they are cleanly populated with empty strings or non-blocking placeholders, enabling automated, headless, and containerized execution without manual intervention.
* **Continuous Master Registry Accumulator & Shell Overwrite Guard**:
  * Guarantees that previously discovered servers can never be dropped or overwritten mid-run. The orchestrator maintains an in-memory master accumulator of all configured servers across sequential and concurrent iterations, automatically restoring any missing servers if an agent turn attempts a raw shell overwrite (`python3 -c 'with open(\".mcp.json\", \"w\")'`). Furthermore, `execute_shell_command` strictly blocks direct shell truncation, and `write_local_file_tool` enforces a mandatory read-and-merge policy on `.mcp.json`.
* **Mandatory URL / Endpoint Specification Standard**:
  * Guarantees that every MCP server targeting a remote service has its operational URL or endpoint explicitly visible in `.mcp.json` (either in `args` or in `env`), preventing anonymous or unidentifiable server registrations.
* **Dynamic API Gateway Negotiation & Universal Content Sanitizer**:
  * Automatically differentiates human Web Portals from machine REST Gateways without domain hardcoding, and shields clients from HTML error dumps by wrapping upstream faults in structured JSON diagnostics.
* **Dynamic Registry Preservation & Merging Safeguard**:
  * Programmatically preserves and scans any pre-existing `.mcp.json` registry file at the start of Phase 4 (both in master and standalone executions). This prevents data loss from interrupted runs or manual config edits, dynamically merging new discoveries inline while overwriting only actively targeted server blocks.
* **100% Unified Visual UX Symmetrical Reporting**:
  * Running Phase 4 standalone (`phase4_mcp_auto_discovery.py`) now dynamically prints the exact same beautiful summary boards (`PHASE 4: MCP AUTO-DISCOVERY EXECUTION SUMMARY`), consolidated reports (`📦 CONSOLIDATED MCP REGISTRY DISCOVERY REPORT`), and registry pointers as the master run, keeping reporting consistent across all execution modes.
* **Dynamic Import Cache Invalidation (`importlib.invalidate_caches()`)**:
  * Cures the classic Python runtime caching bug where Python fails to load Playwright on a first-time bootstrap because the files were written to disk *after* the parent process started. Calling `invalidate_caches()` dynamically evicts the index, allowing Playwright to load instantly on the very first machine bootstrap!
* **Modern Python MCP Runner Pre-Configuration & JIT Auto-Healing (`uv` / `uvx`)**:
  * Pre-configures the modern Python `uv` package in Phase 1, making `uv` and `uvx` available as first-class runners. Furthermore, Phase 4 and the AI Agent feature thread-safe Just-In-Time (JIT) runner and Python module resolution: if a server requires a missing runner or crashes with `ModuleNotFoundError`, the engine automatically installs the missing package in user space and cleanly retries the handshake once.

---

## 2. Directory Map & Architecture Design

```
google-ai-initialization/
├── initialize_google_ai.py       # Master Orchestrator (Phase 1-4 Coordinator)
├── phase1_install.py             # Phase 1: Dependency & OS Compiler Setup
├── phase2_authenticate.py        # Phase 2: Multi-Channel Auth Manager
├── phase3_provision.py           # Phase 3: Project defaults & API Provisioner
├── phase4_mcp_auto_discovery.py  # Phase 4: Batch Auto-Discovery & Handshaker
├── agent_orchestrator.py         # Autonomous ReAct Setup Agent Framework
├── utils.py                      # Common logger, environment, and timeout utilities
├── run_tests.py                  # Automated test suite runner (all 35 unit/integration tests)
├── .gitignore                    # Credentials & local cache protection shield
├── README.md                     # Comprehensive architecture & operational guide
├── agents/
│   └── google-ai-initializer.md  # Custom AI Agent Persona & Playbook
├── bridges/                      # Verified standalone Python MCP bridge implementations
│   ├── build_opensuse_mcp.py
│   ├── build_suse_de_mcp_server.py
│   ├── developerknowledge_mcp_bridge.py
│   ├── gitlab_mcp_server.py
│   ├── jira_suse_mcp.py
│   ├── openqa_mcp_server.py
│   ├── sd_suse_mcp_server.py
│   └── smelt_mcp_server.py
├── skills/
│   └── google-ai-initialization/
│       └── SKILL.md              # Detailed Playbook Manual
└── tests/                        # Comprehensive hermetic test suite (35+ test cases)
    ├── test_utils.py
    ├── test_phase1.py
    ├── test_phase2.py
    ├── test_phase3.py
    ├── test_phase4.py
    └── test_orchestrator.py
```

---

## 3. Detailed Modules & Phases Deep-Dive

```
 [Target Website / GCP Project]
               │
               ▼
 ┌─────────────────────────┐
 │ Phase 1: Setup & Verify ├─────── [FAIL] ──────> [Self-Heal Pip, PATH, and NPM]
 └─────────────┬───────────┘
               │ [PASS]
               ▼
 ┌─────────────────────────┐
 │ Phase 2: Authentication ├─────── [FAIL] ──────> [Manual URL Copy-Paste Fallback]
 └─────────────┬───────────┘
               │ [PASS]
               ▼
 ┌─────────────────────────┐
 │ Phase 3: Cloud Provision├─────── [FAIL] ──────> [Warn & Bypass REST/SDK/Agent Checks]
 └─────────────┬───────────┘
               │ [PASS]
               ▼
 ┌─────────────────────────┐
 │ Phase 4: MCP Discovery  ├─────── [FAIL] ──────> [Local Offline Procedural Dictionary]
 └─────────────────────────┘
```

### 3.1. Phase 1: Environment Setup & Verify (`phase1_install.py`)
Checks and installs prerequisites: Node.js, NPM, Google Cloud SDK, Playwright, Antigravity CLI (`agy`), and key Python libraries (`google-genai`, `playwright`, `mcp`). If standard `pip` fails due to locked operating systems (PEP 668), it automatically appends `--break-system-packages` to self-heal.

### 3.2. Phase 2: Google Cloud Authentication (`phase2_authenticate.py`)
Performs multi-channel login. It first attempts to launch Playwright headed browsers. If X11 displays are missing or `--no-automatic-browser` is passed, it falls back to printing secure OAuth authentication URLs, awaiting alphanumeric verification codes, and executing the ADC token exchange.

### 3.3. Phase 3: Project Configuration & API Enablement (`phase3_provision.py`)
Sets CLI configurations. It sets GCloud CLI defaults and enables `aiplatform.googleapis.com` (Vertex AI API), `generativelanguage.googleapis.com` (Gemini API), and `apikeys.googleapis.com` (GCP API Keys). 
Finally, it runs a **diagnostics connection report** checking standard library urllib POST calls, python GenAI SDK pings, and `agy` agent socket handshakes independently.

### 3.4. Phase 4: AI-Powered MCP Discovery & Handshake (`phase4_mcp_auto_discovery.py`)
Resolves the correct stdio command, args, and required env vars for target domains sequentially.
It executes the **Strict Double-Handshake** (`initialize` -> `initialized` -> `tools/list`) and strips any instructions from `.mcp.json` to keep the configuration 100% compliant with standard MCP IDE schemas.

---

## 4. Execution Modes

### 4.1. Autonomous AI Agent Mode (Default)
If Phase 4 discovers that GCloud ADC or a `GEMINI_API_KEY` is present, it automatically initiates the **Autonomous ReAct Agent Loop (`agent_orchestrator.py`)** targeting the flagship model `gemini-3.7-flash`:
1. **Direct URL Probing**: The agent uses cURL to probe target websites directly for native endpoints (like `/mcp` or `/v1/mcp`).
2. **Community Packages**: If native endpoints are absent, it searches the web for NPM/pip packages (such as `@modelcontextprotocol/server-postgres`).
3. **Bridge Synthesis**: If existing packages are missing or broken (failing tools schemas), the agent calls `write_local_file_tool` to **invent custom Python/Node.js bridges** from scratch.

### 4.2. Traditional Offline Procedural Fallback Mode
If GCloud tokens are completely missing, or if you run the script with **`--no-agent`**, Phase 4 skips LLMs entirely:
1. **Workspace Script Auditing**: It scans your workspace root directory for existing custom Python servers (like `bugzilla_mcp.py` or `openqa_mcp_server.py`).
2. **Local Registry Database**: Falls back to an optimized, built-in local JSON dictionary mapping common endpoints (GitHub, GitLab, Postgres, Redmine, Bugzilla, Service Desk) to their corresponding standard NPM/npx packages.

### 4.3. Enterprise Resilience & Route Cascading
Custom bridges and auto-discovered configurations feature production-grade hardening:
* **100% Headless Auto-Discovery**: All discovery paths run completely non-blocking without pausing on interactive stdin/keyboard prompts. If validation fails, errors and setup instructions are captured in the Consolidated Report without freezing automated pipelines.
* **Universal Entity Key Normalization**: Full web URLs passed by developers are dynamically stripped down to their bare canonical keys or IDs (e.g. `https://sd.suse.com/.../SD-204666` ➔ `SD-204666`).
* **Dynamic Credential Hot-Reloading**: Long-running MCP server subprocesses dynamically re-read refreshed tokens from live process environment, `settings.json`, `.mcp.json`, and `.env` per request without requiring process restart.
* **Hierarchical Route Cascading**: In multi-tiered platforms (such as Jira Service Management), requests automatically cascade from Core REST APIs (`/rest/api/2/issue`) to Customer Portal APIs (`/rest/servicedeskapi/request`) upon hitting license or permission barriers (HTTP 500/401/403).
* **Polymorphic Native Transport Support (`http`, `sse`, `stdio`)**: Natively supports and discovers both modern Streamable HTTP native endpoints (`type: "http"`, e.g. openQA), Server-Sent Events (`type: "sse"`), and local command-line processes (`stdio`), preserving maximum client portability.
* **Strict HTML Rejection & Anti-Webpage Guard**: Native MCP probing strictly requires JSON-RPC 2.0 payloads (`application/json` or `text/event-stream`). Any endpoint returning `Content-Type: text/html` or HTML document bodies (`<!DOCTYPE`, `<html`) is immediately rejected, eliminating false positives from web dashboards, reverse proxies, and SSO login portals (such as Jira or Service Desk) and ensuring working local enterprise bridges are activated instead.
* **Dual-Key Streamable HTTP Transport (`httpUrl` + `url`)**: Streamable HTTP endpoints dynamically populate both `httpUrl` (for Gemini CLI's `StreamableHTTPClientTransport`) and `url` (for SSE and general MCP clients), guaranteeing 100% protocol compliance and immediate connection status across all editors.
* **First-Class Keyless Vertex AI ADC Recognition (`is_agent_ready_to_run`)**: Evaluates Agent Mode readiness across all supported authentication channels (Keyless `~/.config/gcloud/application_default_credentials.json`, `GOOGLE_APPLICATION_CREDENTIALS`, Key-based `GEMINI_API_KEY`, and active GCloud CLI login). Keyless Vertex AI setups are recognized instantly as first-class citizens, completely eliminating accidental fallbacks to standard procedural mode on machines authenticated without an explicit API key.
* **Dynamic GCloud SDK Executable Resolution (`resolve_gcloud_bin`)**: Dynamically resolves the active `gcloud` binary across environment variables, standard PATH, and common standalone installations (`/root/google-cloud-sdk/bin/gcloud`, `~/google-cloud-sdk/bin/gcloud`, Homebrew, `/opt/google-cloud-sdk/bin`). Guarantees that authentication and project checks succeed even when `gcloud` is not in standard `/usr/bin/PATH`.
* **Automatic Enterprise TLS Certificate Injection (`NODE_EXTRA_CA_CERTS`)**: Host operating system CA bundles (`/etc/ssl/ca-bundle.pem`, etc.) are automatically detected and exported across `~/.bashrc`, `~/.profile`, `/etc/profile.d/node_extra_ca_certs.sh`, `.env`, and `~/.gemini/.env`. This ensures Node.js runtimes and Gemini CLI natively trust internal enterprise corporate certificates (`*.suse.de`) without `UNABLE_TO_GET_ISSUER_CERT_LOCALLY` errors.
* **Strict Clean Settings Invariant (No Metadata Infiltration)**: Configuration registries (`.mcp.json` and `~/.gemini/settings.json`) strictly enforce pure JSON-RPC 2.0 schemas. Instructional metadata, commentary, and setup guidance are logged to console banners and file logs only, and are strictly prohibited from infiltrating server definition blocks.
* **Challenge-Aware Native Probing & In-Flight Authentication**: Detects HTTP 401/403 responses accompanied by JSON error payloads or `WWW-Authenticate` headers as live authenticated native MCP servers rather than missing endpoints. Automatically auto-injects in-flight credentials (such as `OPENQA_API_KEY`, `OPENQA_API_SECRET`, Bearer tokens) to validate native authentication directly on Tier 1.
* **Deferred-Authentication Tolerance for Token-Gated Native Endpoints**: Accurately identifies token-gated Native HTTP endpoints (e.g. openQA returning HTTP 403 `no api key`). In headless automated environments before credentials are configured in `.env`, the endpoint and protocol are confirmed active (`[DEFERRED AUTHENTICATION]`), preserving the native configuration in `.mcp.json` without failing the handshake or causing the Agent to panic and cascade to local bridges.
* **Polymorphic 3-Level Validation Engine**: Validates both `stdio` subprocesses and `http` endpoints across Level 1 (`initialize`), Level 1.5 (`tools/list` schema check), and Level 3 (`tools/call` smoke test), ensuring identical verification rigor across all transports.
* **Transport & DNS Outage Decoupling**: Decouples transport-level network disruptions (`[Errno -3] Temporary failure in name resolution`, `The read operation timed out`, `Server disconnected without sending a response`) from LLM reasoning attempts. Enters an extended 20–35s recovery window to allow host DNS and network interfaces to recover without decrementing session attempt quotas.
* **Zero-Latency Handshake Standard for Local Bridges**: Local MCP stdio bridges (`google_developer_knowledge_bridge.py`, etc.) respond immediately to `initialize` and `tools/list` with 0ms in-memory schemas, eliminating network dependencies during stdio double-handshake verification.
* **Mandatory Write-Before-Verify Invariant**: Mandates writing server configurations to `.mcp.json` before initiating live verification testing, ensuring discovered configurations are preserved on disk and enabling `[RESILIENT RECOVERY]` if external network connections drop.
* **Mandatory Turn-1 Fast Native Probing (Tier 1 Precedence)**: The Autonomous Agent is strictly mandated to execute native endpoint probing first for 100% of targets on Turn 1 using a single batch `curl` command. Repetitive single-path `curl` sweeps across separate conversational turns are strictly prohibited, preserving turn budgets and eliminating Vertex AI 429 quota limits while guaranteeing that any native MCP server upstream is prioritized.
* **Symmetrical Deterministic Python Tools**: Replaces error-prone LLM shell scripting (`curl`, `npm view`, `cat`) with 4 high-speed deterministic Python tools available to the Agent: `probe_native_remote_endpoint_tool` (concurrent HTTP/SSE probing in <1.5s), `search_package_registries_tool` (dual npm+PyPI JSON querying in 50ms), `list_local_bridges_tool` (AST-based environment introspection in 5ms), and `generate_mcp_bridge_template_tool` (zero-dependency scaffolding). These guarantee 100% deterministic I/O across Agent and Traditional modes without wasting context tokens or triggering Vertex AI 429 quota exhaustion.
* **Monotonic Tiered Candidate Scoring (Native = Score 100)**: In procedural mode, native endpoints hold absolute top priority (`score: 100`), ensuring they are always Candidate 1 over local bridges (scores 20-39) and official packages (score 50), with all candidates sorted descending to preserve strict discovery tiers.
* **FQDN Domain-Aware Mapping in High-Signal Knowledge Base**: Exact corporate domains (`build.opensuse.org`, `build.suse.de`, `progress.opensuse.org`, `smelt.suse.de`, `sd.suse.com`) are mapped explicitly in `COMMON_MCP_SERVERS`, enabling agents to recognize and configure canonical bridges on Turn 2 if native probing proves negative.
* **Mandatory Physical Disk Verification for Agent Success**: Eliminates false-positive reports by strictly validating that discovered servers are physically present in `.mcp.json` before accepting any claim of success, preventing discrepancies between discovery summaries and final reconciliation audits.
* **Dynamic Per-Attempt Configuration & Thinking Mode Isolation**: Dynamically reconstructs `attempt_config` inside the retry loop for each attempt, guaranteeing that fallback models (`gemini-2.5-flash`) enforce `thinking_budget=0`, preventing thinking token accumulation and 429 quota exhaustion.
* **Forced Function-Calling Mode (`mode="ANY"`) on Multi-Turn Follow-Ups**: When an agent model emits conversational planning text rather than tool calls, follow-up turns are sent with forced function-calling configuration (`mode="ANY"`), physically compelling the model to return a structured tool call (`write_mcp_settings_tool` or `verify_mcp_connection_tool`) rather than conversational text.
* **Active Multi-Turn Conversational Follow-Up Loop**: If the agent model emits conversational planning text rather than tool calls, the orchestrator automatically keeps the session active and sends an explicit follow-up turn directing it to execute `write_mcp_settings_tool` and `verify_mcp_connection_tool`.
* **Stdio MCP Process Protection in Shell Tool**: Intercepts and blocks commands that attempt to execute unpiped stdio MCP bridge scripts (`execute_shell_command`), eliminating 180-second command timeouts.
* **Valibot-Compliant Bugzilla Bridge Routing**: Maps Bugzilla instances in `COMMON_MCP_SERVERS` directly to `bridges/bugzilla_mcp_server.py`, ensuring all tool responses are enclosed in Object/Record envelopes and eliminating `@kud/mcp-bugzilla` schema crashes (`expected record, received array`).
* **Dedicated Phase Summary Reporting**: Distinctly isolates `🤖 AUTONOMOUS AGENT DISCOVERY SUMMARY`, `🔧 PROCEDURAL RECOVERY SUMMARY`, and `📋 TARGET RECONCILIATION AUDIT`, ensuring complete transparency across discovery phases.
* **ThinkingConfig Token Optimization for Gemini 2.5 Agents**: For tool-orchestrating agents where function calls are strictly expected, `thinking_config=types.ThinkingConfig(thinking_budget=0)` disables internal chain-of-thought token depletion, directing 100% of generation output to schema-validated tool executions.
* **Thread-Isolated Model Quota Failover**: On 429 quota exhaustion, dynamic model failover operates within thread-local context (`_thread_local.active_model`), preventing transient rate limits on one worker thread from poisoning the global environment for sibling threads.
* **Non-Blocking Resilient Pre-Flight DNS**: Resolves DNS lookups with non-blocking resilience across both Agent and Procedural modes, logging informative warnings on corporate proxy resolver delays rather than aborting discovery runs.
* **Strict Anti-LSP Guardrails & Exact MCP Stdio Specification (CRITICAL 13)**: The agent prompt explicitly defines the standard JSON-RPC 2.0 schemas for MCP (`initialize`, silent notifications, `tools/list`, and `tools/call` Object envelope) and strictly forbids Language Server Protocol (LSP) boilerplate (`textDocumentSync`, `completionProvider`). In addition, `write_local_file_tool` automatically intercepts and rejects any file writes containing LSP code before they can touch the disk.
* **Indestructible Embedded Canonical Bridge Registry & Thread Mutex**: `utils.py` enforces thread-safe I/O via `bridge_io_lock`, detects and purges corrupted LSP scripts, and synthesizes or restores canonical bridges automatically without depending on fragile cross-machine directory trees.
* **Dual-Directory Local Bridge Search & Enterprise Cascading**: `discover_all_mcp_candidates` evaluates both `bridges/` and `WORKSPACE_ROOT`, cascading Jira Service Management (`sd.suse.com`) to official `uvx mcp-atlassian` packages and OBS targets to canonical OBS bridge runners.
* **Strict JSON-RPC Request ID Correlation**: Handshake validation (`validate_mcp_connection` & `verify_mcp_connection_tool`) correlates JSON-RPC responses directly by matching request IDs (`id: 2`), safely skipping unhandled notification errors (`notifications/initialized`) and noise messages emitted by minimal or standard JSON-RPC bridges.
* **Autonomous Headless Credential Provisioning (CRITICAL 12)**: The Autonomous Agent's core mandate strictly prohibits halting to prompt users for API tokens or passwords in conversational text. It immediately writes standard configurations with placeholder credentials (`""`, `"<REQUIRED_TOKEN>"`), documents setup instructions, and completes connection testing.
* **Strict Orchestrator Script Exclusion in Bridge Healing**: `ensure_bridges_directory_intact` explicitly excludes pipeline and runner scripts (`phase4_mcp_auto_discovery.py`, `agent_orchestrator.py`, etc.) from being copied into `bridges/`, preventing core pipeline scripts from being misidentified as local bridges.
* **Dual MCP SDK Compatibility Shim (`FastMCP` + `MCPServer`)**: Auto-heals legacy and AI-generated MCP bridge scripts by dynamically aliasing `FastMCP` to `MCPServer` under `mcp 2.x`, preventing `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. In addition, `phase1_install.py` pins `mcp<2` for maximum out-of-the-box ecosystem compatibility.
* **Defensive Agent Tool Typing**: `write_mcp_settings_tool` and `verify_mcp_connection_tool` handle `NoneType`, stringified JSON, and heterogeneous arrays defensively, preventing unhandled exceptions inside the Agent's tool-execution loop.
* **Self-Healing Bridge Synchronizer (`ensure_bridges_directory_intact`)**: Proactively audits the `bridges/` directory at the start of Phase 4 and before discovery, automatically recovering missing or corrupted standard bridges from repository templates and root directories.
* **Pre-Existing Registry Skip Protection**: When a target is already registered and verified in `.mcp.json`, procedural auto-discovery automatically skips redundant re-discovery (`[ALREADY REGISTERED]`), preserving verified configurations and preventing accidental deletion or regression.
* **Multi-Candidate Cascading Discovery**: Default procedural mode exhaustively evaluates all candidate tiers (Fingerprints, Local Bridges, Standard Packages, OpenAPI specs, and AI generators) in priority order, automatically cascading to subsequent candidates if an earlier candidate fails connection validation.
* **Deferred-Tool Token Verification**: Validates token-gated servers (such as `uvx mcp-atlassian` for Jira and Confluence) when they successfully initialize via standard JSON-RPC, gracefully recognizing that dynamic tool populations will activate once authentication credentials are provided in `.env`.
* **Thread-Isolated Target Hinting**: Uses a thread-local (`threading.local`) context for target hints during concurrent Agent discovery, preventing race conditions where parallel threads could overwrite each other's domain context.
* **Domain-Scoped Credential Isolation**: Independent subdomains (such as `jira.suse.com` and `sd.suse.com`) are treated as separate authentication realms, preventing token cross-pollution.
* **Zero-Dependency Redmine & Issue Tracker Bridges**: Custom local standard-library bridge (`bridges/redmine_mcp_server.py`) for Redmine instances (such as `progress.opensuse.org`), eliminating reliance on external npm packages and providing 100% immunity to npm registry timeouts (`ETIMEDOUT`) behind firewalls.
* **Bulletproof Agent Session Self-Healing**: Transient API network glitches (e.g. `Server disconnected without sending a response`, connection resets, HTTP 5xx/429) automatically trigger session-level recovery with exponential backoff and .mcp.json state preservation instead of abandoning the agent loop.
* **Pre-Flight DNS Fast-Fail**: Validates target hostnames via non-blocking socket lookups in < 0.1s, immediately rejecting mistyped or unreachable domains before wasting turn budgets.
* **Concurrency Watchdog & Socket Timeout Protection**: Sets explicit 120s socket timeouts in `HttpOptions` and wraps thread pools in an adaptive deadline watchdog, preventing threads from hanging permanently on silent socket drops.
* **Phase 1 Environment Completeness & Pre-Flight Prerequisites**: Proactively verifies and installs core Python dependencies (`python-dotenv`, `requests`, `pyyaml`, `packaging`), Playwright Linux OS-level shared libraries (`--with-deps`), Node.js v18+ runtime verification, essential command-line tools (`git`, `curl`), and system root CA certificate bundles (`ca-certificates-mozilla` / `ca-certificates`).
* **Universal Record Envelope Instruction**: The autonomous agent's system prompt enforces CRITICAL 10, strictly requiring that all generated Python MCP bridge scripts wrap tool outputs in Object/Record dictionaries (`{"count": len(items), "items": items}`) with explicit `structuredContent`.
* **Anti-Array Record Envelope Normalization**: Custom bridges automatically wrap list-based tool outputs into semantic Record dictionaries (`{"count": len(items), "items": items}`) with explicit `structuredContent`, completely eliminating client-side Valibot/Zod schema validation crashes (`expected record, received array`). This normalization covers `gitlab_mcp_server.py`, `bugzilla_mcp_server.py`, `sd_suse_mcp_server.py`, and `redmine_mcp_server.py`.
* **Indestructible AFC Tool Dispatching (`ResilientFunctionMap`)**: Automatically wraps function execution mapping in the Google GenAI SDK to handle internal model calls (`google:python_interpreter`) and unmapped tool invocations gracefully without raising `KeyError` crashes, eliminating retry exhaustion in Agent Mode.
* **Multi-Machine Path Self-Healing**: `get_workspace_root()` dynamically validates that path overrides and environment variables physically exist on the local disk before accepting them. Stale paths copied from other user environments or machines are automatically detected and replaced with the current host directory, guaranteeing that local bridge scripts (`gitlab_mcp_server.py`, `bugzilla_mcp_server.py`, `redmine_mcp_server.py`) are always discovered and prioritized.
* **Concurrency-Aware Rate Limiting with Jitter Backoff & Supported Model Pinning**: In multi-threaded concurrent execution (`--concurrency`), parallel agent threads apply randomized exponential jitter backoff on `429 RESOURCE_EXHAUSTED` rate limits. Fallback models on Vertex AI `global` are pinned strictly to active publisher models (`gemini-3.7-flash`, `gemini-2.5-flash`), eliminating fatal `404 NOT_FOUND` crashes on unsupported models like `gemini-2.0-flash`.
* **Strictly Scoped Procedural Fallback**: When Agent Mode completes with partial target failures, procedural recovery executes *only* on the failed subset (`failed_targets`). Targets that already succeeded in Agent Mode are fully preserved in `.mcp.json` and are never re-evaluated or overwritten.
* **Thread-Safe Master Registry Synchronization**: All reads, merges, and writes in `sync_master_mcp_registry` are protected by `mcp_write_lock`, preventing race conditions and lost server entries when worker threads finish concurrently.
* **Specificity-Guarded Bridge Scoring**: Local bridge discovery requires a service-identifying keyword match (`gitlab`, `openqa`, `bugzilla`, `obs`, `build`, etc.) before awarding match points. Generic domain tokens (`suse`, `opensuse`, `org`, `com`, `de`) only grant secondary bonus points, preventing unrelated services (such as `confluence.suse.com`) from being hijacked by openQA or other bridges.
* **Universal Open Build Service (OBS) Bridge Server**: `bridges/obs_mcp_server.py` serves as a canonical wrapper that dynamically delegates to `build_opensuse_mcp.py` based on `OBS_BASE_URL`, guaranteeing valid physical script resolution for both `build.opensuse.org` and `build.suse.de`.
* **Dynamic 429 Quota Failover**: When API rate limits (`429 RESOURCE_EXHAUSTED`) occur on the primary model tier (`gemini-3.7-flash`), the agent session self-healing automatically adapts the active session model to high-capacity tiers (`gemini-2.5-flash` or `gemini-1.5-flash`), preventing exhausted quota limits from aborting agent mode.
* **Resilient Non-Blocking DNS Pre-Flight**: Pre-flight DNS verification employs an 8.0-second timeout with automatic retries and logs non-fatal warnings rather than fatally terminating agent discovery on transient resolver latency.
* **Turn-Budget Subprocess Protection**: Blocks multi-minute test runner invocations (`run_tests.py`) inside discovery shell commands, preventing 180s command timeouts and preserving turn budgets for MCP configuration.
* **Deep Smoke Test Timeout Resilience**: Level 3 Functional Smoke Testing uses a 15.0-second timeout to accommodate corporate proxy and enterprise network latency (such as `build.suse.de`).
* **Commit-on-Success MCP Registration**: Discovery flows across both Agent and Traditional modes validate server connectivity and tool double-handshaking *before* committing configurations to `.mcp.json` or `/root/.gemini/settings.json`. Failed connection attempts or speculative guesses are never written to disk, preventing dummy or broken entries (such as `example-com`) from cluttering user registries.
* **Native Zero-Dependency Bugzilla MCP Bridge**: Dedicated standard-library bridge (`bridges/bugzilla_mcp_server.py`) for SUSE and openSUSE Bugzilla instances (`bugzilla.suse.com`, `bugzilla.opensuse.org`), eliminating external npm dependencies and wrapping all list responses (`search_bugs`, `get_products`, `get_comments`, `get_attachments`) in semantic Record dictionaries (`{"count": len(...), "...": [...]}`) for 100% Valibot schema conformance.
* **Ground-Truth Credential Verification**: Authentication flows decouple token exchange from optional quota checks by passing `--disable-quota-project` and inspecting `application_default_credentials.json` directly. If valid tokens exist on disk or in the active profile, Phase 2 succeeds even if upstream CLI warnings or disabled APIs in legacy projects produce non-zero exit codes.
* **Enterprise Vertex AI ADC Routing, Millisecond Timeouts & Scored Bridges**: Keyless agent initialization explicitly targets Vertex AI (`vertexai=True`, `GOOGLE_GENAI_USE_ENTERPRISE="True"`) with designated project and location scopes, isolating from all lingering API keys (`GEMINI_API_KEY` and `GOOGLE_API_KEY`). Socket timeouts in `google-genai` SDK are enforced in milliseconds (`120_000.0` for 120s), eliminating sub-second timeout traps. Verification pings employ adaptive model fallbacks (`[target_model, "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]`), and DNS checks run in private thread pools without mutating global socket defaults. Subprocesses enforce working directory anchoring (`cwd=WORKSPACE_ROOT`), merge configuration environments (`config['env']`), and employ tokenized scored matching across local bridges.
* **Vertex AI Tool Exclusivity & Physical File Existence Guard**:
  * *Tool Exclusivity Enforcement*: Vertex AI prohibits mixing Google Search grounding tools with custom function calling tools (`400 INVALID_ARGUMENT`). On Vertex AI, the agent operates in strict function calling mode, preventing turn-1 aborts, with reactive session self-healing if tool conflicts occur.
  * *Physical File Existence Guard (`is_valid_mcp_config`)*: Procedural discovery and LLM recommendations strictly verify that any referenced Python script physically exists on disk before accepting or executing the configuration, eliminating phantom file errors (`[Errno 2] No such file or directory`).
  * *Curated Package Priority*: Verified community packages (`COMMON_MCP_SERVERS`, e.g. `uvx openqa-mcp`, `npx @kud/mcp-bugzilla`) are evaluated before falling back to generative LLM queries.

### 4.4. Filesystem Quotas & Pre-Phase Disk Space Verification
To prevent partial writes, corrupted configuration files, or hanging compilers during setup:
* **Pre-Phase 1024 MB Free Space Safeguard**: Before starting every scheduled phase, the engine verifies that all underlying target filesystems (`WORKSPACE_ROOT`, `/tmp`, and `~`) have at least **1024 MB** of free unprivileged space.
* **Early Halt & Diagnostics**: If any target filesystem has less than 1024 MB available, the run stops immediately *before* that phase begins, names the affected filesystem and mount point, and scans and lists the top largest disk space consumers.
* **Actionable Manual Cache Remediation**: The engine outputs copy-pasteable manual commands to reclaim shared cache space (e.g. `pip cache purge`, `npm cache clean --force`, `python3 -m playwright uninstall --all`, `rm -rf /tmp/pip-* /tmp/npm-* ~/.cache/*`, `docker system prune -f`). **The program strictly never executes these cleanup commands itself**, protecting your developer environment.
* **Physical-Corroborated ENOSPC Interception**: Accurately distinguishes genuine OS disk exhaustion (`OSError(ENOSPC)` during file I/O or verified stderr errors when available space is < 50 MB) from harmless command text. Subprocess stdout (such as `grep` searching source code or test definitions) is never scanned for crash conditions. Agent shell commands return output directly to the agent model rather than abruptly killing the process, eliminating false-positive terminations when disk space is plentiful.

---

## 5. Complete CLI Argument Reference

| Argument | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--gcloud-project` | `str` | *Auto-resolved* | Target Google Cloud Project ID for APIs, Vertex AI, and ADC billing quotas. |
| `--gcloud-location` | `str` | `global` | GCloud Region/Zone for Vertex AI API routing. |
| `--use-enterprise` | `str` | `True` | Forces the Google GenAI SDK to route requests through your Vertex AI project quota. |
| `--genai-model` | `str` | `gemini-3.7-flash` | The specific Gemini model targeting verification checks and the Setup Agent. |
| `--env-file` | `str` | `.env` | Path to load and persist configurations/credentials. |
| `--mcp-server` | `str` | `bugzilla.suse.com` | Comma-separated list of websites/databases to discover sequentially in Phase 4. |
| `--phases` | `str` | `1,2,3,4` | Comma-separated phase numbers to schedule (e.g. `3,4` to configure and discover). |
| `--no-automatic-browser`, `--headless` | `flag` | `False` | Bypasses Playwright headed browser launches entirely to prevent display hangs in headless SSH sessions, providing direct manual copy-paste URLs. |
| `--no-allow-service-disabled` | `flag` | `False` | Forces Strict API Mode: Aborts Phase 3 immediately if GCloud API enablement or verification fails. |
| `--no-agent` | `flag` | `False` | Forces Phase 4 to use offline, procedural local dictionaries, skipping the AI Setup Agent. |
| `--force-auth` | `flag` | `False` | Forces GCloud re-authentication while preserving and auto-filling existing account/login info (overwrites in-place). |
| `--force-auth-with-reset` | `flag` | `False` | Forces GCloud re-authentication AND actively purges stale ADC cache files on disk first, enforcing a clean-slate re-entry. |
| `--force-install` | `flag` | `False` | Triggers Phase 1 to force clean reinstall of Node.js, NPM, GCloud, and Python SDKs. |
| `--break-dependencies` | `flag` | `False` | Passes conflict bypass flags to standard compilers, forcing OS pip installs. |
| `--downgrade` | `flag` | `False` | Force-installs older stable versions of libraries if modern SDKs conflict on older machines. |
| `--timeout` | `int` | `180` | Specifies the absolute process timeout in seconds across all subprocess calls and handshakes. |
| `--dry-run` | `flag` | `False` | Simulates execution across all scheduled phases in read-only mode without installing packages, enabling services, modifying credentials, or creating files. |
| `--do-cleanup` | `flag` | `True` | Automatically purges all temporary workspaces, scratchpads, and diagnostic artifacts upon run completion (enabled by default). |
| `--no-cleanup` | `flag` | `False` | Disables automated cleanup upon run completion, preserving temporary workspaces and diagnostic artifacts on disk for post-mortem debugging. |

---

## 6. How to Run (Typical Production Scenarios)

### Scenario A: Comprehensive Enterprise Bootstrap with Custom Servers & Auth Reset (Verified Production Flow)
The primary, fully-featured command to bootstrap an entire enterprise workspace from scratch, refresh all dependencies and authentication tokens, and discover custom internal and cloud MCP servers:

```bash
python3 /path/to/google-ai-initialization/initialize_google_ai.py \
  --gcloud-project "your-gcp-project-id" \
  --gcloud-location "global" \
  --use-enterprise true \
  --env-file "/path/to/.env" \
  --force-install \
  --force-auth \
  --mcp-server "srv1.corp.com,srv2.corp.us,srv3.google.com" \
  --agent-max-turns 15 \
  --concurrency 4
```
*(Note: If installed via `pip install -e .` or executed directly from the project directory, you can also run `python3 initialize_google_ai.py` or the `google-ai-init` CLI entry point with identical arguments).*

#### Parameter Breakdown & Detailed Descriptions:
* **`--gcloud-project "your-gcp-project-id"`**: *(Mandatory/Recommended)* The target Google Cloud Project ID to configure. Binds Vertex AI quotas, Gemini model routing, and enabled API services directly to this project.
* **`--gcloud-location "global"`**: Sets the regional API endpoint (e.g. `global`, `us-central1`, `europe-west1`) for Vertex AI and Gemini SDK client calls.
* **`--use-enterprise true`**: Enables Enterprise Vertex AI SDK routing (`GOOGLE_GENAI_USE_ENTERPRISE=True`) rather than consumer Gemini API endpoints.
* **`--env-file "/path/to/.env"`**: Specifies the explicit filesystem path to your local environment file. Discovered credentials, active project IDs, and detected TLS certificate paths are read from and securely persisted here.
* **`--force-install`**: Forces a fresh verification and re-installation of required Python packages (`google-genai`, `playwright`, `mcp`), host utilities, and Playwright Chromium drivers (Phase 1).
* **`--force-auth`**: Forces clean re-authentication for Google Cloud standard login and Application Default Credentials (ADC), refreshing stale or expired OAuth sessions (Phase 2).
* **`--mcp-server "srv1.corp.com,srv2.corp.us,srv3.google.com"`**: Comma-separated list of target servers/services to auto-discover, synthesize bridges for, and register into `.mcp.json`.
* **`--agent-max-turns 15`**: *(Optional)* Maximum reasoning/action ReAct decision loops allocated per target server for the Autonomous AI Agent. Default is 15 turns.
* **`--concurrency 4`**: *(Optional)* Number of parallel worker threads used during Phase 4 multi-server discovery, with atomic log grouping to prevent output interleaving.

#### What Happens Under the Hood (Execution Lifecycle):
1. **Phase 1 (Environment & CA Trust)**: Auto-detects corporate CA certificates (`/etc/ssl/ca-bundle.pem`), activates `NODE_EXTRA_CA_CERTS`, pre-heals bridges, and verifies Playwright Chromium.
2. **Phase 2 (Dual Authentication)**: Re-authenticates Google Cloud standard login and Application Default Credentials (ADC), generating fresh OAuth tokens via automated browser or terminal URL code exchange.
3. **Phase 3 (GCP Provisioning & Quota Binding)**: Verifies GCP project binding, enables required services (`aiplatform.googleapis.com`, `generativelanguage.googleapis.com`, `apikeys.googleapis.com`), and validates keyless Vertex AI REST & SDK connectivity.
4. **Phase 4 (Autonomous MCP Discovery)**: Launches the Autonomous AI Agent across 4 parallel threads, tests candidates through Level 1 (init), Level 1.5 (schema), and Level 3 (functional tool call) gates, and writes clean, standard JSON-RPC 2.0 entries to `.mcp.json` and `/root/.gemini/settings.json`.

#### Post-Run Instant Verification:
Once the command outputs `🚀 All scheduled phases completed successfully! Workspace is ready`:
```bash
# 1. Launch Gemini CLI to inspect active MCP tools:
gemini

# 2. Type /mcp inside Gemini CLI:
# All discovered servers will show as 'connected' and their tools will be instantly available!
```

---

### Scenario B: The Complete Enterprise Engineering Suite (All 14 Servers)
Bootstrap a full developer workspace targeting all SUSE, openSUSE, and Google developer platforms:
```bash
python3 initialize_google_ai.py \
  --gcloud-project "your-gcp-project-id" \
  --mcp-server "github.com,gitlab.suse.de,openqa.suse.de,openqa.opensuse.org,bugzilla.suse.com,bugzilla.opensuse.org,confluence.suse.com,jira.suse.com,sd.suse.com,smelt.suse.de,build.suse.de,build.opensuse.org,progress.opensuse.org,developers.google.com/knowledge/api"
```

### Scenario C: High-Speed Concurrent Discovery (Multi-Worker Execution)
Accelerate multi-server discovery across parallel worker threads with atomic log grouping:
```bash
python3 initialize_google_ai.py \
  --phases 4 \
  --concurrency 4 \
  --mcp-server "build.suse.de,build.opensuse.org,jira.suse.com,confluence.suse.com,sd.suse.com"
```

### Scenario D: Standalone MCP Discovery (Phase 4 Only)
When Python packages and Google Cloud ADC are already initialized on the host, skip Phases 1–3 and run Phase 4 directly:
```bash
python3 initialize_google_ai.py \
  --phases 4 \
  --mcp-server "jira.suse.com,confluence.suse.com"
```

### Scenario E: Quiet Headless VM / Docker Setup (No Browser)
For remote SSH sessions, container builds, or cloud VMs without an X11/Wayland desktop display:
```bash
python3 initialize_google_ai.py \
  --phases 3,4 \
  --gcloud-project "your-gcp-project-id" \
  --mcp-server "github.com,gitlab.suse.de" \
  --no-automatic-browser
```

### Scenario F: Competitive Candidate Evaluation (`--the-best-mcp`)
Direct the AI agent to evaluate all candidates (native, official package, local bridge), test all of them, and commit only the single highest-performing option:
```bash
python3 initialize_google_ai.py \
  --phases 4 \
  --the-best-mcp \
  --mcp-server "jira.suse.com,sd.suse.com"
```

### Scenario G: Multi-Variant Comprehensive Registration (`--exhaustive-mcp`)
Direct the engine to discover and register all working candidates side-by-side using suffix-differentiated keys:
```bash
python3 initialize_google_ai.py \
  --phases 4 \
  --exhaustive-mcp \
  --mcp-server "openqa.opensuse.org,jira.suse.com"
```

### Scenario H: Corporate VPN / High-Latency Network Tuning
Scale socket and subprocess timeouts when operating over corporate VPNs or slow proxy connections:
```bash
python3 initialize_google_ai.py \
  --timeout 300 \
  --timeout-scale 1.5 \
  --mcp-server "build.suse.de,smelt.suse.de"
```

### Scenario I: Verbose Debugging & Custom File Logging
Stream low-level protocol diagnostics to console and capture complete logs to a file:
```bash
python3 initialize_google_ai.py \
  --log-level debug \
  --log-file /tmp/mcp_discovery_debug.log \
  --mcp-server "progress.opensuse.org"
```

### Scenario H: Traditional 100% Offline Procedural Mode (Bypassing AI)
Run discovery without making any remote inference calls to Google Cloud Vertex AI, utilizing purely local Python AST scans and cached packages:
```bash
python3 initialize_google_ai.py \
  --phases 4 \
  --no-agent \
  --mcp-server "build.opensuse.org,bugzilla.opensuse.org"
```

### Scenario I: Safe Dry-Run Simulation (Preview Without Making Changes)
Preview what packages, bridges, and configurations would be created without altering `.mcp.json` or `.env`:
```bash
python3 initialize_google_ai.py --dry-run
```

### Scenario J: Verification of Configured Servers in Gemini CLI
How to verify and interact with the newly configured servers once the run completes:
```bash
# Verify active MCP servers in Gemini CLI
gemini
> /mcp

# Or inspect the generated JSON registry directly
cat .mcp.json
cat ~/.gemini/settings.json
```

---

## 7. Interactive AI Personas & Skills

This repository is pre-configured to automatically inject expert personas and playbooks into supporting CLI interfaces (such as Gemini CLI or Claude Code):

* **The Setup Skill (`SKILL.md`)**:
  Located under `skills/google-ai-initialization/SKILL.md`. It provides CLI interfaces with complete procedural recovery guides, GCloud configuration sequences, and the 12 key system optimizations.
* **The Setup Agent (`google-ai-initializer.md`)**:
  Located under `agents/google-ai-initializer.md`. It configures a custom AI persona targeting `gemini-3.7-flash` equipped with instructions to automatically append headless flags, scan stdout streams for OAuth links, and report verification status checks accurately.

---

## 8. Running Automated Tests

The repository includes a comprehensive, standalone automated test suite (`tests/`) covering unit testing, mocks, schema validation, commit message quality gates, and phase scheduling:

```bash
# Run all unit and integration tests:
python3 run_tests.py

# Or run via Python unittest directly:
python3 -m unittest discover tests -v
```

All 137+ automated tests run completely hermetically and offline without requiring active cloud credentials or network connections.

---

## 9. 🛡️ Continuous Integration & Pull Request Quality Gates

This repository enforces strict CI/CD quality gates on every Pull Request via `.github/workflows/ci.yml`:

1. **📝 Commit Message Title & Non-Empty Body**:
   - Every commit must contain a descriptive title (first line) and an explanatory body.
   - Commits with empty bodies are **rejected by CI**.
2. **🔀 Single-Commit / Squash Enforcement**:
   - Pull requests must contain **exactly 1 commit**. Multi-commit pull requests must be squashed prior to merging (`git rebase -i origin/main`).
3. **🔒 Secret Shielding Audit**:
   - Automated scans ensure no hardcoded API keys, Personal Access Tokens, or private keys are introduced.
4. **🧪 Hermetic Test Execution**:
   - The test suite is executed across Python 3.10, 3.11, and 3.12 runners in headless dry-run isolation.

You can verify your pull request branch locally prior to pushing:
```bash
python3 scripts/validate_pr_commits.py --base origin/main
```

---

## 10. 🔌 Supported Enterprise MCP Server Reference

The orchestrator includes out-of-the-box discovery and automated bridge synthesis for 14 major enterprise and developer services:

| Service | Target Domain | Canonical Key | Default Protocol | Required Credentials / Tokens |
|:---|:---|:---|:---:|:---|
| **Google Developer Knowledge** | `developers.google.com/knowledge/api` | `developers-google-com` | `stdio` (Python Bridge) | `GEMINI_API_KEY` |
| **GitHub** | `github.com` | `github-com` | `stdio` (`npx`) | `GITHUB_PERSONAL_ACCESS_TOKEN` |
| **GitLab SUSE** | `gitlab.suse.de` | `gitlab-suse-de` | `stdio` (Python Bridge) | `GITLAB_SUSE_PAT` |
| **openQA Enterprise** | `openqa.suse.de` | `openqa-suse-de` | `stdio` (Bridge) / `http` | `OPENQA_SUSE_DE_API_KEY`, `OPENQA_SUSE_DE_API_SECRET` |
| **openQA Community** | `openqa.opensuse.org` | `openqa-opensuse-org` | `http` (Native Streamable) | `OPENQA_OPENSUSE_ORG_API_KEY`, `OPENQA_OPENSUSE_ORG_API_SECRET` |
| **Bugzilla SUSE** | `bugzilla.suse.com` | `bugzilla-suse-com` | `stdio` (Python Bridge) | `BUGZILLA_SUSE_API_KEY` |
| **Bugzilla openSUSE** | `bugzilla.opensuse.org` | `bugzilla-opensuse-org` | `stdio` (Python Bridge) | `BUGZILLA_OPENSUSE_API_KEY` |
| **Confluence SUSE** | `confluence.suse.com` | `confluence-suse-com` | `stdio` (`uvx`) | `CONFLUENCE_SUSE_PAT` |
| **Jira SUSE** | `jira.suse.com` | `jira-suse-com` | `stdio` (Python Bridge) | `JIRA_SUSE_PAT` |
| **Service Desk SUSE** | `sd.suse.com` | `sd-suse-com` | `stdio` (Python Bridge) | `SD_SUSE_PAT` |
| **SMELT Maintenance** | `smelt.suse.de` | `smelt-suse-de` | `stdio` (Python Bridge) | None (Public read) |
| **Open Build Service SUSE** | `build.suse.de` | `build-suse-de` | `stdio` (Python Bridge) | `OBS_SUSE_USER`, `OBS_SUSE_PASSWORD` |
| **Open Build Service openSUSE**| `build.opensuse.org` | `build-opensuse-org` | `stdio` (Python Bridge) | `OBS_OPENSUSE_USER`, `OBS_OPENSUSE_PASSWORD` |
| **Redmine / Progress** | `progress.opensuse.org` | `progress-opensuse-org` | `stdio` (Python Bridge) | `REDMINE_OPENSUSE_API_KEY` |

---

## 11. Contributing

Please review [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming conventions, commit standards, and PR submission guidelines. All community participants are expected to adhere to our [Code of Conduct](CODE_OF_CONDUCT.md).

---

## 12. License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.
