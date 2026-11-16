import os
import sys
import json
import subprocess
import argparse

# Pre-parse --no-automatic-browser to set it in the environment before any local imports run
for arg in sys.argv:
    if arg == "--no-automatic-browser":
        os.environ["NO_AUTOMATIC_BROWSER_OVERRIDE"] = "True"
        break

# Pre-parse --no-allow-service-disabled to set it in the environment before any local imports run
for arg in sys.argv:
    if arg == "--no-allow-service-disabled":
        os.environ["NO_ALLOW_SERVICE_DISABLED_OVERRIDE"] = "True"
        break

from utils import setup_logger, with_retry, WORKSPACE_ROOT, ENV_FILE_PATH, ENV_FILE_EXISTS, get_process_timeout, is_dry_run, cleanup_runtime_artifacts, register_temp_path, verify_prephase_disk_space, is_enospc_error, halt_on_enospc, resolve_gcloud_bin

logger = setup_logger("phase3_provision")
GCLOUD_BIN = resolve_gcloud_bin()

REQUIRED_SERVICES = [
    "aiplatform.googleapis.com",          # Vertex AI API
    "generativelanguage.googleapis.com",  # Gemini API
    "apikeys.googleapis.com",             # API Keys API
]

HAS_ATTEMPTED_REAUTH = False

def align_active_gcloud_account() -> bool:
    """
    Detects the active authenticated account from 'gcloud auth list' and
    explicitly aligns GCloud's active configuration profile to use it,
    mimicking Google's standard setup_adc.sh account alignment block.
    """
    logger.info("- Status : Aligning GCloud active configuration account profile...")
    code, stdout, _ = run_command([
        GCLOUD_BIN, "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"
    ])
    if code == 0 and stdout.strip():
        active_account = stdout.strip().splitlines()[0].strip()
        logger.info(f"  + Detected active authenticated account: '{active_account}'")
        # Explicitly set active account to prevent stale/different profile locks
        code, _, _ = run_command([GCLOUD_BIN, "config", "set", "account", active_account])
        if code == 0:
            logger.info(f"  + GCloud CLI config account aligned to '{active_account}' successfully.")
            return True
    return False

def run_command(cmd, input_data=None):
    """Utility to run a CLI command and return exit code, stdout, and stderr, with dynamic self-healing reauth on token expiration."""
    if is_dry_run() and any(action in cmd for action in ("set", "enable")):
        logger.info(f"🔍 [DRY-RUN] Simulating GCloud command: {' '.join(cmd)} (skipped execution).")
        return 0, "dry-run-success", ""
    global HAS_ATTEMPTED_REAUTH
    logger.info(f"Running: {' '.join(cmd)}")
    try:
        res = subprocess.run(
            cmd,
            input=input_data,
            capture_output=True,
            text=True,
            check=False,
            timeout=get_process_timeout()
        )
        
        stdout_str = res.stdout.strip() if res.stdout else ""
        stderr_str = res.stderr.strip() if res.stderr else ""
        if res.returncode != 0 and is_enospc_error(stderr_str):
            halt_on_enospc(f"GCloud execution: {' '.join(cmd)}", stderr_str, log=logger)
        # Check if command failed due to expired GCloud authentication tokens
        if res.returncode != 0 and ("Reauthentication failed" in stderr_str or "gcloud auth login" in stderr_str):
            if not HAS_ATTEMPTED_REAUTH:
                logger.warning("------------------------------------------------------")
                logger.warning("⚠️  [EXPIRED CREDENTIALS] Detected expired or missing GCloud session tokens!")
                logger.warning("- Status : Initiating self-healing re-authentication (Phase 2)...")
                logger.warning("------------------------------------------------------")
                HAS_ATTEMPTED_REAUTH = True
                
                try:
                    # Dynamically import and run Phase 2 Authentication!
                    from phase2_authenticate import run_gcloud_auth
                    auth_success = run_gcloud_auth(force_auth=True)
                    if auth_success:
                        logger.info("- Status : Re-authentication completed successfully! Aligning accounts and retrying...")
                        # Dynamic Account alignment prior to retry
                        align_active_gcloud_account()
                        # Re-run the command on a clean slate!
                        return run_command(cmd, input_data)
                    else:
                        logger.error("❌ Self-healing re-authentication failed.")
                except Exception as auth_err:
                    logger.error(f"❌ Failed to trigger self-healing re-authentication: {auth_err}")
            else:
                logger.error("❌ Re-authentication was already attempted but tokens are still invalid.")
                
        if res.returncode != 0:
            logger.warning(f"Command returned non-zero exit code {res.returncode}")
            if res.stderr:
                logger.warning(f"Error output: {res.stderr.strip()}")
                
        return res.returncode, res.stdout.strip() if res.stdout else "", stderr_str
    except Exception as e:
        logger.error(f"Failed to execute command '{cmd}': {e}")
        return -1, "", str(e)

