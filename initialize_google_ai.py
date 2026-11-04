import os
import sys
import json
import time

# Pre-parse --work-dir to set it in the environment before any local imports run
for i, arg in enumerate(sys.argv):
    if arg == "--work-dir" and i + 1 < len(sys.argv):
        os.environ["WORKSPACE_ROOT_OVERRIDE"] = sys.argv[i + 1]
    elif arg.startswith("--work-dir="):
        os.environ["WORKSPACE_ROOT_OVERRIDE"] = arg.split("=", 1)[1]

# Pre-parse --env-file to set it in the environment before any local imports run
for i, arg in enumerate(sys.argv):
    if arg == "--env-file" and i + 1 < len(sys.argv):
        os.environ["ENV_FILE_PATH_OVERRIDE"] = sys.argv[i + 1]
    elif arg.startswith("--env-file="):
        os.environ["ENV_FILE_PATH_OVERRIDE"] = arg.split("=", 1)[1]

# Pre-parse --timeout to set it in the environment before any local imports run
for i, arg in enumerate(sys.argv):
    if arg == "--timeout" and i + 1 < len(sys.argv):
        os.environ["PROCESS_TIMEOUT_OVERRIDE"] = sys.argv[i + 1]
    elif arg.startswith("--timeout="):
        os.environ["PROCESS_TIMEOUT_OVERRIDE"] = arg.split("=", 1)[1]

# Pre-parse --no-automatic-browser / --headless to set it in the environment before any local imports run
for arg in sys.argv:
    if arg in ("--no-automatic-browser", "--headless"):
        os.environ["NO_AUTOMATIC_BROWSER_OVERRIDE"] = "True"
        break

# Pre-parse --no-allow-service-disabled to set it in the environment before any local imports run
for arg in sys.argv:
    if arg == "--no-allow-service-disabled":
        os.environ["NO_ALLOW_SERVICE_DISABLED_OVERRIDE"] = "True"
        break

import argparse
from utils import setup_logger, WORKSPACE_ROOT, ENV_FILE_PATH, ENV_FILE_EXISTS, get_process_timeout, match_target_to_registry_key, cleanup_runtime_artifacts, is_dry_run, register_temp_path, verify_prephase_disk_space, is_enospc_error, halt_on_enospc, verify_target_dns, ensure_bridges_directory_intact

logger = setup_logger("main")

# Import standard sub-programs
try:
    from phase1_install import main as run_setup
    from phase2_authenticate import run_gcloud_auth
    from phase3_provision import run_gcloud_config
    from phase4_mcp_auto_discovery import run_mcp_discovery
except ImportError as e:
    logger.error(f"Failed to import sub-programs: {e}")
    sys.exit(1)

# Try importing the autonomous AI agent
agent_available = False
try:
    from agent_orchestrator import run_agentic_initialization
    agent_available = True
except Exception as agent_import_err:
    logger.warning(f"⚠️ Note: Autonomous AI Agent orchestrator could not be imported: {agent_import_err}")

def is_agent_ready_to_run() -> tuple:
    """
    Evaluates whether the Autonomous AI Agent has valid authentication credentials,
    checking all supported channels:
      1. Key-based: GEMINI_API_KEY is present and non-empty.
      2. Keyless ADC: ~/.config/gcloud/application_default_credentials.json is present.
      3. Service Account: GOOGLE_APPLICATION_CREDENTIALS file is present.
      4. GCloud CLI Login: check_gcloud_auth_status() returns True.
    """
    if not agent_available:
        return False, "Agent module (agent_orchestrator.py) is not available."

    if os.getenv("GEMINI_API_KEY"):
        return True, "Key-based authentication verified via GEMINI_API_KEY."

    adc_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    if os.path.exists(adc_path) and os.path.getsize(adc_path) > 0:
        return True, f"Keyless Vertex AI authentication verified via ADC ({adc_path})."

    try:
        if check_gcloud_auth_status():
            return True, "Active GCloud CLI authenticated account verified."
    except Exception:
        pass

    return False, "No active GEMINI_API_KEY, GCloud CLI account, or Application Default Credentials (ADC) file found."