def get_current_gcloud_project() -> str:
    """Gets the currently set project in gcloud configuration."""
    _, stdout, _ = run_command([GCLOUD_BIN, "config", "get-value", "project"])
    return stdout if stdout and "(unset)" not in stdout else ""

@with_retry(max_attempts=3, backoff_seconds=5)
def set_default_project(project_id: str) -> bool:
    """Sets the default Google Cloud project with clean telemetry."""
    logger.info(f"- Status : Setting default GCloud CLI active project to '{project_id}'...")
    code, _, _ = run_command([GCLOUD_BIN, "config", "set", "project", project_id])
    if code == 0:
        logger.info(f"  + Active project set to '{project_id}' successfully.")
        return True
    return False

@with_retry(max_attempts=3, backoff_seconds=5)
def set_quota_project(project_id: str) -> bool:
    """Sets the quota project for application-default credentials with clean logs."""
    logger.info(f"- Status : Setting Application Default Credentials (ADC) billing/quota project...")
    code, _, _ = run_command([GCLOUD_BIN, "auth", "application-default", "set-quota-project", project_id])
    if code == 0:
        logger.info(f"  + ADC quota billing project set to '{project_id}' successfully.")
        return True
    return False

@with_retry(max_attempts=3, backoff_seconds=5)
def set_billing_quota_project(project_id: str) -> bool:
    """Sets GCloud active CLI config billing/quota_project default with clean logs."""
    logger.info(f"- Status : Setting GCloud CLI active config billing/quota_project to '{project_id}'...")
    code, _, _ = run_command([GCLOUD_BIN, "config", "set", "billing/quota_project", project_id])
    if code == 0:
        logger.info(f"  + GCloud CLI billing/quota_project set to '{project_id}' successfully.")
        return True
    return False

@with_retry(max_attempts=3, backoff_seconds=10)
def enable_service_with_retry(service_name: str, project_id: str) -> bool:
    """Enables a specific API service in Google Cloud with descriptive outputs."""
    if is_dry_run():
        logger.info(f"🔍 [DRY-RUN] Simulating API service enablement: '{service_name}' on '{project_id}' (skipped).")
        return True
    logger.info(f"- Action : Enabling API service: {service_name}...")
    code, stdout, stderr = run_command([
        GCLOUD_BIN, "services", "enable", service_name, f"--project={project_id}"
    ])
    if code == 0:
        logger.info(f"  + Successfully enabled API: {service_name}")
        return True
    return False

import urllib.request
import json

def verify_vertex_ai_access(project_id: str, genai_model: str = "gemini-3.7-flash") -> bool:
    """
    Natively verifies keyless Vertex AI access using GCloud's active access token,
    mimicking Google's standard setup_adc.sh REST/cURL verification flow.
    """
    logger.info("------------------------------------------------------")
    logger.info("📡 [TESTING] Verifying Keyless Vertex AI REST Connection...")
    logger.info("------------------------------------------------------")
    
    # 1. Fetch active access token from GCloud
    try:
        res = subprocess.run(
            [GCLOUD_BIN, "auth", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=10
        )
        token = res.stdout.strip()
        if not token:
            logger.warning("⚠️  Could not retrieve GCloud access token. GCloud is not authenticated.")
            return False
    except Exception as err:
        logger.warning(f"⚠️  Failed to query gcloud access token: {err}")
        return False
        
    # 2. Build direct Vertex AI POST request dynamically targeting the specified model
    url = f"https://aiplatform.googleapis.com/v1/projects/{project_id}/locations/global/publishers/google/models/{genai_model}:generateContent"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    data = {
        "contents": [{
            "role": "user",
            "parts": [{"text": "Reply ONLY with the word SUCCESS"}]
        }]
    }
    
    # 3. Send request using urllib (standard library, zero external dependencies!)
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        # Use a short timeout of 15 seconds to prevent hanging
        with urllib.request.urlopen(req, timeout=15) as response:
            res_body = response.read().decode("utf-8")
            # If the response contains SUCCESS, we are 100% verified and active!
            if "SUCCESS" in res_body.upper() or "candidates" in res_body or "text" in res_body:
                logger.info("🎉 [SUCCESS] Keyless Vertex AI access is fully operational and verified!")
                return True
            else:
                logger.warning(f"⚠️  Connection succeeded, but unexpected response: {res_body[:150]}")
                return False
    except Exception as err:
        logger.warning(f"⚠️  Vertex AI REST call failed (Verify that billing is active and Vertex AI API is enabled on project '{project_id}').")
        logger.warning(f"Error Details: {err}")
        return False

def verify_python_sdk_access(project_id: str) -> bool:
    """
    Executes a lightweight, keyless python-genai SDK verification check
    as a subprocess, ensuring the actual library is 100% compatible.
    """
    logger.info("------------------------------------------------------")
    logger.info("🐍 [TESTING] Verifying Keyless python-genai SDK Connection...")
    logger.info("------------------------------------------------------")
    
    # Inline python snippet to execute keylessly
    # We set max_output_tokens=5 to make it lightning fast and free
    python_code = f"""
import sys
import os
try:
    from google import genai
    from google.genai import types
    from google.genai.types import HttpOptions
    
    # Set enterprise env override for the subprocess dynamically
    os.environ["GOOGLE_CLOUD_PROJECT"] = "{project_id}"
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
    os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "True"
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ.pop("GOOGLE_API_KEY", None)
    
    client = genai.Client(
        vertexai=True,
        project="{project_id}",
        location="global",
        http_options=HttpOptions(api_version="v1", timeout=120000.0)
    )
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents="Reply ONLY with the word SUCCESS",
        config=types.GenerateContentConfig(max_output_tokens=5)
    )
    if response.text and ("SUCCESS" in response.text.upper() or "result" in str(response) or "candidates" in str(response)):
        print("SDK_VERIFIED")
        sys.exit(0)
    else:
        print(f"SDK_UNEXPECTED: {{response.text}}")
        sys.exit(1)
except Exception as e:
    print(f"SDK_ERROR: {{e}}")
    sys.exit(2)
"""
    try:
        # We run the python snippet in the same python binary context
        res = subprocess.run(
            [sys.executable, "-c", python_code],
            capture_output=True,
            text=True,
            timeout=get_process_timeout()
        )
        stdout = res.stdout.strip() if res.stdout else ""
        if res.returncode == 0 and "SDK_VERIFIED" in stdout:
            logger.info("🎉 [SUCCESS] Keyless python-genai SDK access is fully functional!")
            return True
        else:
            logger.warning(f"⚠️  Python SDK verification returned non-zero code {res.returncode}.")
            if stdout:
                logger.warning(f"  + Output: {stdout}")
            if res.stderr:
                logger.warning(f"  + Error : {res.stderr.strip()}")
            return False
    except Exception as e:
        logger.warning(f"⚠️  Subprocess crash while verifying Python SDK: {e}")
        return False

def verify_antigravity_agent_access(project_id: str) -> bool:
    """
    Executes a lightweight, keyless Antigravity SDK programmatic Agent check
    as a subprocess, ensuring the dynamic AI Agent flow is 100% compatible.
    """
    logger.info("------------------------------------------------------")
    logger.info("🚀 [TESTING] Verifying Keyless Antigravity SDK Agent Flow...")
    logger.info("------------------------------------------------------")
    
    # Inline python snippet to execute keylessly
    python_code = f"""
import sys
import os
import asyncio

try:
    # Set enterprise env override for the subprocess dynamically
    os.environ["GOOGLE_CLOUD_PROJECT"] = "{project_id}"
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
    os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "True"
    
    from google.antigravity import Agent, LocalAgentConfig
    
    async def main():
        config = LocalAgentConfig()
        async with Agent(config) as agent:
            # Short chat request to verify dynamic LLM and REST tool hooks
            response = await agent.chat("Reply ONLY with the word SUCCESS")
            txt = await response.text()
            if "SUCCESS" in txt.upper() or len(txt) > 0:
                print("ANTIGRAVITY_AGENT_VERIFIED")
                sys.exit(0)
            else:
                print(f"ANTIGRAVITY_AGENT_UNEXPECTED: {{txt}}")
                sys.exit(1)
                
    asyncio.run(main())
except Exception as e:
    print(f"ANTIGRAVITY_AGENT_ERROR: {{e}}")
    sys.exit(2)
"""
    try:
        # We run the python snippet in the same python binary context
        res = subprocess.run(
            [sys.executable, "-c", python_code],
            capture_output=True,
            text=True,
            timeout=get_process_timeout()
        )
        stdout = res.stdout.strip() if res.stdout else ""
        if res.returncode == 0 and "ANTIGRAVITY_AGENT_VERIFIED" in stdout:
            logger.info("🎉 [SUCCESS] Keyless Antigravity SDK Agent flow is fully functional!")
            return True
        else:
            logger.warning(f"⚠️  Antigravity Agent verification returned non-zero code {res.returncode}.")
            if stdout:
                logger.warning(f"  + Output: {stdout}")
            if res.stderr:
                logger.warning(f"  + Error : {res.stderr.strip()}")
            return False
    except Exception as e:
        logger.warning(f"⚠️  Subprocess crash while verifying Antigravity Agent: {e}")
        return False

def print_bordered_command_output(title: str, cmd: list):
    """Executes a diagnostic GCloud command and prints its output inside a beautifully bordered box."""
    logger.info(f"\n📋 [DIAGNOSTIC] Running: {' '.join(cmd)}")
    logger.info("┌────────────────────────────────────────────────────────────────────────┐")
    logger.info(f"  {title.upper()}")
    logger.info("├────────────────────────────────────────────────────────────────────────┤")
    try:
        # We run the command without any local re-authentication interceptors to prevent recursion
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                logger.info(f"  | {line}")
        else:
            logger.warning(f"  | ⚠️  Empty diagnostic output or command returned non-zero. Code: {res.returncode}")
            if res.stderr:
                logger.warning(f"  | Error Details: {res.stderr.strip()[:150]}")
    except Exception as e:
        logger.warning(f"  | ⚠️  Failed to spawn diagnostic subprocess: {e}")
    logger.info("└────────────────────────────────────────────────────────────────────────┘")

def print_workspace_diagnostics_report(project_id: str):
    """
    Queries and prints a beautifully formatted, comprehensive GCloud and Antigravity Workspace Diagnostics Report.
    Highlights warnings/gaps (like missing ADC, agy, or SDK files) without throwing any failures.
    """
    logger.info("\n====================================================== ")
    logger.info("📊  GOOGLE AI & ANTIGRAVITY WORKSPACE DIAGNOSTICS")
    logger.info("====================================================== ")
    
    # 0. Output raw GCloud CLI Diagnostics command lines in pretty bordered boxes
    print_bordered_command_output("Active GCloud Authenticated Accounts", [GCLOUD_BIN, "auth", "list"])
    print_bordered_command_output("Active GCloud Config defaults & Properties", [GCLOUD_BIN, "config", "list"])
    print_bordered_command_output("Active Enabled API Services Registry", [GCLOUD_BIN, "services", "list", f"--project={project_id}", "--enabled", "--format=table(config.name, config.title)"])
    
    logger.info("\n====================================================== ")
    logger.info("🔍  WORKSPACE COMPONENT INTEGRATION SUMMARY")
    logger.info("====================================================== ")
    
    # 1. Resolve Active GCloud Account
    _, active_account, _ = run_command([GCLOUD_BIN, "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"])
    if active_account:
        logger.info(f"👤 Active GCloud Account   : {active_account}")
    else:
        logger.warning("👤 Active GCloud Account   : ⚠️  WARNING: No active account set in GCloud CLI config!")
        
    # 2. Resolve Active GCloud Project
    _, current_proj, _ = run_command([GCLOUD_BIN, "config", "get-value", "project"])
    if current_proj and "(unset)" not in current_proj:
        logger.info(f"📂 Active GCloud Project   : {current_proj}")
        if current_proj != project_id:
            logger.warning(f"  + ⚠️  Project ID mismatch: Local config is '{current_proj}' but workspace is '{project_id}'")
    else:
        logger.warning("📂 Active GCloud Project   : ⚠️  WARNING: No project ID set in GCloud CLI config!")
        
    # 3. Check GCloud Application Default Credentials (ADC) File
    adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    if os.path.exists(adc_path):
        logger.info("🔑 Local ADC File Status   : ✅ PRESENT (ADC is successfully configured)")
    else:
        logger.warning("🔑 Local ADC File Status   : ⚠️  WARNING/MISSING: Keyless client libraries will fail!")
        logger.warning("                            (Run Phase 2 with --force-auth or --adc-user to re-create)")
        
    # 4. Check Antigravity CLI (agy) Setup
    import shutil
    agy_path = shutil.which("agy")
    if not agy_path:
        home_dir = os.path.expanduser("~")
        candidate = os.path.join(home_dir, ".local", "bin", "agy")
        if os.path.exists(candidate):
            agy_path = candidate
            
    if agy_path:
        try:
            res = subprocess.run([agy_path, "--version"], capture_output=True, text=True, timeout=5)
            agy_version = res.stdout.strip() if res.returncode == 0 else "Unknown Version"
            logger.info(f"🚀 Antigravity CLI (agy)   : ✅ PRESENT (Version: {agy_version})")
        except Exception:
            logger.info("🚀 Antigravity CLI (agy)   : ✅ PRESENT (Executable found)")
    else:
        logger.warning("🚀 Antigravity CLI (agy)   : ⚠️  WARNING/MISSING: 'agy' command not found in PATH!")

    # 5. Check Antigravity Python SDK Setup
    try:
        python_code = "import google.antigravity; print('SDK_OK')"
        res = subprocess.run([sys.executable, "-c", python_code], capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and "SDK_OK" in res.stdout:
            logger.info("📦 Antigravity Python SDK  : ✅ PRESENT (google-antigravity successfully imported)")
        else:
            logger.warning("📦 Antigravity Python SDK  : ⚠️  WARNING/MISSING: google-antigravity library is missing or corrupt!")
    except Exception:
        logger.warning("📦 Antigravity Python SDK  : ⚠️  WARNING/MISSING: google-antigravity library is missing!")

    # 6. Check Environment File (.env) State
    logger.info(f"📄 Active Environment File : {ENV_FILE_PATH}")
    for env_key in ["GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GCLOUD_BIN_PATH", "GEMINI_BIN_PATH"]:
        val = os.getenv(env_key)
        if val:
            masked = val[:10] + "..." if env_key == "GEMINI_API_KEY" else val
            logger.info(f"  + {env_key:<26}: {masked}")
        else:
            if env_key in ["GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"]:
                logger.warning(f"  + {env_key:<26}: ⚠️  WARNING/MISSING (No fallback available)")
            else:
                logger.info(f"  + {env_key:<26}: Not Configured (Optional)")
                
    logger.info("======================================================\n")

def verify_enabled_services(project_id: str) -> bool:
    """Verifies that mandatory services are enabled with structured output boxes, logging warnings for optional ones."""
    logger.info("- Status : Querying Google Cloud API Service Registry...")
    code, stdout, _ = run_command([
        GCLOUD_BIN, "services", "list", f"--project={project_id}", "--enabled",
        "--format=value(config.name)"
    ])
    if code != 0:
        return False
        
    enabled_list = stdout.splitlines()
    logger.info("------------------------------------------------------")
    logger.info("🛡️ [VERIFICATION] Google Cloud API Enablement Status")
    logger.info("------------------------------------------------------")
    
    mandatory_service = "aiplatform.googleapis.com"
    mandatory_ok = False
    
    for service in REQUIRED_SERVICES:
        is_enabled = service in enabled_list
        if service == mandatory_service:
            if is_enabled:
                logger.info(f"✅ Verified (Mandatory): {service} is ENABLED.")
                mandatory_ok = True
            else:
                logger.error(f"❌ FAILED (Mandatory)  : {service} is NOT enabled!")
        else:
            if is_enabled:
                logger.info(f"✅ Verified (Optional) : {service} is ENABLED.")
            else:
                logger.warning(f"⚠️  Warning (Optional)  : {service} is NOT enabled (Non-critical).")
                
    logger.info("------------------------------------------------------")
    return mandatory_ok

def write_env_file(project_id: str, location: str = "global", use_enterprise: str = "True", genai_model: str = "gemini-3.7-flash"):
    """Writes standard project environment variables to the workspace .env file."""
    env_path = ENV_FILE_PATH
    
    # Read existing env lines to preserve other keys (like GEMINI_API_KEY)
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
    # Filter out the keys we are setting
    keys_to_set = {
        "GOOGLE_CLOUD_PROJECT": project_id,
        "GOOGLE_CLOUD_LOCATION": location,
        "GOOGLE_GENAI_USE_ENTERPRISE": use_enterprise,
        "GOOGLE_GENAI_MODEL": genai_model
    }

    from utils import persist_system_ca_environment
    persist_system_ca_environment(log=logger)
    
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in keys_to_set:
                continue
        new_lines.append(line)
        
    # Append our keys
    for k, v in keys_to_set.items():
        new_lines.append(f"{k}=\"{v}\"\n")
        # Also set them in current process memory instantly
        os.environ[k] = v
        
    try:
        if is_dry_run():
            logger.info("🔍 [DRY-RUN] Simulating workspace .env persistence (skipped file write).")
        else:
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        logger.info(f"Persisted credentials to workspace .env: {env_path}")
        
        # Also synchronize to global Gemini CLI environment (~/.gemini/.env)
        if is_dry_run():
            logger.info("🔍 [DRY-RUN] Simulating global ~/.gemini/.env sync (skipped file write).")
            return True
        try:
            global_gemini_dir = os.path.expanduser("~/.gemini")
            os.makedirs(global_gemini_dir, exist_ok=True)
            global_env_path = os.path.join(global_gemini_dir, ".env")
            g_lines = []
            if os.path.exists(global_env_path):
                with open(global_env_path, "r", encoding="utf-8") as gf:
                    g_lines = gf.readlines()
            g_dict = {}
            for gl in g_lines:
                gl = gl.strip()
                if gl and not gl.startswith("#") and "=" in gl:
                    gk, gv = gl.split("=", 1)
                    g_dict[gk.strip()] = gv.strip().strip("'\"")
            for k, v in keys_to_set.items():
                if v and not str(v).startswith("<"):
                    g_dict[k] = str(v).strip()
            with open(global_env_path, "w", encoding="utf-8") as gf:
                for k, v in g_dict.items():
                    gf.write(f'{k}="{v}"\n')
            logger.info(f"🌐 [GLOBAL ENV] Synchronized project parameters to global Gemini CLI env: {global_env_path}")
        except Exception as g_err:
            logger.warning(f"⚠️  Could not update global .env at ~/.gemini/.env: {g_err}")
    except Exception as e:
        logger.error(f"Failed to write .env file: {e}")

def run_gcloud_config(project_id: str = None, location: str = None, use_enterprise: str = None, no_allow_service_disabled: bool = False, genai_model: str = None) -> bool:
    """Coordinates the entire project configuration and service enablement flow, supporting permissive service policies and customizable model targets."""
    if not verify_prephase_disk_space("Phase 3: Project Configuration & Services", log=logger):
        return False
    # Environment file verification safeguard
    is_configuring_now = bool(project_id or location)
    
    if not ENV_FILE_EXISTS:
        if not is_configuring_now:
            logger.error("======================================================================")
            logger.error("🛑 FATAL ERROR: ENVIRONMENT CONFIGURATION NOT FOUND")
            logger.error("======================================================================")
            logger.error(f"No environment file (.env) was found at: {ENV_FILE_PATH}")
            logger.error("And no project configurations (--gcloud-project or --gcloud-location) were provided.")
            logger.error("We cannot proceed safely as subsequent setup steps require active credentials.")
            logger.error("----------------------------------------------------------------------")
            logger.error("REMEDIATION STEPS:")
            logger.error("1. Place a valid .env file in your active workspace directory.")
            logger.error("2. Specify the absolute path to your env file using: --env-file <path>")
            logger.error("3. Pass your project explicitly to initialize a new file: --gcloud-project <project_id>")
            logger.error("======================================================================")
            return False
        else:
            logger.info(f"Environment file not found. Creating a new configuration file at: {ENV_FILE_PATH}")
            
    # 1. Resolve Project ID
    if not project_id:
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
        
    if not project_id:
        project_id = get_current_gcloud_project()
        if project_id:
            logger.info(f"Using current gcloud configured project ID: {project_id}")
            
    if not project_id:
        logger.error("======================================================================")
        logger.error("🛑 FATAL ERROR: GOOGLE CLOUD PROJECT ID NOT RESOLVED")
        logger.error("======================================================================")
        logger.error("No Google Cloud project ID was provided or could be automatically resolved.")
        logger.error("The project ID is mandatory for setting GCloud CLI defaults and enabling APIs.")
        logger.error("----------------------------------------------------------------------")
        logger.error("REMEDIATION STEPS:")
        logger.error("1. Pass your project ID explicitly using: --gcloud-project <project_id>")
        logger.error("2. Define 'GOOGLE_CLOUD_PROJECT' inside your active environment (.env) file.")
        logger.error("3. Set an active project in GCloud: 'gcloud config set project <project_id>'")
        logger.error("======================================================================")
        return False
        
    # Resolve location, use_enterprise, and genai_model
    if not location:
        location = os.getenv("GOOGLE_CLOUD_LOCATION") or "global"
    if not use_enterprise:
        use_enterprise = os.getenv("GOOGLE_GENAI_USE_ENTERPRISE") or "True"
    if not genai_model:
        genai_model = os.getenv("GOOGLE_GENAI_MODEL") or "gemini-3.7-flash"
        
    logger.info("------------------------------------------------------")
    logger.info("⚙️  [CONFIGURING] Google Cloud Workspace & API Services")
    logger.info("------------------------------------------------------")
    logger.info(f"- Project ID    : {project_id}")
    logger.info(f"- Region/Zone   : {location}")
    logger.info(f"- Enterprise SDK: {use_enterprise}")
    logger.info(f"- GenAI Model   : {genai_model}")
    logger.info(f"- Target APIs   : {REQUIRED_SERVICES}")
    logger.info(f"- Strict Mode   : {'Yes' if no_allow_service_disabled else 'No (Permissive Mode)'}")
    logger.info("- Status        : Resolving workspace parameter profiles...")
    
    # Write these settings to .env file immediately
    write_env_file(project_id, location, use_enterprise, genai_model)
    
    # 1.5. Align GCloud CLI Account with newly authenticated credentials
    align_active_gcloud_account()
    
    # 2. Set Quota Project (Application-Default Credentials)
    logger.info("- Status : Aligning developer billing quota project...")
    set_quota_project(project_id)
    
    # 3. Set Default Project
    if not set_default_project(project_id):
        logger.error("------------------------------------------------------")
        logger.error(f"❌ [FAILURE] Failed to configure default active project to '{project_id}'")
        logger.error("======================================================")
        return False
    
    # 3.5. Set CLI config billing/quota_project
    set_billing_quota_project(project_id)
    
    # 4. Enable All Required APIs
    logger.info("- Status : Requesting Google Cloud service activations...")
    failures_mandatory = []
    failures_optional = []
    
    mandatory_service = "aiplatform.googleapis.com"
    
    for service in REQUIRED_SERVICES:
        success = enable_service_with_retry(service, project_id)
        if not success:
            if service == mandatory_service:
                failures_mandatory.append(service)
            else:
                failures_optional.append(service)
                
    if failures_mandatory:
        if no_allow_service_disabled:
            logger.error("------------------------------------------------------")
            logger.error(f"❌ [FAILURE] Failed to enable mandatory Vertex AI API service: {failures_mandatory}")
            logger.error("This is a critical service, and we cannot proceed without it.")
            logger.error("======================================================")
            return False
        else:
            logger.warning("------------------------------------------------------")
            logger.warning(f"⚠️  [WARNING] Failed to enable mandatory Vertex AI service: {failures_mandatory}")
            logger.warning("Proceeding anyway because strict service mode (--no-allow-service-disabled) is not active.")
            logger.warning("Ensure Vertex AI API is already pre-enabled on your project by your administrator!")
            logger.warning("------------------------------------------------------")
        
    if failures_optional:
        logger.warning("------------------------------------------------------")
        logger.warning(f"⚠️  [WARNING] Failed to enable optional services: {failures_optional}")
        logger.warning("Continuing setup as mandatory Vertex AI is successfully enabled.")
        logger.warning("------------------------------------------------------")
        
    # 5. Verify Enablement
    mandatory_ok = verify_enabled_services(project_id)
    
    # Unconditionally display the pretty, comprehensive Workspace Diagnostics Report FIRST (after local config, before connections checks)
    print_workspace_diagnostics_report(project_id)
    
    if mandatory_ok:
        # Run all three direct checks independently for comprehensive, non-blocking diagnostics!
        access_verified = verify_vertex_ai_access(project_id, genai_model=genai_model)
        sdk_verified = verify_python_sdk_access(project_id)
        antigravity_verified = verify_antigravity_agent_access(project_id)
        
        if access_verified and sdk_verified and antigravity_verified:
            logger.info("------------------------------------------------------")
            logger.info("🎉 [SUCCESS] Workspace Project, REST, Python SDK, and Antigravity Agent verified as fully working!")
            logger.info("------------------------------------------------------")
        else:
            # Report individual diagnostic warnings
            logger.warning("------------------------------------------------------")
            logger.warning("⚠️  [WARNING] Local configurations succeeded, but some dynamic access checks reported warnings:")
            logger.warning(f"  + REST Connection  : {'✅ WORKING' if access_verified else '⚠️  FAILED (Check Billing / APIs)'}")
            logger.warning(f"  + Python GenAI SDK : {'✅ WORKING' if sdk_verified else '⚠️  FAILED (Check Library / Key)'}")
            logger.warning(f"  + Antigravity Agent: {'✅ WORKING' if antigravity_verified else '⚠️  FAILED (Check Package)'}")
            logger.warning("Continuing setup as core workspace configuration was successful.")
            logger.warning("------------------------------------------------------")
        return True
    else:
        if no_allow_service_disabled:
            logger.error("------------------------------------------------------")
            logger.error("❌ [FAILURE] Cloud services verification scan reported active service gaps.")
            logger.error("Ensure the required 'aiplatform.googleapis.com' API is enabled on your project.")
            logger.error("======================================================")
            return False
        else:
            logger.warning("------------------------------------------------------")
            logger.warning("⚠️  [WARNING] Cloud services verification scan reported active service gaps.")
            logger.warning("Proceeding anyway because strict service mode (--no-allow-service-disabled) is not active.")
            logger.warning("Make sure Vertex AI is pre-enabled on your GCP Console!")
            logger.warning("------------------------------------------------------")
            
            # Run all three direct checks independently for comprehensive, non-blocking diagnostics!
            access_verified = verify_vertex_ai_access(project_id, genai_model=genai_model)
            sdk_verified = verify_python_sdk_access(project_id)
            antigravity_verified = verify_antigravity_agent_access(project_id)
            
            if access_verified and sdk_verified and antigravity_verified:
                logger.info("------------------------------------------------------")
                logger.info("🎉 [SUCCESS] Even though GCloud queries warned, keyless REST, Python SDK, and Antigravity Agent are verified as fully working!")
                logger.info("------------------------------------------------------")
            else:
                # Report individual diagnostic warnings
                logger.warning("------------------------------------------------------")
                logger.warning("⚠️  [WARNING] GCloud service scans warned, and some dynamic access checks failed:")
                logger.warning(f"  + REST Connection  : {'✅ WORKING' if access_verified else '⚠️  FAILED (Check Billing / APIs)'}")
                logger.warning(f"  + Python GenAI SDK : {'✅ WORKING' if sdk_verified else '⚠️  FAILED (Check Library / Key)'}")
                logger.warning(f"  + Antigravity Agent: {'✅ WORKING' if antigravity_verified else '⚠️  FAILED (Check Package)'}")
                logger.warning("Proceeding anyway because strict service mode (--no-allow-service-disabled) is not active.")
                logger.warning("------------------------------------------------------")
            return True

def main():
    parser = argparse.ArgumentParser(description="Google Cloud Project & API Services Configurator")
    parser.add_argument("--gcloud-project", help="The target Google Cloud Project ID to configure.")
    parser.add_argument("--gcloud-location", help="Google Cloud Location/Region (default: global)")
    parser.add_argument("--use-enterprise", help="Whether to use enterprise SDK (default: True)")
    parser.add_argument("--genai-model", help="Specify the Gemini model name to utilize inside the workspace (default: gemini-3.7-flash)", default="gemini-3.7-flash")
    parser.add_argument("--env-file", help="Absolute path to a custom environment (.env) file.")
    parser.add_argument(
        "--no-allow-service-disabled",
        help="Strict Mode: Disallow Phase 3 from proceeding if GCloud service enablement fails (forces hard failures).",
        action="store_true"
    )
    parser.add_argument(
        "--no-automatic-browser",
        help="Skip launching Playwright headed browser automatically, forcing manual copy-paste authentication URLs.",
        action="store_true"
    )
    parser.add_argument(
        "--timeout",
        help="Specify the absolute execution timeout in seconds for any spawned GCloud commands (defaults to 180 seconds).",
        type=int,
        default=180
    )
    args = parser.parse_args()
    
    # Pre-parse timeout override if passed
    if args.timeout:
        os.environ["PROCESS_TIMEOUT_OVERRIDE"] = str(args.timeout)
    
    # Pre-parse environment file override if passed as parameter
    if args.env_file:
        os.environ["ENV_FILE_PATH_OVERRIDE"] = args.env_file
        global ENV_FILE_PATH, ENV_FILE_EXISTS
        from utils import get_env_file_path, load_env_file
        ENV_FILE_PATH = get_env_file_path()
        ENV_FILE_EXISTS = os.path.exists(ENV_FILE_PATH)
        load_env_file()
    
    logger.info("==============================================")
    logger.info("Starting Phase 3: Google Cloud Configuration")
    logger.info("==============================================")
    
    try:
        success = run_gcloud_config(args.gcloud_project, args.gcloud_location, args.use_enterprise, no_allow_service_disabled=args.no_allow_service_disabled, genai_model=args.genai_model)
        if success:
            logger.info("Phase 3 completed successfully!")
            return True
        else:
            logger.error("Phase 3 failed.")
            return False
    except Exception as e:
        logger.error(f"Phase 3 execution failed: {e}")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