def main():
    parser = argparse.ArgumentParser(description="Google AI Workspace & MCP Initialization Orchestrator")
    parser.add_argument(
        "--gcloud-project", 
        help="The target Google Cloud Project ID to configure (overrides active environment file settings).", 
        default=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
    )
    parser.add_argument(
        "--gcloud-location",
        help="Google Cloud Location/Region (default: global)",
        default=os.getenv("GOOGLE_CLOUD_LOCATION") or "global"
    )
    parser.add_argument(
        "--use-enterprise",
        help="Whether to use enterprise SDK (default: True)",
        default=os.getenv("GOOGLE_GENAI_USE_ENTERPRISE") or "True"
    )
    parser.add_argument(
        "--no-allow-service-disabled",
        help="Strict Mode: Disallow Phase 3 from proceeding if GCloud service enablement fails (forces hard failures).",
        action="store_true"
    )
    parser.add_argument(
        "--genai-model",
        help="Specify the Gemini model name to utilize inside the workspace (default: gemini-3.7-flash)",
        default="gemini-3.7-flash"
    )
    parser.add_argument(
        "--mcp-server", 
        help="Comma-separated list of target services/websites/databases for MCP auto-discovery (e.g. bugzilla.suse.com,github.com).", 
        default="bugzilla.suse.com"
    )
    parser.add_argument(
        "--phases", 
        help="Comma-separated list of phases to run (e.g., 1,2 or 1,3,4). Runs all by default.", 
        default="1,2,3,4"
    )
    parser.add_argument(
        "--no-agent",
        help="Force standard procedural execution instead of the autonomous AI agent.",
        action="store_true"
    )
    parser.add_argument(
        "--the-best-mcp",
        help="Pick and write only the single best discovered, validated, and functioning MCP configuration.",
        action="store_true"
    )
    parser.add_argument(
        "--exhaustive-mcp",
        help="Find, validate, and write all discovered and functioning MCP configurations, using distinct descriptive names.",
        action="store_true"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        nargs="?",
        const=4,
        default=None,
        help="Perform Phase 4 MCP auto-discoveries concurrently with a configurable number of parallel workers (defaults to 4 if passed without value)."
    )
    parser.add_argument(
        "--work-dir",
        help="Specify the Google AI Workspace root working directory (defaults to dynamic search)."
    )
    parser.add_argument(
        "--env-file",
        help="Specify the absolute path to a custom environment (.env) file."
    )
    parser.add_argument(
        "--timeout",
        help="Specify the absolute execution timeout in seconds for any spawned subprocesses, installations, or cloud configs (defaults to 180 seconds).",
        type=int,
        default=180
    )
    parser.add_argument(
        "--timeout-scale",
        type=float,
        help="Scale factor to multiply the active subprocess/validation timeout value (must provide a float value)."
    )
    parser.add_argument(
        "--api-key",
        help="Lightweight Gemini API Key to configure directly inside your workspace."
    )
    parser.add_argument(
        "--agent-max-turns",
        type=int,
        help="Specify the maximum conversational tool-calling turns allowed for the Autonomous AI Agent (must provide an integer value)."
    )
    parser.add_argument(
        "--client-file",
        help="Absolute path to your Google Cloud Service Account JSON credentials key file. This file contains secure private keys generated in the GCP Console under IAM -> Service Accounts, enabling fully automated, key-based headless authentication."
    )
    parser.add_argument(
        "--adc-user",
        help="Trigger standard GCloud Application Default Credentials (ADC) login flow instead of standard user login.",
        action="store_true"
    )
    parser.add_argument(
        "--force-auth",
        help="Force GCloud re-authentication even if an active account is already detected (without deleting previous cache files).",
        action="store_true"
    )
    parser.add_argument(
        "--force-auth-with-reset",
        help="Force GCloud re-authentication AND actively delete previous application default credentials files on disk first.",
        action="store_true"
    )
    parser.add_argument(
        "--log-level",
        choices=["trace", "debug", "info", "warning", "error"],
        default="info",
        help="Specify the global execution logging level (default: info, choices: trace, debug, info, warning, error)."
    )
    parser.add_argument(
        "--log-file",
        default="google_ai_initialization.log",
        help="Specify the absolute path to the global execution log file (default: google_ai_initialization.log)."
    )
    parser.add_argument(
        "--no-automatic-browser",
        "--headless",
        dest="no_automatic_browser",
        help="Skip launching Playwright headed browser automatically, forcing manual copy-paste authentication URLs (headless mode).",
        action="store_true"
    )
    parser.add_argument(
        "--force-install",
        help="Force reinstall of all packages and tools even if already installed.",
        action="store_true"
    )
    parser.add_argument(
        "--break-dependencies",
        help="Force install packages ignoring standard conflict blocks (breaks constraints if needed).",
        action="store_true"
    )
    parser.add_argument(
        "--downgrade",
        help="Downgrade and install compatible older stable versions of key libraries.",
        action="store_true"
    )
    parser.add_argument(
        "--dry-run",
        help="Simulate execution through all scheduled phases without installing packages, enabling services, modifying credentials, or creating files.",
        action="store_true"
    )
    parser.add_argument(
        "--do-cleanup",
        dest="cleanup",
        action="store_true",
        default=True,
        help="Clean up all temporary workspaces, directories, and files upon run completion (default: enabled)."
    )
    parser.add_argument(
        "--no-cleanup",
        dest="cleanup",
        action="store_false",
        help="Preserve temporary workspaces, directories, and diagnostic files upon run completion for debugging."
    )
    
    args = parser.parse_args()
    
    if args.dry_run:
        os.environ["DRY_RUN_MODE"] = "1"
        logger.info("🔍 [DRY-RUN SIMULATION] Operating in read-only simulation mode. No permanent system changes will be committed.")
        
    os.environ["CLEANUP_TEMP_FILES"] = "1" if args.cleanup else "0"
    
    # Enforce mutual exclusivity
    if args.the_best_mcp and args.exhaustive_mcp:
        logger.error("🛑 VALUE ERROR: --the-best-mcp and --exhaustive-mcp are mutually exclusive! Please select only one mode.")
        sys.exit(1)
        
    # Sync discovery mode into environment variables for subprocesses & agent
    if args.the_best_mcp:
        os.environ["MCP_MODE_THE_BEST"] = "True"
    elif args.exhaustive_mcp:
        os.environ["MCP_MODE_EXHAUSTIVE"] = "True"
        
    if args.agent_max_turns:
        os.environ["AGENT_MAX_TURNS"] = str(args.agent_max_turns)
        
    # Dynamically scale timeout values globally
    base_timeout = args.timeout if args.timeout else 180
    scale_factor = args.timeout_scale if args.timeout_scale else 1.0
    final_timeout = int(base_timeout * scale_factor)
    os.environ["PROCESS_TIMEOUT_OVERRIDE"] = str(final_timeout)
    
    # Parse selected phases
    try:
        selected_phases = [int(p.strip()) for p in args.phases.split(",") if p.strip()]
    except ValueError:
        logger.error(f"Invalid format for --phases: '{args.phases}'. Use comma-separated integers.")
        sys.exit(1)
        
    logger.info("==========================================================")
    logger.info("  GOOGLE AI DEVELOPMENT WORKSPACE INITIALIZER")
    logger.info("==========================================================")
    logger.info(f"Workspace Root    : {WORKSPACE_ROOT}")
    logger.info(f"Environment File  : {ENV_FILE_PATH}")
    logger.info(f"Target Project ID : {args.gcloud_project}")
    logger.info(f"Location/Region   : {args.gcloud_location}")
    logger.info(f"Use Enterprise    : {args.use_enterprise}")
    logger.info(f"GenAI Model       : {args.genai_model}")
    logger.info(f"MCP Discovery Servers: {args.mcp_server}")
    logger.info(f"The Best MCP Mode : {'Yes' if args.the_best_mcp else 'No'}")
    logger.info(f"Exhaustive MCP Mode: {'Yes' if args.exhaustive_mcp else 'No'}")
    logger.info(f"Agent Max Turns   : {args.agent_max_turns if args.agent_max_turns else '20 (Default)'}")
    logger.info(f"Phases Scheduled    : {selected_phases}")
    logger.info(f"Dry-Run Mode        : {'Yes (Simulation Only)' if args.dry_run else 'No'}")
    logger.info(f"Auto Cleanup        : {'Yes (Default)' if args.cleanup else 'No (--no-cleanup)'}")
    logger.info(f"Force Reinstall   : {args.force_install}")
    logger.info(f"Break Dependencies: {args.break_dependencies}")
    logger.info(f"Downgrade Versions: {args.downgrade}")
    if args.force_auth_with_reset:
        logger.info("Force Auth Mode   : Yes (With Cache Reset - Fresh Login)")
    elif args.force_auth:
        logger.info("Force Auth Mode   : Yes (Preserving Existing Account Info)")
    else:
        logger.info("Force Auth Mode   : No")
    logger.info(f"No Auto-Browser   : {'Yes' if args.no_automatic_browser else 'No'}")
    logger.info(f"Strict APIs Mode  : {'Yes' if args.no_allow_service_disabled else 'No (Permissive Mode)'}")
    logger.info(f"Base Timeout      : {base_timeout} seconds")
    logger.info(f"Timeout Scale     : {scale_factor}x")
    logger.info(f"Scaled Timeout    : {final_timeout} seconds")
    if args.api_key:
        logger.info("Authentication Mode: Gemini API Key (Direct)")
    elif args.client_file:
        logger.info(f"Authentication Mode: Service Account Key JSON ({args.client_file})")
    elif args.adc_user:
        logger.info("Authentication Mode: GCloud Application Default Credentials (ADC)")
    else:
        logger.info("Authentication Mode: Standard User Browser Authentication")
    logger.info("==========================================================")
    
    results = {p: "Skipped" for p in (1, 2, 3, 4)}
    
    # 1. ALWAYS run Phase 1 locally to download & verify all prerequisites (Python packages, gcloud, Playwright, CLIs)
    if 1 in selected_phases:
        if not verify_prephase_disk_space("Phase 1: Environment Setup", log=logger):
            logger.error("🛑 Aborting run before Phase 1 due to critical filesystem space exhaustion.")
            sys.exit(1)
        logger.info("\n--- Running Phase 1: Environment Setup ---")
        try:
            results[1] = run_setup(
                force_install=args.force_install,
                break_dependencies=args.break_dependencies,
                downgrade=args.downgrade,
                timeout=args.timeout
            )
        except Exception as e:
            logger.error(f"Phase 1 execution threw an unhandled exception: {e}")
            results[1] = False
    else:
        results[1] = "Skipped"
        
    # Check if Phase 1 succeeded (or was skipped). If failed, we cannot continue safely.
    if results[1] is False:
        logger.error("======================================================================")
        logger.error("🛑 FATAL ERROR: PHASE 1 ENVIRONMENT SETUP FAILED")
        logger.error("======================================================================")
        logger.error("Critical package installations, CLI setups, or workspace symlinks failed.")
        logger.error("To prevent unstable execution and broken states, we must immediately halt.")
        logger.error("----------------------------------------------------------------------")
        logger.error("REMEDIATION STEPS:")
        logger.error("1. Check your network connection to ensure GCloud, NPM, or Pip can download.")
        logger.error("2. Run GCloud and Pip installers with --break-dependencies if in system-managed python.")
        logger.error("3. Inspect the 'google_ai_initialization.log' file for specific traceback logs.")
        logger.error("======================================================================")
        sys.exit(1)

    # 1.5. Dynamic Environment & Credentials Verification Safeguard
    remaining_phases = [p for p in selected_phases if p in (2, 3, 4)]
    if remaining_phases and not ENV_FILE_EXISTS:
        # Check if we can confirm existing active credentials
        try:
            from phase2_authenticate import check_gcloud_auth_status
            auth_confirmed = check_gcloud_auth_status() or os.getenv("GEMINI_API_KEY") is not None
        except Exception:
            auth_confirmed = False
            
        is_initializing_now = bool(2 in selected_phases or args.api_key or args.client_file or args.adc_user)
        
        if not auth_confirmed and not is_initializing_now:
            logger.error("======================================================================")
            logger.error("🛑 FATAL ERROR: MISSING ENVIRONMENT CONFIGURATION")
            logger.error("======================================================================")
            logger.error(f"Your environment (.env) file was not found at: {ENV_FILE_PATH}")
            logger.error("No active Google Cloud account or GEMINI_API_KEY is currently present.")
            logger.error("We cannot proceed safely as subsequent setup steps require active credentials.")
            logger.error("----------------------------------------------------------------------")
            logger.error("REMEDIATION STEPS:")
            logger.error("1. Place a valid .env file in your active workspace directory.")
            logger.error("2. Specify the absolute path to your env file using: --env-file <path>")
            logger.error("3. Run with Phase 2 enabled (--phases 1,2,3) to generate a new .env automatically.")
            logger.error("======================================================================")
            sys.exit(1)
        else:
            logger.warning(f"⚠️ Warning: Environment (.env) file not found at: {ENV_FILE_PATH}")
            logger.warning("Active authentication is confirmed in memory. Continuing setup...")

    # 2. RUN WORKSPACE CONFIGURATION PHASES (1, 2, 3) PROCEDURALLY
    # Running Phases 1, 2, and 3 procedurally guarantees absolute reliability for bootstrap,
    # as authentication requires secure browser UI interactions and configuration sets CLI parameters.
    
    # Run Phase 2
    if 2 in selected_phases:
        if not verify_prephase_disk_space("Phase 2: Google Cloud Authentication", log=logger):
            logger.error("🛑 Aborting run before Phase 2 due to critical filesystem space exhaustion.")
            sys.exit(1)
        logger.info("\n--- Running Phase 2: Google Cloud Authentication ---")
        try:
            results[2] = run_gcloud_auth(
                force_auth=args.force_auth,
                force_auth_with_reset=args.force_auth_with_reset,
                api_key=args.api_key,
                client_file=args.client_file,
                adc_user=args.adc_user
            )
        except Exception as e:
            logger.error(f"Phase 2 execution threw an unhandled exception: {e}")
            results[2] = False
    else:
        results[2] = "Skipped"
        
    # Check if Phase 2 succeeded. If failed, we cannot continue safely.
    if results[2] is False:
        logger.error("======================================================================")
        logger.error("🛑 FATAL ERROR: PHASE 2 AUTHENTICATION FAILED")
        logger.error("======================================================================")
        logger.error("Your chosen credentials setup (API key, service account, or browser login) failed.")
        logger.error("We must halt now to prevent cascading configuration failures in subsequent phases.")
        logger.error("----------------------------------------------------------------------")
        logger.error("REMEDIATION STEPS:")
        logger.error("1. Check the console output above for the specific error details.")
        logger.error("2. Verify that your service account JSON file exists and is structurally valid.")
        logger.error("3. If using standard login, ensure you have display environments or follow terminal prompts.")
        logger.error("======================================================================")
        sys.exit(1)
        
    # Run Phase 3
    if 3 in selected_phases:
        if not verify_prephase_disk_space("Phase 3: Project Configuration & Services", log=logger):
            logger.error("🛑 Aborting run before Phase 3 due to critical filesystem space exhaustion.")
            sys.exit(1)
        logger.info("\n--- Running Phase 3: Google Cloud Configuration & Services ---")
        try:
            results[3] = run_gcloud_config(args.gcloud_project, args.gcloud_location, args.use_enterprise, no_allow_service_disabled=args.no_allow_service_disabled, genai_model=args.genai_model)
        except Exception as e:
            logger.error(f"Phase 3 execution threw an unhandled exception: {e}")
            results[3] = False
    else:
        results[3] = "Skipped"
        
    # Check if Phase 3 succeeded. If failed, we cannot continue safely.
    if results[3] is False:
        logger.error("======================================================================")
        logger.error("🛑 FATAL ERROR: PHASE 3 PROJECT CONFIGURATION FAILED")
        logger.error("======================================================================")
        logger.error("We failed to configure GCloud defaults or enable mandatory API services.")
        logger.error("Subsequent phases (such as Phase 4 MCP Auto-Discovery) cannot run safely.")
        logger.error("----------------------------------------------------------------------")
        logger.error("REMEDIATION STEPS:")
        logger.error(f"1. Verify that GCloud billing is enabled on your project '{args.gcloud_project}'.")
        logger.error("2. Check if your authenticated account has 'Service Usage Admin' or 'Owner' permissions.")
        logger.error("3. Inspect 'google_ai_initialization.log' for detailed service enablement tracebacks.")
        logger.error("======================================================================")
        sys.exit(1)
        
    # Phase 4: MCP Auto-Discovery & Live Connection Handshake
    # This is a highly creative, dynamic task requiring web searches, dynamic credential injections,
    # and live connection handshakes—making it the PERFECT candidate for our Autonomous AI Agent!
    if 4 in selected_phases:
        if not verify_prephase_disk_space("Phase 4: MCP Auto-Discovery", log=logger):
            logger.error("🛑 Aborting run before Phase 4 due to critical filesystem space exhaustion.")
            sys.exit(1)
        # Preserve pre-existing .mcp.json and dynamically merge discoveries to prevent data loss or staleness
        mcp_path = os.path.join(WORKSPACE_ROOT, ".mcp.json")
        if os.path.exists(mcp_path):
            logger.info("- Status : Pre-existing .mcp.json file detected; maintaining all previous configurations and merging newly discovered servers inline.")
        else:
            logger.info("- Status : No pre-existing .mcp.json file found; a new registry will be initialized dynamically.")

        logger.info("\n==========================================================")
        logger.info("🔑  [PREPARATION] Phase 4 MCP Credentials Checklist")
        logger.info("==========================================================")
        logger.info("MCP auto-discovery may require authentication tokens, API keys,")
        logger.info("or secrets (such as BUGZILLA_API_KEY, GITHUB_TOKEN, etc.).")
        logger.info("  1. We recommend saving these inside your active .env file first.")
        logger.info("  2. If they are missing, the program will prompt you to enter")
        logger.info("     them manually, and will automatically persist them back to .env.")
        logger.info("Please have your service credentials ready!")
        logger.info("==========================================================\n")
        
        # Ensure bridges/ directory and standard bridge scripts are intact before starting Phase 4
        try:
            ensure_bridges_directory_intact(log=logger)
        except Exception:
            pass

        # Continuous Master Registry Accumulator: Prevents rogue mid-run server overwrites
        mcp_json_path = os.path.join(WORKSPACE_ROOT, ".mcp.json")
        global_settings_path = os.path.expanduser("~/.gemini/settings.json")
        master_mcp_servers = {}
        from phase4_mcp_auto_discovery import mcp_write_lock

        def sync_master_mcp_registry(log=logger):
            """Ensures all previously validated servers are preserved in master_mcp_servers and .mcp.json (Thread-safe)."""
            nonlocal master_mcp_servers
            with mcp_write_lock:
                disk_servers = {}
                if os.path.exists(mcp_json_path):
                    try:
                        with open(mcp_json_path, "r", encoding="utf-8") as f:
                            disk_data = json.load(f)
                        if isinstance(disk_data, dict) and "mcpServers" in disk_data:
                            disk_servers = disk_data["mcpServers"]
                    except Exception:
                        pass
                
                missing_keys = set(master_mcp_servers.keys()) - set(disk_servers.keys())
                if missing_keys:
                    log.warning(f"🛡️  [REGISTRY HEAL] Detected {len(missing_keys)} missing servers in .mcp.json ({', '.join(missing_keys)}). Auto-restoring from in-memory master accumulator...")
                    disk_servers.update(master_mcp_servers)
                    try:
                        with open(mcp_json_path, "w", encoding="utf-8") as f:
                            json.dump({"mcpServers": disk_servers}, f, indent=2)
                    except Exception as err:
                        log.error(f"Failed to auto-restore .mcp.json: {err}")
                else:
                    master_mcp_servers.update(disk_servers)

                if os.path.exists(global_settings_path) and master_mcp_servers:
                    try:
                        with open(global_settings_path, "r", encoding="utf-8") as gf:
                            g_data = json.load(gf)
                        if "mcpServers" not in g_data:
                            g_data["mcpServers"] = {}
                        g_data["mcpServers"].update(master_mcp_servers)
                        with open(global_settings_path, "w", encoding="utf-8") as gf:
                            json.dump(g_data, gf, indent=2)
                    except Exception:
                        pass

        # Pre-load initial master registry
        sync_master_mcp_registry()

        use_agent = False
        if not args.no_agent:
            agent_ready, auth_reason = is_agent_ready_to_run()
            if agent_ready:
                use_agent = True
                logger.info(f"🔑 [AGENT READINESS] Autonomous AI Agent ready: {auth_reason}")
            else:
                logger.info(f"ℹ️  [AUTH STATUS] {auth_reason} Defaulting to standard procedural mode.")
                
        if use_agent:
            logger.info("\n==========================================================")
            logger.info("🤖 DELEGATING MCP DISCOVERY TO AUTONOMOUS AI AGENT")
            logger.info("==========================================================")
            logger.info("The agent will now use Playwright and Google Search Grounding")
            logger.info("to discover your MCP server config and test its connections.")
            logger.info("==========================================================\n")
            
            # Sync command-line arguments to environment variables for the agent process
            if args.gcloud_project:
                os.environ["GOOGLE_CLOUD_PROJECT"] = args.gcloud_project
            if args.gcloud_location:
                os.environ["GOOGLE_CLOUD_LOCATION"] = args.gcloud_location
            if args.use_enterprise:
                os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = args.use_enterprise
            if args.concurrency:
                os.environ["CONCURRENCY_ACTIVE"] = "1"

            try:
                servers_to_discover = [s.strip() for s in args.mcp_server.split(",") if s.strip()]
                all_servers_success = True
                phase4_discovery_results = {}

                # Dynamic Concurrency Check
                concurrency = args.concurrency
                if concurrency:
                    import concurrent.futures
                    from phase4_mcp_auto_discovery import ServerLogBuffer
                    logger.info(f"⚡ Concurrency enabled: Running {len(servers_to_discover)} AI Agent discoveries in parallel (max workers: {concurrency})...")
                    
                    def agent_worker(server):
                        buffer = ServerLogBuffer(server, logger_name="agent_orchestrator")
                        if not verify_target_dns(server):
                            buffer.warning(f"⚠️ [DNS WARNING] Pre-flight DNS resolution for target domain '{server}' was slow or unresolvable via local DNS. Proceeding with agent discovery anyway...")

                        try:
                            res = run_agentic_initialization(args.gcloud_project, server, log=buffer)
                            if not res:
                                sync_master_mcp_registry()
                                matched_key, _ = match_target_to_registry_key(server, master_mcp_servers)
                                if matched_key:
                                    res = True
                            buffer.flush()
                            return server, res
                        except Exception as err:
                            buffer.error(f"AI Agent worker thread crashed: {err}")
                            buffer.flush()
                            return server, False
                            
                    agent_mode_results = {}
                    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                        futures = {}
                        for idx, s in enumerate(servers_to_discover):
                            futures[executor.submit(agent_worker, s)] = s
                            if idx < len(servers_to_discover) - 1:
                                time.sleep(1.0)
                        watchdog_timeout = max(900, int(get_process_timeout() * len(servers_to_discover) * 2.0))
                        try:
                            for future in concurrent.futures.as_completed(futures, timeout=watchdog_timeout):
                                server, success = future.result()
                                sync_master_mcp_registry()
                                if success:
                                    agent_mode_results[server] = "✅ SUCCESS"
                                    phase4_discovery_results[server] = "✅ SUCCESS"
                                else:
                                    agent_mode_results[server] = "❌ FAILED"
                                    phase4_discovery_results[server] = "❌ FAILED"
                        except concurrent.futures.TimeoutError:
                            logger.error("🛑 [CONCURRENCY WATCHDOG] Concurrency pool reached execution deadline. Reclaiming worker threads...")
                            for fut, s in futures.items():
                                if not fut.done():
                                    fut.cancel()
                                    agent_mode_results[s] = "❌ TIMED_OUT"
                                    phase4_discovery_results[s] = "❌ TIMED_OUT"
                else:
                    # Standard sequential loop
                    agent_mode_results = {}
                    for i, server in enumerate(servers_to_discover, 1):
                        logger.info(f"\n──────────────────────────────────────────────")
                        logger.info(f"🔍 [SERVER {i}/{len(servers_to_discover)}] Discovering: {server}")
                        logger.info(f"──────────────────────────────────────────────")
                        agent_success = run_agentic_initialization(args.gcloud_project, server)
                        if not agent_success:
                            sync_master_mcp_registry()
                            matched_key, _ = match_target_to_registry_key(server, master_mcp_servers)
                            if matched_key:
                                logger.info(f"🎉 [AGENT RECONCILIATION] Confirmed target '{server}' registered as '{matched_key}'!")
                                agent_success = True
                        if agent_success:
                            agent_mode_results[server] = "✅ SUCCESS"
                            phase4_discovery_results[server] = "✅ SUCCESS"
                        else:
                            agent_mode_results[server] = "❌ FAILED"
                            phase4_discovery_results[server] = "❌ FAILED"
                        # Continuous Accumulator Synchronization after each server
                        sync_master_mcp_registry()
                        
                # Final registry sync before summary report
                sync_master_mcp_registry()
                
                # Dedicated summary for Autonomous Agent Mode
                logger.info("\n==========================================================")
                logger.info("🤖  AUTONOMOUS AGENT DISCOVERY SUMMARY")
                logger.info("==========================================================")
                for server, state in agent_mode_results.items():
                    logger.info(f"  + {server:<35} : {state}")
                logger.info("==========================================================\n")
                
                failed_targets = [s for s, state in agent_mode_results.items() if "SUCCESS" not in state]
                if not failed_targets:
                    logger.info("🚀 Autonomous Agent successfully completed all scheduled MCP discoveries!")
                    results[4] = True
                    all_servers_success = True
                else:
                    logger.warning(f"⚠️ Autonomous Agent finished with failures on {len(failed_targets)} server(s) ({', '.join(failed_targets)}). Engaging procedural recovery strictly for failed targets...")
                    use_agent = False
                    procedural_servers_to_retry = failed_targets
            except Exception as agent_err:
                logger.warning(f"⚠️ Autonomous Agent crashed: {agent_err}. Falling back to standard procedural mode...")
                use_agent = False
                procedural_servers_to_retry = [s.strip() for s in args.mcp_server.split(",") if s.strip()]
                
        if not use_agent:
            logger.info("\n--- Running Phase 4 in Standard Procedural Mode ---")
            try:
                servers_to_discover = procedural_servers_to_retry if 'procedural_servers_to_retry' in locals() else [s.strip() for s in args.mcp_server.split(",") if s.strip()]
                logger.info(f"Targets queued for procedural recovery ({len(servers_to_discover)}): {', '.join(servers_to_discover)}")
                if 'phase4_discovery_results' not in locals() or phase4_discovery_results is None:
                    phase4_discovery_results = {}
                
                # Dynamic Concurrency Check
                concurrency = args.concurrency
                if concurrency:
                    import concurrent.futures
                    from phase4_mcp_auto_discovery import ServerLogBuffer
                    max_workers_proc = min(concurrency, max(1, len(servers_to_discover)))
                    logger.info(f"⚡ Concurrency enabled: Running {len(servers_to_discover)} procedural discoveries in parallel (max workers: {max_workers_proc})...")
                    
                    def proc_worker(server):
                        buffer = ServerLogBuffer(server)
                        try:
                            res = run_mcp_discovery(server, log=buffer)
                            buffer.flush()
                            return server, res
                        except Exception as err:
                            buffer.error(f"Procedural worker thread crashed: {err}")
                            buffer.flush()
                            return server, False
                            
                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers_proc) as executor:
                        futures = {executor.submit(proc_worker, s): s for s in servers_to_discover}
                        for future in concurrent.futures.as_completed(futures):
                            server, success = future.result()
                            phase4_discovery_results[server] = "✅ SUCCESS" if success else "❌ FAILED"
                else:
                    # Standard sequential loop
                    for i, server in enumerate(servers_to_discover, 1):
                        logger.info(f"\n──────────────────────────────────────────────")
                        logger.info(f"🔍 [PROCEDURAL SERVER {i}/{len(servers_to_discover)}] Sourcing: {server}")
                        logger.info(f"──────────────────────────────────────────────")
                        res = run_mcp_discovery(server)
                        phase4_discovery_results[server] = "✅ SUCCESS" if res else "❌ FAILED"
                        
                sync_master_mcp_registry()
                all_servers_success = all("SUCCESS" in state for state in phase4_discovery_results.values())
                results[4] = all_servers_success
                
                # Print procedural recovery summary if recovery was invoked
                if 'procedural_servers_to_retry' in locals() and procedural_servers_to_retry:
                    logger.info("\n==========================================================")
                    logger.info("🔧  PROCEDURAL RECOVERY EXECUTION SUMMARY")
                    logger.info("==========================================================")
                    for server in procedural_servers_to_retry:
                        state = phase4_discovery_results.get(server, "❌ FAILED")
                        status_str = "✅ RECOVERED" if "SUCCESS" in state else "❌ FAILED"
                        logger.info(f"  + {server:<35} : {status_str}")
                    logger.info("==========================================================\n")

                # Print a consolidated execution summary for Phase 4 targets
                logger.info("\n==========================================================")
                logger.info("📡  PHASE 4: MCP AUTO-DISCOVERY EXECUTION SUMMARY")
                logger.info("==========================================================")
                for server, state in phase4_discovery_results.items():
                    logger.info(f"  + {server:<35} : {state}")
                logger.info("==========================================================\n")
            except Exception as e:
                logger.error(f"Phase 4 execution threw an unhandled exception: {e}")
                results[4] = False
            
    # Print unified report
    logger.info("\n==========================================================")
    logger.info("  INITIALIZATION STATUS SUMMARY REPORT")
    logger.info("==========================================================")
    
    phase_names = {
        1: "Phase 1: Environment Setup (NPM, Python, agy, gcloud)",
        2: "Phase 2: Google Cloud Authentication",
        3: "Phase 3: Project Config & API Services Enablement",
        4: "Phase 4: MCP Server Discovery, Settings, & Live Handshake"
    }
    
    all_passed = True
    for phase in sorted(phase_names.keys()):
        status = results.get(phase)
        if status is True:
            logger.info(f"[  PASS  ] {phase_names[phase]}")
        elif status == "Skipped":
            logger.info(f"[ SKIPPED ] {phase_names[phase]}")
        else:
            logger.info(f"[  FAIL  ] {phase_names[phase]}")
            all_passed = False
            
    logger.info("==========================================================")
    logger.info(f"Log output recorded in: {os.path.abspath('google_ai_initialization.log')}")
    
    # Check if .mcp.json exists and read it to output a beautiful, consolidated discovery report
    mcp_path = os.path.join(WORKSPACE_ROOT, ".mcp.json")
    if os.path.exists(mcp_path):
        try:
            with open(mcp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                servers = data.get("mcpServers", {})
                if servers:
                    logger.info("\n==========================================================")
                    logger.info("📦  CONSOLIDATED MCP REGISTRY DISCOVERY REPORT")
                    logger.info("==========================================================")
                    for name, config in list(servers.items()):
                        cmd = config.get("command", "")
                        args_list = config.get("args", [])
                        args_str = " ".join(args_list) if isinstance(args_list, list) else str(args_list)
                        env_keys = ", ".join(config.get("env", {}).keys())
                        instructions = config.get("instructions", "") or config.get("_instructions", "")
                        
                        # Dynamically verify if this server actually passed during our execution loops
                        # We do this by checking if the name in .mcp.json matches any of our known FAILED targets
                        is_verified = True
                        if 'phase4_discovery_results' in locals():
                            for failed_url, state in phase4_discovery_results.items():
                                if "FAILED" in state:
                                    matched_failed_key, _ = match_target_to_registry_key(failed_url, {name: config})
                                    if matched_failed_key:
                                        is_verified = False
                                        break
                                        
                        logger.info(f"🔹 Server Name   : {name}")
                        logger.info(f"   Command-Line  : {cmd} {args_str[:70]}...")
                        logger.info(f"   Credentials   : {env_keys if env_keys else 'None required'}")
                        if is_verified:
                            logger.info(f"   Handshake     : ✅ ACTIVE & VERIFIED")
                        else:
                            logger.info(f"   Handshake     : ❌ FAILED (Unverified/Broken Configuration)")
                            
                        if instructions:
                            logger.info("   📋 Extra Setup Instructions:")
                            for line in instructions.strip().splitlines():
                                logger.info(f"      | {line}")
                        logger.info("  --------------------------------------------------------")

                    # Target Reconciliation Audit: compare every scheduled target against .mcp.json
                    scheduled_targets = [s.strip() for s in args.mcp_server.split(",") if s.strip()] if args.mcp_server else []
                    if scheduled_targets:
                        logger.info("----------------------------------------------------------")
                        logger.info("📋  TARGET RECONCILIATION AUDIT (Scheduled vs Registered)")
                        logger.info("----------------------------------------------------------")
                        for tgt in scheduled_targets:
                            matched_key, _ = match_target_to_registry_key(tgt, servers)
                            if matched_key:
                                logger.info(f"  + {tgt:<37} -> {matched_key:<32} [✅ REGISTERED]")
                            else:
                                logger.warning(f"  + {tgt:<37} -> ❌ MISSING FROM .mcp.json REGISTRY!")
                        logger.info("==========================================================")
                    else:
                        logger.info("==========================================================")
        except Exception as e:
            logger.warning(f"Could not parse .mcp.json for diagnostic summary: {e}")
            
    # Print prominent Registry Location Pointer showing where the settings file is located
    logger.info("\n==========================================================")
    logger.info("📂  MODEL CONTEXT PROTOCOL REGISTRY LOCATION")
    logger.info("==========================================================")
    logger.info("Your compiled, functional MCP registry file is located at:")
    logger.info(f"    👉  {os.path.abspath(mcp_path)}")
    logger.info("  --------------------------------------------------------")
    logger.info("  You can copy and use this file directly to register these")
    logger.info("  custom server tools inside your Gemini CLI, Claude Code,")
    logger.info("  Cursor, or VS Code settings files to enable them!")
    logger.info("==========================================================\n")
    
    # Execute automated cleanup of temporary runtime artifacts
    cleanup_runtime_artifacts(enabled=args.cleanup, log=logger)

    if all_passed:
        logger.info("🚀 All scheduled phases completed successfully! Workspace is ready.")
        sys.exit(0)
    else:
        logger.error("❌ One or more scheduled phases failed. Please inspect the log file above.")
        sys.exit(1)

if __name__ == "__main__":
    main()
